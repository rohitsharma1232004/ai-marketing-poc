import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from campaign_store import CampaignStore
from review_service import ReviewService, ReviewServiceConfig


WHATSAPP_CHECKBOX = "Send senior and client approval links through WhatsApp"
CONSENT_CHECKBOX = (
    "I confirm both reviewers asked to receive these WhatsApp review messages"
)
SIGNING_SECRET = "test-only-review-signing-secret-that-is-long-enough"
SENIOR_NAME = "Private Senior QA 8472"
CLIENT_NAME = "Private Client QA 5931"
SENIOR_PHONE = "+919876543210"
CLIENT_PHONE = "+919123456789"


class MarketingWebhookHandler(BaseHTTPRequestHandler):
    calendar_requests = []
    review_requests = []

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))

        if self.path == "/calendar":
            type(self).calendar_requests.append(
                {"headers": dict(self.headers), "body": body}
            )
            posts = int(body["expected_posts"])
            lines = [
                "| Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            for index in range(posts):
                lines.append(
                    "| Placeholder | Instagram | Educational | Image | "
                    f"Content idea {index + 1} | keyword {index + 1} | Learn more |"
                )
            response = {
                "contract_version": "calendar.generate.v1",
                "ok": True,
                "request_id": body["request_id"],
                "campaign_id": body.get("campaign_id"),
                "provider": "n8n",
                "upstream_provider": "groq",
                "model": body["model"],
                "expected_posts": posts,
                "calendar_markdown": "\n".join(lines),
                "finish_reason": "stop",
                "usage": None,
            }
        elif self.path == "/whatsapp":
            type(self).review_requests.append(
                {"headers": dict(self.headers), "body": body}
            )
            response = {
                "contract_version": "marketing.whatsapp-review-notification.v1",
                "ok": True,
                "event_id": body["event_id"],
                "review_request_id": body["review_request_id"],
                "status": "accepted",
                "provider": "whatsapp_cloud_api",
                "provider_message_id": f"wamid.test.{len(type(self).review_requests)}",
            }
        else:
            self.send_error(404)
            return

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


class AppWhatsAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        MarketingWebhookHandler.calendar_requests = []
        MarketingWebhookHandler.review_requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MarketingWebhookHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.app_path = Path(__file__).resolve().parents[1] / "app.py"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.temp_directory.cleanup()

    def environment(self, *, include_whatsapp_config=True):
        values = {
            "CALENDAR_GENERATION_PROVIDER": "n8n",
            "N8N_CALENDAR_WEBHOOK_URL": (
                f"http://127.0.0.1:{self.server.server_port}/calendar"
            ),
            "N8N_WEBHOOK_SECRET": "calendar-test-secret",
            "CAMPAIGN_DB_PATH": str(
                Path(self.temp_directory.name) / "campaigns.sqlite3"
            ),
            "APPROVED_OUTPUT_DIR": str(
                Path(self.temp_directory.name) / "approved-exports"
            ),
            "DEFAULT_WHATSAPP_APPROVALS": "false",
        }
        if include_whatsapp_config:
            values.update(
                {
                    "APPROVAL_LINK_SIGNING_SECRET": SIGNING_SECRET,
                    "APPROVAL_PUBLIC_BASE_URL": "https://review.example.test",
                    "N8N_WHATSAPP_REVIEW_WEBHOOK_URL": (
                        f"http://127.0.0.1:{self.server.server_port}/whatsapp"
                    ),
                    "N8N_REVIEW_WEBHOOK_SECRET": "review-test-secret",
                    "APPROVAL_LINK_TTL_HOURS": "72",
                    "APPROVAL_SESSION_TTL_MINUTES": "30",
                }
            )
        return values

    @staticmethod
    def widget(app, collection_name, label):
        collection = getattr(app, collection_name)
        try:
            return next(item for item in collection if item.label == label)
        except StopIteration as error:
            labels = [getattr(item, "label", None) for item in collection]
            raise AssertionError(
                f"Could not find {collection_name} labelled {label!r}; "
                f"found {labels!r}"
            ) from error

    @classmethod
    def set_text(cls, app, label, value):
        for collection_name in ("text_input", "text_area"):
            for item in getattr(app, collection_name):
                if item.label == label:
                    item.set_value(value)
                    return
        raise AssertionError(f"Could not find a text widget labelled {label!r}")

    @staticmethod
    def button_labels(app):
        return {item.label for item in app.button}

    @staticmethod
    def public_page_text(app):
        values = []
        for collection_name in (
            "title",
            "header",
            "subheader",
            "markdown",
            "caption",
            "text",
            "code",
            "info",
            "success",
            "warning",
            "error",
        ):
            for item in getattr(app, collection_name):
                values.append(str(getattr(item, "value", "")))
        return "\n".join(values)

    def start_app(self, environment):
        app = AppTest.from_file(str(self.app_path), default_timeout=20)
        app.secrets.update(environment)
        app.run()
        return app

    def enable_whatsapp(self, app):
        self.widget(app, "checkbox", WHATSAPP_CHECKBOX).set_value(True)

    def fill_whatsapp_reviewers(self, app):
        self.set_text(app, "Senior Approver Name", SENIOR_NAME)
        self.set_text(app, "Senior WhatsApp Number", SENIOR_PHONE)
        self.set_text(app, "Client Approver Name", CLIENT_NAME)
        self.set_text(app, "Client WhatsApp Number", CLIENT_PHONE)
        self.widget(app, "checkbox", CONSENT_CHECKBOX).set_value(True)

    def generate(self, app):
        self.widget(app, "button", "Generate Content Calendar").click().run(
            timeout=20
        )

    def test_checkbox_off_preserves_local_approval_flow(self):
        environment = self.environment()
        with patch.dict(os.environ, environment, clear=False):
            app = self.start_app(environment)
            self.generate(app)

            store = CampaignStore(environment["CAMPAIGN_DB_PATH"])
            campaign_summary = store.list_campaigns()[0]
            campaign = store.get_campaign(campaign_summary["id"])

        self.assertEqual(app.exception, [])
        self.assertEqual(campaign["intake"]["approval_delivery"], "local_self_reported")
        self.assertEqual(store.list_review_recipients(campaign["id"]), [])
        self.assertEqual(store.list_review_requests(campaign["id"]), [])
        self.assertEqual(store.list_notification_outbox(), [])
        self.assertIn("Senior Approve", self.button_labels(app))
        self.assertIn("Senior Reject", self.button_labels(app))
        self.assertEqual(MarketingWebhookHandler.review_requests, [])
        self.assertFalse(
            any(
                item.label == "Download Fully Approved Excel"
                for item in app.download_button
            )
        )

    def test_whatsapp_requires_names_consent_e164_and_server_configuration(self):
        environment = self.environment(include_whatsapp_config=False)
        with patch.dict(os.environ, environment, clear=False):
            app = self.start_app(environment)
            self.enable_whatsapp(app)
            self.generate(app)
            self.assertIn(
                "Enter both the Senior Approver Name and Client Approver Name.",
                [item.value for item in app.error],
            )

            self.set_text(app, "Senior Approver Name", SENIOR_NAME)
            self.set_text(app, "Client Approver Name", CLIENT_NAME)
            self.generate(app)
            self.assertTrue(
                any("agreed to receive WhatsApp" in item.value for item in app.error)
            )

            self.widget(app, "checkbox", CONSENT_CHECKBOX).set_value(True)
            self.set_text(app, "Senior WhatsApp Number", "987654")
            self.set_text(app, "Client WhatsApp Number", "123456")
            self.generate(app)
            self.assertTrue(
                any("international format" in item.value for item in app.error)
            )

            self.set_text(app, "Senior WhatsApp Number", SENIOR_PHONE)
            self.set_text(app, "Client WhatsApp Number", CLIENT_PHONE)
            self.generate(app)
            self.assertTrue(
                any(
                    "N8N_WHATSAPP_REVIEW_WEBHOOK_URL" in item.value
                    for item in app.error
                )
            )

            store = CampaignStore(environment["CAMPAIGN_DB_PATH"])

        self.assertEqual(app.exception, [])
        self.assertEqual(store.list_campaigns(), [])
        self.assertEqual(MarketingWebhookHandler.calendar_requests, [])
        self.assertEqual(MarketingWebhookHandler.review_requests, [])

    def test_whatsapp_generation_reload_external_decisions_and_excel_gate(self):
        environment = self.environment()
        with patch.dict(os.environ, environment, clear=False):
            app = self.start_app(environment)
            self.enable_whatsapp(app)
            self.fill_whatsapp_reviewers(app)
            self.generate(app)

            self.assertEqual(app.exception, [])
            self.assertEqual(app.error, [])
            store = CampaignStore(environment["CAMPAIGN_DB_PATH"])
            campaign_summary = store.list_campaigns()[0]
            campaign = store.get_campaign(campaign_summary["id"])
            campaign_id = campaign["id"]
            calendar = store.get_latest_calendar(campaign_id)
            recipients = {
                item["role"]: item
                for item in store.list_review_recipients(campaign_id)
            }
            senior_requests = store.list_review_requests(campaign_id, "senior")
            initial_outbox = store.list_notification_outbox()
            initial_exports = list(
                Path(environment["APPROVED_OUTPUT_DIR"]).glob("*.xlsx")
            )

            generation_body = MarketingWebhookHandler.calendar_requests[0]["body"]
            serialized_generation = json.dumps(generation_body)
            senior_delivery = MarketingWebhookHandler.review_requests[0]["body"]
            senior_token = senior_delivery["review_token_suffix"]

            # Reopen from only the full campaign ID, as a fresh browser session.
            app = self.start_app(environment)
            self.set_text(app, "Campaign ID", campaign_id)
            self.widget(app, "button", "Open Saved Campaign").click().run(
                timeout=20
            )

            config = ReviewServiceConfig(
                signing_secret=SIGNING_SECRET,
                public_base_url="https://review.example.test",
                webhook_url=environment["N8N_WHATSAPP_REVIEW_WEBHOOK_URL"],
                webhook_secret=environment["N8N_REVIEW_WEBHOOK_SECRET"],
            )
            service = ReviewService(store, config)
            opened_senior = service.exchange_link(senior_token)
            senior_decision = service.decide(
                opened_senior.session_token,
                opened_senior.csrf_token,
                "approved",
            )

            self.widget(app, "button", "Refresh Approval Status").click().run(
                timeout=20
            )
            status_after_senior_refresh = app.session_state["status"]
            exports_after_senior = list(
                Path(environment["APPROVED_OUTPUT_DIR"]).glob("*.xlsx")
            )
            client_request = store.get_review_request(
                senior_decision.next_review_request_id
            )
            client_token = service.reconstruct_review_token(client_request["id"])
            opened_client = service.exchange_link(client_token)
            service.decide(
                opened_client.session_token,
                opened_client.csrf_token,
                "approved",
            )

            self.widget(app, "button", "Refresh Approval Status").click().run(
                timeout=20
            )
            final_campaign = store.get_campaign(campaign_id)
            final_exports = list(
                Path(environment["APPROVED_OUTPUT_DIR"]).glob("*.xlsx")
            )
            final_public_text = self.public_page_text(app)

        self.assertIsNotNone(calendar)
        self.assertEqual(campaign["intake"]["approval_delivery"], "whatsapp_link")
        self.assertEqual(set(recipients), {"senior", "client"})
        self.assertEqual(recipients["senior"]["display_name"], SENIOR_NAME)
        self.assertEqual(recipients["senior"]["phone_e164"], SENIOR_PHONE)
        self.assertEqual(recipients["client"]["display_name"], CLIENT_NAME)
        self.assertEqual(recipients["client"]["phone_e164"], CLIENT_PHONE)
        self.assertEqual(len(senior_requests), 1)
        self.assertEqual(senior_requests[0]["calendar_version_id"], calendar["id"])
        self.assertEqual(len(initial_outbox), 1)
        self.assertEqual(initial_outbox[0]["status"], "sent")
        self.assertEqual(senior_delivery["role"], "senior")
        self.assertEqual(senior_delivery["recipient_phone_e164"], SENIOR_PHONE)
        self.assertNotIn(SENIOR_NAME, serialized_generation)
        self.assertNotIn(CLIENT_NAME, serialized_generation)
        self.assertNotIn(SENIOR_PHONE, serialized_generation)
        self.assertNotIn(CLIENT_PHONE, serialized_generation)
        self.assertEqual(initial_exports, [])
        self.assertEqual(app.session_state["approval_delivery"], "whatsapp_link")
        self.assertNotIn("Senior Approve", self.button_labels(app))
        self.assertNotIn("Senior Reject", self.button_labels(app))
        self.assertNotIn("Client Approve", self.button_labels(app))
        self.assertNotIn("Client Reject", self.button_labels(app))
        self.assertEqual(status_after_senior_refresh, "pending_client_review")
        self.assertEqual(exports_after_senior, [])
        self.assertEqual(final_campaign["status"], "fully_approved")
        self.assertEqual(len(final_exports), 1)
        self.assertTrue(
            any(
                item.label == "Download Fully Approved Excel"
                for item in app.download_button
            )
        )
        self.assertNotIn(SENIOR_PHONE, final_public_text)
        self.assertNotIn(CLIENT_PHONE, final_public_text)
        self.assertNotIn(senior_token, final_public_text)
        self.assertNotIn(client_token, final_public_text)
        self.assertIn("+91****3210", final_public_text)
        self.assertIn("+91****6789", final_public_text)


if __name__ == "__main__":
    unittest.main()
