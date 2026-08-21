"""Generation-provider boundary for calendar creation.

The Streamlit UI owns prompt construction and deterministic calendar validation.
This module only sends the prompts to the configured generation provider and
returns the provider's text response in a small, versioned result object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

import requests


DEFAULT_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
N8N_CALENDAR_CONTRACT_VERSION = "calendar.generate.v1"
SUPPORTED_GENERATION_PROVIDERS = ("groq", "n8n")
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_GENERATED_CONTENT_CHARS = 500_000


@dataclass(frozen=True)
class GenerationResult:
    """Normalized result returned by every generation provider."""

    content: str
    request_id: str
    provider: str
    model: str
    finish_reason: str = "unknown"
    usage: Mapping[str, int] | None = None


class GenerationProviderError(RuntimeError):
    """Safe, user-displayable provider failure with a correlation ID."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code
        self.retryable = retryable


_N8N_ERROR_MESSAGES = {
    "INVALID_REQUEST": "n8n rejected the calendar request configuration.",
    "GROQ_RATE_LIMIT": "Groq rate limit reached. Please try again later.",
    "GROQ_AUTH_ERROR": "The Groq credential configured in n8n is invalid.",
    "GROQ_REQUEST_FAILED": "n8n could not complete the Groq request.",
    "GROQ_OUTPUT_LIMIT": "Groq reached its output limit before returning the calendar.",
    "GROQ_EMPTY_RESPONSE": "Groq returned an empty calendar through n8n.",
}


def normalize_generation_provider(value: str | None) -> str:
    provider = (value or "groq").strip().lower()
    if provider not in SUPPORTED_GENERATION_PROVIDERS:
        choices = ", ".join(SUPPORTED_GENERATION_PROVIDERS)
        raise ValueError(
            f"Unsupported calendar generation provider '{provider}'. Use: {choices}."
        )
    return provider


def generate_calendar_content(
    *,
    provider: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    expected_posts: int,
    groq_api_key: str = "",
    groq_api_url: str = DEFAULT_GROQ_API_URL,
    n8n_webhook_url: str = "",
    n8n_webhook_secret: str = "",
    campaign_id: str | None = None,
    request_id: str | None = None,
    http_client: Any = requests,
) -> GenerationResult:
    """Generate calendar text through either direct Groq or an n8n webhook.

    Provider fallback is intentionally not automatic. An ambiguous timeout can
    occur after an upstream request has already completed, so retrying through
    another provider could create duplicate work and cost.
    """

    normalized_provider = normalize_generation_provider(provider)
    correlation_id = request_id or str(uuid4())

    if normalized_provider == "groq":
        return _generate_with_groq(
            request_id=correlation_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            api_key=groq_api_key,
            api_url=groq_api_url,
            http_client=http_client,
        )

    return _generate_with_n8n(
        request_id=correlation_id,
        campaign_id=campaign_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        expected_posts=expected_posts,
        webhook_url=n8n_webhook_url,
        webhook_secret=n8n_webhook_secret,
        http_client=http_client,
    )


