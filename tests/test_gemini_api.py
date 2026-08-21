import base64

import pytest

from gemini_api import GeminiAPIError, generate_image, generate_text


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
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_generate_text_uses_stateless_interactions_and_extracts_model_output():
    client = FakeHTTP(
        FakeResponse(
            200,
            {
                "id": "int_123",
                "status": "completed",
                "model": "gemini-3.7-flash",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "calendar markdown"}],
                    }
                ],
                "usage": {"total_tokens": 123},
            },
        )
    )
    result = generate_text(
        system_prompt="system",
        user_prompt="user",
        api_key="secret",
        model="gemini-3.7-flash",
        request_id="req-1",
        http_client=client,
    )
    assert result.content == "calendar markdown"
    assert result.interaction_id == "int_123"
    payload = client.calls[0][1]["json"]
    assert payload["store"] is False
    assert payload["system_instruction"] == "system"
    assert payload["response_format"] == {"type": "text"}
    assert client.calls[0][1]["headers"]["x-goog-api-key"] == "secret"


def test_generate_image_requests_ratio_and_decodes_image():
    raw = b"generated-image-bytes"
    client = FakeHTTP(
        FakeResponse(
            200,
            {
                "id": "int_img",
                "status": "completed",
                "model": "gemini-3.1-flash-lite-image",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(raw).decode("ascii"),
                            }
                        ],
                    }
                ],
            },
        )
    )
    result = generate_image(
        prompt="create the visual",
        api_key="secret",
        aspect_ratio="4:5",
        image_size="1K",
        request_id="req-img",
        http_client=client,
    )
    assert result.image_bytes == raw
    payload = client.calls[0][1]["json"]
    assert payload["store"] is False
    assert payload["response_format"]["aspect_ratio"] == "4:5"
    assert payload["response_format"]["image_size"] == "1K"


def test_generate_image_can_send_previous_creative_as_revision_reference():
    output = b"revised"
    previous = b"previous-creative"
    client = FakeHTTP(
        FakeResponse(
            200,
            {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(output).decode("ascii"),
                            }
                        ],
                    }
                ]
            },
        )
    )
    generate_image(
        prompt="make only the requested revision",
        api_key="secret",
        reference_image_bytes=previous,
        reference_image_mime_type="image/png",
        http_client=client,
    )
    interaction_input = client.calls[0][1]["json"]["input"]
    assert interaction_input[0]["type"] == "image"
    assert base64.b64decode(interaction_input[0]["data"]) == previous
    assert interaction_input[1]["type"] == "text"


def test_flash_lite_rejects_unsupported_2k_request_before_network():
    with pytest.raises(ValueError, match="supports 1K"):
        generate_image(
            prompt="visual",
            api_key="secret",
            model="gemini-3.1-flash-lite-image",
            image_size="2K",
        )


def test_gemini_rate_limit_is_safe_and_retryable():
    client = FakeHTTP(FakeResponse(429, {"error": {"message": "quota"}}))
    with pytest.raises(GeminiAPIError) as caught:
        generate_text(
            system_prompt="system",
            user_prompt="user",
            api_key="secret",
            http_client=client,
        )
    assert caught.value.code == "GEMINI_RATE_LIMIT"
    assert caught.value.retryable is True
