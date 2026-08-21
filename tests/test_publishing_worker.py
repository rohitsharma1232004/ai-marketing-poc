from types import SimpleNamespace

import publishing_worker


class FakeStore:
    def __init__(self, job, connection):
        self.job = dict(job)
        self.connection = dict(connection)
        self.finished = None

    def get_job_bundle(self, _job_id):
        return {"job": self.job, "connection": self.connection}

    def mark_failed(self, job_id, **kwargs):
        self.finished = {"id": job_id, "status": "failed", **kwargs}
        return self.finished

    def mark_outcome_unknown(self, job_id, **kwargs):
        self.finished = {"id": job_id, "status": "outcome_unknown", **kwargs}
        return self.finished

    def mark_published(self, job_id, **kwargs):
        self.finished = {"id": job_id, "status": "published", **kwargs}
        return self.finished


def _job(platform="instagram"):
    return {
        "id": "job-1",
        "status": "publishing",
        "platform": platform,
        "public_media_url": "https://cdn.example.com/post.png",
        "caption": "Approved caption",
    }


def _connection():
    return {
        "credential_ref": "META_TOKEN_CLIENT_1",
        "facebook_page_id": "123",
        "instagram_user_id": "987",
    }


def test_missing_runtime_secret_marks_confirmed_failure():
    store = FakeStore(_job(), _connection())
    result = publishing_worker.dispatch_claimed_job(
        store,
        _job(),
        token_resolver=lambda _ref: "",
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "META_TOKEN_MISSING"


def test_success_marks_platform_post_id(monkeypatch):
    store = FakeStore(_job(), _connection())

    def fake_publish(**kwargs):
        assert kwargs["instagram_user_id"] == "987"
        assert kwargs["page_access_token"] == "runtime-secret"
        return SimpleNamespace(platform_post_id="ig-media-1", request_id="job-1")

    monkeypatch.setattr(publishing_worker, "publish_instagram_image", fake_publish)
    result = publishing_worker.dispatch_claimed_job(
        store,
        _job(),
        token_resolver=lambda _ref: "runtime-secret",
    )
    assert result["status"] == "published"
    assert result["platform_post_id"] == "ig-media-1"