def _generate_with_groq(
    *,
    request_id: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    api_key: str,
    api_url: str,
    http_client: Any,
) -> GenerationResult:
    if not api_key.strip():
        raise GenerationProviderError(
            "GROQ_API_KEY is missing from the server configuration.",
            request_id=request_id,
            code="GROQ_KEY_MISSING",
        )

    _validate_http_url(api_url, "GROQ_API_URL", request_id)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_completion_tokens": 8192,
    }
    if model.startswith("openai/gpt-oss-"):
        payload.update({"reasoning_effort": "low", "include_reasoning": False})

    try:
        response = http_client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
            },
            json=payload,
            timeout=(5, 90),
        )
    except requests.exceptions.Timeout as error:
        raise GenerationProviderError(
            "Groq took too long to respond.",
            request_id=request_id,
            code="GROQ_TIMEOUT",
            retryable=True,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise GenerationProviderError(
            "Could not connect to Groq. Check the internet connection.",
            request_id=request_id,
            code="GROQ_CONNECTION_ERROR",
            retryable=True,
        ) from error
    except requests.exceptions.RequestException as error:
        raise GenerationProviderError(
            "The Groq request failed before a valid response was received.",
            request_id=request_id,
            code="GROQ_REQUEST_ERROR",
            retryable=True,
        ) from error

    _enforce_response_size(response, request_id)
    status_code = int(getattr(response, "status_code", 0))
    if status_code == 401 or status_code == 403:
        raise GenerationProviderError(
            "The configured Groq API key is invalid or unauthorized.",
            request_id=request_id,
            code="GROQ_AUTH_ERROR",
        )
    if status_code == 429:
        raise GenerationProviderError(
            "Groq rate limit reached. Please try again later.",
            request_id=request_id,
            code="GROQ_RATE_LIMIT",
            retryable=True,
        )
    if status_code < 200 or status_code >= 300:
        raise GenerationProviderError(
            "Groq returned an upstream error.",
            request_id=request_id,
            code="GROQ_UPSTREAM_ERROR",
            retryable=status_code >= 500,
        )

    response_data = _read_json_object(response, request_id, "Groq")
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GenerationProviderError(
            "Groq response did not include a completion choice.",
            request_id=request_id,
            code="GROQ_INVALID_RESPONSE",
            retryable=True,
        )

    choice = choices[0] if isinstance(choices[0], Mapping) else {}
    message = choice.get("message") if isinstance(choice, Mapping) else {}
    message = message if isinstance(message, Mapping) else {}
    raw_content = message.get("content")
    content = raw_content.strip() if isinstance(raw_content, str) else ""
    finish_reason = str(choice.get("finish_reason") or "unknown")
    if not content:
        if finish_reason == "length":
            message_text = "Groq reached its output limit before returning the calendar."
            code = "GROQ_OUTPUT_LIMIT"
        else:
            message_text = "Groq returned an empty calendar."
            code = "GROQ_EMPTY_RESPONSE"
        raise GenerationProviderError(
            message_text,
            request_id=request_id,
            code=code,
            retryable=True,
        )
    _enforce_content_size(content, request_id)

    return GenerationResult(
        content=content,
        request_id=request_id,
        provider="groq",
        model=model,
        finish_reason=finish_reason,
        usage=_normalize_usage(response_data.get("usage")),
    )


def _generate_with_n8n(
    *,
    request_id: str,
    campaign_id: str | None,
    system_prompt: str,
    user_prompt: str,
    model: str,
    expected_posts: int,
    webhook_url: str,
    webhook_secret: str,
    http_client: Any,
) -> GenerationResult:
    if not webhook_url.strip():
        raise GenerationProviderError(
            "N8N_CALENDAR_WEBHOOK_URL is missing from the server configuration.",
            request_id=request_id,
            code="N8N_URL_MISSING",
        )
    if not webhook_secret.strip():
        raise GenerationProviderError(
            "N8N_WEBHOOK_SECRET is missing from the server configuration.",
            request_id=request_id,
            code="N8N_SECRET_MISSING",
        )
    _validate_http_url(webhook_url, "N8N_CALENDAR_WEBHOOK_URL", request_id)
    if not 1 <= int(expected_posts) <= 30:
        raise GenerationProviderError(
            "The n8n request must contain between 1 and 30 posts.",
            request_id=request_id,
            code="N8N_INVALID_POST_COUNT",
        )

    payload = {
        "contract_version": N8N_CALENDAR_CONTRACT_VERSION,
        "request_id": request_id,
        "campaign_id": campaign_id,
        "model": model,
        "expected_posts": int(expected_posts),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }
    try:
        response = http_client.post(
            webhook_url,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": webhook_secret,
                "X-Request-ID": request_id,
                "Idempotency-Key": request_id,
            },
            json=payload,
            timeout=(5, 120),
        )
    except requests.exceptions.Timeout as error:
        raise GenerationProviderError(
            "n8n did not respond in time. Check its execution using the request ID.",
            request_id=request_id,
            code="N8N_TIMEOUT",
            retryable=True,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise GenerationProviderError(
            "Could not connect to n8n. Confirm that Docker and the workflow are running.",
            request_id=request_id,
            code="N8N_CONNECTION_ERROR",
            retryable=True,
        ) from error
    except requests.exceptions.RequestException as error:
        raise GenerationProviderError(
            "The n8n request failed before a valid response was received.",
            request_id=request_id,
            code="N8N_REQUEST_ERROR",
            retryable=True,
        ) from error

    _enforce_response_size(response, request_id)
    status_code = int(getattr(response, "status_code", 0))
    response_data: Mapping[str, Any] | None = None
    try:
        response_data = _read_json_object(response, request_id, "n8n")
    except GenerationProviderError:
        if 200 <= status_code < 300:
            raise

    if status_code == 401 or status_code == 403:
        raise GenerationProviderError(
            "n8n rejected the webhook secret.",
            request_id=request_id,
            code="N8N_AUTH_ERROR",
        )
    if status_code == 429:
        raise GenerationProviderError(
            "n8n is currently rate limited. Please try again later.",
            request_id=request_id,
            code="N8N_RATE_LIMIT",
            retryable=True,
        )
    if status_code < 200 or status_code >= 300:
        code, retryable = _read_n8n_error(response_data)
        raise GenerationProviderError(
            _N8N_ERROR_MESSAGES.get(code, "n8n could not generate the calendar."),
            request_id=request_id,
            code=code,
            retryable=retryable or status_code >= 500,
        )

    if response_data is None:
        raise GenerationProviderError(
            "n8n returned an invalid response.",
            request_id=request_id,
            code="N8N_INVALID_RESPONSE",
            retryable=True,
        )
    if response_data.get("contract_version") != N8N_CALENDAR_CONTRACT_VERSION:
        raise GenerationProviderError(
            "n8n returned an unsupported contract version.",
            request_id=request_id,
            code="N8N_CONTRACT_MISMATCH",
        )
    if response_data.get("request_id") != request_id:
        raise GenerationProviderError(
            "n8n returned a response for a different request.",
            request_id=request_id,
            code="N8N_REQUEST_ID_MISMATCH",
        )
    if response_data.get("campaign_id") != campaign_id:
        raise GenerationProviderError(
            "n8n returned a response for a different campaign.",
            request_id=request_id,
            code="N8N_CAMPAIGN_ID_MISMATCH",
        )
    if response_data.get("ok") is not True:
        code, retryable = _read_n8n_error(response_data)
        raise GenerationProviderError(
            _N8N_ERROR_MESSAGES.get(code, "n8n could not generate the calendar."),
            request_id=request_id,
            code=code,
            retryable=retryable,
        )

    returned_posts = response_data.get("expected_posts")
    if returned_posts is not None and returned_posts != int(expected_posts):
        raise GenerationProviderError(
            "n8n returned a response with the wrong expected post count.",
            request_id=request_id,
            code="N8N_POST_COUNT_MISMATCH",
        )
    if response_data.get("model") != model:
        raise GenerationProviderError(
            "n8n returned a response from an unexpected model.",
            request_id=request_id,
            code="N8N_MODEL_MISMATCH",
        )
    raw_content = response_data.get("calendar_markdown")
    content = raw_content.strip() if isinstance(raw_content, str) else ""
    if not content:
        raise GenerationProviderError(
            "n8n returned an empty calendar.",
            request_id=request_id,
            code="N8N_EMPTY_RESPONSE",
            retryable=True,
        )
    _enforce_content_size(content, request_id)

    return GenerationResult(
        content=content,
        request_id=request_id,
        provider="n8n",
        model=str(response_data.get("model") or model),
        finish_reason=str(response_data.get("finish_reason") or "unknown"),
        usage=_normalize_usage(response_data.get("usage")),
    )


def _validate_http_url(value: str, setting_name: str, request_id: str) -> None:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GenerationProviderError(
            f"{setting_name} must be a valid HTTP or HTTPS URL.",
            request_id=request_id,
            code="INVALID_PROVIDER_URL",
        )
    if parsed.username or parsed.password:
        raise GenerationProviderError(
            f"{setting_name} must not contain credentials in the URL.",
            request_id=request_id,
            code="INVALID_PROVIDER_URL",
        )


def _read_json_object(
    response: Any, request_id: str, provider_name: str
) -> Mapping[str, Any]:
    try:
        data = response.json()
    except (TypeError, ValueError) as error:
        raise GenerationProviderError(
            f"{provider_name} returned a non-JSON response.",
            request_id=request_id,
            code=f"{provider_name.upper()}_INVALID_JSON",
            retryable=True,
        ) from error
    if not isinstance(data, Mapping):
        raise GenerationProviderError(
            f"{provider_name} returned an unexpected response structure.",
            request_id=request_id,
            code=f"{provider_name.upper()}_INVALID_RESPONSE",
            retryable=True,
        )
    return data


def _enforce_response_size(response: Any, request_id: str) -> None:
    header_value = getattr(response, "headers", {}).get("Content-Length", "")
    try:
        declared_size = int(header_value)
    except (TypeError, ValueError):
        declared_size = 0
    raw_content = getattr(response, "content", b"")
    actual_size = len(raw_content) if isinstance(raw_content, (bytes, bytearray)) else 0
    if declared_size > MAX_PROVIDER_RESPONSE_BYTES or actual_size > MAX_PROVIDER_RESPONSE_BYTES:
        raise GenerationProviderError(
            "The generation provider returned an unexpectedly large response.",
            request_id=request_id,
            code="PROVIDER_RESPONSE_TOO_LARGE",
        )


def _enforce_content_size(content: str, request_id: str) -> None:
    if len(content) > MAX_GENERATED_CONTENT_CHARS:
        raise GenerationProviderError(
            "The generated calendar is unexpectedly large.",
            request_id=request_id,
            code="PROVIDER_CONTENT_TOO_LARGE",
        )


def _read_n8n_error(data: Mapping[str, Any] | None) -> tuple[str, bool]:
    if not isinstance(data, Mapping):
        return "N8N_REQUEST_FAILED", False
    error = data.get("error")
    if not isinstance(error, Mapping):
        return "N8N_REQUEST_FAILED", False
    raw_code = error.get("code")
    code = str(raw_code) if isinstance(raw_code, str) else "N8N_REQUEST_FAILED"
    return code, bool(error.get("retryable", False))


def _normalize_usage(value: Any) -> Mapping[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw_value = value.get(key)
        if isinstance(raw_value, (int, float)) and raw_value >= 0:
            normalized[key] = int(raw_value)
    return normalized or None
