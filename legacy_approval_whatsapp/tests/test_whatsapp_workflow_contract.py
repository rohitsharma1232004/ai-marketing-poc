import ast
import json
import re
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "n8n_workflows"
    / "whatsapp_review_notify_v1.json"
)
CONTRACT_VERSION = "marketing.whatsapp-review-notification.v1"
REQUEST_FIELDS = {
    "contract_version",
    "event_id",
    "review_request_id",
    "campaign_id",
    "calendar_version_id",
    "content_hash",
    "role",
    "recipient_name",
    "recipient_phone_e164",
    "review_due_at",
    "review_token_suffix",
}


def _workflow():
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _nodes_by_name(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def test_whatsapp_review_workflow_is_importable_and_has_expected_graph():
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    assert workflow["name"] == "AI Marketing - WhatsApp Review Notify v1"
    assert workflow["active"] is False
    assert len(workflow["nodes"]) == 6
    assert len(nodes) == 6
    assert set(nodes) == {
        "WhatsApp Review Notify Webhook",
        "Validate Review Notification Contract",
        "Request Valid?",
        "Send WhatsApp Review Template",
        "Build Notification Response",
        "Return Notification Response",
    }

    webhook = nodes["WhatsApp Review Notify Webhook"]
    assert webhook["type"] == "n8n-nodes-base.webhook"
    assert webhook["parameters"]["httpMethod"] == "POST"
    assert webhook["parameters"]["authentication"] == "headerAuth"
    assert webhook["parameters"]["responseMode"] == "responseNode"

    branch = workflow["connections"]["Request Valid?"]["main"]
    assert branch[0][0]["node"] == "Send WhatsApp Review Template"
    assert branch[1][0]["node"] == "Return Notification Response"


def test_whatsapp_review_workflow_contract_and_template_are_locked_down():
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)
    serialized = json.dumps(workflow)

    validator = nodes["Validate Review Notification Contract"]["parameters"][
        "jsCode"
    ]
    allowed_match = re.search(r"new Set\((\[[^\]]+\])\)", validator)
    assert allowed_match is not None
    assert set(ast.literal_eval(allowed_match.group(1))) == REQUEST_FIELDS
    assert CONTRACT_VERSION in validator
    assert "unknown request fields" in validator
    assert "recipient_phone_e164 must use E.164 format" in validator
    assert "review_token_suffix must be a bound rv1 review token" in validator
    assert "tokenParts[1] === data.review_request_id" in validator
    assert "Math.floor(Date.parse(data.review_due_at) / 1000) === tokenExpiry" in (
        validator
    )

    whatsapp = nodes["Send WhatsApp Review Template"]
    params = whatsapp["parameters"]
    assert whatsapp["type"] == "n8n-nodes-base.whatsApp"
    assert whatsapp["typeVersion"] == 1.1
    assert params["operation"] == "sendTemplate"
    assert params["phoneNumberId"] == ""
    assert params["recipientPhoneNumber"] == (
        "={{ $json.recipient_phone_e164 }}"
    )
    assert params["template"] == "content_calendar_review_v1|en_US"

    components = params["components"]["component"]
    body_parameters = components[0]["bodyParameters"]["parameter"]
    assert [item["text"] for item in body_parameters] == [
        "={{ $json.recipient_name }}",
        "={{ $json.role }}",
        "={{ $json.campaign_id }}",
        "={{ $json.review_due_at }}",
    ]
    button = components[1]
    assert button["type"] == "button"
    assert button["sub_type"] == "url"
    assert button["index"] == 0
    assert button["buttonParameters"]["parameter"]["text"] == (
        "={{ $json.review_token_suffix }}"
    )

    response_builder = nodes["Build Notification Response"]["parameters"][
        "jsCode"
    ]
    for response_field in (
        "event_id",
        "review_request_id",
        "provider_message_id",
        "status:'accepted'",
        "provider:'whatsapp_cloud_api'",
    ):
        assert response_field in response_builder

    for forbidden in (
        "gsk_",
        "Bearer EAA",
        "$execution.resumeUrl",
        "recipient_message",
        "sender_phone_e164",
    ):
        assert forbidden not in serialized
