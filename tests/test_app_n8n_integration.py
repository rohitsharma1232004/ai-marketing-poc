import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from campaign_store import CampaignStore


class CalendarWebhookHandler(BaseHTTPRequestHandler):
    received_headers = None
    received_body = None
    blank_first_keyword = False

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))
        type(self).received_headers = dict(self.headers)
        type(self).received_body = body

        posts = int(body["expected_posts"])
        lines = [
            "| Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA | Caption | Reel Script |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for index in range(posts):
            keyword = "" if self.blank_first_keyword and index == 0 else f"keyword {index + 1}"
            lines.append(
                "| Placeholder | Instagram | Educational | Image | "
                f"Content idea {index + 1} | {keyword} | Learn more | "
                f"Helpful caption {index + 1} | Not applicable |"
            )

        response_body = json.dumps(
            {
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
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, _format, *_args):
        return


class AppN8nIntegrationTests(unittest.TestCase):
    @staticmethod
    def _widget(app, collection_name, label):
        collection = getattr(app, collection_name)
        try:
            return next(item for item in collection if item.label == label)
        except StopIteration as error:
            labels = [getattr(item, "label", None) for item in collection]
            raise AssertionError(
                f"Could not find {collection_name} labelled {label!r}; found {labels!r}"
            ) from error

    @classmethod
    def _set_text(cls, app, label, value):
        for collection_name in ("text_input", "text_area"):
            for item in getattr(app, collection_name):
                if item.label == label:
                    item.set_value(value)
                    return
        raise AssertionError(f"Could not find a text widget labelled {label!r}")

    @staticmethod
    def _has_download(app, label="Download Approved Content Package Excel"):
        return any(item.label == label for item in app.download_button)

    def test_streamlit_requires_senior_approval_before_excel(self):
        CalendarWebhookHandler.blank_first_keyword = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), CalendarWebhookHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        webhook_url = f"http://127.0.0.1:{server.server_port}/calendar"
        database_directory = tempfile.TemporaryDirectory()

        environment = {
            "CALENDAR_GENERATION_PROVIDER": "n8n",
            "N8N_CALENDAR_WEBHOOK_URL": webhook_url,
            "N8N_WEBHOOK_SECRET": "integration-test-secret",
            "CAMPAIGN_DB_PATH": str(Path(database_directory.name) / "campaigns.sqlite3"),
            "GENERATED_OUTPUT_DIR": str(Path(database_directory.name) / "exports"),
        }
        try:
            with patch.dict(os.environ, environment, clear=False):
                app_path = Path(__file__).resolve().parents[1] / "app.py"
                app = AppTest.from_file(str(app_path), default_timeout=20)
                app.secrets.update(environment)
                app.run()
                self._widget(app, "number_input", "Image").set_value(12)
                self._widget(app, "number_input", "Educational").set_value(12)
                self._widget(app, "button", "Generate Content Package").click().run(timeout=20)

                store = CampaignStore(environment["CAMPAIGN_DB_PATH"])
                saved_campaigns = store.list_campaigns()
                campaign_id = saved_campaigns[0]["id"]
                saved_campaign = store.get_campaign(campaign_id)
                saved_calendar = store.get_latest_calendar(campaign_id)
                export_directory = Path(environment["GENERATED_OUTPUT_DIR"])
                excel_before_approval = list(export_directory.glob("*.xlsx"))
                download_before_approval = self._has_download(app)
                errors_before_approval = [item.value for item in app.error]
                exceptions_before_approval = [item.message for item in app.exception]

                store.record_approval(
                    campaign_id,
                    saved_calendar["id"],
                    "senior",
                    "approved",
                    "Senior Reviewer",
                    "senior@example.com",
                    senior_is_final=True,
                )

                # Reopen the same persisted campaign after exact-version Senior approval.
                app = AppTest.from_file(str(app_path), default_timeout=20)
                app.secrets.update(environment)
                app.run()
                self._set_text(app, "Campaign ID", campaign_id)
                self._widget(app, "button", "Open Saved Calendar").click().run(timeout=20)
                download_after_approval = self._has_download(app)
                reopened_campaign_id = app.session_state["campaign_id"]
                excel_after_approval = list(export_directory.glob("*.xlsx"))
                errors_after_approval = [item.value for item in app.error]
                exceptions_after_approval = [item.message for item in app.exception]

                self.assertEqual(excel_before_approval, [])
                self.assertFalse(download_before_approval)
                self.assertEqual(len(excel_after_approval), 1)
                self.assertTrue(download_after_approval)

                with zipfile.ZipFile(excel_after_approval[0]) as workbook:
                    client_details_xml = workbook.read(
                        "xl/worksheets/sheet2.xml"
                    ).decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            database_directory.cleanup()

        self.assertEqual(
            CalendarWebhookHandler.received_headers.get("X-Webhook-Secret"),
            "integration-test-secret",
        )
        self.assertEqual(
            CalendarWebhookHandler.received_body["contract_version"],
            "calendar.generate.v1",
        )
        self.assertEqual(CalendarWebhookHandler.received_body["expected_posts"], 12)
        self.assertEqual(CalendarWebhookHandler.received_body["campaign_id"], campaign_id)
        self.assertEqual(len(saved_campaigns), 1)
        self.assertEqual(saved_campaign["status"], "pending_senior_review")
        self.assertEqual(
            saved_campaign["intake"]["format_mix"],
            [{"label": "Image", "count": 12}],
        )
        self.assertEqual(
            saved_campaign["intake"]["pillar_mix"],
            [{"label": "Educational", "count": 12}],
        )
        self.assertIsNotNone(saved_calendar)
        self.assertEqual(errors_before_approval, [])
        self.assertEqual(exceptions_before_approval, [])
        self.assertEqual(errors_after_approval, [])
        self.assertEqual(exceptions_after_approval, [])
        self.assertEqual(reopened_campaign_id, campaign_id)
        self.assertIn(campaign_id, client_details_xml)
        self.assertIn(saved_calendar["id"], client_details_xml)
        self.assertIn(saved_calendar["content_hash"], client_details_xml)

    def test_streamlit_rejects_blank_required_calendar_cell(self):
        CalendarWebhookHandler.blank_first_keyword = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), CalendarWebhookHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        webhook_url = f"http://127.0.0.1:{server.server_port}/calendar"
        database_directory = tempfile.TemporaryDirectory()

        environment = {
            "CALENDAR_GENERATION_PROVIDER": "n8n",
            "N8N_CALENDAR_WEBHOOK_URL": webhook_url,
            "N8N_WEBHOOK_SECRET": "integration-test-secret",
            "CAMPAIGN_DB_PATH": str(Path(database_directory.name) / "campaigns.sqlite3"),
            "GENERATED_OUTPUT_DIR": str(Path(database_directory.name) / "exports"),
        }
        try:
            with patch.dict(os.environ, environment, clear=False):
                app_path = Path(__file__).resolve().parents[1] / "app.py"
                app = AppTest.from_file(str(app_path), default_timeout=20)
                app.secrets.update(environment)
                app.run()
                self._widget(app, "button", "Generate Content Package").click().run(timeout=20)
                store = CampaignStore(environment["CAMPAIGN_DB_PATH"])
                saved_campaigns = store.list_campaigns()
                saved_calendar = store.get_latest_calendar(saved_campaigns[0]["id"])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            CalendarWebhookHandler.blank_first_keyword = False
            database_directory.cleanup()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 1)
        self.assertIn("blank required cells", app.error[0].value)
        self.assertEqual(saved_campaigns[0]["status"], "generation_failed")
        self.assertIsNone(saved_calendar)


if __name__ == "__main__":
    unittest.main()
