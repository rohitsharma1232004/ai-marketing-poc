import json

import pytest
import requests

from meta_publisher import MetaPublishError, publish_facebook_photo, publish_instagram_image


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def json(self):
        return self._payload


class RecordingHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_facebook_photo_uses_bearer_token_and_versioned_photos_endpoint():
    client = RecordingHTTP([FakeResponse(200, {"post_id": "123_456"})])
    result = publish_facebook_photo(
        page_id="123",
        page_access_token="secret-token",
        image_url="https://cdn.example.com/post.jpg",
        caption="Approved caption",
        request_id="req-1",
        http_client=client,
    )
    assert result.platform == "facebook"
    assert result.platform_post_id == "123_456"
    url, call = client.calls[0]
    assert url.endswith("/v25.0/123/photos")
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert "access_token" not in call["data"]
    assert call["data"]["caption"] == "Approved caption"


def test_instagram_image_uses_container_then_media_publish():
    client = RecordingHTTP(
        [
            FakeResponse(200, {"id": "container-1"}),
            FakeResponse(200, {"id": "media-1"}),
        ]
    )
    result = publish_instagram_image(
        instagram_user_id="987",
        page_access_token="secret-token",
        image_url="https://cdn.example.com/post.png",
        caption="Approved caption",
        request_id="req-2",
        http_client=client,
    )
    assert result.platform == "instagram"
    assert result.platform_post_id == "media-1"
    assert result.container_id == "container-1"
    assert client.calls[0][0].endswith("/v25.0/987/media")
    assert client.calls[0][1]["data"]["image_url"].startswith("https://")
    assert client.calls[1][0].endswith("/v25.0/987/media_publish")
    assert client.calls[1][1]["data"] == {"creation_id": "container-1"}


def test_publish_timeout_is_outcome_unknown_to_prevent_duplicate_retry():
    client = RecordingHTTP([requests.exceptions.Timeout("late timeout")])
    with pytest.raises(MetaPublishError) as caught:
        publish_facebook_photo(
            page_id="123",
            page_access_token="secret-token",
            image_url="https://cdn.example.com/post.jpg",
            caption="Approved caption",
            request_id="req-timeout",
            http_client=client,
        )
    assert caught.value.code == "META_TIMEOUT"
    assert caught.value.outcome_unknown is True
    assert caught.value.retryable is False


def test_instagram_container_timeout_is_retryable_because_no_live_post_exists_yet():
    client = RecordingHTTP([requests.exceptions.Timeout("container timeout")])
    with pytest.raises(MetaPublishError) as caught:
        publish_instagram_image(
            instagram_user_id="987",
            page_access_token="secret-token",
            image_url="https://cdn.example.com/post.png",
            caption="Approved caption",
            request_id="req-container-timeout",
            http_client=client,
        )
    assert caught.value.outcome_unknown is False
    assert caught.value.retryable is True


def test_rejects_non_https_media_url_before_network():
    client = RecordingHTTP([])
    with pytest.raises(ValueError, match="public HTTPS"):
        publish_facebook_photo(
            page_id="123",
            page_access_token="secret-token",
            image_url="http://localhost/post.jpg",
            caption="Approved caption",
            http_client=client,
        )
    assert client.calls == []
