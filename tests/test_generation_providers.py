import json
import unittest
from pathlib import Path

import requests

from generation_providers import (
    GenerationProviderError,
    N8N_CALENDAR_CONTRACT_VERSION,
    generate_calendar_content,
)


CALENDAR_MARKDOWN = """| Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA |
| --- | --- | --- | --- | --- | --- | --- |
| Mon, Aug 24 | Instagram | Educational | Image | Helpful idea | useful keyword | Learn more |"""


class FakeResponse:
    def __init__(self, status_code, data=None, raw_content=None, headers=None):
        self.status_code = status_code
        self._data = data
        if raw_content is None:
            raw_content = json.dumps(data).encode("utf-8") if data is not None else b""
        self.content = raw_content
        self.headers = headers or {"Content-Length": str(len(self.content))}

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class RecordingHttpClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


class GenerationProviderTests(unittest.TestCase):
    def test_n8n_success_does_not_require_local_groq_key(self):
        def handler(_url, kwargs):
            body = kwargs["json"]
            return FakeResponse(
                200,
                {
                    "contract_version": N8N_CALENDAR_CONTRACT_VERSION,
                    "ok": True,
                    "request_id": body["request_id"],
                    "campaign_id": None,
                    "provider": "n8n",
                    "upstream_provider": "groq",
                    "model": body["model"],
                    "expected_posts": body["expected_posts"],
                    "calendar_markdown": CALENDAR_MARKDOWN,
                    "finish_reason": "stop",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    },
                },
            )

        client = RecordingHttpClient(handler)
        result = generate_calendar_content(
            provider="n8n",
            system_prompt="system",
            user_prompt="user",
            model="openai/gpt-oss-120b",
            expected_posts=1,
            groq_api_key="",
            n8n_webhook_url="http://localhost:5678/webhook/calendar",
            n8n_webhook_secret="shared-secret",
            request_id="request-123",
            http_client=client,
        )

        self.assertEqual(result.content, CALENDAR_MARKDOWN)
        self.assertEqual(result.request_id, "request-123")
        self.assertEqual(result.provider, "n8n")
        self.assertEqual(result.usage["total_tokens"], 30)
        _, call = client.calls[0]
        self.assertEqual(call["headers"]["X-Webhook-Secret"], "shared-secret")
        self.assertEqual(call["headers"]["Idempotency-Key"], "request-123")
        self.assertEqual(call["json"]["expected_posts"], 1)
        self.assertEqual(call["timeout"], (5, 120))

    def test_n8n_rejects_mismatched_request_id(self):
        client = RecordingHttpClient(
            lambda _url, _kwargs: FakeResponse(
                200,
                {
                    "contract_version": N8N_CALENDAR_CONTRACT_VERSION,
                    "ok": True,
                    "request_id": "another-request",
                    "calendar_markdown": CALENDAR_MARKDOWN,
                },
            )
        )

        with self.assertRaises(GenerationProviderError) as raised:
            generate_calendar_content(
                provider="n8n",
                system_prompt="system",
                user_prompt="user",
                model="openai/gpt-oss-120b",
                expected_posts=1,
                n8n_webhook_url="http://localhost:5678/webhook/calendar",
                n8n_webhook_secret="shared-secret",
                request_id="expected-request",
                http_client=client,
            )

        self.assertEqual(raised.exception.code, "N8N_REQUEST_ID_MISMATCH")
        self.assertEqual(raised.exception.request_id, "expected-request")

    def test_n8n_rejects_mismatched_model(self):
        def handler(_url, kwargs):
            body = kwargs["json"]
            return FakeResponse(
                200,
                {
                    "contract_version": N8N_CALENDAR_CONTRACT_VERSION,
                    "ok": True,
                    "request_id": body["request_id"],
                    "campaign_id": body.get("campaign_id"),
                    "model": "openai/gpt-oss-20b",
                    "expected_posts": body["expected_posts"],
                    "calendar_markdown": CALENDAR_MARKDOWN,
                },
            )

        with self.assertRaises(GenerationProviderError) as raised:
            generate_calendar_content(
                provider="n8n",
                system_prompt="system",
                user_prompt="user",
                model="openai/gpt-oss-120b",
                expected_posts=1,
                n8n_webhook_url="http://localhost:5678/webhook/calendar",
                n8n_webhook_secret="shared-secret",
                request_id="model-request",
                http_client=RecordingHttpClient(handler),
            )

        self.assertEqual(raised.exception.code, "N8N_MODEL_MISMATCH")

    def test_n8n_uses_safe_message_for_upstream_failure(self):
        client = RecordingHttpClient(
            lambda _url, _kwargs: FakeResponse(
                502,
                {
                    "contract_version": N8N_CALENDAR_CONTRACT_VERSION,
                    "ok": False,
                    "request_id": "request-123",
                    "error": {
                        "code": "GROQ_REQUEST_FAILED",
                        "message": "private upstream body that must not be displayed",
                        "retryable": True,
                    },
                },
            )
        )

        with self.assertRaises(GenerationProviderError) as raised:
            generate_calendar_content(
                provider="n8n",
                system_prompt="system",
                user_prompt="user",
                model="openai/gpt-oss-120b",
                expected_posts=1,
                n8n_webhook_url="http://localhost:5678/webhook/calendar",
                n8n_webhook_secret="shared-secret",
                request_id="request-123",
                http_client=client,
            )

        self.assertEqual(raised.exception.code, "GROQ_REQUEST_FAILED")
        self.assertNotIn("private upstream", str(raised.exception))
        self.assertTrue(raised.exception.retryable)

    def test_n8n_timeout_has_request_id_and_no_automatic_fallback(self):
        def timeout(_url, _kwargs):
            raise requests.exceptions.Timeout("timed out")

        client = RecordingHttpClient(timeout)
        with self.assertRaises(GenerationProviderError) as raised:
            generate_calendar_content(
                provider="n8n",
                system_prompt="system",
                user_prompt="user",
                model="openai/gpt-oss-120b",
                expected_posts=1,
                groq_api_key="unused-local-key",
                n8n_webhook_url="http://localhost:5678/webhook/calendar",
                n8n_webhook_secret="shared-secret",
                request_id="timeout-request",
                http_client=client,
            )

        self.assertEqual(raised.exception.code, "N8N_TIMEOUT")
        self.assertEqual(raised.exception.request_id, "timeout-request")
        self.assertEqual(len(client.calls), 1)

    def test_direct_groq_preserves_gpt_oss_reasoning_controls(self):
        def handler(_url, _kwargs):
            return FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {"content": CALENDAR_MARKDOWN},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        client = RecordingHttpClient(handler)
        result = generate_calendar_content(
            provider="groq",
            system_prompt="system",
            user_prompt="user",
            model="openai/gpt-oss-120b",
            expected_posts=1,
            groq_api_key="test-key",
            request_id="groq-request",
            http_client=client,
        )

        self.assertEqual(result.content, CALENDAR_MARKDOWN)
        _, call = client.calls[0]
        self.assertEqual(call["json"]["reasoning_effort"], "low")
        self.assertFalse(call["json"]["include_reasoning"])
        self.assertEqual(call["json"]["max_completion_tokens"], 8192)
        self.assertEqual(call["timeout"], (5, 90))

    def test_direct_groq_requires_key(self):
        with self.assertRaises(GenerationProviderError) as raised:
            generate_calendar_content(
                provider="groq",
                system_prompt="system",
                user_prompt="user",
                model="openai/gpt-oss-120b",
                expected_posts=1,
                groq_api_key="",
                request_id="missing-key",
            )

        self.assertEqual(raised.exception.code, "GROQ_KEY_MISSING")

    def test_workflow_export_is_valid_json_and_contains_no_api_key(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "n8n_workflows"
            / "calendar_generate_v1.json"
        )
        raw = workflow_path.read_text(encoding="utf-8")
        workflow = json.loads(raw)

        self.assertEqual(workflow["name"], "AI Marketing - Generate Calendar v1")
        self.assertIn("Calendar Generate Webhook", {node["name"] for node in workflow["nodes"]})
        self.assertNotIn("gsk_", raw)


if __name__ == "__main__":
    unittest.main()
