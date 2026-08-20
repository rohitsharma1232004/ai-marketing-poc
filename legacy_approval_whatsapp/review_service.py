"""Application service for secure WhatsApp calendar-review links.

The SQLite store remains the authority for ordering, exact calendar versions,
single-use sessions, and immutable decisions.  This layer owns the capabilities
that must never be stored in plaintext: signed review-link tokens, session
cookies, CSRF values, and the notification payload sent to n8n.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from campaign_store import CampaignStore, CampaignStoreError
from review_notifications import (
    ReviewNotificationError,
    ReviewNotificationResult,
    build_notification_payload,
    send_review_notification,
)
from review_tokens import (
    ExpiredReviewToken,
    InvalidReviewToken,
    create_review_token,
    derive_csrf_token,
    generate_session_token,
    hash_session_token,
    hash_token,
    normalize_expires_at,
    verify_csrf_token,
    verify_review_token,
)


Clock = Callable[[], datetime]
NotificationSender = Callable[..., ReviewNotificationResult]


class ReviewServiceError(RuntimeError):
    """Base application error with a stable, non-sensitive machine code."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class ReviewServiceConfigError(ReviewServiceError, ValueError):
    """Raised when server-side review configuration is unsafe or incomplete."""


class ReviewLinkUnavailable(ReviewServiceError):
    """Raised for every invalid, expired, revoked, or already-used link."""


class ReviewSessionUnavailable(ReviewServiceError):
    """Raised when a browser review session is missing, expired, or consumed."""


class ReviewDecisionError(ReviewServiceError):
    """Raised when a decision is invalid or cannot be committed."""


@dataclass(frozen=True)
class ReviewServiceConfig:
    """Validated server-side configuration for external review links."""

    signing_secret: str = field(repr=False)
    public_base_url: str
    webhook_url: str = ""
    webhook_secret: str = field(default="", repr=False)
    review_ttl_hours: int = 72
    session_ttl_minutes: int = 30
    notification_max_attempts: int = 5
    notification_retry_minutes: int = 5
    allow_insecure_localhost: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.signing_secret, str)
            or len(self.signing_secret.encode("utf-8")) < 32
        ):
            raise ReviewServiceConfigError(
                "The approval signing secret must be at least 32 bytes.",
                code="SIGNING_SECRET_INVALID",
            )
        if not isinstance(self.allow_insecure_localhost, bool):
            raise ReviewServiceConfigError(
                "allow_insecure_localhost must be true or false.",
                code="PUBLIC_URL_INVALID",
            )
        object.__setattr__(
            self,
            "public_base_url",
            _validated_public_base_url(
                self.public_base_url,
                allow_insecure_localhost=self.allow_insecure_localhost,
            ),
        )
        webhook_url = str(self.webhook_url or "").strip()
        webhook_secret = str(self.webhook_secret or "").strip()
        if bool(webhook_url) != bool(webhook_secret):
            raise ReviewServiceConfigError(
                "Configure both the n8n webhook URL and its shared secret.",
                code="WEBHOOK_CONFIG_INCOMPLETE",
            )
        if webhook_url:
            object.__setattr__(self, "webhook_url", _validated_webhook_url(webhook_url))
            object.__setattr__(self, "webhook_secret", webhook_secret)
        _bounded_integer(self.review_ttl_hours, "review_ttl_hours", 1, 168)
        _bounded_integer(self.session_ttl_minutes, "session_ttl_minutes", 5, 120)
        _bounded_integer(
            self.notification_max_attempts, "notification_max_attempts", 1, 20
        )
        _bounded_integer(
            self.notification_retry_minutes, "notification_retry_minutes", 1, 1440
        )


@dataclass(frozen=True)
class IssuedReview:
    review_request_id: str
    campaign_id: str
    calendar_version_id: str
    role: str
    expires_at: str
    review_url: str = field(repr=False)
    outbox_id: str | None


@dataclass(frozen=True)
class ReviewLinkPreview:
    review_request_id: str
    role: str
    expires_at: str


@dataclass(frozen=True)
class OpenedReviewSession:
    review_request_id: str
    role: str
    expires_at: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class PublicReviewBundle:
    campaign_id: str
    campaign_status: str
    client_name: str
    review_request_id: str
    role: str
    reviewer_name: str
    reviewer_phone_masked: str
    session_expires_at: str
    calendar_version_id: str
    calendar_version: int
    content_hash: str
    headers: tuple[str, ...]
    rows: tuple[Any, ...]
    approvals: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReviewDecisionResult:
    approval_id: str
    campaign_id: str
    role: str
    decision: str
    campaign_status: str
    next_review_request_id: str | None
    next_notification_outbox_id: str | None


