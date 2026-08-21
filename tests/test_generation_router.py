import pytest

import generation_router
from generation_providers import GenerationResult
from gemini_api import GeminiAPIError, GeminiTextResult


def test_router_normalizes_supported_providers():
    assert generation_router.normalize_text_provider(" Groq ") == "groq"
    assert generation_router.normalize_text_provider(" Gemini ") == "gemini"
    with pytest.raises(ValueError):
        generation_router.normalize_text_provider("n8n")
    with pytest.raises(ValueError):
        generation_router.normalize_text_provider("unknown")


def test_router_routes_groq_directly(monkeypatch):
    def fake_generate_groq_content(**kwargs):
        assert kwargs["provider"] == "groq"
        assert kwargs["groq_api_key"] == "groq-key"
        assert kwargs["groq_api_url"].startswith("https://api.groq.com/")
        return GenerationResult(
            content="| Date | Platform |",
            request_id="req-groq",
            provider="groq",
            model="openai/gpt-oss-120b",
            finish_reason="stop",
            usage={"total_tokens": 20},
        )

    monkeypatch.setattr(
        generation_router, "generate_groq_content", fake_generate_groq_content
    )
    result = generation_router.generate_calendar_content(
        provider="groq",
        system_prompt="system",
        user_prompt="user",
        model="openai/gpt-oss-120b",
        expected_posts=1,
        groq_api_key="groq-key",
        request_id="req-groq",
        # An upgraded local app may still pass this until its cleanup patch runs.
        n8n_webhook_url="http://ignored.local",
    )
    assert result.provider == "groq"
    assert result.content.startswith("| Date")


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
