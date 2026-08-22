import base64
from io import BytesIO

import pytest
from PIL import Image

from cloudflare_images import (
    CloudflareImageError,
    DEFAULT_CLOUDFLARE_IMAGE_MODEL,
    generate_image,
)


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


def jpeg_bytes(size=(32, 24)):
    output = BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(output, format="JPEG")
    return output.getvalue()


def test_cloudflare_flux_uses_official_rest_endpoint_and_decodes_jpeg():
    raw = jpeg_bytes((40, 30))
    client = FakeHTTP(
        FakeResponse(200, {"success": True, "result": {"image": base64.b64encode(raw).decode("ascii")}})
    )
    result = generate_image(
        prompt="Create a premium real-estate social creative.",
        account_id="abcdef1234567890",
        api_token="secret-token",
        aspect_ratio="4:5",
        request_id="req-cf-1",
        seed=123,
        http_client=client,
    )
    assert result.image_bytes == raw
    assert result.mime_type == "image/jpeg"
    assert result.width == 40
    assert result.height == 30
    assert result.seed == 123
    assert result.model == DEFAULT_CLOUDFLARE_IMAGE_MODEL
    url, call = client.calls[0]
    assert url.endswith(f"/ai/run/{DEFAULT_CLOUDFLARE_IMAGE_MODEL}")
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert call["json"]["steps"] == 4
    assert call["json"]["seed"] == 123
    assert "4:5 social-media layout" in call["json"]["prompt"]


def test_cloudflare_long_prompt_is_compacted_within_provider_limit():
    raw = jpeg_bytes()
    client = FakeHTTP(
        FakeResponse(200, {"result": {"image": base64.b64encode(raw).decode("ascii")}})
    )
    result = generate_image(
        prompt="A" * 5000 + " final brand constraints",
        account_id="abcdef1234567890",
        api_token="secret-token",
        http_client=client,
    )
    sent = client.calls[0][1]["json"]["prompt"]
    assert len(sent) <= 2048
    assert "final brand constraints" in sent
    assert result.prompt_compacted is True
    assert result.provider_prompt_chars == len(sent)


def test_cloudflare_rate_limit_is_retryable():
    client = FakeHTTP(FakeResponse(429, {"errors": [{"code": 3040, "message": "capacity"}]}))
    with pytest.raises(CloudflareImageError) as caught:
        generate_image(
            prompt="visual",
            account_id="abcdef1234567890",
            api_token="secret-token",
            http_client=client,
        )
    assert caught.value.code == "CLOUDFLARE_RATE_LIMIT"
    assert caught.value.retryable is True


def test_cloudflare_auth_error_does_not_leak_token():
    token = "super-secret-token"
    client = FakeHTTP(
        FakeResponse(403, {"errors": [{"message": f"bad token {token}"}]})
    )
    with pytest.raises(CloudflareImageError) as caught:
        generate_image(
            prompt="visual",
            account_id="abcdef1234567890",
            api_token=token,
            http_client=client,
        )
    assert caught.value.code == "CLOUDFLARE_AUTH_ERROR"
    assert token not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_cloudflare_rejects_non_jpeg_provider_output():
    raw = b"not-a-jpeg"
    client = FakeHTTP(
        FakeResponse(200, {"result": {"image": base64.b64encode(raw).decode("ascii")}})
    )
    with pytest.raises(CloudflareImageError) as caught:
        generate_image(
            prompt="visual",
            account_id="abcdef1234567890",
            api_token="secret-token",
            http_client=client,
        )
    assert caught.value.code == "CLOUDFLARE_IMAGE_TYPE"
