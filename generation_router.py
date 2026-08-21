"""Backward-compatible text generation router with Gemini support.

Groq and n8n continue to use the existing stable provider module. Gemini uses
the current Interactions API through ``gemini_api``.
"""

from __future__ import annotations

from typing import Any

import requests

from generation_providers import (
    DEFAULT_GROQ_API_URL,
    GenerationProviderError,
    GenerationResult,
    generate_calendar_content as generate_existing_content,
)
from gemini_api import (
    DEFAULT_GEMINI_INTERACTIONS_URL,
    DEFAULT_GEMINI_TEXT_MODEL,
    GeminiAPIError,
    generate_text,
)

SUPPORTED_TEXT_PROVIDERS = ("groq", "gemini", "n8n")


def normalize_text_provider(value: str | None) -> str:
    provider = str(value or "groq").strip().lower()
    if provider not in SUPPORTED_TEXT_PROVIDERS:
        raise ValueError(
            "Unsupported generation provider. Use: "
            + ", ".join(SUPPORTED_TEXT_PROVIDERS)
            + "."
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
    gemini_api_key: str = "",
    gemini_api_url: str = DEFAULT_GEMINI_INTERACTIONS_URL,
    n8n_webhook_url: str = "",
    n8n_webhook_secret: str = "",
    campaign_id: str | None = None,
    request_id: str | None = None,
    http_client: Any = requests,
) -> GenerationResult:
    normalized = normalize_text_provider(provider)
    if normalized != "gemini":
        return generate_existing_content(
            provider=normalized,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            expected_posts=expected_posts,
            groq_api_key=groq_api_key,
            groq_api_url=groq_api_url,
            n8n_webhook_url=n8n_webhook_url,
            n8n_webhook_secret=n8n_webhook_secret,
            campaign_id=campaign_id,
            request_id=request_id,
            http_client=http_client,
        )

    chosen_model = str(model or DEFAULT_GEMINI_TEXT_MODEL).strip()
    try:
        result = generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            api_key=gemini_api_key,
            model=chosen_model,
            request_id=request_id,
            api_url=gemini_api_url,
            http_client=http_client,
        )
    except GeminiAPIError as error:
        raise GenerationProviderError(
            str(error),
            request_id=error.request_id,
            code=error.code,
            retryable=error.retryable,
        ) from error
    return GenerationResult(
        content=result.content,
        request_id=result.request_id,
        provider="gemini",
        model=result.model,
        finish_reason=result.status,
        usage=result.usage,
    )
