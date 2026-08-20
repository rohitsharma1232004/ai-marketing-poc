import unittest
from uuid import uuid4

import requests

from review_notifications import (
    CONTRACT_VERSION,
    ReviewNotificationError,
    build_notification_payload,
    send_review_notification,
    validate_phone_e164,
)


class FakeResponse:
    def __init__(self, status_code=202, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class ReviewNotificationTests(unittest.TestCase):
    def setUp(self):
        self.event_id = str(uuid4())
        self.review_request_id = str(uuid4())
        self.campaign_id = str(uuid4())
        self.calendar_version_id = str(uuid4())
        self.review_token = (
            f"rv1.{self.review_request_id}.1788000000."
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        self.payload = build_notification_payload(
            event_id=self.event_id,
            review_request_id=self.review_request_id,
            campaign_id=self.campaign_id,
            calendar_version_id=self.calendar_version_id,
            content_hash="a" * 64,
            role="senior",
            recipient_name="Senior Reviewer",
            recipient_phone_e164="+91 98765 43210",
            review_due_at="2026-08-23T10:00:00Z",
            review_token_suffix=self.review_token,
        )

    def test_e164_validation_normalizes_formatting(self):
        self.assertEqual(validate_phone_e164("+91 98765-43210"), "+919876543210")
        for invalid in ("9876543210", "+012345678", "+91abc", "+123"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_phone_e164(invalid)

    def test_build_payload_is_allowlisted(self):
        self.assertEqual(self.payload["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.payload["recipient_phone_e164"], "+919876543210")
        self.assertNotIn("client_document", self.payload)

    def test_send_uses_secret_and_idempotency_headers(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(
                data={
                    "contract_version": CONTRACT_VERSION,
                    "ok": True,
                    "event_id": self.event_id,
                    "review_request_id": self.review_request_id,
                    "status": "accepted",
                    "provider": "whatsapp_cloud_api",
                    "provider_message_id": "wamid.123",
                }
            )

        result = send_review_notification(
            "http://localhost:5678/webhook/review", "shared-secret", self.payload,
            post=fake_post,
        )
        self.assertEqual(result.provider_message_id, "wamid.123")
        self.assertEqual(calls[0][1]["headers"]["X-Webhook-Secret"], "shared-secret")
        self.assertEqual(calls[0][1]["headers"]["Idempotency-Key"], self.event_id)

    def test_response_cannot_swap_event_or_request(self):
        def fake_post(_url, **_kwargs):
            return FakeResponse(
                data={
                    "contract_version": CONTRACT_VERSION,
                    "ok": True,
                    "event_id": "different",
                    "review_request_id": self.review_request_id,
                    "status": "accepted",
                    "provider": "whatsapp_cloud_api",
                    "provider_message_id": "wamid.123",
                }
            )

        with self.assertRaises(ReviewNotificationError) as raised:
            send_review_notification("http://n8n", "secret", self.payload, post=fake_post)
        self.assertEqual(raised.exception.code, "RESPONSE_MISMATCH")

    def test_timeout_is_retryable_and_does_not_echo_secret_or_token(self):
        def fake_post(_url, **_kwargs):
            raise requests.Timeout("leaked rv1.secret-token shared-secret")

        with self.assertRaises(ReviewNotificationError) as raised:
            send_review_notification(
                "http://n8n", "shared-secret", self.payload, post=fake_post
            )
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("shared-secret", str(raised.exception))
        self.assertNotIn("rv1", str(raised.exception))

    def test_auth_failure_is_safe(self):
        def fake_post(_url, **_kwargs):
            return FakeResponse(status_code=401, data={"detail": "raw upstream"})

        with self.assertRaises(ReviewNotificationError) as raised:
            send_review_notification("http://n8n", "secret", self.payload, post=fake_post)
        self.assertEqual(raised.exception.code, "WEBHOOK_UNAUTHORIZED")
        self.assertNotIn("raw upstream", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
