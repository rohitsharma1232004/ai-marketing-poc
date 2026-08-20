import unittest
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from review_api import (
    CSRF_COOKIE,
    DEV_CSRF_COOKIE,
    DEV_SESSION_COOKIE,
    SESSION_COOKIE,
    _reject_placeholder_setting,
    create_app,
)
from review_service import (
    OpenedReviewSession,
    PublicReviewBundle,
    ReviewDecisionError,
    ReviewDecisionResult,
    ReviewLinkPreview,
    ReviewLinkUnavailable,
    ReviewSessionUnavailable,
)


TOKEN = "rv1.11111111-1111-4111-8111-111111111111.secret-link-signature"
SESSION = "private-browser-session-value"
CSRF = "derived-csrf-value"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
OTHER_REQUEST_ID = "22222222-2222-4222-8222-222222222222"
CAMPAIGN_ID = "33333333-3333-4333-8333-333333333333"
CALENDAR_ID = "44444444-4444-4444-8444-444444444444"
OUTBOX_ID = "55555555-5555-4555-8555-555555555555"


def review_bundle(*, role="senior"):
    return PublicReviewBundle(
        campaign_id=CAMPAIGN_ID,
        campaign_status=(
            "pending_senior_review" if role == "senior" else "pending_client_review"
        ),
        client_name="Example Client",
        review_request_id=REQUEST_ID,
        role=role,
        reviewer_name="Review Person",
        reviewer_phone_masked="+91••••3210",
        session_expires_at="2026-08-20T12:30:00Z",
        calendar_version_id=CALENDAR_ID,
        calendar_version=2,
        content_hash="a" * 64,
        headers=("Date", "SEO Keyword Focus"),
        rows=(
            ("__WEEK_HEADING__:Week 1 (Aug 24 - Aug 30)",),
            ("Mon, Aug 24", "safe water purifier"),
        ),
        approvals=(),
    )


