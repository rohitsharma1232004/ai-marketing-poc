"""Small, dependency-light Gemini Interactions API client.

The current Gemini Interactions API is used for both text and native image
creation. Requests set ``store=false`` so this application does not opt into
server-side interaction history for one-shot campaign generation.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import requests

DEFAULT_GEMINI_INTERACTIONS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)
DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.7-flash"
DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-lite-image"

SUPPORTED_GEMINI_TEXT_MODELS = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
)
SUPPORTED_GEMINI_IMAGE_MODELS = (
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
)
SUPPORTED_IMAGE_ASPECT_RATIOS = (
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
)
MAX_GEMINI_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_GEMINI_TEXT_CHARS = 500_000
MAX_IMAGE_BYTES = 15 * 1024 * 1024


@dataclass(frozen=True)
class GeminiTextResult:
    content: str
    request_id: str
    model: str
    interaction_id: str
    status: str
    usage: Mapping[str, int]


@dataclass(frozen=True)
class GeminiImageResult:
    image_bytes: bytes
    mime_type: str
    request_id: str
    model: str
    interaction_id: str
    status: str
    aspect_ratio: str
    image_size: str
    usage: Mapping[str, int]


class GeminiAPIError(RuntimeError):
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


def _model(value: str, supported: tuple[str, ...], label: str) -> str:
    model = str(value or "").strip()
    if model not in supported:
        raise ValueError(f"Unsupported {label}: {model}. Use one of: {', '.join(supported)}.")
    return model


def _validate_api_key(api_key: str, request_id: str) -> str:
    value = str(api_key or "").strip()
    if not value:
        raise GeminiAPIError(
            "GEMINI_API_KEY is missing from the server configuration.",
            request_id=request_id,
            code="GEMINI_KEY_MISSING",
        )
    return value


def _post_interaction(
    *,
    payload: Mapping[str, Any],
    api_key: str,
    api_url: str,
    request_id: str,
    http_client: Any,
    timeout_seconds: int,
) -> Mapping[str, Any]:
    key = _validate_api_key(api_key, request_id)
    if not re.match(r"^https://generativelanguage\.googleapis\.com/(?:v1|v1beta)/interactions$", api_url.strip()):
        raise GeminiAPIError(
            "GEMINI_INTERACTIONS_URL must use the official Google Generative Language interactions endpoint.",
            request_id=request_id,
            code="GEMINI_INVALID_URL",
        )
    try:
        response = http_client.post(
            api_url,
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
            },
            json=dict(payload),
            timeout=(5, timeout_seconds),
        )
    except requests.exceptions.Timeout as error:
        raise GeminiAPIError(
            "Gemini took too long to respond.",
            request_id=request_id,
            code="GEMINI_TIMEOUT",
            retryable=True,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise GeminiAPIError(
            "Could not connect to Gemini. Check the internet connection.",
            request_id=request_id,
            code="GEMINI_CONNECTION_ERROR",
            retryable=True,
        ) from error
    except requests.exceptions.RequestException as error:
        raise GeminiAPIError(
            "The Gemini request failed before a valid response was received.",
            request_id=request_id,
            code="GEMINI_REQUEST_ERROR",
            retryable=True,
        ) from error

    body = getattr(response, "content", b"") or b""
    if len(body) > MAX_GEMINI_RESPONSE_BYTES:
        raise GeminiAPIError(
            "Gemini returned a response that is too large.",
            request_id=request_id,
            code="GEMINI_RESPONSE_TOO_LARGE",
        )
    status_code = int(getattr(response, "status_code", 0))
    if status_code in {401, 403}:
        raise GeminiAPIError(
            "The configured Gemini API key is invalid or unauthorized.",
            request_id=request_id,
            code="GEMINI_AUTH_ERROR",
        )
    if status_code == 429:
        raise GeminiAPIError(
            "Gemini API quota or rate limit reached. Check Google AI Studio billing/quota.",
            request_id=request_id,
            code="GEMINI_RATE_LIMIT",
            retryable=True,
        )
    if status_code < 200 or status_code >= 300:
        raise GeminiAPIError(
            "Gemini returned an upstream error.",
            request_id=request_id,
            code="GEMINI_UPSTREAM_ERROR",
            retryable=status_code >= 500,
        )
    try:
        data = response.json()
    except (TypeError, ValueError) as error:
        raise GeminiAPIError(
            "Gemini returned a non-JSON response.",
            request_id=request_id,
            code="GEMINI_INVALID_JSON",
            retryable=True,
        ) from error
    if not isinstance(data, Mapping):
        raise GeminiAPIError(
            "Gemini returned an invalid response object.",
            request_id=request_id,
            code="GEMINI_INVALID_RESPONSE",
            retryable=True,
        )
    return data


def _model_output_blocks(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    blocks: list[Mapping[str, Any]] = []
    steps = data.get("steps")
    if not isinstance(steps, list):
        return blocks
    for step in steps:
        if not isinstance(step, Mapping) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping):
                blocks.append(block)
    return blocks


def _usage(data: Mapping[str, Any]) -> dict[str, int]:
    raw = data.get("usage")
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            result[str(key)] = value
    return result


def generate_text(
    *,
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    model: str = DEFAULT_GEMINI_TEXT_MODEL,
    request_id: str | None = None,
    api_url: str = DEFAULT_GEMINI_INTERACTIONS_URL,
    http_client: Any = requests,
) -> GeminiTextResult:
    correlation_id = request_id or str(uuid4())
    chosen_model = _model(model, SUPPORTED_GEMINI_TEXT_MODELS, "Gemini text model")
    payload = {
        "model": chosen_model,
        "system_instruction": str(system_prompt),
        "input": str(user_prompt),
        "store": False,
        "response_format": {"type": "text"},
    }
    data = _post_interaction(
        payload=payload,
        api_key=api_key,
        api_url=api_url,
        request_id=correlation_id,
        http_client=http_client,
        timeout_seconds=120,
    )
    text_parts = [
        str(block.get("text") or "")
        for block in _model_output_blocks(data)
        if block.get("type") == "text" and str(block.get("text") or "").strip()
    ]
    content = "\n".join(text_parts).strip()
    if not content:
        raise GeminiAPIError(
            "Gemini returned an empty text response.",
            request_id=correlation_id,
            code="GEMINI_EMPTY_RESPONSE",
            retryable=True,
        )
    if len(content) > MAX_GEMINI_TEXT_CHARS:
        raise GeminiAPIError(
            "Gemini returned more text than this application accepts.",
            request_id=correlation_id,
            code="GEMINI_OUTPUT_TOO_LARGE",
        )
    return GeminiTextResult(
        content=content,
        request_id=correlation_id,
        model=str(data.get("model") or chosen_model),
        interaction_id=str(data.get("id") or ""),
        status=str(data.get("status") or "completed"),
        usage=_usage(data),
    )


def generate_image(
    *,
    prompt: str,
    api_key: str,
    model: str = DEFAULT_GEMINI_IMAGE_MODEL,
    aspect_ratio: str = "4:5",
    image_size: str = "1K",
    request_id: str | None = None,
    api_url: str = DEFAULT_GEMINI_INTERACTIONS_URL,
    http_client: Any = requests,
) -> GeminiImageResult:
    correlation_id = request_id or str(uuid4())
    chosen_model = _model(model, SUPPORTED_GEMINI_IMAGE_MODELS, "Gemini image model")
    ratio = str(aspect_ratio or "").strip()
    if ratio not in SUPPORTED_IMAGE_ASPECT_RATIOS:
        raise ValueError("Unsupported Gemini image aspect ratio.")
    size = str(image_size or "").strip().upper()
    if chosen_model == "gemini-3.1-flash-lite-image":
        if size != "1K":
            raise ValueError("Gemini 3.1 Flash Lite Image supports 1K output only.")
    elif size not in {"1K", "2K", "4K"}:
        raise ValueError("Gemini image_size must be 1K, 2K, or 4K for this model.")
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Gemini image prompt must not be empty.")
    if len(clean_prompt) > 12_000:
        raise ValueError("Gemini image prompt must be at most 12,000 characters.")

    payload = {
        "model": chosen_model,
        "input": clean_prompt,
        "store": False,
        "response_format": {
            "type": "image",
            "mime_type": "image/png",
            "aspect_ratio": ratio,
            "image_size": size,
        },
    }
    data = _post_interaction(
        payload=payload,
        api_key=api_key,
        api_url=api_url,
        request_id=correlation_id,
        http_client=http_client,
        timeout_seconds=180,
    )
    image_block = next(
        (
            block
            for block in _model_output_blocks(data)
            if block.get("type") == "image" and block.get("data")
        ),
        None,
    )
    if image_block is None:
        raise GeminiAPIError(
            "Gemini completed without returning an image.",
            request_id=correlation_id,
            code="GEMINI_IMAGE_MISSING",
            retryable=True,
        )
    mime_type = str(image_block.get("mime_type") or "image/png").lower()
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise GeminiAPIError(
            "Gemini returned an unsupported image type.",
            request_id=correlation_id,
            code="GEMINI_IMAGE_TYPE",
        )
    try:
        image_bytes = base64.b64decode(str(image_block["data"]), validate=True)
    except (ValueError, TypeError) as error:
        raise GeminiAPIError(
            "Gemini returned invalid image data.",
            request_id=correlation_id,
            code="GEMINI_IMAGE_DATA",
            retryable=True,
        ) from error
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise GeminiAPIError(
            "Gemini returned an empty or oversized image.",
            request_id=correlation_id,
            code="GEMINI_IMAGE_SIZE",
        )
    return GeminiImageResult(
        image_bytes=image_bytes,
        mime_type=mime_type,
        request_id=correlation_id,
        model=str(data.get("model") or chosen_model),
        interaction_id=str(data.get("id") or ""),
        status=str(data.get("status") or "completed"),
        aspect_ratio=ratio,
        image_size=size,
        usage=_usage(data),
    )
