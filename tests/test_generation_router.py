import pytest

import generation_router
from gemini_api import GeminiAPIError, GeminiTextResult


def test_router_normalizes_supported_providers():
    assert generation_router.normalize_text_provider(" Gemini ") == "gemini"
    with pytest.raises(ValueError):
        generation_router.normalize_text_provider("unknown")


def test_router_returns_existing_generation_result_for_gemini(monkeypatch):
    def fake_generate_text(**kwargs):
        assert kwargs["api_key"] == "gem-key"
        assert kwargs["model"] == "gemini-3.7-flash"
        return GeminiTextResult(
            content="| Date | Platform |",
            request_id="req-1",
            model="gemini-3.7-flash",
            interaction_id="int-1",
            status="completed",
            usage={"total_tokens": 55},
        )

    monkeypatch.setattr(generation_router, "generate_text", fake_generate_text)
    result = generation_router.generate_calendar_content(
        provider="gemini",
        system_prompt="system",
        user_prompt="user",
        model="gemini-3.7-flash",
        expected_posts=1,
        gemini_api_key="gem-key",
        request_id="req-1",
    )
    assert result.provider == "gemini"
    assert result.content.startswith("| Date")
    assert result.usage == {"total_tokens": 55}


def test_router_maps_gemini_api_error_to_existing_provider_error(monkeypatch):
    def fail(**kwargs):
        raise GeminiAPIError(
            "quota reached",
            request_id="req-2",
            code="GEMINI_RATE_LIMIT",
            retryable=True,
        )

    monkeypatch.setattr(generation_router, "generate_text", fail)
    with pytest.raises(generation_router.GenerationProviderError) as caught:
        generation_router.generate_calendar_content(
            provider="gemini",
            system_prompt="system",
            user_prompt="user",
            model="gemini-3.7-flash",
            expected_posts=1,
            gemini_api_key="gem-key",
            request_id="req-2",
        )
    assert caught.value.code == "GEMINI_RATE_LIMIT"
    assert caught.value.retryable is True
