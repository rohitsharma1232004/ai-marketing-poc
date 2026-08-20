import re
import unittest
from datetime import datetime, timedelta, timezone

from campaign_store import CampaignStore
from review_notifications import (
    CONTRACT_VERSION,
    ReviewNotificationError,
    ReviewNotificationResult,
)
from review_service import (
    ReviewDecisionError,
    ReviewLinkUnavailable,
    ReviewService,
    ReviewServiceConfig,
    ReviewServiceConfigError,
)


SECRET = "review-service-test-signing-secret-with-32-plus-bytes"
SENIOR_PHONE = "+919876543210"
CLIENT_PHONE = "+919123456789"
HEADERS = [
    "Date",
    "Platform",
    "Pillar",
    "Format",
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
]
ROWS = [
    [
        "Mon, Aug 24",
        "Instagram",
        "Educational",
        "Image",
        "Explain a useful feature",
        "useful local keyword",
        "Learn more",
    ]
]


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.store = CampaignStore(":memory:")
        client = self.store.create_or_update_client("Example Client")
        campaign = self.store.create_campaign(
            client["id"], {"client_name": "Example Client"}
        )
        self.calendar = self.store.complete_generation(
            campaign["id"],
            HEADERS,
            ROWS,
            client_metadata={"location": "Faridabad"},
            generation_metadata={"provider": "test"},
        )
        self.campaign_id = campaign["id"]
        consent_at = self._iso(self.now - timedelta(minutes=1))
        self.store.upsert_review_recipient(
            self.campaign_id,
            "senior",
            "Senior Reviewer",
            SENIOR_PHONE,
            consent_at,
        )
        self.store.upsert_review_recipient(
            self.campaign_id,
            "client",
            "Client Reviewer",
            CLIENT_PHONE,
            consent_at,
        )
        self.config = ReviewServiceConfig(
            signing_secret=SECRET,
            public_base_url="http://localhost:8000",
            webhook_url="http://n8n:5678/webhook/whatsapp-review",
            webhook_secret="n8n-test-secret",
            allow_insecure_localhost=True,
        )
        self.service = ReviewService(self.store, self.config, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()

    @staticmethod
    def _iso(value):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _token(issued):
        return issued.review_url.rsplit("/", 1)[-1]

    def test_config_requires_https_except_explicit_local_development(self):
        with self.assertRaises(ReviewServiceConfigError):
            ReviewServiceConfig(SECRET, "http://review.example.com")
        with self.assertRaises(ReviewServiceConfigError):
            ReviewServiceConfig(SECRET, "https://user:password@example.com")
        with self.assertRaises(ReviewServiceConfigError):
            ReviewServiceConfig(SECRET, "https://example.com?secret=value")

        config = ReviewServiceConfig(
            SECRET,
            "http://127.0.0.1:8000/base/",
            allow_insecure_localhost=True,
        )
        self.assertEqual(config.public_base_url, "http://127.0.0.1:8000/base")
        self.assertNotIn(SECRET, repr(config))

    def test_issue_preview_is_read_only_and_exchange_is_one_use(self):
        issued = self.service.issue_senior_review(self.campaign_id)
        token = self._token(issued)

        first = self.service.preview_link(token)
        second = self.service.preview_link(token)
        self.assertEqual(first, second)
        self.assertEqual(first.role, "senior")
        self.assertEqual(
            self.store.get_review_request(issued.review_request_id)["status"],
            "pending",
        )
        self.assertNotIn(token, repr(issued))

        opened = self.service.exchange_link(token)
        self.assertEqual(opened.role, "senior")
        self.assertNotIn(opened.session_token, repr(opened))
        self.assertNotIn(opened.csrf_token, repr(opened))
        self.assertEqual(
            self.store.get_review_request(issued.review_request_id)["status"],
            "opened",
        )
        with self.assertRaises(ReviewLinkUnavailable):
            self.service.preview_link(token)
        with self.assertRaises(ReviewLinkUnavailable):
            self.service.exchange_link(token)

    def test_public_bundle_masks_phone_and_exposes_exact_calendar_only(self):
        issued = self.service.issue_senior_review(self.campaign_id)
        opened = self.service.exchange_link(self._token(issued))

        bundle = self.service.load_review(opened.session_token)

        self.assertEqual(bundle.client_name, "Example Client")
        self.assertEqual(bundle.calendar_version_id, self.calendar["id"])
        self.assertEqual(bundle.content_hash, self.calendar["content_hash"])
        self.assertEqual(bundle.headers, tuple(HEADERS))
        self.assertEqual(bundle.rows, tuple(ROWS))
        self.assertNotIn(SENIOR_PHONE, repr(bundle))
        self.assertTrue(bundle.reviewer_phone_masked.endswith("3210"))
        self.assertNotIn("phone_e164", repr(bundle.approvals))

    def test_senior_approval_atomically_creates_client_request_then_client_approves(
        self,
    ):
        senior = self.service.issue_senior_review(self.campaign_id)
        senior_session = self.service.exchange_link(self._token(senior))

        senior_result = self.service.decide(
            senior_session.session_token,
            senior_session.csrf_token,
            "approved",
            "Ready for the client.",
        )

        self.assertEqual(senior_result.campaign_status, "pending_client_review")
        self.assertIsNotNone(senior_result.next_review_request_id)
        self.assertIsNotNone(senior_result.next_notification_outbox_id)
        client_request = self.store.get_review_request(
            senior_result.next_review_request_id
        )
        self.assertEqual(client_request["role"], "client")
        self.assertEqual(client_request["calendar_version_id"], self.calendar["id"])
        self.assertEqual(client_request["content_hash"], self.calendar["content_hash"])

        client_token = self.service.reconstruct_review_token(client_request["id"])
        self.assertEqual(
            self.service.review_url_for_request(client_request["id"]),
            f"http://localhost:8000/r/{client_token}",
        )
        client_session = self.service.exchange_link(client_token)
        client_result = self.service.decide(
            client_session.session_token,
            client_session.csrf_token,
            "approved",
        )

        self.assertEqual(client_result.campaign_status, "fully_approved")
        approvals = self.store.list_approvals(self.campaign_id)
        self.assertEqual(
            [(item["role"], item["decision"]) for item in approvals],
            [("senior", "approved"), ("client", "approved")],
        )
        self.assertTrue(
            all(item["identity_channel"] == "whatsapp_link" for item in approvals)
        )
        self.assertTrue(all(item["approver_email"] is None for item in approvals))

    def test_invalid_csrf_and_missing_rejection_feedback_do_not_consume_session(self):
        issued = self.service.issue_senior_review(self.campaign_id)
        opened = self.service.exchange_link(self._token(issued))

        with self.assertRaises(ReviewDecisionError) as invalid_csrf:
            self.service.decide(
                opened.session_token,
                "cv1." + "A" * 43,
                "approved",
            )
        self.assertEqual(invalid_csrf.exception.code, "CSRF_INVALID")
        self.service.load_review(opened.session_token)

        with self.assertRaises(ReviewDecisionError) as no_feedback:
            self.service.decide(
                opened.session_token,
                opened.csrf_token,
                "rejected",
            )
        self.assertEqual(no_feedback.exception.code, "FEEDBACK_REQUIRED")
        result = self.service.decide(
            opened.session_token,
            opened.csrf_token,
            "rejected",
            "Please revise the CTA.",
        )
        self.assertEqual(result.campaign_status, "revision_required")

    def test_missing_client_reviewer_does_not_commit_senior_approval(self):
        other_store = CampaignStore(":memory:")
        self.addCleanup(other_store.close)
        client = other_store.create_or_update_client("No Client Reviewer")
        campaign = other_store.create_campaign(client["id"], {})
        other_store.complete_generation(campaign["id"], HEADERS, ROWS)
        other_store.upsert_review_recipient(
            campaign["id"],
            "senior",
            "Senior Only",
            SENIOR_PHONE,
            self._iso(self.now - timedelta(minutes=1)),
        )
        service = ReviewService(other_store, self.config, clock=lambda: self.now)
        issued = service.issue_senior_review(campaign["id"])
        opened = service.exchange_link(self._token(issued))

        with self.assertRaises(ReviewDecisionError) as raised:
            service.decide(opened.session_token, opened.csrf_token, "approved")

        self.assertEqual(raised.exception.code, "CLIENT_REVIEWER_MISSING")
        self.assertEqual(
            other_store.get_campaign(campaign["id"])["status"],
            "pending_senior_review",
        )
        self.assertEqual(other_store.list_approvals(campaign["id"]), [])
        service.load_review(opened.session_token)

    def test_tampered_link_error_never_echoes_capability_secret_or_phone(self):
        issued = self.service.issue_senior_review(self.campaign_id)
        token = self._token(issued)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

        with self.assertRaises(ReviewLinkUnavailable) as raised:
            self.service.preview_link(tampered)

        message = str(raised.exception)
        self.assertNotIn(tampered, message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(SENIOR_PHONE, message)

    def test_service_clock_enforces_expiry_without_mutating_request(self):
        current = {"value": self.now}
        service = ReviewService(self.store, self.config, clock=lambda: current["value"])
        issued = service.issue_senior_review(self.campaign_id)
        current["value"] = self.now + timedelta(hours=73)

        with self.assertRaises(ReviewLinkUnavailable):
            service.preview_link(self._token(issued))

        self.assertEqual(
            self.store.get_review_request(issued.review_request_id)["status"],
            "pending",
        )

    def test_notification_worker_sends_strict_contract_and_marks_sent(self):
        captured = []

        def fake_sender(webhook_url, webhook_secret, payload):
            captured.append((webhook_url, webhook_secret, payload))
            return ReviewNotificationResult(
                event_id=payload["event_id"],
                review_request_id=payload["review_request_id"],
                status="accepted",
                provider="whatsapp_cloud_api",
                provider_message_id="wamid.test-123",
            )

        service = ReviewService(
            self.store,
            self.config,
            notification_sender=fake_sender,
            clock=lambda: self.now,
        )
        issued = service.issue_senior_review(self.campaign_id)
        attempts = service.process_notification_outbox(limit=10)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "sent")
        self.assertEqual(
            self.store.get_notification_outbox(issued.outbox_id)["status"], "sent"
        )
        webhook_url, webhook_secret, payload = captured[0]
        self.assertEqual(webhook_url, self.config.webhook_url)
        self.assertEqual(webhook_secret, self.config.webhook_secret)
        self.assertEqual(
            set(payload),
            {
                "contract_version",
                "event_id",
                "review_request_id",
                "campaign_id",
                "calendar_version_id",
                "content_hash",
                "recipient_name",
                "review_due_at",
                "review_token_suffix",
                "role",
                "recipient_phone_e164",
            },
        )
        self.assertEqual(payload["contract_version"], CONTRACT_VERSION)
        self.assertEqual(payload["recipient_phone_e164"], SENIOR_PHONE)
        self.assertRegex(
            payload["review_token_suffix"],
            re.compile(r"^rv1\.[0-9a-f-]{36}\.[1-9][0-9]*\.[A-Za-z0-9_-]{43}$"),
        )
        self.assertEqual(payload["review_token_suffix"], self._token(issued))

    def test_retryable_notification_error_is_rescheduled_without_leaking_error(self):
        def failing_sender(_url, _secret, _payload):
            raise ReviewNotificationError(
                "Temporary notification outage.",
                code="WEBHOOK_TIMEOUT",
                retryable=True,
            )

        service = ReviewService(
            self.store,
            self.config,
            notification_sender=failing_sender,
            clock=lambda: self.now,
        )
        issued = service.issue_senior_review(self.campaign_id)
        attempts = service.process_notification_outbox(limit=1)

        self.assertEqual(attempts[0].status, "pending")
        self.assertTrue(attempts[0].retryable)
        self.assertEqual(attempts[0].error_code, "WEBHOOK_TIMEOUT")
        outbox = self.store.get_notification_outbox(issued.outbox_id)
        self.assertEqual(outbox["status"], "pending")
        self.assertEqual(outbox["attempt_count"], 1)
        self.assertEqual(outbox["last_error_code"], "WEBHOOK_TIMEOUT")

    def test_unexpected_sender_exception_is_sanitized_and_job_is_failed(self):
        def unsafe_sender(_url, _secret, payload):
            raise RuntimeError(
                f"leak {SECRET} {SENIOR_PHONE} {payload['review_token_suffix']}"
            )

        service = ReviewService(
            self.store,
            self.config,
            notification_sender=unsafe_sender,
            clock=lambda: self.now,
        )
        issued = service.issue_senior_review(self.campaign_id)

        attempts = service.process_notification_outbox(limit=1)

        self.assertEqual(attempts[0].status, "failed")
        self.assertFalse(attempts[0].retryable)
        self.assertEqual(attempts[0].error_code, "NOTIFICATION_DELIVERY_FAILED")
        self.assertNotIn(SECRET, repr(attempts))
        self.assertNotIn(SENIOR_PHONE, repr(attempts))
        outbox = self.store.get_notification_outbox(issued.outbox_id)
        self.assertEqual(outbox["last_error_code"], "NOTIFICATION_DELIVERY_FAILED")


if __name__ == "__main__":
    unittest.main()