@dataclass(frozen=True)
class NotificationAttempt:
    outbox_id: str
    review_request_id: str
    status: str
    retryable: bool
    provider_message_id: str | None = None
    error_code: str | None = None


class ReviewService:
    """Coordinate secure review links without persisting bearer capabilities."""

    def __init__(
        self,
        store: CampaignStore,
        config: ReviewServiceConfig,
        *,
        notification_sender: NotificationSender = send_review_notification,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(config, ReviewServiceConfig):
            raise TypeError("config must be a ReviewServiceConfig instance.")
        if not callable(notification_sender):
            raise TypeError("notification_sender must be callable.")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable.")
        self.store = store
        self.config = config
        self._notification_sender = notification_sender
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue_senior_review(self, campaign_id: str) -> IssuedReview:
        """Create the initial senior request and identifier-only outbox job."""

        try:
            bundle = self.store.get_campaign_review_bundle(campaign_id)
            campaign = bundle["campaign"]
            calendar = bundle["latest_calendar"]
            if campaign["status"] != "pending_senior_review" or calendar is None:
                raise ReviewDecisionError(
                    "This campaign is not ready for senior review.",
                    code="CAMPAIGN_NOT_READY",
                )
            recipient = self._recipient_for(campaign["id"], "senior")
            request_id = str(uuid4())
            expires_at = self._future_iso(hours=self.config.review_ttl_hours)
            token = create_review_token(
                request_id, expires_at, self.config.signing_secret
            )
            created = self.store.create_review_request(
                campaign["id"],
                calendar["id"],
                "senior",
                recipient["id"],
                hash_token(token),
                expires_at,
                review_request_id=request_id,
                outbox_dedupe_key=f"whatsapp-review:{request_id}:v1",
            )
        except ReviewServiceError:
            raise
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewDecisionError(
                "The senior review request could not be created.",
                code="REVIEW_ISSUE_FAILED",
            ) from error

        request = created["review_request"]
        outbox = created.get("outbox")
        return IssuedReview(
            review_request_id=request["id"],
            campaign_id=request["campaign_id"],
            calendar_version_id=request["calendar_version_id"],
            role=request["role"],
            expires_at=request["expires_at"],
            review_url=self._review_url(token),
            outbox_id=outbox["id"] if outbox else None,
        )

    def preview_link(self, token: str) -> ReviewLinkPreview:
        """Validate a link for GET/interstitial rendering without mutating state."""

        request = self._active_request_from_token(token)
        return ReviewLinkPreview(
            review_request_id=request["id"],
            role=request["role"],
            expires_at=request["expires_at"],
        )

    def exchange_link(self, token: str) -> OpenedReviewSession:
        """Exchange a one-use link capability for a short-lived browser session."""

        preview = self.preview_link(token)
        session_token = generate_session_token()
        now = self._now()
        link_expiry = datetime.fromtimestamp(
            normalize_expires_at(preview.expires_at), tz=timezone.utc
        )
        session_expiry = min(
            link_expiry,
            now + timedelta(minutes=self.config.session_ttl_minutes),
        )
        expires_at = _iso_utc(session_expiry)
        try:
            opened = self.store.open_review_session(
                hash_token(token),
                hash_session_token(session_token),
                expires_at,
            )
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewLinkUnavailable(
                "This review link is no longer available.",
                code="REVIEW_LINK_UNAVAILABLE",
            ) from error
        request = opened["review_request"]
        session = opened["review_session"]
        return OpenedReviewSession(
            review_request_id=request["id"],
            role=request["role"],
            expires_at=session["expires_at"],
            session_token=session_token,
            csrf_token=derive_csrf_token(session_token, self.config.signing_secret),
        )

    def load_review(self, session_token: str) -> PublicReviewBundle:
        """Load the exact version bound to a valid browser session, without PII."""

        try:
            session_hash = hash_session_token(session_token)
            bundle = self.store.get_review_session_bundle(session_hash)
            campaign = bundle["campaign"]
            calendar = bundle["latest_calendar"]
            request = bundle["review_request"]
            session = bundle["review_session"]
            recipient = bundle["recipient"]
            client = self.store.get_client(campaign["client_id"])
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewSessionUnavailable(
                "This review session is no longer available.",
                code="REVIEW_SESSION_UNAVAILABLE",
            ) from error

        approvals = tuple(_public_approval(item) for item in bundle["approvals"])
        return PublicReviewBundle(
            campaign_id=campaign["id"],
            campaign_status=campaign["status"],
            client_name=client["name"],
            review_request_id=request["id"],
            role=request["role"],
            reviewer_name=recipient["display_name"],
            reviewer_phone_masked=_mask_phone(recipient["phone_e164"]),
            session_expires_at=session["expires_at"],
            calendar_version_id=calendar["id"],
            calendar_version=calendar["version"],
            content_hash=calendar["content_hash"],
            headers=tuple(calendar["headers"]),
            rows=tuple(calendar["rows"]),
            approvals=approvals,
        )

    def decide(
        self,
        session_token: str,
        csrf_token: str,
        decision: str,
        feedback: str = "",
    ) -> ReviewDecisionResult:
        """Commit one decision and atomically enqueue client review when required."""

        try:
            csrf_valid = verify_csrf_token(
                csrf_token, session_token, self.config.signing_secret
            )
        except (TypeError, ValueError) as error:
            raise ReviewSessionUnavailable(
                "This review session is no longer available.",
                code="REVIEW_SESSION_UNAVAILABLE",
            ) from error
        if not csrf_valid:
            raise ReviewDecisionError(
                "The review form expired. Refresh the review page and try again.",
                code="CSRF_INVALID",
            )
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"approved", "rejected"}:
            raise ReviewDecisionError(
                "Choose Approve or Request changes.",
                code="DECISION_INVALID",
            )
        clean_feedback = str(feedback or "").strip()
        if len(clean_feedback) > 5_000:
            raise ReviewDecisionError(
                "Feedback must be 5,000 characters or fewer.",
                code="FEEDBACK_TOO_LONG",
            )
        if normalized_decision == "rejected" and not clean_feedback:
            raise ReviewDecisionError(
                "Add feedback explaining the requested changes.",
                code="FEEDBACK_REQUIRED",
            )

        bundle = self.load_review(session_token)
        next_spec: dict[str, str] | None = None
        if bundle.role == "senior" and normalized_decision == "approved":
            try:
                recipient = self._recipient_for(bundle.campaign_id, "client")
                request_id = str(uuid4())
                expires_at = self._future_iso(hours=self.config.review_ttl_hours)
                token = create_review_token(
                    request_id, expires_at, self.config.signing_secret
                )
                next_spec = {
                    "recipient_id": recipient["id"],
                    "token_hash": hash_token(token),
                    "expires_at": expires_at,
                    "review_request_id": request_id,
                    "outbox_dedupe_key": f"whatsapp-review:{request_id}:v1",
                }
            except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
                raise ReviewDecisionError(
                    "Configure a consented client WhatsApp reviewer before approval.",
                    code="CLIENT_REVIEWER_MISSING",
                ) from error

        try:
            result = self.store.decide_review_session(
                hash_session_token(session_token),
                normalized_decision,
                clean_feedback,
                next_review_request=next_spec,
            )
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewDecisionError(
                "The review decision could not be saved.",
                code="DECISION_NOT_SAVED",
            ) from error
        approval = result["approval"]
        campaign = result["campaign"]
        next_request = result.get("next_review_request")
        outbox = result.get("outbox")
        return ReviewDecisionResult(
            approval_id=approval["id"],
            campaign_id=campaign["id"],
            role=approval["role"],
            decision=approval["decision"],
            campaign_status=campaign["status"],
            next_review_request_id=next_request["id"] if next_request else None,
            next_notification_outbox_id=outbox["id"] if outbox else None,
        )

    def reconstruct_review_token(self, review_request_id: str) -> str:
        """Rebuild and cross-check a request token for an internal outbox worker."""

        try:
            request = self.store.get_review_request(review_request_id)
            token = create_review_token(
                request["id"], request["expires_at"], self.config.signing_secret
            )
            matched = self.store.get_review_request_by_token_hash(hash_token(token))
            if matched["id"] != request["id"]:
                raise ValueError("request mismatch")
            return token
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewLinkUnavailable(
                "The review notification link cannot be created.",
                code="REVIEW_TOKEN_UNAVAILABLE",
            ) from error

    def review_url_for_request(self, review_request_id: str) -> str:
        return self._review_url(self.reconstruct_review_token(review_request_id))

    def deliver_outbox(self, outbox_id: str) -> ReviewNotificationResult:
        """Deliver one already-claimed outbox job and persist its outcome."""

        try:
            outbox = self.store.get_notification_outbox(outbox_id)
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewServiceError(
                "The notification job is unavailable.", code="OUTBOX_UNAVAILABLE"
            ) from error
        if outbox["status"] != "processing":
            raise ReviewServiceError(
                "The notification job must be claimed before delivery.",
                code="OUTBOX_NOT_CLAIMED",
            )
        try:
            payload = self._notification_payload(outbox)
            result = self._notification_sender(
                self.config.webhook_url,
                self.config.webhook_secret,
                payload,
            )
            self.store.mark_notification_outbox(
                outbox["id"],
                "sent",
                provider_message_id=result.provider_message_id,
            )
            return result
        except ReviewNotificationError as error:
            self._mark_delivery_error(outbox, error.code, retryable=error.retryable)
            raise
        except ReviewServiceError as error:
            self._mark_delivery_error(outbox, error.code, retryable=False)
            raise
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            self._mark_delivery_error(
                outbox, "NOTIFICATION_BUILD_FAILED", retryable=False
            )
            raise ReviewServiceError(
                "The WhatsApp notification could not be prepared.",
                code="NOTIFICATION_BUILD_FAILED",
            ) from error
        except Exception as error:
            # The sender is an external boundary. Never allow an unexpected
            # transport exception to expose its URL, credentials, or payload.
            self._mark_delivery_error(
                outbox, "NOTIFICATION_DELIVERY_FAILED", retryable=False
            )
            raise ReviewServiceError(
                "The WhatsApp notification could not be delivered.",
                code="NOTIFICATION_DELIVERY_FAILED",
            ) from error

    def process_notification_outbox(
        self, *, limit: int = 10
    ) -> list[NotificationAttempt]:
        """Claim and deliver a bounded batch without aborting after one failure."""

        try:
            claimed = self.store.claim_notification_outbox(limit=limit)
        except (CampaignStoreError, TypeError, ValueError) as error:
            raise ReviewServiceError(
                "Notification jobs could not be claimed.", code="OUTBOX_CLAIM_FAILED"
            ) from error
        attempts: list[NotificationAttempt] = []
        for outbox in claimed:
            try:
                result = self.deliver_outbox(outbox["id"])
            except ReviewNotificationError as error:
                attempts.append(
                    NotificationAttempt(
                        outbox_id=outbox["id"],
                        review_request_id=outbox["review_request_id"],
                        status=self.store.get_notification_outbox(outbox["id"])[
                            "status"
                        ],
                        retryable=error.retryable,
                        error_code=error.code,
                    )
                )
            except ReviewServiceError as error:
                attempts.append(
                    NotificationAttempt(
                        outbox_id=outbox["id"],
                        review_request_id=outbox["review_request_id"],
                        status=self.store.get_notification_outbox(outbox["id"])[
                            "status"
                        ],
                        retryable=False,
                        error_code=error.code,
                    )
                )
            else:
                attempts.append(
                    NotificationAttempt(
                        outbox_id=outbox["id"],
                        review_request_id=outbox["review_request_id"],
                        status="sent",
                        retryable=False,
                        provider_message_id=result.provider_message_id,
                    )
                )
        return attempts

    def _active_request_from_token(self, token: str) -> dict[str, Any]:
        try:
            claims = verify_review_token(
                token, self.config.signing_secret, now=self._now()
            )
            request = self.store.get_review_request_by_token_hash(hash_token(token))
            if (
                request["id"] != claims.review_request_id
                or normalize_expires_at(request["expires_at"]) != claims.expires_at
                or request["status"] != "pending"
            ):
                raise ValueError("request mismatch")
            # This read performs the store's stage/latest-version/content-hash
            # checks. It intentionally does not open the link or mutate state.
            context = self.store.get_review_request_notification_bundle(request["id"])
            if context["review_request"]["id"] != request["id"]:
                raise ValueError("request context mismatch")
            return request
        except (ExpiredReviewToken, InvalidReviewToken, CampaignStoreError) as error:
            raise ReviewLinkUnavailable(
                "This review link is invalid, expired, or already used.",
                code="REVIEW_LINK_UNAVAILABLE",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ReviewLinkUnavailable(
                "This review link is invalid, expired, or already used.",
                code="REVIEW_LINK_UNAVAILABLE",
            ) from error

    def _recipient_for(self, campaign_id: str, role: str) -> dict[str, Any]:
        recipients = self.store.list_review_recipients(campaign_id)
        matched = [item for item in recipients if item.get("role") == role]
        if len(matched) != 1:
            raise ReviewDecisionError(
                f"Configure exactly one {role} WhatsApp reviewer.",
                code=f"{role.upper()}_REVIEWER_MISSING",
            )
        return matched[0]

    def _notification_payload(self, outbox: Mapping[str, Any]) -> dict[str, str]:
        if not self.config.webhook_url or not self.config.webhook_secret:
            raise ReviewServiceError(
                "WhatsApp notification delivery is not configured.",
                code="WEBHOOK_NOT_CONFIGURED",
            )
        request_id = str(outbox["review_request_id"])
        try:
            bundle = self.store.get_review_request_notification_bundle(request_id)
            request = bundle["review_request"]
            campaign = bundle["campaign"]
            calendar = bundle["latest_calendar"]
            recipient = bundle["recipient"]
            token = self.reconstruct_review_token(request_id)
            return build_notification_payload(
                event_id=str(outbox["id"]),
                review_request_id=request["id"],
                campaign_id=campaign["id"],
                calendar_version_id=calendar["id"],
                content_hash=calendar["content_hash"],
                role=request["role"],
                recipient_name=recipient["display_name"],
                recipient_phone_e164=recipient["phone_e164"],
                review_due_at=request["expires_at"],
                review_token_suffix=token,
            )
        except ReviewServiceError:
            raise
        except (CampaignStoreError, KeyError, TypeError, ValueError) as error:
            raise ReviewServiceError(
                "The WhatsApp notification could not be prepared.",
                code="NOTIFICATION_BUILD_FAILED",
            ) from error

    def _mark_delivery_error(
        self, outbox: Mapping[str, Any], error_code: str, *, retryable: bool
    ) -> None:
        attempts = int(outbox.get("attempt_count") or 0)
        if retryable and attempts < self.config.notification_max_attempts:
            multiplier = min(2 ** max(attempts - 1, 0), 12)
            retry_at = self._future_iso(
                minutes=self.config.notification_retry_minutes * multiplier
            )
            self.store.mark_notification_outbox(
                outbox["id"],
                "pending",
                error_code=str(error_code or "NOTIFICATION_FAILED")[:200],
                retry_at=retry_at,
            )
        else:
            self.store.mark_notification_outbox(
                outbox["id"],
                "failed",
                error_code=str(error_code or "NOTIFICATION_FAILED")[:200],
            )

    def _review_url(self, token: str) -> str:
        return f"{self.config.public_base_url}/r/{token}"

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ReviewServiceConfigError(
                "The review clock must return a timezone-aware time.",
                code="CLOCK_INVALID",
            )
        return value.astimezone(timezone.utc)

    def _future_iso(self, *, hours: int = 0, minutes: int = 0) -> str:
        return _iso_utc(self._now() + timedelta(hours=hours, minutes=minutes))


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewServiceConfigError(
            f"{label} must be an integer.", code="CONFIG_VALUE_INVALID"
        )
    if not minimum <= value <= maximum:
        raise ReviewServiceConfigError(
            f"{label} must be between {minimum} and {maximum}.",
            code="CONFIG_VALUE_INVALID",
        )
    return value


def _validated_public_base_url(value: str, *, allow_insecure_localhost: bool) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ReviewServiceConfigError(
            "The public approval URL is invalid.", code="PUBLIC_URL_INVALID"
        ) from error
    hostname = (parsed.hostname or "").lower()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    secure = parsed.scheme.lower() == "https"
    allowed_local_http = (
        parsed.scheme.lower() == "http" and local and allow_insecure_localhost
    )
    if (
        not raw
        or not hostname
        or not (secure or allowed_local_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReviewServiceConfigError(
            "Use a public HTTPS approval URL without credentials, query, or fragment.",
            code="PUBLIC_URL_INVALID",
        )
    netloc = parsed.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def _validated_webhook_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ReviewServiceConfigError(
            "The n8n webhook URL is invalid.", code="WEBHOOK_URL_INVALID"
        ) from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ReviewServiceConfigError(
            "The n8n webhook URL is invalid.", code="WEBHOOK_URL_INVALID"
        )
    return value.rstrip("/")


def _iso_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _mask_phone(phone: str) -> str:
    value = str(phone or "")
    if len(value) < 5:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


def _public_approval(approval: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "role",
        "decision",
        "approver_name",
        "feedback",
        "content_hash",
        "decided_at",
    )
    return {name: approval.get(name) for name in allowed}


__all__ = [
    "IssuedReview",
    "NotificationAttempt",
    "OpenedReviewSession",
    "PublicReviewBundle",
    "ReviewDecisionError",
    "ReviewDecisionResult",
    "ReviewLinkPreview",
    "ReviewLinkUnavailable",
    "ReviewService",
    "ReviewServiceConfig",
    "ReviewServiceConfigError",
    "ReviewServiceError",
    "ReviewSessionUnavailable",
]
