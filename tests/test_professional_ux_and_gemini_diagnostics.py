import pytest

from gemini_api import GeminiAPIError, generate_text


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"response"

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, response):
        self.response = response

    def post(self, url, **kwargs):
        return self.response


def test_failed_precondition_surfaces_billing_guidance_and_provider_detail():
    client = FakeHTTP(
        FakeResponse(
            400,
            {
                "error": {
                    "code": "failed_precondition",
                    "message": "Billing must be enabled for this project.",
                }
            },
        )
    )
    with pytest.raises(GeminiAPIError) as caught:
        generate_text(
            system_prompt="system",
            user_prompt="user",
            api_key="secret-key",
            http_client=client,
        )
    assert caught.value.code == "GEMINI_BILLING_REQUIRED"
    assert "billing" in str(caught.value).lower()
    assert "Billing must be enabled" in str(caught.value)
    assert "secret-key" not in str(caught.value)


def test_invalid_request_surfaces_safe_google_detail():
    client = FakeHTTP(
        FakeResponse(
            400,
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Unsupported image_size value.",
                }
            },
        )
    )
    with pytest.raises(GeminiAPIError) as caught:
        generate_text(
            system_prompt="system",
            user_prompt="user",
            api_key="secret",
            http_client=client,
        )
    assert caught.value.code == "GEMINI_INVALID_REQUEST"
    assert "Unsupported image_size value" in str(caught.value)


def test_model_not_found_has_specific_error_code():
    client = FakeHTTP(
        FakeResponse(
            404,
            {"error": {"code": "not_found", "message": "Model is not available."}},
        )
    )
    with pytest.raises(GeminiAPIError) as caught:
        generate_text(
            system_prompt="system",
            user_prompt="user",
            api_key="secret",
            http_client=client,
        )
    assert caught.value.code == "GEMINI_MODEL_UNAVAILABLE"


def test_transformed_app_uses_compact_brand_kit_and_clean_creative_studio_labels():
    text = open("app.py", encoding="utf-8").read()
    assert "Professional Brand Kit UX" in text
    assert '"Set Up Brand Kit"' in text
    assert '"View / Edit Brand Kit"' in text
    assert 'with st.expander("AI Creative Studio", expanded=False):' in text
    assert '"Technical details"' in text
    assert '"Save as Creative Version"' in text
    assert "AI Creative Studio (Gemini)" not in text
