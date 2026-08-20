"""Strict client for the n8n WhatsApp review-notification webhook."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping
from uuid import UUID

import requests


CONTRACT_VERSION = "marketing.whatsapp-review-notification.v1"
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
ALLOWED_ROLES = frozenset({"senior", "client"})
REVIEW_TOKEN_PATTERN = re.compile(
    r"^rv1\.[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[1-9]\d{0,11}\.[A-Za-z0-9_-]{43}$"
)


class ReviewNotificationError(RuntimeError):
    """A safe notification error that never includes credentials or link tokens."""

    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ReviewNotificationResult:
    event_id: str
    review_request_id: str
    status: str
    provider: str
    provider_message_id: str


def validate_phone_e164(value: str) -> str:
    """Return a normalized E.164 number or raise a user-safe validation error."""
    normalized = re.sub(r"[\s()-]", "", str(value or "").strip())
    if not E164_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Enter a WhatsApp number in international format, for example "
            "+919876543210."
        )
    return normalized


def _canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = UUID(str(value or "").strip())
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a valid UUID.") from error
    canonical = str(parsed)
    if canonical != str(value or "").strip().lower() or parsed.int == 0:
        raise ValueError(f"{label} must be a canonical UUID.")
    return canonical


def _iso_timestamp(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    iso_value = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO timestamp with a timezone.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be an ISO timestamp with a timezone.")
    return cleaned


def build_notification_payload(
    *,
    event_id: str,
    review_request_id: str,
    campaign_id: str,
    calendar_version_id: str,
    content_hash: str,
    role: str,
    recipient_name: str,
    recipient_phone_e164: str,
    review_due_at: str,
    review_token_suffix: str,
) -> dict[str, str]:
    """Build the allowlisted payload accepted by the n8n workflow."""
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in ALLOWED_ROLES:
        raise ValueError("Review role must be senior or client.")
    clean_name = str(recipient_name or "").strip()
    if (
        not clean_name
        or len(clean_name) > 120
        or any(ord(char) < 32 for char in clean_name)
    ):
        raise ValueError("recipient_name must be 1 to 120 characters.")
    clean_hash = str(content_hash or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", clean_hash) is None:
        raise ValueError("content_hash must be a lowercase SHA-256 digest.")
    clean_token = str(review_token_suffix or "").strip()
    if REVIEW_TOKEN_PATTERN.fullmatch(clean_token) is None:
        raise ValueError("review_token_suffix must be a valid signed review token.")

    payload = {
        "contract_version": CONTRACT_VERSION,
        "event_id": _canonical_uuid(event_id, "event_id"),
        "review_request_id": _canonical_uuid(
            review_request_id, "review_request_id"
        ),
        "campaign_id": _canonical_uuid(campaign_id, "campaign_id"),
        "calendar_version_id": _canonical_uuid(
            calendar_version_id, "calendar_version_id"
        ),
        "content_hash": clean_hash,
        "role": normalized_role,
        "recipient_name": clean_name,
        "recipient_phone_e164": validate_phone_e164(recipient_phone_e164),
        "review_due_at": _iso_timestamp(review_due_at, "review_due_at"),
        "review_token_suffix": clean_token,
    }
    return payload


def send_review_notification(
    webhook_url: str,
    webhook_secret: str,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float = 20.0,
    post: Callable[..., Any] = requests.post,
) -> ReviewNotificationResult:
    """Send one idempotent notification request and validate n8n's response."""
    url = str(webhook_url or "").strip()
    secret = str(webhook_secret or "").strip()
    if not url:
        raise ReviewNotificationError(
            "The WhatsApp notification webhook is not configured.",
            code="WEBHOOK_URL_MISSING",
        )
    if not secret:
        raise ReviewNotificationError(
            "The WhatsApp notification webhook secret is not configured.",
            code="WEBHOOK_SECRET_MISSING",
        )

    body = dict(payload)
    if body.get("contract_version") != CONTRACT_VERSION:
        raise ReviewNotificationError(
            "The WhatsApp notification contract version is invalid.",
            code="CONTRACT_INVALID",
        )
    event_id = str(body.get("event_id") or "").strip()
    review_request_id = str(body.get("review_request_id") or "").strip()
    if not event_id or not review_request_id:
        raise ReviewNotificationError(
            "The WhatsApp notification identifiers are missing.",
            code="CONTRACT_INVALID",
        )

    try:
        response = post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": secret,
                "X-Request-ID": event_id,
                "Idempotency-Key": event_id,
            },
            timeout=(5.0, float(timeout_seconds)),
        )
    except requests.Timeout as error:
        raise ReviewNotificationError(
            "The WhatsApp notification request timed out and remains retryable.",
            code="WEBHOOK_TIMEOUT",
            retryable=True,
        ) from error
    except requests.RequestException as error:
        raise ReviewNotificationError(
            "The WhatsApp notification service could not be reached.",
            code="WEBHOOK_UNAVAILABLE",
            retryable=True,
        ) from error

    if response.status_code in {401, 403}:
        raise ReviewNotificationError(
            "The WhatsApp notification webhook credential is invalid.",
            code="WEBHOOK_UNAUTHORIZED",
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise ReviewNotificationError(
            "The WhatsApp notification service is temporarily unavailable.",
            code="WEBHOOK_RETRYABLE",
            retryable=True,
        )
    if response.status_code not in {200, 201, 202}:
        raise ReviewNotificationError(
            "The WhatsApp notification request was rejected.",
            code="WEBHOOK_REJECTED",
        )

    try:
        data = response.json()
    except (TypeError, ValueError) as error:
        raise ReviewNotificationError(
            "The WhatsApp notification service returned an invalid response.",
            code="RESPONSE_INVALID",
            retryable=True,
        ) from error
    if not isinstance(data, dict):
        raise ReviewNotificationError(
            "The WhatsApp notification service returned an invalid response.",
            code="RESPONSE_INVALID",
            retryable=True,
        )

    expected = {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "event_id": event_id,
        "review_request_id": review_request_id,
        "status": "accepted",
        "provider": "whatsapp_cloud_api",
    }
    if any(data.get(name) != value for name, value in expected.items()):
        raise ReviewNotificationError(
            "The WhatsApp notification service response did not match the request.",
            code="RESPONSE_MISMATCH",
            retryable=True,
        )
    provider_message_id = str(data.get("provider_message_id") or "").strip()
    if not provider_message_id:
        raise ReviewNotificationError(
            "WhatsApp did not return a message identifier.",
            code="PROVIDER_ID_MISSING",
            retryable=True,
        )

    return ReviewNotificationResult(
        event_id=event_id,
        review_request_id=review_request_id,
        status="accepted",
        provider="whatsapp_cloud_api",
        provider_message_id=provider_message_id,
    )
