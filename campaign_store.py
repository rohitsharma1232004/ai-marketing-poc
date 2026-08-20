"""Local SQLite persistence for clients, campaigns, and calendar versions.

The store deliberately accepts structured JSON-compatible values only. Uploaded
documents and generated assets belong in external/object storage; callers may
persist identifiers, metadata, and bounded text excerpts here, but not file
bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4


SCHEMA_VERSION = 4
CAMPAIGN_STATUSES = frozenset(
    {
        "generating",
        "generation_unknown",
        "pending_senior_review",
        "pending_client_review",
        "revision_required",
        "fully_approved",
        # Legacy v1 states remain readable during the staged UI rollout.
        "pending_review",
        "approved",
        "rejected",
        "generation_failed",
    }
)
ALLOWED_STATUS_TRANSITIONS = {
    "generating": frozenset(
        {
            "pending_senior_review",
            "pending_review",
            "generation_failed",
            "generation_unknown",
        }
    ),
    "generation_unknown": frozenset({"generating"}),
    "pending_senior_review": frozenset({"generating"}),
    "pending_client_review": frozenset({"generating"}),
    "revision_required": frozenset({"generating"}),
    "fully_approved": frozenset(),
    "pending_review": frozenset({"approved", "rejected", "generating"}),
    "approved": frozenset(),
    "rejected": frozenset({"generating"}),
    "generation_failed": frozenset({"generating"}),
}
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MAX_JSON_CHARS = 1_000_000
MAX_APPROVER_NAME_CHARS = 200
MAX_APPROVER_EMAIL_CHARS = 320
MAX_APPROVAL_FEEDBACK_CHARS = 5_000
MAX_RECIPIENT_NAME_CHARS = 200
MAX_MANUAL_SHARE_NOTE_CHARS = 2_000
MAX_DEDUPE_KEY_CHARS = 300
MAX_PROVIDER_ID_CHARS = 500
MAX_ERROR_CODE_CHARS = 200
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


class CampaignStoreError(RuntimeError):
    """Base class for persistence errors safe to handle at the app boundary."""


class RecordNotFound(CampaignStoreError):
    """Raised when a requested client or campaign does not exist."""


class StoreConflict(CampaignStoreError):
    """Raised when a stable identifier conflicts with an existing record."""


class InvalidStatusTransition(CampaignStoreError):
    """Raised when a campaign lifecycle transition is not allowed."""


class CampaignStore:
    """Small repository around a caller-selected SQLite database file."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
            raise TypeError("busy_timeout_ms must be an integer.")
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive.")

        self.busy_timeout_ms = busy_timeout_ms
        self._keeper: sqlite3.Connection | None = None
        raw_path = str(db_path)
        if not raw_path.strip():
            raise ValueError("db_path must not be empty.")

        if raw_path == ":memory:":
            # A shared-cache URI plus a keeper connection lets operation-scoped
            # connections behave like a normal file-backed database in tests.
            self.db_path = raw_path
            self._target = f"file:campaign_store_{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._new_connection()
        else:
            path = Path(db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
            self._target = self.db_path
            self._uri = False

        self._initialize_schema()

    def close(self) -> None:
        """Release the keeper used by an in-memory store; file stores need no close."""

        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def __enter__(self) -> "CampaignStore":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def create_or_update_client(
        self,
        name: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a client or update it by stable UUID/normalized name.

        Supplying an unknown UUID for a name already owned by another UUID is a
        conflict. This avoids silently merging two externally identified clients.
        """

        display_name, normalized_name = _normalize_client_name(name)
        clean_id = _canonical_uuid(client_id, "client_id") if client_id else None
        metadata_value = _require_mapping(metadata, "metadata")
        timestamp = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_id = (
                connection.execute(
                    "SELECT * FROM clients WHERE id = ?", (clean_id,)
                ).fetchone()
                if clean_id
                else None
            )
            by_name = connection.execute(
                "SELECT * FROM clients WHERE normalized_name = ?", (normalized_name,)
            ).fetchone()

            if by_id is not None:
                if by_name is not None and by_name["id"] != by_id["id"]:
                    raise StoreConflict(
                        "That normalized client name belongs to another client ID."
                    )
                record_id = by_id["id"]
                merged_metadata = _deserialize_json(by_id["metadata_json"])
                merged_metadata.update(metadata_value)
                connection.execute(
                    """
                    UPDATE clients
                    SET name = ?, normalized_name = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        display_name,
                        normalized_name,
                        _serialize_json(merged_metadata, "metadata"),
                        timestamp,
                        record_id,
                    ),
                )
            elif clean_id is not None and by_name is not None:
                raise StoreConflict(
                    "That normalized client name already has a different client ID."
                )
            elif by_name is not None:
                record_id = by_name["id"]
                merged_metadata = _deserialize_json(by_name["metadata_json"])
                merged_metadata.update(metadata_value)
                connection.execute(
                    """
                    UPDATE clients
                    SET name = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        display_name,
                        _serialize_json(merged_metadata, "metadata"),
                        timestamp,
                        record_id,
                    ),
                )
            else:
                record_id = clean_id or str(uuid4())
                connection.execute(
                    """
                    INSERT INTO clients (
                        id, name, normalized_name, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        display_name,
                        normalized_name,
                        _serialize_json(metadata_value, "metadata"),
                        timestamp,
                        timestamp,
                    ),
                )
            connection.commit()

        return self.get_client(record_id)

    # Familiar repository naming for callers that prefer "upsert".
    upsert_client = create_or_update_client

    def get_client(self, client_id: str) -> dict[str, Any]:
        clean_id = _canonical_uuid(client_id, "client_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM clients WHERE id = ?", (clean_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Client {clean_id} was not found.")
        return _client_from_row(row)

    def create_campaign(
        self,
        client_id: str,
        intake: Mapping[str, Any],
        *,
        external_id: str | None = None,
        request_id: str | None = None,
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a campaign in ``generating`` state after local validation."""

        clean_client_id = _canonical_uuid(client_id, "client_id")
        clean_campaign_id = (
            _canonical_uuid(campaign_id, "campaign_id")
            if campaign_id
            else str(uuid4())
        )
        clean_external_id = _optional_identifier(external_id, "external_id")
        clean_request_id = _optional_identifier(request_id, "request_id")
        intake_value = _require_mapping(intake, "intake")
        intake_json = _serialize_json(intake_value, "intake")
        timestamp = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not _row_exists(connection, "clients", clean_client_id):
                raise RecordNotFound(f"Client {clean_client_id} was not found.")
            try:
                connection.execute(
                    """
                    INSERT INTO campaigns (
                        id, client_id, external_id, request_id, status,
                        intake_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'generating', ?, ?, ?)
                    """,
                    (
                        clean_campaign_id,
                        clean_client_id,
                        clean_external_id,
                        clean_request_id,
                        intake_json,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    "The campaign ID or request ID is already in use."
                ) from error
            self._insert_event(
                connection,
                campaign_id=clean_campaign_id,
                event_type="campaign_created",
                details={
                    "external_id": clean_external_id,
                    "request_id": clean_request_id,
                },
                from_status=None,
                to_status="generating",
                timestamp=timestamp,
            )
            connection.commit()

        return self.get_campaign(clean_campaign_id)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id = ?", (clean_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Campaign {clean_id} was not found.")
        return _campaign_from_row(row)

    def list_campaigns(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return a bounded newest-first campaign summary including client name."""

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer.")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT campaigns.id, campaigns.client_id, campaigns.external_id,
                       campaigns.request_id, campaigns.status,
                       campaigns.created_at, campaigns.updated_at,
                       clients.name AS client_name
                FROM campaigns
                JOIN clients ON clients.id = campaigns.client_id
                ORDER BY campaigns.created_at DESC, campaigns.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_calendar_version(
        self,
        campaign_id: str,
        headers: Sequence[str],
        rows: Sequence[Any],
        *,
        client_metadata: Mapping[str, Any] | None = None,
        generation_metadata: Mapping[str, Any] | None = None,
        mark_pending_review: bool = False,
    ) -> dict[str, Any]:
        """Append an immutable generated calendar version.

        A version may only be added while the campaign is ``generating``. To
        regenerate, transition a review/rejected/failed campaign back first.
        """

        if not isinstance(mark_pending_review, bool):
            raise TypeError("mark_pending_review must be a boolean.")
        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        header_values = _validate_headers(headers)
        row_values = _validate_rows(rows)
        client_metadata_value = _require_mapping(client_metadata, "client_metadata")
        generation_metadata_value = _require_mapping(
            generation_metadata, "generation_metadata"
        )
        headers_json = _serialize_json(header_values, "headers")
        rows_json = _serialize_json(row_values, "rows")
        client_metadata_json = _serialize_json(
            client_metadata_value, "client_metadata"
        )
        generation_metadata_json = _serialize_json(
            generation_metadata_value, "generation_metadata"
        )
        content_hash = _calendar_content_hash(
            header_values,
            row_values,
            client_metadata_value,
            generation_metadata_value,
        )
        version_id = str(uuid4())
        timestamp = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT status FROM campaigns WHERE id = ?", (clean_id,)
            ).fetchone()
            if campaign is None:
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            if campaign["status"] != "generating":
                raise InvalidStatusTransition(
                    "Calendar versions can only be saved while a campaign is generating."
                )
            next_version = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM calendar_versions
                WHERE campaign_id = ?
                """,
                (clean_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO calendar_versions (
                    id, campaign_id, version, headers_json, rows_json,
                    client_metadata_json, generation_metadata_json, content_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    clean_id,
                    next_version,
                    headers_json,
                    rows_json,
                    client_metadata_json,
                    generation_metadata_json,
                    content_hash,
                    timestamp,
                ),
            )
            self._insert_event(
                connection,
                campaign_id=clean_id,
                event_type="calendar_version_saved",
                details={"calendar_version_id": version_id, "version": next_version},
                from_status="generating",
                to_status="generating",
                timestamp=timestamp,
            )
            if mark_pending_review:
                connection.execute(
                    "UPDATE campaigns SET status = 'pending_senior_review', "
                    "updated_at = ? WHERE id = ?",
                    (timestamp, clean_id),
                )
                self._insert_event(
                    connection,
                    campaign_id=clean_id,
                    event_type="generation_succeeded",
                    details={
                        "calendar_version_id": version_id,
                        "version": next_version,
                    },
                    from_status="generating",
                    to_status="pending_senior_review",
                    timestamp=timestamp,
                )
            connection.commit()

        return self._get_calendar_version(version_id)

    def complete_generation(
        self,
        campaign_id: str,
        headers: Sequence[str],
        rows: Sequence[Any],
        *,
        client_metadata: Mapping[str, Any] | None = None,
        generation_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically save a calendar and move it to senior review."""

        return self.save_calendar_version(
            campaign_id,
            headers,
            rows,
            client_metadata=client_metadata,
            generation_metadata=generation_metadata,
            mark_pending_review=True,
        )

    # Descriptive alias used by review-oriented UI integrations.
    save_calendar_for_review = complete_generation

    def get_latest_calendar(self, campaign_id: str) -> dict[str, Any] | None:
        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", clean_id):
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            row = connection.execute(
                """
                SELECT * FROM calendar_versions
                WHERE campaign_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
        return _calendar_from_row(row) if row is not None else None

    def list_calendar_versions(self, campaign_id: str) -> list[dict[str, Any]]:
        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", clean_id):
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            rows = connection.execute(
                """
                SELECT * FROM calendar_versions
                WHERE campaign_id = ?
                ORDER BY version ASC
                """,
                (clean_id,),
            ).fetchall()
        return [_calendar_from_row(row) for row in rows]

    def record_approval(
        self,
        campaign_id: str,
        calendar_version_id: str,
        role: str,
        decision: str,
        approver_name: str,
        approver_email: str,
        feedback: str = "",
    ) -> dict[str, Any]:
        """Append one version-bound decision and advance review atomically.

        A senior must decide first. Only a senior approval opens client review;
        either rejection sends the campaign to revision. Decisions are immutable,
        and the stored digest is recomputed before every decision so a modified
        calendar record cannot be approved under its original hash.
        """

        clean_campaign_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_version_id = _canonical_uuid(
            calendar_version_id, "calendar_version_id"
        )
        clean_role = _approval_choice(role, "role", {"senior", "client"})
        clean_decision = _approval_choice(
            decision, "decision", {"approved", "rejected"}
        )
        clean_name = _required_text(
            approver_name, "approver_name", max_length=MAX_APPROVER_NAME_CHARS
        )
        clean_email = _required_text(
            approver_email, "approver_email", max_length=MAX_APPROVER_EMAIL_CHARS
        )
        clean_feedback = _bounded_optional_text(
            feedback, "feedback", max_length=MAX_APPROVAL_FEEDBACK_CHARS
        )
        timestamp = _utc_now()
        approval_id = str(uuid4())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT status FROM campaigns WHERE id = ?",
                (clean_campaign_id,),
            ).fetchone()
            if campaign is None:
                raise RecordNotFound(
                    f"Campaign {clean_campaign_id} was not found."
                )

            version = connection.execute(
                """
                SELECT * FROM calendar_versions
                WHERE id = ? AND campaign_id = ?
                """,
                (clean_version_id, clean_campaign_id),
            ).fetchone()
            if version is None:
                raise RecordNotFound(
                    "That calendar version does not belong to this campaign."
                )
            latest_version = connection.execute(
                """
                SELECT id FROM calendar_versions
                WHERE campaign_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (clean_campaign_id,),
            ).fetchone()
            if latest_version is None or latest_version["id"] != clean_version_id:
                raise StoreConflict(
                    "Only the latest calendar version can receive a decision."
                )

            duplicate = connection.execute(
                """
                SELECT 1 FROM approvals
                WHERE campaign_id = ? AND calendar_version_id = ? AND role = ?
                """,
                (clean_campaign_id, clean_version_id, clean_role),
            ).fetchone()
            if duplicate is not None:
                raise StoreConflict(
                    f"The {clean_role} decision for this calendar version already exists."
                )

            calculated_hash = _calendar_content_hash(
                _deserialize_json(version["headers_json"]),
                _deserialize_json(version["rows_json"]),
                _deserialize_json(version["client_metadata_json"]),
                _deserialize_json(version["generation_metadata_json"]),
            )
            if calculated_hash != version["content_hash"]:
                raise StoreConflict(
                    "The calendar content no longer matches its stored hash."
                )

            old_status = campaign["status"]
            if old_status in {"approved", "fully_approved"}:
                raise InvalidStatusTransition(
                    "A fully approved campaign cannot receive another decision."
                )
            if clean_role == "senior":
                if old_status not in {"pending_senior_review", "pending_review"}:
                    raise InvalidStatusTransition(
                        "A senior decision is only allowed during senior review."
                    )
                new_status = (
                    "pending_client_review"
                    if clean_decision == "approved"
                    else "revision_required"
                )
            else:
                if old_status != "pending_client_review":
                    raise InvalidStatusTransition(
                        "A client decision requires prior senior approval."
                    )
                senior_approval = connection.execute(
                    """
                    SELECT content_hash FROM approvals
                    WHERE campaign_id = ? AND calendar_version_id = ?
                      AND role = 'senior' AND decision = 'approved'
                    """,
                    (clean_campaign_id, clean_version_id),
                ).fetchone()
                if (
                    senior_approval is None
                    or senior_approval["content_hash"] != calculated_hash
                ):
                    raise InvalidStatusTransition(
                        "Client review requires a hash-matched senior approval."
                    )
                new_status = (
                    "fully_approved"
                    if clean_decision == "approved"
                    else "revision_required"
                )

            try:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        id, campaign_id, calendar_version_id, role, decision,
                        approver_name, approver_email, approver_phone_e164,
                        identity_channel, review_request_id, feedback,
                        content_hash, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL,
                              'local_self_reported', NULL, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        clean_campaign_id,
                        clean_version_id,
                        clean_role,
                        clean_decision,
                        clean_name,
                        clean_email,
                        clean_feedback,
                        calculated_hash,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"The {clean_role} decision for this calendar version already exists."
                ) from error
            connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, timestamp, clean_campaign_id),
            )
            # Deliberately omit reviewer PII and feedback from the duplicate audit
            # stream. Those values live only in the access-controlled approval row.
            self._insert_event(
                connection,
                campaign_id=clean_campaign_id,
                event_type="approval_recorded",
                details={
                    "approval_id": approval_id,
                    "calendar_version_id": clean_version_id,
                    "role": clean_role,
                    "decision": clean_decision,
                    "content_hash": calculated_hash,
                },
                from_status=old_status,
                to_status=new_status,
                timestamp=timestamp,
            )
            connection.commit()

        return self._get_approval(approval_id)

    def list_approvals(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return every immutable approval decision for a campaign in order."""

        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", clean_id):
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE campaign_id = ?
                ORDER BY decided_at ASC, rowid ASC
                """,
                (clean_id,),
            ).fetchall()
        return [_approval_from_row(row) for row in rows]

    def get_campaign_review_bundle(self, campaign_id: str) -> dict[str, Any]:
        """Return campaign state, latest calendar, and its approval audit trail."""

        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id = ?", (clean_id,)
            ).fetchone()
            if campaign is None:
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            calendar = connection.execute(
                """
                SELECT * FROM calendar_versions
                WHERE campaign_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
            approvals = connection.execute(
                """
                SELECT * FROM approvals
                WHERE campaign_id = ?
                ORDER BY decided_at ASC, rowid ASC
                """,
                (clean_id,),
            ).fetchall()
        return {
            "campaign": _campaign_from_row(campaign),
            "latest_calendar": (
                _calendar_from_row(calendar) if calendar is not None else None
            ),
            "approvals": [_approval_from_row(row) for row in approvals],
        }

    # Manual client-share audit APIs appear before the automated review APIs.
    def upsert_review_recipient(
        self,
        campaign_id: str,
        role: str,
        display_name: str,
        phone_e164: str,
        consent_at: str,
        *,
        recipient_id: str | None = None,
    ) -> dict[str, Any]:
        """Configure one consented WhatsApp reviewer for a campaign role."""

        cid = _canonical_uuid(campaign_id, "campaign_id")
        clean_role = _approval_choice(role, "role", {"senior", "client"})
        name = _required_text(
            display_name, "display_name", max_length=MAX_RECIPIENT_NAME_CHARS
        )
        phone = _e164_phone(phone_e164)
        consent = _utc_timestamp(consent_at, "consent_at")
        requested_id = (
            _canonical_uuid(recipient_id, "recipient_id") if recipient_id else None
        )
        now = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not _row_exists(connection, "campaigns", cid):
                raise RecordNotFound(f"Campaign {cid} was not found.")
            existing = connection.execute(
                "SELECT * FROM review_recipients WHERE campaign_id=? AND role=?",
                (cid, clean_role),
            ).fetchone()
            if existing is not None:
                if requested_id and requested_id != existing["id"]:
                    raise StoreConflict(
                        f"The {clean_role} recipient already has a different ID."
                    )
                changed = (name, phone, consent) != (
                    existing["display_name"],
                    existing["phone_e164"],
                    existing["consent_at"],
                )
                active = connection.execute(
                    """
                    SELECT 1 FROM review_requests
                    WHERE recipient_id=? AND status IN ('pending','opened')
                      AND expires_at>? LIMIT 1
                    """,
                    (existing["id"], now),
                ).fetchone()
                if changed and active is not None:
                    raise StoreConflict(
                        "Revoke the active request before changing that recipient."
                    )
                if changed:
                    connection.execute(
                        """
                        UPDATE review_recipients
                        SET display_name=?, phone_e164=?, consent_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (name, phone, consent, now, existing["id"]),
                    )
                resolved_id = existing["id"]
            else:
                resolved_id = requested_id or str(uuid4())
                try:
                    connection.execute(
                        """
                        INSERT INTO review_recipients (
                            id, campaign_id, role, display_name, phone_e164,
                            consent_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (resolved_id, cid, clean_role, name, phone, consent, now, now),
                    )
                except sqlite3.IntegrityError as error:
                    raise StoreConflict(
                        "The recipient ID or campaign role is already configured."
                    ) from error
            self._insert_event(
                connection,
                campaign_id=cid,
                event_type="review_recipient_configured",
                details={"recipient_id": resolved_id, "role": clean_role},
                from_status=None,
                to_status=None,
                timestamp=now,
            )
            row = connection.execute(
                "SELECT * FROM review_recipients WHERE id=?", (resolved_id,)
            ).fetchone()
            connection.commit()
        return _review_recipient_from_row(row)

    def list_review_recipients(self, campaign_id: str) -> list[dict[str, Any]]:
        cid = _canonical_uuid(campaign_id, "campaign_id")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", cid):
                raise RecordNotFound(f"Campaign {cid} was not found.")
            rows = connection.execute(
                "SELECT * FROM review_recipients WHERE campaign_id=? ORDER BY role DESC",
                (cid,),
            ).fetchall()
        return [_review_recipient_from_row(row) for row in rows]

    def create_review_request(
        self,
        campaign_id: str,
        calendar_version_id: str,
        role: str,
        recipient_id: str,
        token_hash: str,
        expires_at: str,
        *,
        review_request_id: str | None = None,
        outbox_dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a stage-bound request and optional identifier-only outbox row."""

        spec = {
            "campaign_id": _canonical_uuid(campaign_id, "campaign_id"),
            "calendar_version_id": _canonical_uuid(
                calendar_version_id, "calendar_version_id"
            ),
            "role": _approval_choice(role, "role", {"senior", "client"}),
            "recipient_id": _canonical_uuid(recipient_id, "recipient_id"),
            "token_hash": _sha256_hash(token_hash, "token_hash"),
            "expires_at": _future_utc_timestamp(expires_at, "expires_at"),
            "review_request_id": (
                _canonical_uuid(review_request_id, "review_request_id")
                if review_request_id else str(uuid4())
            ),
            "outbox_dedupe_key": (
                _dedupe_key(outbox_dedupe_key)
                if outbox_dedupe_key is not None else None
            ),
        }
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request_id, outbox_id = self._insert_review_request_locked(
                connection, spec, now
            )
            connection.commit()
        return {
            "review_request": self.get_review_request(request_id),
            "outbox": (
                self._get_notification_outbox(outbox_id) if outbox_id else None
            ),
        }

    def get_review_request(self, review_request_id: str) -> dict[str, Any]:
        request_id = _canonical_uuid(review_request_id, "review_request_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Review request {request_id} was not found.")
        return _review_request_from_row(row)

    def get_review_request_by_token_hash(self, token_hash: str) -> dict[str, Any]:
        clean_hash = _sha256_hash(token_hash, "token_hash")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_requests WHERE token_hash=?", (clean_hash,)
            ).fetchone()
        if row is None:
            raise RecordNotFound("Review request was not found.")
        return _review_request_from_row(row)

    def list_review_requests(
        self,
        campaign_id: str,
        role: str | None = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded newest-first request metadata without credential hashes."""

        cid = _canonical_uuid(campaign_id, "campaign_id")
        clean_role = (
            _approval_choice(role, "role", {"senior", "client"})
            if role is not None else None
        )
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer.")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", cid):
                raise RecordNotFound(f"Campaign {cid} was not found.")
            if clean_role is None:
                rows = connection.execute(
                    "SELECT * FROM review_requests WHERE campaign_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                    (cid, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM review_requests WHERE campaign_id=? AND role=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                    (cid, clean_role, limit),
                ).fetchall()
        return [_review_request_from_row(row) for row in rows]

    def get_review_request_notification_bundle(
        self, review_request_id: str
    ) -> dict[str, Any]:
        """Return the separately authorized PII context needed by a sender."""

        request_id = _canonical_uuid(review_request_id, "review_request_id")
        now = _utc_now()
        with self._connection() as connection:
            request = connection.execute(
                "SELECT * FROM review_requests WHERE id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise RecordNotFound(f"Review request {request_id} was not found.")
            if request["status"] != "pending" or request["expires_at"] <= now:
                raise StoreConflict("That review request is not available for delivery.")
            campaign, calendar, recipient, _ = self._load_review_context_locked(
                connection, request, now
            )
        return {
            "campaign": _campaign_from_row(campaign),
            "latest_calendar": _calendar_from_row(calendar),
            "review_request": _review_request_from_row(request),
            "recipient": _review_recipient_from_row(recipient),
        }

    def get_notification_outbox(self, outbox_id: str) -> dict[str, Any]:
        clean_id = _canonical_uuid(outbox_id, "outbox_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (clean_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Outbox item {clean_id} was not found.")
        return _notification_outbox_from_row(row)

    _get_notification_outbox = get_notification_outbox

    def revoke_review_request(self, review_request_id: str) -> dict[str, Any]:
        request_id = _canonical_uuid(review_request_id, "review_request_id")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Review request {request_id} was not found.")
            if row["status"] == "decided":
                raise StoreConflict("A decided review request cannot be revoked.")
            if row["status"] != "revoked":
                connection.execute(
                    "UPDATE review_requests SET status='revoked',revoked_at=? WHERE id=?",
                    (now, request_id),
                )
                connection.execute(
                    "UPDATE review_sessions SET consumed_at=COALESCE(consumed_at,?) "
                    "WHERE review_request_id=?",
                    (now, request_id),
                )
                connection.execute(
                    "UPDATE notification_outbox SET status='cancelled',updated_at=? "
                    "WHERE review_request_id=? AND status IN ('pending','processing')",
                    (now, request_id),
                )
                self._insert_event(
                    connection, campaign_id=row["campaign_id"],
                    event_type="review_request_revoked",
                    details={"review_request_id": request_id, "role": row["role"]},
                    from_status=None, to_status=None, timestamp=now,
                )
            result = connection.execute(
                "SELECT * FROM review_requests WHERE id=?", (request_id,)
            ).fetchone()
            connection.commit()
        return _review_request_from_row(result)

    def open_review_session(
        self,
        token_hash: str,
        session_hash: str,
        expires_at: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Consume a one-use link token and create a short-lived review session."""

        clean_token_hash = _sha256_hash(token_hash, "token_hash")
        clean_session_hash = _sha256_hash(session_hash, "session_hash")
        clean_expires_at = _future_utc_timestamp(expires_at, "expires_at")
        clean_session_id = (
            _canonical_uuid(session_id, "session_id") if session_id else str(uuid4())
        )
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM review_requests WHERE token_hash=?",
                (clean_token_hash,),
            ).fetchone()
            if request is None:
                raise RecordNotFound("Review request was not found.")
            if request["status"] != "pending":
                raise StoreConflict("This review link has already been used or revoked.")
            if request["expires_at"] <= now:
                raise StoreConflict("This review link has expired.")
            if clean_expires_at > request["expires_at"]:
                raise ValueError("Session expiry cannot exceed review-link expiry.")
            self._load_review_context_locked(connection, request, now)
            try:
                connection.execute(
                    """
                    INSERT INTO review_sessions (
                        id, review_request_id, session_hash, expires_at,
                        consumed_at, created_at
                    ) VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (clean_session_id, request["id"], clean_session_hash,
                     clean_expires_at, now),
                )
                connection.execute(
                    "UPDATE review_requests SET status='opened',opened_at=? WHERE id=?",
                    (now, request["id"]),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict("That review session is already in use.") from error
            self._insert_event(
                connection, campaign_id=request["campaign_id"],
                event_type="review_link_opened",
                details={
                    "review_request_id": request["id"],
                    "review_session_id": clean_session_id,
                    "role": request["role"],
                },
                from_status=None, to_status=None, timestamp=now,
            )
            request_row = connection.execute(
                "SELECT * FROM review_requests WHERE id=?", (request["id"],)
            ).fetchone()
            session_row = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (clean_session_id,)
            ).fetchone()
            connection.commit()
        return {
            "review_request": _review_request_from_row(request_row),
            "review_session": _review_session_from_row(session_row),
        }

    def get_review_session_bundle(self, session_hash: str) -> dict[str, Any]:
        """Read a still-valid session's exact immutable review material."""

        clean_hash = _sha256_hash(session_hash, "session_hash")
        now = _utc_now()
        with self._connection() as connection:
            session = connection.execute(
                "SELECT * FROM review_sessions WHERE session_hash=?", (clean_hash,)
            ).fetchone()
            if session is None:
                raise RecordNotFound("Review session was not found.")
            if session["consumed_at"] is not None:
                raise StoreConflict("This review session has already been consumed.")
            if session["expires_at"] <= now:
                raise StoreConflict("This review session has expired.")
            request = connection.execute(
                "SELECT * FROM review_requests WHERE id=?",
                (session["review_request_id"],),
            ).fetchone()
            if request["status"] != "opened" or request["expires_at"] <= now:
                raise StoreConflict("This review request is no longer active.")
            campaign, calendar, recipient, _ = self._load_review_context_locked(
                connection, request, now
            )
            approvals = connection.execute(
                "SELECT * FROM approvals WHERE campaign_id=? "
                "ORDER BY decided_at,rowid",
                (request["campaign_id"],),
            ).fetchall()
        return {
            "campaign": _campaign_from_row(campaign),
            "latest_calendar": _calendar_from_row(calendar),
            "review_request": _review_request_from_row(request),
            "review_session": _review_session_from_row(session),
            "recipient": _review_recipient_from_row(recipient),
            "approvals": [_approval_from_row(row) for row in approvals],
        }

    def decide_review_session(
        self,
        session_hash: str,
        decision: str,
        feedback: str = "",
        *,
        next_review_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume a session and record a WhatsApp-identified decision atomically.

        A senior approval may include a precomputed client request specification;
        that request and its optional outbox marker commit with the approval.
        """

        clean_session_hash = _sha256_hash(session_hash, "session_hash")
        clean_decision = _approval_choice(
            decision, "decision", {"approved", "rejected"}
        )
        clean_feedback = _bounded_optional_text(
            feedback, "feedback", max_length=MAX_APPROVAL_FEEDBACK_CHARS
        )
        if clean_decision == "rejected" and not clean_feedback:
            raise ValueError("feedback is required when a review is rejected.")
        next_spec = _next_review_request_spec(next_review_request)
        if next_spec is not None and clean_decision != "approved":
            raise ValueError("A client request can only follow a senior approval.")
        now = _utc_now()
        approval_id = str(uuid4())
        next_request_id: str | None = None
        outbox_id: str | None = None

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM review_sessions WHERE session_hash=?",
                (clean_session_hash,),
            ).fetchone()
            if session is None:
                raise RecordNotFound("Review session was not found.")
            if session["consumed_at"] is not None:
                raise StoreConflict("This review session has already been consumed.")
            if session["expires_at"] <= now:
                raise StoreConflict("This review session has expired.")
            request = connection.execute(
                "SELECT * FROM review_requests WHERE id=?",
                (session["review_request_id"],),
            ).fetchone()
            if request["status"] != "opened":
                raise StoreConflict("This review request is no longer open.")
            if request["expires_at"] <= now:
                raise StoreConflict("This review request has expired.")
            campaign, calendar, recipient, content_hash = (
                self._load_review_context_locked(connection, request, now)
            )
            role = request["role"]
            if next_spec is not None and role != "senior":
                raise ValueError(
                    "Only a senior approval can create the next client request."
                )
            old_status = campaign["status"]
            new_status = (
                "pending_client_review"
                if role == "senior" and clean_decision == "approved"
                else "fully_approved"
                if role == "client" and clean_decision == "approved"
                else "revision_required"
            )
            try:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        id, campaign_id, calendar_version_id, role, decision,
                        approver_name, approver_email, approver_phone_e164,
                        identity_channel, review_request_id, feedback,
                        content_hash, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?,
                              'whatsapp_link', ?, ?, ?, ?)
                    """,
                    (approval_id, request["campaign_id"], request["calendar_version_id"],
                     role, clean_decision, recipient["display_name"],
                     recipient["phone_e164"], request["id"], clean_feedback,
                     content_hash, now),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"The {role} decision for this version already exists."
                ) from error
            connection.execute(
                "UPDATE campaigns SET status=?,updated_at=? WHERE id=?",
                (new_status, now, request["campaign_id"]),
            )
            connection.execute(
                "UPDATE review_requests SET status='decided',decided_at=? WHERE id=?",
                (now, request["id"]),
            )
            connection.execute(
                "UPDATE review_sessions SET consumed_at=? WHERE id=?",
                (now, session["id"]),
            )
            connection.execute(
                "UPDATE notification_outbox SET status='cancelled',updated_at=? "
                "WHERE review_request_id=? AND status IN ('pending','processing')",
                (now, request["id"]),
            )
            self._insert_event(
                connection, campaign_id=request["campaign_id"],
                event_type="approval_recorded",
                details={
                    "approval_id": approval_id,
                    "calendar_version_id": request["calendar_version_id"],
                    "review_request_id": request["id"],
                    "role": role, "decision": clean_decision,
                    "content_hash": content_hash,
                },
                from_status=old_status, to_status=new_status, timestamp=now,
            )
            if next_spec is not None:
                linked_spec = dict(next_spec)
                linked_spec.update({
                    "campaign_id": request["campaign_id"],
                    "calendar_version_id": request["calendar_version_id"],
                    "role": "client",
                })
                next_request_id, outbox_id = self._insert_review_request_locked(
                    connection, linked_spec, now
                )
            approval_row = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            campaign_row = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (request["campaign_id"],)
            ).fetchone()
            next_row = (
                connection.execute(
                    "SELECT * FROM review_requests WHERE id=?", (next_request_id,)
                ).fetchone()
                if next_request_id else None
            )
            outbox_row = (
                connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)
                ).fetchone()
                if outbox_id else None
            )
            connection.commit()
        return {
            "approval": _approval_from_row(approval_row),
            "campaign": _campaign_from_row(campaign_row),
            "next_review_request": (
                _review_request_from_row(next_row) if next_row else None
            ),
            "outbox": _notification_outbox_from_row(outbox_row) if outbox_row else None,
        }

    def list_notification_outbox(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List identifier-only notification jobs without recipient PII."""

        clean_status = (
            _approval_choice(
                status, "status",
                {"pending", "processing", "sent", "failed", "cancelled"},
            )
            if status is not None else None
        )
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer.")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._connection() as connection:
            if clean_status is None:
                rows = connection.execute(
                    "SELECT * FROM notification_outbox ORDER BY created_at,rowid LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM notification_outbox WHERE status=? "
                    "ORDER BY created_at,rowid LIMIT ?",
                    (clean_status, limit),
                ).fetchall()
        return [_notification_outbox_from_row(row) for row in rows]

    def claim_notification_outbox(
        self, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Atomically claim due jobs and increment their delivery attempts."""

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer.")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ids = [
                row["id"] for row in connection.execute(
                    """
                    SELECT id FROM notification_outbox
                    WHERE status='pending' AND available_at<=?
                    ORDER BY available_at,created_at,rowid LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
            for outbox_id in ids:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET status='processing',attempt_count=attempt_count+1,
                        claimed_at=?,updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (now, now, outbox_id),
                )
            rows = [
                connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (item,)
                ).fetchone()
                for item in ids
            ]
            connection.commit()
        return [_notification_outbox_from_row(row) for row in rows]

    def mark_notification_outbox(
        self,
        outbox_id: str,
        status: str,
        *,
        provider_message_id: str | None = None,
        error_code: str | None = None,
        retry_at: str | None = None,
    ) -> dict[str, Any]:
        """Complete, fail, or reschedule one claimed notification job."""

        clean_id = _canonical_uuid(outbox_id, "outbox_id")
        clean_status = _approval_choice(
            status, "status", {"sent", "failed", "pending"}
        )
        provider_id = (
            _required_text(
                provider_message_id, "provider_message_id",
                max_length=MAX_PROVIDER_ID_CHARS,
            )
            if provider_message_id is not None else None
        )
        clean_error = (
            _required_text(error_code, "error_code", max_length=MAX_ERROR_CODE_CHARS)
            if error_code is not None else None
        )
        if clean_status == "pending":
            if retry_at is None:
                raise ValueError("retry_at is required when rescheduling a job.")
            available_at = _future_utc_timestamp(retry_at, "retry_at")
        else:
            if retry_at is not None:
                raise ValueError("retry_at is only allowed for pending retries.")
            available_at = None
        now = _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (clean_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Outbox item {clean_id} was not found.")
            if row["status"] != "processing":
                raise StoreConflict("Only a claimed notification can be marked.")
            connection.execute(
                """
                UPDATE notification_outbox
                SET status=?, provider_message_id=?,
                    available_at=COALESCE(?,available_at),
                    claimed_at=CASE WHEN ?='pending' THEN NULL ELSE claimed_at END,
                    sent_at=CASE WHEN ?='sent' THEN ? ELSE NULL END,
                    last_error_code=?, updated_at=?
                WHERE id=?
                """,
                (clean_status, provider_id, available_at, clean_status,
                 clean_status, now, clean_error, now, clean_id),
            )
            result = connection.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (clean_id,)
            ).fetchone()
            connection.commit()
        return _notification_outbox_from_row(result)

    def transition_campaign_status(
        self,
        campaign_id: str,
        new_status: str,
        *,
        event_type: str = "status_changed",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply and audit one valid lifecycle transition atomically."""

        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_status = _validate_status(new_status)
        clean_event_type = _required_text(event_type, "event_type", max_length=120)
        details_value = _require_mapping(details, "details")
        # Serialize before opening a write transaction so invalid values cannot
        # leave the connection in an open transaction.
        _serialize_json(details_value, "details")
        timestamp = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM campaigns WHERE id = ?", (clean_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            old_status = row["status"]
            if clean_status == old_status:
                connection.rollback()
                return self.get_campaign(clean_id)
            if clean_status not in ALLOWED_STATUS_TRANSITIONS[old_status]:
                raise InvalidStatusTransition(
                    f"Campaign cannot transition from {old_status} to {clean_status}."
                )
            connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                (clean_status, timestamp, clean_id),
            )
            self._insert_event(
                connection,
                campaign_id=clean_id,
                event_type=clean_event_type,
                details=details_value,
                from_status=old_status,
                to_status=clean_status,
                timestamp=timestamp,
            )
            connection.commit()

        return self.get_campaign(clean_id)

    def append_event(
        self,
        campaign_id: str,
        event_type: str,
        details: Mapping[str, Any] | None = None,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> dict[str, Any]:
        """Append metadata about workflow progress without changing campaign state."""

        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_event_type = _required_text(event_type, "event_type", max_length=120)
        details_value = _require_mapping(details, "details")
        clean_from = _validate_optional_status(from_status, "from_status")
        clean_to = _validate_optional_status(to_status, "to_status")
        timestamp = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not _row_exists(connection, "campaigns", clean_id):
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            event_id = self._insert_event(
                connection,
                campaign_id=clean_id,
                event_type=clean_event_type,
                details=details_value,
                from_status=clean_from,
                to_status=clean_to,
                timestamp=timestamp,
            )
            connection.commit()
        return self._get_event(event_id)

    def list_events(
        self, campaign_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        clean_id = _canonical_uuid(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer.")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._connection() as connection:
            if not _row_exists(connection, "campaigns", clean_id):
                raise RecordNotFound(f"Campaign {clean_id} was not found.")
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE campaign_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (clean_id, limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _insert_review_request_locked(
        self, connection: sqlite3.Connection, spec: Mapping[str, Any], now: str,
    ) -> tuple[str, str | None]:
        campaign, _calendar, _recipient, content_hash = (
            self._load_review_context_for_fields_locked(
                connection, spec["campaign_id"], spec["calendar_version_id"],
                spec["role"], spec["recipient_id"], None, now,
            )
        )
        if spec["expires_at"] <= now:
            raise ValueError("expires_at must be in the future.")
        expired = connection.execute(
            """
            SELECT id FROM review_requests
            WHERE campaign_id=? AND calendar_version_id=? AND role=?
              AND status IN ('pending','opened') AND expires_at<=?
            """,
            (spec["campaign_id"], spec["calendar_version_id"], spec["role"], now),
        ).fetchall()
        for row in expired:
            connection.execute(
                "UPDATE review_requests SET status='revoked',revoked_at=? WHERE id=?",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE review_sessions SET consumed_at=COALESCE(consumed_at,?) "
                "WHERE review_request_id=?", (now, row["id"]),
            )
            connection.execute(
                "UPDATE notification_outbox SET status='cancelled',updated_at=? "
                "WHERE review_request_id=? AND status IN ('pending','processing')",
                (now, row["id"]),
            )
        active = connection.execute(
            """
            SELECT 1 FROM review_requests
            WHERE campaign_id=? AND calendar_version_id=? AND role=?
              AND status IN ('pending','opened') LIMIT 1
            """,
            (spec["campaign_id"], spec["calendar_version_id"], spec["role"]),
        ).fetchone()
        if active is not None:
            raise StoreConflict("An active request already exists for that review stage.")
        try:
            connection.execute(
                """
                INSERT INTO review_requests (
                    id,campaign_id,calendar_version_id,content_hash,role,
                    recipient_id,token_hash,status,expires_at,opened_at,
                    decided_at,revoked_at,created_at
                ) VALUES (?,?,?,?,?,?,?,'pending',?,NULL,NULL,NULL,?)
                """,
                (spec["review_request_id"], spec["campaign_id"],
                 spec["calendar_version_id"], content_hash, spec["role"],
                 spec["recipient_id"], spec["token_hash"], spec["expires_at"], now),
            )
        except sqlite3.IntegrityError as error:
            raise StoreConflict("The review request ID or token hash is already in use.") from error
        self._insert_event(
            connection, campaign_id=campaign["id"],
            event_type="review_request_created",
            details={
                "review_request_id": spec["review_request_id"],
                "calendar_version_id": spec["calendar_version_id"],
                "recipient_id": spec["recipient_id"], "role": spec["role"],
                "content_hash": content_hash,
            },
            from_status=None, to_status=None, timestamp=now,
        )
        outbox_id = None
        if spec.get("outbox_dedupe_key") is not None:
            outbox_id = str(uuid4())
            try:
                connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        id,review_request_id,dedupe_key,status,attempt_count,
                        provider_message_id,available_at,claimed_at,sent_at,
                        last_error_code,created_at,updated_at
                    ) VALUES (?,?,?,'pending',0,NULL,?,NULL,NULL,NULL,?,?)
                    """,
                    (outbox_id, spec["review_request_id"],
                     spec["outbox_dedupe_key"], now, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict("The notification dedupe key is already in use.") from error
        return spec["review_request_id"], outbox_id

    def _load_review_context_locked(
        self, connection: sqlite3.Connection, request: Mapping[str, Any], now: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, str]:
        return self._load_review_context_for_fields_locked(
            connection,
            request["campaign_id"], request["calendar_version_id"],
            request["role"], request["recipient_id"], request["content_hash"], now,
        )

    def _load_review_context_for_fields_locked(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        calendar_version_id: str,
        role: str,
        recipient_id: str,
        expected_hash: str | None,
        now: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, str]:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        if campaign is None:
            raise RecordNotFound(f"Campaign {campaign_id} was not found.")
        calendar = connection.execute(
            "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
            (calendar_version_id, campaign_id),
        ).fetchone()
        if calendar is None:
            raise RecordNotFound("That calendar version does not belong to the campaign.")
        latest = connection.execute(
            "SELECT id FROM calendar_versions WHERE campaign_id=? "
            "ORDER BY version DESC LIMIT 1", (campaign_id,),
        ).fetchone()
        if latest is None or latest["id"] != calendar_version_id:
            raise StoreConflict("Only the latest calendar version can be reviewed.")
        calculated_hash = _calendar_content_hash(
            _deserialize_json(calendar["headers_json"]),
            _deserialize_json(calendar["rows_json"]),
            _deserialize_json(calendar["client_metadata_json"]),
            _deserialize_json(calendar["generation_metadata_json"]),
        )
        if calculated_hash != calendar["content_hash"]:
            raise StoreConflict("The calendar content no longer matches its stored hash.")
        if expected_hash is not None and expected_hash != calculated_hash:
            raise StoreConflict("The review request does not match the calendar hash.")
        recipient = connection.execute(
            "SELECT * FROM review_recipients WHERE id=? AND campaign_id=? AND role=?",
            (recipient_id, campaign_id, role),
        ).fetchone()
        if recipient is None:
            raise StoreConflict("The review recipient does not match campaign and role.")
        status = campaign["status"]
        if role == "senior":
            if status not in {"pending_senior_review", "pending_review"}:
                raise InvalidStatusTransition(
                    "A senior link is only valid during senior review."
                )
        elif status != "pending_client_review":
            raise InvalidStatusTransition(
                "A client link requires prior senior approval."
            )
        else:
            senior = connection.execute(
                """
                SELECT content_hash FROM approvals
                WHERE campaign_id=? AND calendar_version_id=?
                  AND role='senior' AND decision='approved'
                """,
                (campaign_id, calendar_version_id),
            ).fetchone()
            if senior is None or senior["content_hash"] != calculated_hash:
                raise InvalidStatusTransition(
                    "Client review requires a hash-matched senior approval."
                )
        return campaign, calendar, recipient, calculated_hash

    def _get_calendar_version(self, version_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM calendar_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:  # Defensive; this method follows a successful insert.
            raise RecordNotFound(f"Calendar version {version_id} was not found.")
        return _calendar_from_row(row)

    def _get_event(self, event_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:  # Defensive; this method follows a successful insert.
            raise RecordNotFound(f"Workflow event {event_id} was not found.")
        return _event_from_row(row)

    def _get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:  # Defensive; this method follows a successful insert.
            raise RecordNotFound(f"Approval {approval_id} was not found.")
        return _approval_from_row(row)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        campaign_id: str,
        event_type: str,
        details: Mapping[str, Any],
        from_status: str | None,
        to_status: str | None,
        timestamp: str,
    ) -> str:
        _reject_sensitive_event_details(details)
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO workflow_events (
                id, campaign_id, event_type, from_status, to_status,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                campaign_id,
                event_type,
                from_status,
                to_status,
                _serialize_json(details, "details"),
                timestamp,
            ),
        )
        return event_id

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise CampaignStoreError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            connection.execute("PRAGMA journal_mode = WAL")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            core_tables = {"clients", "campaigns", "calendar_versions", "workflow_events"}
            if not tables.intersection(core_tables):
                self._create_v2_schema(connection)
            elif not core_tables.issubset(tables):
                missing = ", ".join(sorted(core_tables - tables))
                raise CampaignStoreError(
                    f"Database schema is incomplete; missing table(s): {missing}."
                )
            elif not self._has_v2_core_schema(connection):
                # Also repairs the short-lived partial-v2 state where user_version
                # was advanced before the v2 tables/columns had been installed.
                self._rebuild_core_as_v2(connection)

            self._ensure_v2_auxiliary_schema(connection)
            self._ensure_v3_review_schema(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise CampaignStoreError(
                    "Database migration failed its foreign-key integrity check."
                )

    @staticmethod
    def _create_v2_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        CampaignStore._create_campaigns_v2_table(connection, "campaigns")
        CampaignStore._create_calendar_versions_v2_table(
            connection, "calendar_versions"
        )
        connection.execute(
            """
            CREATE TABLE workflow_events (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            )
            """
        )

    @staticmethod
    def _create_campaigns_v2_table(
        connection: sqlite3.Connection, table_name: str
    ) -> None:
        if table_name not in {"campaigns", "campaigns_v2_migration"}:
            raise ValueError("Unsupported internal campaign table name.")
        connection.execute(
            f"""
            CREATE TABLE {table_name} (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                external_id TEXT,
                request_id TEXT UNIQUE,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'generating', 'generation_unknown',
                        'pending_senior_review', 'pending_client_review',
                        'revision_required', 'fully_approved',
                        'pending_review', 'approved', 'rejected',
                        'generation_failed'
                    )
                ),
                intake_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
            )
            """
        )

    @staticmethod
    def _create_calendar_versions_v2_table(
        connection: sqlite3.Connection, table_name: str
    ) -> None:
        if table_name not in {"calendar_versions", "calendar_versions_v2_migration"}:
            raise ValueError("Unsupported internal calendar table name.")
        connection.execute(
            f"""
            CREATE TABLE {table_name} (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                headers_json TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                client_metadata_json TEXT NOT NULL,
                generation_metadata_json TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                UNIQUE (campaign_id, version),
                UNIQUE (campaign_id, id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            )
            """
        )

    @staticmethod
    def _has_v2_core_schema(connection: sqlite3.Connection) -> bool:
        calendar_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(calendar_versions)"
            ).fetchall()
        }
        campaign_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'campaigns'"
        ).fetchone()
        campaign_sql = (campaign_sql_row[0] or "").lower() if campaign_sql_row else ""
        return "content_hash" in calendar_columns and all(
            status in campaign_sql
            for status in (
                "pending_senior_review",
                "pending_client_review",
                "revision_required",
                "fully_approved",
            )
        )

    def _rebuild_core_as_v2(self, connection: sqlite3.Connection) -> None:
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS campaigns_v2_migration")
            connection.execute("DROP TABLE IF EXISTS calendar_versions_v2_migration")
            self._create_campaigns_v2_table(connection, "campaigns_v2_migration")
            self._create_calendar_versions_v2_table(
                connection, "calendar_versions_v2_migration"
            )
            connection.execute(
                """
                INSERT INTO campaigns_v2_migration (
                    id, client_id, external_id, request_id, status,
                    intake_json, created_at, updated_at
                )
                SELECT id, client_id, external_id, request_id, status,
                       intake_json, created_at, updated_at
                FROM campaigns
                """
            )
            versions = connection.execute(
                """
                SELECT id, campaign_id, version, headers_json, rows_json,
                       client_metadata_json, generation_metadata_json, created_at
                FROM calendar_versions
                ORDER BY campaign_id, version
                """
            ).fetchall()
            for row in versions:
                content_hash = _calendar_content_hash(
                    _deserialize_json(row["headers_json"]),
                    _deserialize_json(row["rows_json"]),
                    _deserialize_json(row["client_metadata_json"]),
                    _deserialize_json(row["generation_metadata_json"]),
                )
                connection.execute(
                    """
                    INSERT INTO calendar_versions_v2_migration (
                        id, campaign_id, version, headers_json, rows_json,
                        client_metadata_json, generation_metadata_json,
                        content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["campaign_id"],
                        row["version"],
                        row["headers_json"],
                        row["rows_json"],
                        row["client_metadata_json"],
                        row["generation_metadata_json"],
                        content_hash,
                        row["created_at"],
                    ),
                )
            connection.execute("DROP TABLE calendar_versions")
            connection.execute("DROP TABLE campaigns")
            connection.execute(
                "ALTER TABLE campaigns_v2_migration RENAME TO campaigns"
            )
            connection.execute(
                "ALTER TABLE calendar_versions_v2_migration "
                "RENAME TO calendar_versions"
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _ensure_v2_auxiliary_schema(connection: sqlite3.Connection) -> None:
        approvals_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
        ).fetchone()
        expected_approval_columns = {
            "id",
            "campaign_id",
            "calendar_version_id",
            "role",
            "decision",
            "approver_name",
            "approver_email",
            "feedback",
            "content_hash",
            "decided_at",
        }
        if approvals_exists:
            actual_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            if not expected_approval_columns.issubset(actual_columns):
                raise CampaignStoreError(
                    "The approvals table exists but does not match schema v2."
                )
        elif connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='approvals_v2_archive'"
        ).fetchone() is not None:
            CampaignStore._create_approvals_v3_table(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO approvals (
                    id,campaign_id,calendar_version_id,role,decision,
                    approver_name,approver_email,approver_phone_e164,
                    identity_channel,review_request_id,feedback,content_hash,decided_at
                )
                SELECT id,campaign_id,calendar_version_id,role,decision,
                       approver_name,approver_email,NULL,
                       'local_self_reported',NULL,feedback,content_hash,decided_at
                FROM approvals_v2_archive
                """
            )
        else:
            connection.execute(
                """
                CREATE TABLE approvals (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    calendar_version_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('senior', 'client')),
                    decision TEXT NOT NULL CHECK (
                        decision IN ('approved', 'rejected')
                    ),
                    approver_name TEXT NOT NULL CHECK (
                        length(approver_name) BETWEEN 1 AND 200
                    ),
                    approver_email TEXT NOT NULL CHECK (
                        length(approver_email) BETWEEN 1 AND 320
                    ),
                    feedback TEXT NOT NULL DEFAULT '' CHECK (
                        length(feedback) <= 5000
                    ),
                    content_hash TEXT NOT NULL CHECK (
                        length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                    decided_at TEXT NOT NULL,
                    UNIQUE (campaign_id, calendar_version_id, role),
                    FOREIGN KEY (campaign_id, calendar_version_id)
                        REFERENCES calendar_versions(campaign_id, id)
                        ON DELETE RESTRICT
                )
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS campaigns_client_idx "
            "ON campaigns(client_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS campaigns_external_idx "
            "ON campaigns(external_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS calendar_versions_campaign_idx "
            "ON calendar_versions(campaign_id, version DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS workflow_events_campaign_idx "
            "ON workflow_events(campaign_id, created_at, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS approvals_campaign_idx "
            "ON approvals(campaign_id, decided_at, id)"
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS approvals_no_update
            BEFORE UPDATE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approval decisions are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS approvals_no_delete
            BEFORE DELETE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approval decisions are append-only');
            END
            """
        )

    @staticmethod
    def _ensure_v3_review_schema(connection: sqlite3.Connection) -> None:
        """Install review-link tables and rebuild v2 approvals without data loss."""

        table_sql = {
            "review_recipients": """
                CREATE TABLE review_recipients (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('senior','client')),
                    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
                    phone_e164 TEXT NOT NULL CHECK (
                        length(phone_e164) BETWEEN 9 AND 16
                        AND substr(phone_e164,1,1)='+'
                        AND substr(phone_e164,2,1) BETWEEN '1' AND '9'
                        AND substr(phone_e164,2) NOT GLOB '*[^0-9]*'
                    ),
                    consent_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (campaign_id, role),
                    UNIQUE (id, campaign_id, role),
                    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
                )
            """,
            "review_requests": """
                CREATE TABLE review_requests (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    calendar_version_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL CHECK (
                        length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                    role TEXT NOT NULL CHECK (role IN ('senior','client')),
                    recipient_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE CHECK (
                        length(token_hash)=64 AND token_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                    status TEXT NOT NULL CHECK (status IN ('pending','opened','decided','revoked')),
                    expires_at TEXT NOT NULL,
                    opened_at TEXT,
                    decided_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (campaign_id, calendar_version_id)
                        REFERENCES calendar_versions(campaign_id, id) ON DELETE RESTRICT,
                    FOREIGN KEY (recipient_id, campaign_id, role)
                        REFERENCES review_recipients(id, campaign_id, role) ON DELETE RESTRICT
                )
            """,
            "review_sessions": """
                CREATE TABLE review_sessions (
                    id TEXT PRIMARY KEY,
                    review_request_id TEXT NOT NULL UNIQUE,
                    session_hash TEXT NOT NULL UNIQUE CHECK (
                        length(session_hash)=64 AND session_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (review_request_id) REFERENCES review_requests(id) ON DELETE RESTRICT
                )
            """,
            "notification_outbox": """
                CREATE TABLE notification_outbox (
                    id TEXT PRIMARY KEY,
                    review_request_id TEXT NOT NULL UNIQUE,
                    dedupe_key TEXT NOT NULL UNIQUE CHECK (length(dedupe_key) BETWEEN 1 AND 300),
                    status TEXT NOT NULL CHECK (status IN ('pending','processing','sent','failed','cancelled')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    provider_message_id TEXT,
                    available_at TEXT NOT NULL,
                    claimed_at TEXT,
                    sent_at TEXT,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (review_request_id) REFERENCES review_requests(id) ON DELETE RESTRICT
                )
            """,
        }
        expected = {
            "review_recipients": {"id","campaign_id","role","display_name","phone_e164","consent_at","created_at","updated_at"},
            "review_requests": {"id","campaign_id","calendar_version_id","content_hash","role","recipient_id","token_hash","status","expires_at","opened_at","decided_at","revoked_at","created_at"},
            "review_sessions": {"id","review_request_id","session_hash","expires_at","consumed_at","created_at"},
            "notification_outbox": {"id","review_request_id","dedupe_key","status","attempt_count","provider_message_id","available_at","claimed_at","sent_at","last_error_code","created_at","updated_at"},
        }
        for table, sql in table_sql.items():
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                connection.execute(sql)
            else:
                columns = {
                    row[1] for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if not expected[table].issubset(columns):
                    raise CampaignStoreError(
                        f"The {table} table does not match schema v3."
                    )

        approval_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)")
        }
        v3_approval_columns = {
            "approver_phone_e164", "identity_channel", "review_request_id"
        }
        if not v3_approval_columns.issubset(approval_columns):
            CampaignStore._rebuild_approvals_as_v3(connection)
        CampaignStore._ensure_v3_indexes_and_triggers(connection)

    @staticmethod
    def _rebuild_approvals_as_v3(connection: sqlite3.Connection) -> None:
        """Copy v2 approvals into v3 while retaining an immutable archive table."""

        archive = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='approvals_v2_archive'"
        ).fetchone()
        if archive is not None:
            raise CampaignStoreError(
                "An approvals v2 archive already exists but migration is incomplete."
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            old_count = connection.execute(
                "SELECT COUNT(*) FROM approvals"
            ).fetchone()[0]
            connection.execute(
                "ALTER TABLE approvals RENAME TO approvals_v2_archive"
            )
            CampaignStore._create_approvals_v3_table(connection)
            connection.execute(
                """
                INSERT INTO approvals (
                    id,campaign_id,calendar_version_id,role,decision,
                    approver_name,approver_email,approver_phone_e164,
                    identity_channel,review_request_id,feedback,content_hash,decided_at
                )
                SELECT id,campaign_id,calendar_version_id,role,decision,
                       approver_name,approver_email,NULL,
                       'local_self_reported',NULL,feedback,content_hash,decided_at
                FROM approvals_v2_archive
                """
            )
            new_count = connection.execute(
                "SELECT COUNT(*) FROM approvals"
            ).fetchone()[0]
            if new_count != old_count:
                raise CampaignStoreError(
                    "Approval migration row-count verification failed."
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _create_approvals_v3_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                calendar_version_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('senior','client')),
                decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
                approver_name TEXT NOT NULL CHECK (length(approver_name) BETWEEN 1 AND 200),
                approver_email TEXT CHECK (
                    approver_email IS NULL OR length(approver_email) BETWEEN 1 AND 320
                ),
                approver_phone_e164 TEXT CHECK (
                    approver_phone_e164 IS NULL OR (
                        length(approver_phone_e164) BETWEEN 9 AND 16
                        AND substr(approver_phone_e164,1,1)='+'
                        AND substr(approver_phone_e164,2,1) BETWEEN '1' AND '9'
                        AND substr(approver_phone_e164,2) NOT GLOB '*[^0-9]*'
                    )
                ),
                identity_channel TEXT NOT NULL CHECK (
                    identity_channel IN ('local_self_reported','whatsapp_link')
                ),
                review_request_id TEXT UNIQUE,
                feedback TEXT NOT NULL DEFAULT '' CHECK (length(feedback) <= 5000),
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                decided_at TEXT NOT NULL,
                CHECK (
                    (identity_channel='local_self_reported'
                     AND approver_email IS NOT NULL
                     AND approver_phone_e164 IS NULL
                     AND review_request_id IS NULL)
                    OR
                    (identity_channel='whatsapp_link'
                     AND approver_email IS NULL
                     AND approver_phone_e164 IS NOT NULL
                     AND review_request_id IS NOT NULL)
                ),
                UNIQUE (campaign_id, calendar_version_id, role),
                FOREIGN KEY (campaign_id, calendar_version_id)
                    REFERENCES calendar_versions(campaign_id, id) ON DELETE RESTRICT,
                FOREIGN KEY (review_request_id)
                    REFERENCES review_requests(id) ON DELETE RESTRICT
            )
            """
        )

    @staticmethod
    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE INDEX IF NOT EXISTS review_recipients_campaign_idx "
            "ON review_recipients(campaign_id,role)",
            "CREATE INDEX IF NOT EXISTS review_requests_campaign_idx "
            "ON review_requests(campaign_id,calendar_version_id,role,created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS review_requests_one_active_stage "
            "ON review_requests(campaign_id,calendar_version_id,role) "
            "WHERE status IN ('pending','opened')",
            "CREATE INDEX IF NOT EXISTS notification_outbox_due_idx "
            "ON notification_outbox(status,available_at,created_at)",
            "CREATE INDEX IF NOT EXISTS approvals_v3_campaign_idx "
            "ON approvals(campaign_id,decided_at,id)",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS approvals_v3_no_update
            BEFORE UPDATE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approval decisions are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS approvals_v3_no_delete
            BEFORE DELETE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approval decisions are append-only');
            END
            """
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._target,
            timeout=self.busy_timeout_ms / 1_000,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection


def _normalize_client_name(name: str) -> tuple[str, str]:
    display = _required_text(name, "name", max_length=300)
    display = " ".join(unicodedata.normalize("NFKC", display).split())
    if not display:
        raise ValueError("name must not be empty.")
    return display, display.casefold()


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a UUID string.")
    try:
        parsed = UUID(value.strip())
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{label} must be a valid UUID.") from error
    return str(parsed)


def _optional_identifier(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, max_length=300)


def _required_text(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be empty.")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters.")
    return cleaned


def _bounded_optional_text(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text.")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters.")
    return cleaned


def _approval_choice(value: Any, label: str, choices: set[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text.")
    cleaned = value.strip().lower()
    if cleaned not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"Unsupported {label}. Use one of: {allowed}.")
    return cleaned


def _sha256_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_HEX_RE.fullmatch(value.strip()) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest.")
    return value.strip()


def _e164_phone(value: Any) -> str:
    if not isinstance(value, str) or E164_RE.fullmatch(value.strip()) is None:
        raise ValueError(
            "phone_e164 must start with + and contain 8 to 15 digits in total."
        )
    return value.strip()


def _utc_timestamp(value: Any, label: str) -> str:
    text = _required_text(value, label, max_length=100)
    iso_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset.")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _future_utc_timestamp(value: Any, label: str) -> str:
    normalized = _utc_timestamp(value, label)
    if normalized <= _utc_now():
        raise ValueError(f"{label} must be in the future.")
    return normalized


def _dedupe_key(value: Any) -> str:
    key = _required_text(value, "outbox_dedupe_key", max_length=MAX_DEDUPE_KEY_CHARS)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", key) is None:
        raise ValueError(
            "outbox_dedupe_key must be an opaque identifier without spaces or PII."
        )
    if SHA256_HEX_RE.fullmatch(key) is not None:
        raise ValueError("outbox_dedupe_key must not be a token hash.")
    return key


def _next_review_request_spec(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("next_review_request must be a mapping.")
    allowed = {
        "recipient_id", "token_hash", "expires_at",
        "review_request_id", "outbox_dedupe_key",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            "Unsupported next_review_request field(s): " + ", ".join(sorted(unknown))
        )
    missing = {"recipient_id", "token_hash", "expires_at"} - set(value)
    if missing:
        raise ValueError(
            "Missing next_review_request field(s): " + ", ".join(sorted(missing))
        )
    request_id = value.get("review_request_id")
    dedupe = value.get("outbox_dedupe_key")
    return {
        "recipient_id": _canonical_uuid(value["recipient_id"], "recipient_id"),
        "token_hash": _sha256_hash(value["token_hash"], "token_hash"),
        "expires_at": _future_utc_timestamp(value["expires_at"], "expires_at"),
        "review_request_id": (
            _canonical_uuid(request_id, "review_request_id")
            if request_id else str(uuid4())
        ),
        "outbox_dedupe_key": _dedupe_key(dedupe) if dedupe is not None else None,
    }


def _require_mapping(
    value: Mapping[str, Any] | None, label: str
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    result = dict(value)
    _serialize_json(result, label)
    return result


def _validate_headers(headers: Sequence[str]) -> list[str]:
    if isinstance(headers, (str, bytes, bytearray)) or not isinstance(headers, Sequence):
        raise TypeError("headers must be a sequence of text values.")
    result = [_required_text(header, "header", max_length=200) for header in headers]
    if not result:
        raise ValueError("headers must not be empty.")
    if len({header.casefold() for header in result}) != len(result):
        raise ValueError("headers must not contain duplicate names.")
    return result


def _validate_rows(rows: Sequence[Any]) -> list[Any]:
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence.")
    result = list(rows)
    _serialize_json(result, "rows")
    return result


def _validate_status(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("status must be text.")
    status = value.strip().lower()
    if status not in CAMPAIGN_STATUSES:
        choices = ", ".join(sorted(CAMPAIGN_STATUSES))
        raise ValueError(f"Unsupported campaign status. Use one of: {choices}.")
    return status


def _validate_optional_status(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return _validate_status(value)
    except (TypeError, ValueError) as error:
        raise type(error)(f"Invalid {label}: {error}") from error


def _calendar_content_hash(
    headers: Sequence[str],
    rows: Sequence[Any],
    client_metadata: Mapping[str, Any],
    generation_metadata: Mapping[str, Any],
) -> str:
    """Return a deterministic SHA-256 digest of immutable calendar content."""

    payload = {
        "headers": list(headers),
        "rows": list(rows),
        "client_metadata": dict(client_metadata),
        "generation_metadata": dict(generation_metadata),
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _serialize_json(value: Any, label: str) -> str:
    _validate_json_value(value, label)
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must contain valid JSON values.") from error
    if len(serialized) > MAX_JSON_CHARS:
        raise ValueError(f"{label} is too large to store in SQLite.")
    return serialized


def _validate_json_value(value: Any, label: str, path: str = "$") -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"{label} contains raw binary data at {path}; store a document ID instead."
        )
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number at {path}.")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} contains a non-text key at {path}.")
            _validate_json_value(item, label, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, label, f"{path}[{index}]")
        return
    raise TypeError(
        f"{label} contains unsupported value {type(value).__name__} at {path}."
    )


def _deserialize_json(value: str) -> Any:
    return json.loads(value)


def _reject_sensitive_event_details(value: Any, path: str = "details") -> None:
    """Keep WhatsApp PII and review secrets out of the generic event stream."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).strip().lower()
            compact = lowered.replace("-", "_")
            if "phone" in compact or compact in {
                "token", "raw_token", "review_token", "access_token",
                "session", "session_token", "session_hash", "token_hash",
                "authorization",
            } or compact.endswith("_token"):
                raise ValueError(
                    f"Sensitive review data is not allowed in workflow events ({path}.{key})."
                )
            _reject_sensitive_event_details(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_sensitive_event_details(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        stripped = value.strip()
        if E164_RE.fullmatch(stripped) or stripped.lower().startswith("bearer "):
            raise ValueError(
                f"Sensitive review data is not allowed in workflow events ({path})."
            )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _row_exists(connection: sqlite3.Connection, table: str, record_id: str) -> bool:
    if table == "clients":
        query = "SELECT 1 FROM clients WHERE id = ?"
    elif table == "campaigns":
        query = "SELECT 1 FROM campaigns WHERE id = ?"
    else:  # Defensive guard: table identifiers must never come from a caller.
        raise ValueError("Unsupported internal table lookup.")
    return (
        connection.execute(query, (record_id,)).fetchone() is not None
    )


def _client_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "normalized_name": row["normalized_name"],
        "metadata": _deserialize_json(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _campaign_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "external_id": row["external_id"],
        "request_id": row["request_id"],
        "status": row["status"],
        "intake": _deserialize_json(row["intake_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _calendar_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "version": row["version"],
        "headers": _deserialize_json(row["headers_json"]),
        "rows": _deserialize_json(row["rows_json"]),
        "client_metadata": _deserialize_json(row["client_metadata_json"]),
        "generation_metadata": _deserialize_json(row["generation_metadata_json"]),
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
    }


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "event_type": row["event_type"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "details": _deserialize_json(row["details_json"]),
        "created_at": row["created_at"],
    }


def _approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "role": row["role"],
        "decision": row["decision"],
        "approver_name": row["approver_name"],
        "approver_email": row["approver_email"],
        "approver_phone_e164": row["approver_phone_e164"],
        "identity_channel": row["identity_channel"],
        "review_request_id": row["review_request_id"],
        "feedback": row["feedback"],
        "content_hash": row["content_hash"],
        "decided_at": row["decided_at"],
    }


def _review_recipient_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "role": row["role"],
        "display_name": row["display_name"],
        "phone_e164": row["phone_e164"],
        "consent_at": row["consent_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _review_request_from_row(row: sqlite3.Row) -> dict[str, Any]:
    # The credential hash is intentionally omitted from ordinary return values.
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "content_hash": row["content_hash"],
        "role": row["role"],
        "recipient_id": row["recipient_id"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "opened_at": row["opened_at"],
        "decided_at": row["decided_at"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
    }


def _review_session_from_row(row: sqlite3.Row) -> dict[str, Any]:
    # The session credential hash is intentionally omitted from return values.
    return {
        "id": row["id"],
        "review_request_id": row["review_request_id"],
        "expires_at": row["expires_at"],
        "consumed_at": row["consumed_at"],
        "created_at": row["created_at"],
    }


def _notification_outbox_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "review_request_id": row["review_request_id"],
        "dedupe_key": row["dedupe_key"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "provider_message_id": row["provider_message_id"],
        "available_at": row["available_at"],
        "claimed_at": row["claimed_at"],
        "sent_at": row["sent_at"],
        "last_error_code": row["last_error_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
