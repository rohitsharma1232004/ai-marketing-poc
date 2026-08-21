"""SQLite publishing queue bound to exact approved content and creative versions.

The queue lives in the same database as CampaignStore but owns only its two
publishing tables. It stores credential *references*, never Meta access tokens.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from publishing_workflow import (
    approved_post_from_row,
    normalize_credential_ref,
    normalize_platform,
    normalize_scheduled_for,
    publication_dedupe_key,
    validate_publishable_image,
)

DEFAULT_BUSY_TIMEOUT_MS = 5_000
FINAL_CONTENT_STATUSES = frozenset({"fully_approved", "approved"})
JOB_STATUSES = frozenset(
    {"queued", "publishing", "published", "failed", "outcome_unknown", "cancelled"}
)
MAX_SAFE_ERROR_CHARS = 2_000


class PublishingStoreError(RuntimeError):
    pass


class PublishingConflict(PublishingStoreError):
    pass


class PublishingNotFound(PublishingStoreError):
    pass


class PublishingStore:
    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        raw = str(db_path or "").strip()
        if not raw:
            raise ValueError("db_path must not be empty.")
        if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
            raise TypeError("busy_timeout_ms must be an integer.")
        self.db_path = raw
        self.busy_timeout_ms = busy_timeout_ms
        if raw != ":memory:":
            Path(raw).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._keeper: sqlite3.Connection | None = None
        if raw == ":memory:":
            self._target = f"file:publishing_store_{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._new_connection()
        else:
            self._target = raw
            self._uri = False
        self._initialize_schema()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._target,
            uri=self._uri,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _initialize_schema(self) -> None:
        with self._new_connection() as connection:
            required = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            source_tables = {
                "clients",
                "campaigns",
                "calendar_versions",
                "approvals",
                "design_briefs",
                "creative_assets",
                "design_approvals",
            }
            missing = sorted(source_tables - required)
            if missing:
                raise PublishingStoreError(
                    "Publishing requires the approved-content/design schema first. Missing: "
                    + ", ".join(missing)
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta_connections (
                    id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    connection_name TEXT NOT NULL,
                    facebook_page_id TEXT NOT NULL DEFAULT '',
                    instagram_user_id TEXT NOT NULL DEFAULT '',
                    credential_ref TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','disconnected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS publication_jobs (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    calendar_version_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    creative_asset_id TEXT NOT NULL,
                    creative_hash TEXT NOT NULL,
                    post_number INTEGER NOT NULL CHECK (post_number > 0),
                    platform TEXT NOT NULL CHECK (platform IN ('facebook','instagram')),
                    connection_id TEXT NOT NULL,
                    public_media_url TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued','publishing','published','failed','outcome_unknown','cancelled')
                    ),
                    dedupe_key TEXT NOT NULL UNIQUE,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    platform_post_id TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (campaign_id, calendar_version_id)
                        REFERENCES calendar_versions(campaign_id, id) ON DELETE RESTRICT,
                    FOREIGN KEY (creative_asset_id)
                        REFERENCES creative_assets(id) ON DELETE RESTRICT,
                    FOREIGN KEY (connection_id)
                        REFERENCES meta_connections(id) ON DELETE RESTRICT
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS meta_connections_one_active_client "
                "ON meta_connections(client_id) WHERE status='active'"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS publication_jobs_due_idx "
                "ON publication_jobs(status, scheduled_for)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS publication_jobs_campaign_idx "
                "ON publication_jobs(campaign_id, post_number, platform, created_at)"
            )
            connection.commit()

    def save_meta_connection(
        self,
        *,
        client_id: str,
        connection_name: str,
        credential_ref: str,
        facebook_page_id: str = "",
        instagram_user_id: str = "",
    ) -> dict[str, Any]:
        clean_client = str(client_id or "").strip()
        clean_name = str(connection_name or "").strip()
        if not clean_client or not clean_name or len(clean_name) > 200:
            raise ValueError("client_id and a bounded connection_name are required.")
        page_id = self._optional_meta_id(facebook_page_id, "facebook_page_id")
        ig_id = self._optional_meta_id(instagram_user_id, "instagram_user_id")
        if not page_id and not ig_id:
            raise ValueError("Configure at least one Facebook Page or Instagram account ID.")
        ref = normalize_credential_ref(credential_ref)
        now = _utc_now()
        connection_id = str(uuid4())
        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM clients WHERE id=?", (clean_client,)).fetchone() is None:
                raise PublishingNotFound("Client was not found.")
            connection.execute(
                "UPDATE meta_connections SET status='disconnected', updated_at=? "
                "WHERE client_id=? AND status='active'",
                (now, clean_client),
            )
            connection.execute(
                """
                INSERT INTO meta_connections (
                    id,client_id,connection_name,facebook_page_id,instagram_user_id,
                    credential_ref,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,'active',?,?)
                """,
                (connection_id, clean_client, clean_name, page_id, ig_id, ref, now, now),
            )
            connection.commit()
        return self.get_meta_connection(connection_id)

    def get_meta_connection(self, connection_id: str) -> dict[str, Any]:
        with self._new_connection() as connection:
            row = connection.execute(
                "SELECT * FROM meta_connections WHERE id=?", (str(connection_id or "").strip(),)
            ).fetchone()
        if row is None:
            raise PublishingNotFound("Meta connection was not found.")
        return dict(row)

    def get_active_meta_connection(self, client_id: str) -> dict[str, Any] | None:
        with self._new_connection() as connection:
            row = connection.execute(
                "SELECT * FROM meta_connections WHERE client_id=? AND status='active'",
                (str(client_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def disconnect_meta_connection(self, connection_id: str) -> dict[str, Any]:
        clean_id = str(connection_id or "").strip()
        now = _utc_now()
        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM meta_connections WHERE id=?", (clean_id,)
            ).fetchone()
            if row is None:
                raise PublishingNotFound("Meta connection was not found.")
            connection.execute(
                "UPDATE meta_connections SET status='disconnected',updated_at=? WHERE id=?",
                (now, clean_id),
            )
            connection.commit()
        return self.get_meta_connection(clean_id)

    def queue_image_publication(
        self,
        *,
        campaign_id: str,
        calendar_version_id: str,
        creative_asset_id: str,
        connection_id: str,
        platform: str,
        public_media_url: str,
        scheduled_for: str | datetime | None = None,
    ) -> dict[str, Any]:
        target = normalize_platform(platform)
        cid = str(campaign_id or "").strip()
        calendar_id = str(calendar_version_id or "").strip()
        asset_id = str(creative_asset_id or "").strip()
        conn_id = str(connection_id or "").strip()
        scheduled = normalize_scheduled_for(scheduled_for)
        now = _utc_now()

        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (cid,)
            ).fetchone()
            if campaign is None:
                raise PublishingNotFound("Campaign was not found.")
            if campaign["status"] not in FINAL_CONTENT_STATUSES:
                raise PublishingConflict(
                    "Final Senior content approval is required before publishing."
                )
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (calendar_id, cid),
            ).fetchone()
            if calendar is None:
                raise PublishingNotFound("Calendar version was not found for this campaign.")
            latest_calendar = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? ORDER BY version DESC LIMIT 1",
                (cid,),
            ).fetchone()
            if latest_calendar is None or latest_calendar["id"] != calendar_id:
                raise PublishingConflict("Only the latest approved content version can publish.")
            content_hash = str(calendar["content_hash"])
            content_approval = connection.execute(
                "SELECT 1 FROM approvals WHERE campaign_id=? AND calendar_version_id=? "
                "AND role='senior' AND decision='approved' AND content_hash=? LIMIT 1",
                (cid, calendar_id, content_hash),
            ).fetchone()
            if content_approval is None:
                raise PublishingConflict(
                    "The latest content version lacks a hash-matched Senior approval."
                )

            meta_connection = connection.execute(
                "SELECT * FROM meta_connections WHERE id=? AND status='active'",
                (conn_id,),
            ).fetchone()
            if meta_connection is None:
                raise PublishingConflict("An active Meta connection is required.")
            if meta_connection["client_id"] != campaign["client_id"]:
                raise PublishingConflict("Meta connection belongs to a different client.")
            if target == "facebook" and not meta_connection["facebook_page_id"]:
                raise PublishingConflict("This client connection has no Facebook Page ID.")
            if target == "instagram" and not meta_connection["instagram_user_id"]:
                raise PublishingConflict("This client connection has no Instagram Professional ID.")

            asset = connection.execute(
                "SELECT * FROM creative_assets WHERE id=? AND campaign_id=? AND calendar_version_id=?",
                (asset_id, cid, calendar_id),
            ).fetchone()
            if asset is None:
                raise PublishingNotFound("Creative asset was not found for this campaign version.")
            latest_asset = connection.execute(
                "SELECT id FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? ORDER BY asset_version DESC LIMIT 1",
                (cid, calendar_id, asset["post_number"]),
            ).fetchone()
            if latest_asset is None or latest_asset["id"] != asset_id:
                raise PublishingConflict("Only the latest creative version can publish.")
            if asset["content_hash"] != content_hash:
                raise PublishingConflict("Creative does not match the approved content hash.")
            design_approval = connection.execute(
                "SELECT * FROM design_approvals WHERE creative_asset_id=?",
                (asset_id,),
            ).fetchone()
            if design_approval is None:
                raise PublishingConflict("Senior Design Approval is required before publishing.")

            self._require_campaign_design_gate(connection, cid, calendar_id, content_hash)

            headers = _json_list(calendar["headers_json"], "calendar headers")
            rows = _json_list(calendar["rows_json"], "calendar rows")
            row_index = int(asset["row_index"])
            if row_index < 0 or row_index >= len(rows) or not isinstance(rows[row_index], list):
                raise PublishingConflict("Approved source row for this creative is unavailable.")
            approved_post = approved_post_from_row(headers, rows[row_index])
            spec = validate_publishable_image(
                approved_post=approved_post,
                creative_asset=dict(asset),
                design_approval=dict(design_approval),
                platform=target,
                public_media_url=public_media_url,
            )
            dedupe = publication_dedupe_key(
                campaign_id=cid,
                calendar_version_id=calendar_id,
                content_hash=content_hash,
                creative_asset_id=asset_id,
                creative_hash=asset["file_sha256"],
                connection_id=conn_id,
                platform=target,
            )
            existing = connection.execute(
                "SELECT * FROM publication_jobs WHERE dedupe_key=?", (dedupe,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing)

            job_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO publication_jobs (
                    id,campaign_id,calendar_version_id,content_hash,creative_asset_id,
                    creative_hash,post_number,platform,connection_id,public_media_url,
                    caption,scheduled_for,status,dedupe_key,attempt_count,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'queued', ?,0,?,?)
                """,
                (
                    job_id,cid,calendar_id,content_hash,asset_id,asset["file_sha256"],
                    asset["post_number"],target,conn_id,spec["public_media_url"],
                    spec["caption"],scheduled,dedupe,now,now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM publication_jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.commit()
        return dict(row)

    def _require_campaign_design_gate(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        calendar_version_id: str,
        content_hash: str,
    ) -> None:
        briefs = connection.execute(
            "SELECT post_number FROM design_briefs WHERE campaign_id=? AND calendar_version_id=? "
            "AND content_hash=? ORDER BY post_number",
            (campaign_id, calendar_version_id, content_hash),
        ).fetchall()
        expected = {int(row["post_number"]) for row in briefs}
        if not expected:
            raise PublishingConflict("Design Briefs are required before publishing.")
        creative_rows = connection.execute(
            "SELECT * FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
            "ORDER BY post_number ASC,asset_version DESC",
            (campaign_id, calendar_version_id),
        ).fetchall()
        latest: dict[int, sqlite3.Row] = {}
        for row in creative_rows:
            latest.setdefault(int(row["post_number"]), row)
        approved: set[int] = set()
        for post_number, asset in latest.items():
            if asset["content_hash"] != content_hash:
                continue
            decision = connection.execute(
                "SELECT decision,asset_hash FROM design_approvals WHERE creative_asset_id=?",
                (asset["id"],),
            ).fetchone()
            if (
                decision is not None
                and decision["decision"] == "approved"
                and decision["asset_hash"] == asset["file_sha256"]
            ):
                approved.add(post_number)
        if approved != expected:
            raise PublishingConflict(
                "Publishing Gate is locked until every latest creative is Senior Design Approved."
            )

    def claim_due_jobs(self, *, limit: int = 10, now: str | datetime | None = None) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        cutoff = normalize_scheduled_for(now)
        started = _utc_now()
        claimed: list[dict[str, Any]] = []
        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM publication_jobs WHERE status='queued' AND scheduled_for<=? "
                "ORDER BY scheduled_for ASC,created_at ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    "UPDATE publication_jobs SET status='publishing',attempt_count=attempt_count+1,"
                    "started_at=?,updated_at=? WHERE id=? AND status='queued'",
                    (started, started, row["id"]),
                )
                if cursor.rowcount == 1:
                    claimed_row = connection.execute(
                        "SELECT * FROM publication_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
                    claimed.append(dict(claimed_row))
            connection.commit()
        return claimed

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._new_connection() as connection:
            row = connection.execute(
                "SELECT * FROM publication_jobs WHERE id=?", (str(job_id or "").strip(),)
            ).fetchone()
        if row is None:
            raise PublishingNotFound("Publication job was not found.")
        return dict(row)

    def get_job_bundle(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        connection = self.get_meta_connection(job["connection_id"])
        return {"job": job, "connection": connection}

    def list_jobs(self, campaign_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._new_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM publication_jobs WHERE campaign_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (str(campaign_id or "").strip(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_published(
        self,
        job_id: str,
        *,
        platform_post_id: str,
        provider_request_id: str,
    ) -> dict[str, Any]:
        post_id = str(platform_post_id or "").strip()
        request_id = str(provider_request_id or "").strip()
        if not post_id or len(post_id) > 300 or len(request_id) > 300:
            raise ValueError("Published job requires bounded provider identifiers.")
        return self._finish_job(
            job_id,
            status="published",
            platform_post_id=post_id,
            provider_request_id=request_id,
        )

    def mark_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        provider_request_id: str = "",
    ) -> dict[str, Any]:
        return self._finish_job(
            job_id,
            status="failed",
            error_code=error_code,
            error_message=error_message,
            provider_request_id=provider_request_id,
        )

    def mark_outcome_unknown(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        provider_request_id: str = "",
    ) -> dict[str, Any]:
        return self._finish_job(
            job_id,
            status="outcome_unknown",
            error_code=error_code,
            error_message=error_message,
            provider_request_id=provider_request_id,
        )

    def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        platform_post_id: str = "",
        provider_request_id: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        if status not in {"published", "failed", "outcome_unknown"}:
            raise ValueError("Unsupported final publication status.")
        clean_id = str(job_id or "").strip()
        safe_code = str(error_code or "").strip()[:200]
        safe_message = str(error_message or "").strip()[:MAX_SAFE_ERROR_CHARS]
        now = _utc_now()
        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM publication_jobs WHERE id=?", (clean_id,)
            ).fetchone()
            if current is None:
                raise PublishingNotFound("Publication job was not found.")
            if current["status"] != "publishing":
                raise PublishingConflict(
                    "Only a claimed publishing job can be completed."
                )
            connection.execute(
                "UPDATE publication_jobs SET status=?,platform_post_id=?,provider_request_id=?,"
                "error_code=?,error_message=?,finished_at=?,updated_at=? WHERE id=?",
                (
                    status,str(platform_post_id or "").strip(),
                    str(provider_request_id or "").strip()[:300],safe_code,safe_message,
                    now,now,clean_id,
                ),
            )
            connection.commit()
        return self.get_job(clean_id)

    def requeue_failed(self, job_id: str) -> dict[str, Any]:
        clean_id = str(job_id or "").strip()
        now = _utc_now()
        with self._new_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM publication_jobs WHERE id=?", (clean_id,)
            ).fetchone()
            if row is None:
                raise PublishingNotFound("Publication job was not found.")
            if row["status"] != "failed":
                raise PublishingConflict(
                    "Only confirmed failed jobs can be manually requeued. Outcome-unknown jobs must be verified on Meta first."
                )
            connection.execute(
                "UPDATE publication_jobs SET status='queued',error_code='',error_message='',"
                "started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?",
                (now, clean_id),
            )
            connection.commit()
        return self.get_job(clean_id)

    @staticmethod
    def _optional_meta_id(value: str, label: str) -> str:
        clean = str(value or "").strip()
        if clean and (len(clean) > 200 or not clean.isdigit()):
            raise ValueError(f"{label} must be a numeric Meta object ID.")
        return clean


def _json_list(raw: str, label: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise PublishingConflict(f"Stored {label} is invalid.") from error
    if not isinstance(value, list):
        raise PublishingConflict(f"Stored {label} must be a list.")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
