import pytest

from supabase_media import SupabaseMediaError, public_object_url, upload_public_creative


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.content = b"{}"


class FakeHTTP:
    def __init__(self, post_status=200, head_status=200):
        self.post_status = post_status
        self.head_status = head_status
        self.posts = []
        self.heads = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.post_status)

    def head(self, url, **kwargs):
        self.heads.append((url, kwargs))
        return FakeResponse(self.head_status)


def test_upload_uses_service_key_only_in_headers_and_returns_public_url():
    client = FakeHTTP()
    result = upload_public_creative(
        project_url="https://abcxyz.supabase.co",
        service_role_key="server-secret",
        bucket="publishing-media",
        campaign_id="campaign-123",
        post_number=2,
        creative_asset_id="asset-456",
        creative_hash="a" * 64,
        file_bytes=b"\xff\xd8\xffjpeg",
        mime_type="image/jpeg",
        http_client=client,
    )
    assert result.public_url.startswith(
        "https://abcxyz.supabase.co/storage/v1/object/public/publishing-media/"
    )
    url, kwargs = client.posts[0]
    assert "server-secret" not in url
    assert kwargs["headers"]["Authorization"] == "Bearer server-secret"
    assert kwargs["headers"]["x-upsert"] == "true"
    assert client.heads[0][0] == result.public_url


def test_public_url_requires_standard_supabase_project_host():
    with pytest.raises(ValueError, match="supabase.co"):
        public_object_url(
            "https://evil.example.com",
            "publishing-media",
            "campaigns/a/post_01/a.jpg",
        )


def test_private_bucket_detection_is_explicit():
    client = FakeHTTP(head_status=404)
    with pytest.raises(SupabaseMediaError) as caught:
        upload_public_creative(
            project_url="https://abcxyz.supabase.co",
            service_role_key="server-secret",
            bucket="publishing-media",
            campaign_id="campaign",
            post_number=1,
            creative_asset_id="asset",
            creative_hash="b" * 64,
            file_bytes=b"\xff\xd8\xffjpeg",
            mime_type="image/jpeg",
            http_client=client,
        )
    assert caught.value.code == "SUPABASE_BUCKET_NOT_PUBLIC"


def test_missing_server_key_is_not_accepted():
    with pytest.raises(SupabaseMediaError) as caught:
        upload_public_creative(
            project_url="https://abcxyz.supabase.co",
            service_role_key="",
            bucket="publishing-media",
            campaign_id="campaign",
            post_number=1,
            creative_asset_id="asset",
            creative_hash="b" * 64,
            file_bytes=b"\xff\xd8\xffjpeg",
            mime_type="image/jpeg",
            verify_public=False,
        )
    assert caught.value.code == "SUPABASE_KEY_MISSING"