class FakeReviewService:
    def __init__(self):
        self.bundle = review_bundle()
        self.preview_calls = 0
        self.exchange_calls = 0
        self.load_calls = 0
        self.decide_calls = []
        self.outbox_calls = 0
        self.preview_error = None
        self.decision_error = None
        self.outbox_error = None
        self.result = ReviewDecisionResult(
            approval_id="66666666-6666-4666-8666-666666666666",
            campaign_id=CAMPAIGN_ID,
            role="senior",
            decision="approved",
            campaign_status="pending_client_review",
            next_review_request_id=OTHER_REQUEST_ID,
            next_notification_outbox_id=OUTBOX_ID,
        )

    def preview_link(self, token):
        self.preview_calls += 1
        if self.preview_error:
            raise self.preview_error
        if token != TOKEN:
            raise ReviewLinkUnavailable(
                "internal secret must not escape", code="REVIEW_LINK_UNAVAILABLE"
            )
        return ReviewLinkPreview(
            review_request_id=REQUEST_ID,
            role=self.bundle.role,
            expires_at="2026-08-23T12:00:00Z",
        )

    def exchange_link(self, token):
        self.exchange_calls += 1
        if token != TOKEN:
            raise ReviewLinkUnavailable(
                "internal secret must not escape", code="REVIEW_LINK_UNAVAILABLE"
            )
        return OpenedReviewSession(
            review_request_id=REQUEST_ID,
            role=self.bundle.role,
            expires_at="2026-08-20T12:30:00Z",
            session_token=SESSION,
            csrf_token=CSRF,
        )

    def load_review(self, session_token):
        self.load_calls += 1
        if session_token != SESSION:
            raise ReviewSessionUnavailable(
                "database detail must not escape", code="REVIEW_SESSION_UNAVAILABLE"
            )
        return self.bundle

    def decide(self, session_token, csrf_token, decision, feedback=""):
        self.decide_calls.append((session_token, csrf_token, decision, feedback))
        if self.decision_error:
            raise self.decision_error
        return self.result

    def process_notification_outbox(self, *, limit=10):
        self.outbox_calls += 1
        if self.outbox_error:
            raise self.outbox_error
        return []


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeReviewService()
        self.client = TestClient(
            create_app(self.service),
            base_url="https://review.example.test",
        )

    def open_review_session(self):
        response = self.client.post(
            f"/r/{TOKEN}/open",
            data={"intent": "open_review"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        return response

    def decision_form(self, **overrides):
        values = {
            "review_request_id": REQUEST_ID,
            "csrf_token": CSRF,
            "decision": "approved",
            "feedback": "",
        }
        values.update(overrides)
        return values

    def test_get_link_is_read_only_and_does_not_echo_bearer_token(self):
        response = self.client.get(f"/r/{TOKEN}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.preview_calls, 1)
        self.assertEqual(self.service.exchange_calls, 0)
        self.assertEqual(self.service.decide_calls, [])
        self.assertEqual(self.service.outbox_calls, 0)
        self.assertNotIn(TOKEN, response.text)
        self.assertIn("does not approve anything", response.text)

    def test_link_exchange_sets_hardened_cookie_and_clean_redirect(self):
        response = self.open_review_session()

        self.assertEqual(response.headers["location"], f"/review/{REQUEST_ID}")
        self.assertNotIn(TOKEN, response.headers["location"])
        self.assertNotIn(TOKEN, response.text)
        cookie_headers = "\n".join(response.headers.get_list("set-cookie")).lower()
        self.assertIn(SESSION_COOKIE.lower(), cookie_headers)
        self.assertIn(CSRF_COOKIE.lower(), cookie_headers)
        self.assertIn("secure", cookie_headers)
        self.assertIn("httponly", cookie_headers)
        self.assertIn("samesite=lax", cookie_headers)
        self.assertNotIn(SESSION, response.text)

    def test_interstitial_empty_action_posts_without_echoing_token(self):
        preview = self.client.get(f"/r/{TOKEN}")
        self.assertIn('action=""', preview.text)

        response = self.client.post(
            f"/r/{TOKEN}",
            data={"intent": "open_review"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/review/{REQUEST_ID}")

    def test_review_renders_exact_calendar_and_escapes_every_value(self):
        self.service.bundle = replace(
            self.service.bundle,
            client_name='<script>alert("client")</script>',
            reviewer_name='<img src=x onerror="alert(1)">',
            headers=("Date", '<script>alert("header")</script>'),
            rows=(
                ("__WEEK_HEADING__:Week <one>",),
                ("Mon, Aug 24", '</td><script>alert("cell")</script>'),
            ),
            approvals=(
                {
                    "role": "senior",
                    "decision": "approved",
                    "approver_name": "<b>Senior</b>",
                    "feedback": "<svg onload=alert(1)>",
                },
            ),
        )
        self.open_review_session()

        response = self.client.get(f"/review/{REQUEST_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>", response.text)
        self.assertNotIn("<img src=x", response.text)
        self.assertNotIn("<svg onload", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("&lt;img src=x", response.text)
        self.assertIn('colspan="2"', response.text)
        self.assertIn("Week &lt;one&gt;", response.text)
        self.assertNotIn("__WEEK_HEADING__", response.text)
        self.assertIn("+91••••3210", response.text)

    def test_csrf_and_request_binding_are_required_before_decision(self):
        self.open_review_session()

        missing_csrf = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            data={"review_request_id": REQUEST_ID, "decision": "approved"},
        )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(self.service.decide_calls, [])

        wrong_request = self.client.post(
            f"/review/{OTHER_REQUEST_ID}/decision",
            data=self.decision_form(review_request_id=OTHER_REQUEST_ID),
        )
        self.assertEqual(wrong_request.status_code, 410)
        self.assertEqual(self.service.decide_calls, [])

        self.open_review_session()
        wrong_csrf = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            data=self.decision_form(csrf_token="attacker-value"),
        )
        self.assertEqual(wrong_csrf.status_code, 400)
        self.assertEqual(self.service.decide_calls, [])

    def test_local_http_mode_uses_non_host_dev_cookies_and_remains_usable(self):
        self.service.config = SimpleNamespace(public_base_url="http://localhost:8000")
        client = TestClient(create_app(self.service), base_url="http://localhost:8000")

        exchanged = client.post(
            f"/r/{TOKEN}/open",
            data={"intent": "open_review"},
            follow_redirects=False,
        )
        cookie_headers = "\n".join(exchanged.headers.get_list("set-cookie")).lower()
        self.assertIn(DEV_SESSION_COOKIE, cookie_headers)
        self.assertIn(DEV_CSRF_COOKIE, cookie_headers)
        self.assertNotIn("; secure", cookie_headers)
        self.assertNotIn("__host-", cookie_headers)

        review = client.get(f"/review/{REQUEST_ID}")
        self.assertEqual(review.status_code, 200)
        self.assertIn("Example Client", review.text)

    def test_placeholder_signing_secrets_are_rejected(self):
        placeholders = (
            "replace_with_at_least_32_random_characters",
            "your_secret_that_is_long_enough_123456",
            "example-development-secret-value-123456",
        )
        for value in placeholders:
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, "private random value"):
                    _reject_placeholder_setting(
                        "APPROVAL_LINK_SIGNING_SECRET", value
                    )

    def test_senior_approval_is_saved_before_best_effort_client_delivery(self):
        self.service.outbox_error = RuntimeError(
            "provider response with gsk_private_value"
        )
        self.open_review_session()

        response = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            data=self.decision_form(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Senior approval saved", response.text)
        self.assertIn("queued for WhatsApp", response.text)
        self.assertEqual(
            self.service.decide_calls,
            [(SESSION, CSRF, "approved", "")],
        )
        self.assertEqual(self.service.outbox_calls, 1)
        self.assertNotIn("gsk_private_value", response.text)
        self.assertNotIn(SESSION, response.text)
        self.assertNotIn(SESSION_COOKIE, self.client.cookies)

    def test_client_approval_reports_excel_is_unlocked(self):
        self.service.bundle = review_bundle(role="client")
        self.service.result = ReviewDecisionResult(
            approval_id="66666666-6666-4666-8666-666666666666",
            campaign_id=CAMPAIGN_ID,
            role="client",
            decision="approved",
            campaign_status="fully_approved",
            next_review_request_id=None,
            next_notification_outbox_id=None,
        )
        self.open_review_session()

        response = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            data=self.decision_form(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Calendar fully approved", response.text)
        self.assertIn("Excel download is now unlocked", response.text)
        self.assertEqual(self.service.outbox_calls, 0)

    def test_decision_errors_are_mapped_without_raw_error_or_secret(self):
        raw_secret = "gsk_test_placeholder"
        self.service.decision_error = ReviewDecisionError(
            f"database failed with {raw_secret}", code="DECISION_NOT_SAVED"
        )
        self.open_review_session()

        response = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            data=self.decision_form(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("decision could not be saved", response.text.lower())
        self.assertNotIn(raw_secret, response.text)

    def test_invalid_link_is_generic_and_does_not_echo_internal_details(self):
        raw_secret = "signing-secret-and-database-path"
        self.service.preview_error = ReviewLinkUnavailable(
            raw_secret, code="REVIEW_LINK_UNAVAILABLE"
        )

        response = self.client.get(f"/r/{TOKEN}")

        self.assertEqual(response.status_code, 410)
        self.assertIn("invalid, expired, already used, or has been replaced", response.text)
        self.assertNotIn(raw_secret, response.text)
        self.assertNotIn(TOKEN, response.text)

    def test_json_and_duplicate_form_fields_cannot_call_decision(self):
        self.open_review_session()

        json_response = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            json=self.decision_form(),
        )
        self.assertEqual(json_response.status_code, 400)

        duplicate_response = self.client.post(
            f"/review/{REQUEST_ID}/decision",
            content=(
                f"review_request_id={REQUEST_ID}&csrf_token={CSRF}"
                "&decision=approved&decision=rejected"
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(duplicate_response.status_code, 400)
        self.assertEqual(self.service.decide_calls, [])

    def test_security_headers_are_present_on_html_json_and_redirects(self):
        responses = [
            self.client.get("/healthz"),
            self.client.get(f"/r/{TOKEN}"),
            self.client.post(
                f"/r/{TOKEN}/open",
                data={"intent": "open_review"},
                follow_redirects=False,
            ),
        ]
        for response in responses:
            with self.subTest(status=response.status_code):
                self.assertEqual(
                    response.headers["cache-control"], "no-store, max-age=0"
                )
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn(
                    "frame-ancestors 'none'",
                    response.headers["content-security-policy"],
                )


if __name__ == "__main__":
    unittest.main()
