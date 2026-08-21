"""Apply the Creative Upload + Senior Design Approval feature to the active branch.

Run once from the repository root:
    python staging/apply_senior_design_approval.py

The script uses exact anchors and stops rather than guessing if the source changed.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "campaign_store.py"
APP_PATH = ROOT / "app.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}.")
    return text.replace(old, new, 1)


def patch_campaign_store() -> None:
    text = STORE_PATH.read_text(encoding="utf-8")
    if "def save_creative_asset(" in text and "_ensure_v8_creative_review_schema" in text:
        print("campaign_store.py already patched")
        return

    text = replace_once(text, "SCHEMA_VERSION = 7", "SCHEMA_VERSION = 8", "schema version")

    methods = r'''
    def save_creative_asset(
        self,
        campaign_id: str,
        calendar_version_id: str,
        content_hash: str,
        post_number: int,
        *,
        file_name: str,
        mime_type: str,
        storage_path: str,
        file_sha256: str,
        file_size: int,
        source_type: str = "manual_upload",
        design_prompt: str = "",
    ) -> dict[str, Any]:
        """Append one immutable creative version for an approved post and design brief."""

        clean_campaign_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_calendar_id = _canonical_uuid(calendar_version_id, "calendar_version_id")
        clean_hash = _sha256_hash(content_hash, "content_hash")
        if not isinstance(post_number, int) or isinstance(post_number, bool) or post_number < 1:
            raise ValueError("post_number must be a positive integer.")
        clean_name = _required_text(file_name, "file_name", max_length=300)
        clean_mime = _required_text(mime_type, "mime_type", max_length=120).lower()
        clean_storage = _required_text(storage_path, "storage_path", max_length=1200)
        clean_file_hash = _sha256_hash(file_sha256, "file_sha256")
        if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 1:
            raise ValueError("file_size must be a positive integer.")
        if file_size > 12 * 1024 * 1024:
            raise ValueError("Creative file must be 12 MB or smaller.")
        clean_source = _approval_choice(
            source_type, "source_type", {"manual_upload", "ai_generated"}
        )
        clean_prompt = _bounded_optional_text(
            design_prompt, "design_prompt", max_length=12_000
        )
        now = _utc_now()
        asset_id = str(uuid4())

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (clean_campaign_id,)
            ).fetchone()
            if campaign is None:
                raise RecordNotFound(f"Campaign {clean_campaign_id} was not found.")
            if campaign["status"] not in {"fully_approved", "approved"}:
                raise InvalidStatusTransition(
                    "Creative assets can be added only after final Senior content approval."
                )
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (clean_calendar_id, clean_campaign_id),
            ).fetchone()
            if calendar is None:
                raise RecordNotFound("That calendar version does not belong to this campaign.")
            latest = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? ORDER BY version DESC LIMIT 1",
                (clean_campaign_id,),
            ).fetchone()
            if latest is None or latest["id"] != clean_calendar_id:
                raise StoreConflict("Creative assets can be added only to the latest content version.")
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if calculated_hash != calendar["content_hash"] or calculated_hash != clean_hash:
                raise StoreConflict("The creative upload does not match the approved content hash.")
            senior_approval = connection.execute(
                "SELECT 1 FROM approvals WHERE campaign_id=? AND calendar_version_id=? "
                "AND role='senior' AND decision='approved' AND content_hash=?",
                (clean_campaign_id, clean_calendar_id, calculated_hash),
            ).fetchone()
            if senior_approval is None:
                raise InvalidStatusTransition(
                    "A hash-matched Senior content approval is required before creative upload."
                )
            design_brief = connection.execute(
                "SELECT * FROM design_briefs WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? AND content_hash=?",
                (clean_campaign_id, clean_calendar_id, post_number, calculated_hash),
            ).fetchone()
            if design_brief is None:
                raise InvalidStatusTransition(
                    "Generate the approved post's Design Brief before uploading a creative."
                )
            next_version = connection.execute(
                "SELECT COALESCE(MAX(asset_version),0)+1 FROM creative_assets "
                "WHERE campaign_id=? AND calendar_version_id=? AND post_number=?",
                (clean_campaign_id, clean_calendar_id, post_number),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO creative_assets (
                    id,campaign_id,calendar_version_id,content_hash,design_brief_id,
                    post_number,row_index,format,asset_version,source_type,file_name,
                    mime_type,storage_path,file_sha256,file_size,design_prompt,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    asset_id, clean_campaign_id, clean_calendar_id, calculated_hash,
                    design_brief["id"], post_number, design_brief["row_index"],
                    design_brief["format"], next_version, clean_source, clean_name,
                    clean_mime, clean_storage, clean_file_hash, file_size, clean_prompt, now,
                ),
            )
            self._insert_event(
                connection,
                campaign_id=clean_campaign_id,
                event_type="creative_asset_uploaded",
                details={
                    "creative_asset_id": asset_id,
                    "calendar_version_id": clean_calendar_id,
                    "post_number": post_number,
                    "asset_version": next_version,
                    "source_type": clean_source,
                    "file_sha256": clean_file_hash,
                },
                from_status=campaign["status"],
                to_status=campaign["status"],
                timestamp=now,
            )
            row = connection.execute(
                "SELECT * FROM creative_assets WHERE id=?", (asset_id,)
            ).fetchone()
            connection.commit()
        return _creative_asset_from_row(row)

    def get_creative_asset(self, creative_asset_id: str) -> dict[str, Any]:
        clean_id = _canonical_uuid(creative_asset_id, "creative_asset_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM creative_assets WHERE id=?", (clean_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Creative asset {clean_id} was not found.")
        return _creative_asset_from_row(row)

    def list_latest_creative_assets(
        self, campaign_id: str, calendar_version_id: str
    ) -> list[dict[str, Any]]:
        """Return the newest creative for each post plus its latest review decision."""

        clean_campaign_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_calendar_id = _canonical_uuid(calendar_version_id, "calendar_version_id")
        with self._connection() as connection:
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (clean_calendar_id, clean_campaign_id),
            ).fetchone()
            if calendar is None:
                raise RecordNotFound("That calendar version does not belong to this campaign.")
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if calculated_hash != calendar["content_hash"]:
                raise StoreConflict("The content version no longer matches its stored hash.")
            rows = connection.execute(
                "SELECT * FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "ORDER BY post_number ASC,asset_version DESC",
                (clean_campaign_id, clean_calendar_id),
            ).fetchall()
            selected: dict[int, sqlite3.Row] = {}
            for row in rows:
                selected.setdefault(int(row["post_number"]), row)
            results = []
            for post_number in sorted(selected):
                asset_row = selected[post_number]
                if asset_row["content_hash"] != calculated_hash:
                    raise StoreConflict("A creative asset does not match the approved content hash.")
                approval = connection.execute(
                    "SELECT * FROM design_approvals WHERE creative_asset_id=?",
                    (asset_row["id"],),
                ).fetchone()
                active_link = connection.execute(
                    "SELECT 1 FROM design_review_links WHERE creative_asset_id=? "
                    "AND status='pending' AND expires_at>? LIMIT 1",
                    (asset_row["id"], _utc_now()),
                ).fetchone()
                item = _creative_asset_from_row(asset_row)
                item["latest_decision"] = approval["decision"] if approval else None
                item["design_feedback"] = approval["feedback"] if approval else ""
                item["design_change_fields"] = (
                    _deserialize_json(approval["change_fields_json"]) if approval else []
                )
                item["active_review_link"] = active_link is not None
                results.append(item)
        return results

    def create_design_review_link(
        self,
        creative_asset_id: str,
        token_hash: str,
        expires_at: str,
    ) -> dict[str, Any]:
        """Create or replace a secure review link for the latest creative version."""

        clean_asset_id = _canonical_uuid(creative_asset_id, "creative_asset_id")
        clean_token_hash = _sha256_hash(token_hash, "token_hash")
        clean_expires = _future_utc_timestamp(expires_at, "expires_at")
        now = _utc_now()
        link_id = str(uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                "SELECT * FROM creative_assets WHERE id=?", (clean_asset_id,)
            ).fetchone()
            if asset is None:
                raise RecordNotFound(f"Creative asset {clean_asset_id} was not found.")
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (asset["campaign_id"],)
            ).fetchone()
            if campaign is None or campaign["status"] not in {"fully_approved", "approved"}:
                raise InvalidStatusTransition(
                    "Design review requires final Senior content approval."
                )
            latest_calendar = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? ORDER BY version DESC LIMIT 1",
                (asset["campaign_id"],),
            ).fetchone()
            if latest_calendar is None or latest_calendar["id"] != asset["calendar_version_id"]:
                raise StoreConflict("Only the latest content version can enter design review.")
            latest_asset = connection.execute(
                "SELECT id FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? ORDER BY asset_version DESC LIMIT 1",
                (asset["campaign_id"], asset["calendar_version_id"], asset["post_number"]),
            ).fetchone()
            if latest_asset is None or latest_asset["id"] != clean_asset_id:
                raise StoreConflict("Only the latest creative version can enter design review.")
            decided = connection.execute(
                "SELECT 1 FROM design_approvals WHERE creative_asset_id=?",
                (clean_asset_id,),
            ).fetchone()
            if decided is not None:
                raise StoreConflict("This creative version already has a Senior design decision.")
            connection.execute(
                "UPDATE design_review_links SET status='revoked',revoked_at=? "
                "WHERE campaign_id=? AND calendar_version_id=? AND post_number=? "
                "AND status='pending'",
                (now, asset["campaign_id"], asset["calendar_version_id"], asset["post_number"]),
            )
            connection.execute(
                """
                INSERT INTO design_review_links (
                    id,creative_asset_id,campaign_id,calendar_version_id,post_number,
                    asset_hash,token_hash,status,expires_at,opened_at,decided_at,
                    revoked_at,created_at
                ) VALUES (?,?,?,?,?,?,?,'pending',?,NULL,NULL,NULL,?)
                """,
                (
                    link_id, clean_asset_id, asset["campaign_id"], asset["calendar_version_id"],
                    asset["post_number"], asset["file_sha256"], clean_token_hash,
                    clean_expires, now,
                ),
            )
            self._insert_event(
                connection,
                campaign_id=asset["campaign_id"],
                event_type="design_review_link_created",
                details={
                    "design_review_link_id": link_id,
                    "creative_asset_id": clean_asset_id,
                    "calendar_version_id": asset["calendar_version_id"],
                    "post_number": asset["post_number"],
                    "asset_version": asset["asset_version"],
                    "expires_at": clean_expires,
                },
                from_status=campaign["status"],
                to_status=campaign["status"],
                timestamp=now,
            )
            row = connection.execute(
                "SELECT * FROM design_review_links WHERE id=?", (link_id,)
            ).fetchone()
            connection.commit()
        return _design_review_link_from_row(row)

    def get_design_review_link_bundle(
        self, token_hash: str, *, mark_opened: bool = False
    ) -> dict[str, Any]:
        """Resolve a still-active design-review capability to immutable review material."""

        clean_hash = _sha256_hash(token_hash, "token_hash")
        now = _utc_now()
        with self._connection() as connection:
            if mark_opened:
                connection.execute("BEGIN IMMEDIATE")
            link = connection.execute(
                "SELECT * FROM design_review_links WHERE token_hash=?", (clean_hash,)
            ).fetchone()
            if link is None:
                raise RecordNotFound("Design review link was not found.")
            if link["status"] != "pending":
                raise StoreConflict("This design review link is no longer active.")
            if link["expires_at"] <= now:
                raise StoreConflict("This design review link has expired.")
            asset = connection.execute(
                "SELECT * FROM creative_assets WHERE id=?", (link["creative_asset_id"],)
            ).fetchone()
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (link["campaign_id"],)
            ).fetchone()
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (link["calendar_version_id"], link["campaign_id"]),
            ).fetchone()
            if asset is None or campaign is None or calendar is None:
                raise StoreConflict("This design review link is unavailable.")
            if campaign["status"] not in {"fully_approved", "approved"}:
                raise InvalidStatusTransition(
                    "This campaign no longer has final Senior content approval."
                )
            latest_calendar = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? ORDER BY version DESC LIMIT 1",
                (link["campaign_id"],),
            ).fetchone()
            if latest_calendar is None or latest_calendar["id"] != link["calendar_version_id"]:
                raise StoreConflict("This design review link is for an older content version.")
            latest_asset = connection.execute(
                "SELECT id FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? ORDER BY asset_version DESC LIMIT 1",
                (link["campaign_id"], link["calendar_version_id"], link["post_number"]),
            ).fetchone()
            if latest_asset is None or latest_asset["id"] != asset["id"]:
                raise StoreConflict("A newer creative version has replaced this review link.")
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if (
                calculated_hash != calendar["content_hash"]
                or asset["content_hash"] != calculated_hash
                or link["asset_hash"] != asset["file_sha256"]
            ):
                raise StoreConflict("This design review link does not match the approved source.")
            if connection.execute(
                "SELECT 1 FROM design_approvals WHERE creative_asset_id=?", (asset["id"],)
            ).fetchone() is not None:
                raise StoreConflict("This creative version already has a Senior design decision.")
            design_brief = connection.execute(
                "SELECT * FROM design_briefs WHERE id=?", (asset["design_brief_id"],)
            ).fetchone()
            client = connection.execute(
                "SELECT * FROM clients WHERE id=?", (campaign["client_id"],)
            ).fetchone()
            if design_brief is None or client is None:
                raise StoreConflict("Design review source material is unavailable.")
            if mark_opened and link["opened_at"] is None:
                connection.execute(
                    "UPDATE design_review_links SET opened_at=? WHERE id=?", (now, link["id"])
                )
                link = connection.execute(
                    "SELECT * FROM design_review_links WHERE id=?", (link["id"],)
                ).fetchone()
                connection.commit()
        return {
            "link": _design_review_link_from_row(link),
            "asset": _creative_asset_from_row(asset),
            "campaign": _campaign_from_row(campaign),
            "calendar": _calendar_from_row(calendar),
            "client": _client_from_row(client),
            "design_brief": _design_brief_from_row(design_brief),
        }

    def decide_design_review_link(
        self,
        token_hash: str,
        decision: str,
        approver_name: str,
        approver_email: str,
        feedback: str = "",
        *,
        change_fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Consume a design-review link without mutating the approved content package."""

        clean_hash = _sha256_hash(token_hash, "token_hash")
        clean_decision = _approval_choice(decision, "decision", {"approved", "rejected"})
        clean_name = _required_text(approver_name, "approver_name", max_length=200)
        clean_email = _required_text(approver_email, "approver_email", max_length=320)
        clean_feedback = _bounded_optional_text(feedback, "feedback", max_length=5000)
        allowed_fields = (
            "Layout", "Image / Visual", "Colors", "Typography", "Text Placement",
            "Logo / Branding", "CTA Placement", "Carousel Slides", "Thumbnail", "Other",
        )
        raw_fields = [] if change_fields is None else list(change_fields)
        requested = {str(value).strip() for value in raw_fields if str(value).strip()}
        unknown = requested.difference(allowed_fields)
        if unknown:
            raise ValueError("Unsupported design change field(s): " + ", ".join(sorted(unknown)))
        clean_fields = [field for field in allowed_fields if field in requested]
        if clean_decision == "rejected":
            if not clean_feedback:
                raise ValueError("feedback is required when a design is rejected.")
            if not clean_fields:
                raise ValueError("Select at least one design field that needs changes.")
        elif clean_fields:
            raise ValueError("change_fields are allowed only when requesting design changes.")
        now = _utc_now()
        approval_id = str(uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            link = connection.execute(
                "SELECT * FROM design_review_links WHERE token_hash=?", (clean_hash,)
            ).fetchone()
            if link is None:
                raise RecordNotFound("Design review link was not found.")
            if link["status"] != "pending" or link["expires_at"] <= now:
                raise StoreConflict("This design review link is no longer active.")
            asset = connection.execute(
                "SELECT * FROM creative_assets WHERE id=?", (link["creative_asset_id"],)
            ).fetchone()
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (link["campaign_id"],)
            ).fetchone()
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (link["calendar_version_id"], link["campaign_id"]),
            ).fetchone()
            if asset is None or campaign is None or calendar is None:
                raise StoreConflict("This design review link is unavailable.")
            if campaign["status"] not in {"fully_approved", "approved"}:
                raise InvalidStatusTransition("Final Senior content approval is required.")
            latest_asset = connection.execute(
                "SELECT id FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? ORDER BY asset_version DESC LIMIT 1",
                (link["campaign_id"], link["calendar_version_id"], link["post_number"]),
            ).fetchone()
            if latest_asset is None or latest_asset["id"] != asset["id"]:
                raise StoreConflict("A newer creative version has replaced this review link.")
            if link["asset_hash"] != asset["file_sha256"]:
                raise StoreConflict("The design review link does not match the creative file.")
            try:
                connection.execute(
                    """
                    INSERT INTO design_approvals (
                        id,creative_asset_id,campaign_id,calendar_version_id,post_number,
                        decision,approver_name,approver_email,feedback,change_fields_json,
                        asset_hash,decided_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        approval_id, asset["id"], link["campaign_id"], link["calendar_version_id"],
                        link["post_number"], clean_decision, clean_name, clean_email,
                        clean_feedback, _serialize_json(clean_fields, "change_fields"),
                        asset["file_sha256"], now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict("This creative version already has a Senior design decision.") from error
            connection.execute(
                "UPDATE design_review_links SET status='decided',decided_at=? WHERE id=?",
                (now, link["id"]),
            )
            connection.execute(
                "UPDATE design_review_links SET status='revoked',revoked_at=? "
                "WHERE creative_asset_id=? AND id<>? AND status='pending'",
                (now, asset["id"], link["id"]),
            )
            self._insert_event(
                connection,
                campaign_id=link["campaign_id"],
                event_type="design_approval_recorded",
                details={
                    "design_approval_id": approval_id,
                    "creative_asset_id": asset["id"],
                    "calendar_version_id": link["calendar_version_id"],
                    "post_number": link["post_number"],
                    "asset_version": asset["asset_version"],
                    "decision": clean_decision,
                    "change_fields": clean_fields,
                    "asset_hash": asset["file_sha256"],
                },
                from_status=campaign["status"],
                to_status=campaign["status"],
                timestamp=now,
            )
            approval = connection.execute(
                "SELECT * FROM design_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            updated_link = connection.execute(
                "SELECT * FROM design_review_links WHERE id=?", (link["id"],)
            ).fetchone()
            connection.commit()
        return {
            "approval": _design_approval_from_row(approval),
            "asset": _creative_asset_from_row(asset),
            "link": _design_review_link_from_row(updated_link),
        }

'''
    text = replace_once(
        text,
        "    # Manual client-share audit APIs appear before the automated review APIs.\n",
        methods + "    # Manual client-share audit APIs appear before the automated review APIs.\n",
        "creative methods",
    )

    text = replace_once(
        text,
        "            self._ensure_v7_design_brief_schema(connection)\n            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "            self._ensure_v7_design_brief_schema(connection)\n            self._ensure_v8_creative_review_schema(connection)\n            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "v8 schema call",
    )

    schema = r'''
    @staticmethod
    def _ensure_v8_creative_review_schema(connection: sqlite3.Connection) -> None:
        """Install immutable creative versions and separate Senior design decisions."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS creative_assets (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                calendar_version_id TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                design_brief_id TEXT NOT NULL,
                post_number INTEGER NOT NULL CHECK (post_number > 0),
                row_index INTEGER NOT NULL CHECK (row_index >= 0),
                format TEXT NOT NULL,
                asset_version INTEGER NOT NULL CHECK (asset_version > 0),
                source_type TEXT NOT NULL CHECK (source_type IN ('manual_upload','ai_generated')),
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                file_sha256 TEXT NOT NULL CHECK (
                    length(file_sha256)=64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                file_size INTEGER NOT NULL CHECK (file_size > 0),
                design_prompt TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (campaign_id,calendar_version_id,post_number,asset_version),
                FOREIGN KEY (campaign_id,calendar_version_id)
                    REFERENCES calendar_versions(campaign_id,id) ON DELETE RESTRICT,
                FOREIGN KEY (design_brief_id) REFERENCES design_briefs(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS design_review_links (
                id TEXT PRIMARY KEY,
                creative_asset_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                calendar_version_id TEXT NOT NULL,
                post_number INTEGER NOT NULL CHECK (post_number > 0),
                asset_hash TEXT NOT NULL CHECK (
                    length(asset_hash)=64 AND asset_hash NOT GLOB '*[^0-9a-f]*'
                ),
                token_hash TEXT NOT NULL UNIQUE CHECK (
                    length(token_hash)=64 AND token_hash NOT GLOB '*[^0-9a-f]*'
                ),
                status TEXT NOT NULL CHECK (status IN ('pending','decided','revoked')),
                expires_at TEXT NOT NULL,
                opened_at TEXT,
                decided_at TEXT,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (creative_asset_id) REFERENCES creative_assets(id) ON DELETE RESTRICT,
                FOREIGN KEY (campaign_id,calendar_version_id)
                    REFERENCES calendar_versions(campaign_id,id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS design_approvals (
                id TEXT PRIMARY KEY,
                creative_asset_id TEXT NOT NULL UNIQUE,
                campaign_id TEXT NOT NULL,
                calendar_version_id TEXT NOT NULL,
                post_number INTEGER NOT NULL CHECK (post_number > 0),
                decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
                approver_name TEXT NOT NULL CHECK (length(approver_name) BETWEEN 1 AND 200),
                approver_email TEXT NOT NULL CHECK (length(approver_email) BETWEEN 1 AND 320),
                feedback TEXT NOT NULL DEFAULT '' CHECK (length(feedback) <= 5000),
                change_fields_json TEXT NOT NULL,
                asset_hash TEXT NOT NULL CHECK (
                    length(asset_hash)=64 AND asset_hash NOT GLOB '*[^0-9a-f]*'
                ),
                decided_at TEXT NOT NULL,
                FOREIGN KEY (creative_asset_id) REFERENCES creative_assets(id) ON DELETE RESTRICT,
                FOREIGN KEY (campaign_id,calendar_version_id)
                    REFERENCES calendar_versions(campaign_id,id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS creative_assets_post_idx "
            "ON creative_assets(campaign_id,calendar_version_id,post_number,asset_version DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS design_review_links_post_idx "
            "ON design_review_links(campaign_id,calendar_version_id,post_number,created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS design_review_links_one_active "
            "ON design_review_links(campaign_id,calendar_version_id,post_number) "
            "WHERE status='pending'"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS design_approvals_campaign_idx "
            "ON design_approvals(campaign_id,calendar_version_id,post_number,decided_at)"
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS design_approvals_no_update
            BEFORE UPDATE ON design_approvals
            BEGIN
                SELECT RAISE(ABORT, 'design approval decisions are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS design_approvals_no_delete
            BEFORE DELETE ON design_approvals
            BEGIN
                SELECT RAISE(ABORT, 'design approval decisions are append-only');
            END
            """
        )

'''
    text = replace_once(
        text,
        "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        schema + "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        "v8 schema method",
    )

    converters = r'''

def _creative_asset_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "content_hash": row["content_hash"],
        "design_brief_id": row["design_brief_id"],
        "post_number": row["post_number"],
        "row_index": row["row_index"],
        "format": row["format"],
        "asset_version": row["asset_version"],
        "source_type": row["source_type"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "storage_path": row["storage_path"],
        "file_sha256": row["file_sha256"],
        "file_size": row["file_size"],
        "design_prompt": row["design_prompt"],
        "created_at": row["created_at"],
    }


def _design_review_link_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "creative_asset_id": row["creative_asset_id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "post_number": row["post_number"],
        "asset_hash": row["asset_hash"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "opened_at": row["opened_at"],
        "decided_at": row["decided_at"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
    }


def _design_approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "creative_asset_id": row["creative_asset_id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "post_number": row["post_number"],
        "decision": row["decision"],
        "approver_name": row["approver_name"],
        "approver_email": row["approver_email"],
        "feedback": row["feedback"],
        "change_fields": _deserialize_json(row["change_fields_json"]),
        "asset_hash": row["asset_hash"],
        "decided_at": row["decided_at"],
    }

'''
    text = replace_once(
        text,
        "\ndef _senior_change_request_from_row(row: sqlite3.Row) -> dict[str, Any]:\n",
        converters + "\ndef _senior_change_request_from_row(row: sqlite3.Row) -> dict[str, Any]:\n",
        "creative row converters",
    )

    STORE_PATH.write_text(text, encoding="utf-8")
    print("patched campaign_store.py")


def patch_app() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "def render_design_review_portal(" in text and "render_creative_asset_controls" in text:
        print("app.py already patched")
        return

    imports_anchor = '''from design_brief import (
    DESIGN_BRIEF_SYSTEM_PROMPT,
    DESIGN_STATUS_BRIEF_READY,
    DESIGN_STATUS_LOCKED,
    DESIGN_STATUS_NOT_GENERATED,
    build_design_brief_prompt,
    display_design_brief_sections,
    parse_design_brief_response,
)
'''
    creative_import = imports_anchor + '''from creative_workflow import (
    DESIGN_CHANGE_FIELDS,
    PUBLISHING_STATUS_READY,
    build_ai_design_prompt,
    content_post_by_number,
    creative_status,
    publishing_status,
    validate_creative_upload,
)
'''
    text = replace_once(text, imports_anchor, creative_import, "creative imports")

    link_import = '''from senior_review_links import (
    build_review_url,
    generate_review_token,
    hash_review_token,
)
'''
    link_import_new = '''from senior_review_links import (
    build_design_review_url,
    build_review_url,
    generate_review_token,
    hash_design_review_token,
    hash_review_token,
)
'''
    text = replace_once(text, link_import, link_import_new, "design review link imports")

    text = replace_once(
        text,
        'DEFAULT_GENERATED_OUTPUT_DIR = Path(__file__).with_name("generated_outputs")\n',
        'DEFAULT_GENERATED_OUTPUT_DIR = Path(__file__).with_name("generated_outputs")\nDEFAULT_CREATIVE_OUTPUT_DIR = DEFAULT_GENERATED_OUTPUT_DIR / "creative_assets"\n',
        "creative output directory",
    )

    query_anchor = '''try:
    REVIEW_MODE_TOKEN = str(st.query_params.get("review", "") or "").strip()
except Exception:
    REVIEW_MODE_TOKEN = ""
'''
    query_new = query_anchor + '''try:
    DESIGN_REVIEW_MODE_TOKEN = str(st.query_params.get("design_review", "") or "").strip()
except Exception:
    DESIGN_REVIEW_MODE_TOKEN = ""
'''
    text = replace_once(text, query_anchor, query_new, "design review query token")
    text = replace_once(
        text,
        "if not REVIEW_MODE_TOKEN:\n",
        "if not REVIEW_MODE_TOKEN and not DESIGN_REVIEW_MODE_TOKEN:\n",
        "main dashboard mode guard",
    )
    text = replace_once(
        text,
        'f"Client details → {provider_label} → Content Package → Senior Approval → Design Briefs / Excel"',
        'f"Client details → {provider_label} → Content Package → Senior Approval → Design Briefs → Creative Review / Excel"',
        "flow caption",
    )

    portal_and_controls = r'''

def render_creative_file(asset, *, key_prefix):
    path = Path(asset["storage_path"])
    if not path.exists():
        st.warning("The creative file is not available on this app instance.")
        return
    if str(asset["mime_type"]).startswith("image/"):
        st.image(str(path), caption=f"{asset['file_name']} — v{asset['asset_version']}")
    elif asset["mime_type"] == "application/pdf":
        with open(path, "rb") as handle:
            st.download_button(
                "Open / Download Creative PDF",
                data=handle.read(),
                file_name=asset["file_name"],
                mime="application/pdf",
                key=f"{key_prefix}_pdf_download",
                use_container_width=True,
            )


def render_design_review_portal(store, raw_token):
    """Render a secure, creative-only Senior review page."""
    st.title("Senior Design Review")
    st.caption(
        "Secure design review — approved content is read-only; only the creative can be approved or sent back."
    )
    if store is None:
        st.error("Review storage is unavailable. Ask the campaign owner to try again.")
        return
    try:
        token_hash = hash_design_review_token(raw_token)
        bundle = store.get_design_review_link_bundle(token_hash, mark_opened=True)
    except PERSISTENCE_EXCEPTIONS:
        st.error(
            "This design review link is invalid, expired, replaced, or already used. "
            "Ask the campaign owner to create a new design review link."
        )
        return

    campaign = bundle["campaign"]
    calendar = bundle["calendar"]
    client = bundle["client"]
    asset = bundle["asset"]
    design_brief = bundle["design_brief"]
    link = bundle["link"]
    try:
        approved_post = content_post_by_number(
            calendar["headers"],
            calendar["rows"],
            int(asset["post_number"]),
            week_heading_prefix=WEEK_HEADING_PREFIX,
        )
    except (TypeError, ValueError) as error:
        st.error(f"The approved source post cannot be displayed safely: {error}")
        return

    st.success("Status: Pending Senior Design Review")
    st.markdown(f"**Client:** {client['name']}")
    st.markdown(
        f"**Post {asset['post_number']} — {asset['format']} — Creative v{asset['asset_version']}**"
    )
    st.caption(f"Campaign ID: {campaign['id']}")
    st.caption(f"Review link expires: {link['expires_at']}")

    st.markdown("### Approved Content (Read-only)")
    for field in ("Content Idea", "CTA", "Caption", "Reel Script"):
        value = approved_post["content"].get(field)
        if value and str(value).strip() and str(value).strip().lower() != "not applicable":
            st.markdown(f"**{field}:** {value}")

    st.markdown("### Approved Design Brief")
    for label, value in display_design_brief_sections(design_brief["brief"]):
        st.markdown(f"**{label}**")
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                st.write(f"{index}. {item}")
        else:
            st.write(value)

    st.markdown("### Creative to Review")
    render_creative_file(asset, key_prefix=f"design_review_{asset['id']}")

    decision_choice = st.radio(
        "Senior Design Decision",
        ("Approve Design", "Request Design Changes"),
        horizontal=True,
        key=f"design_decision_{link['id']}",
    )
    selected_fields = []
    feedback = ""
    if decision_choice == "Request Design Changes":
        selected_fields = st.multiselect(
            "Which design area(s) need changes?",
            list(DESIGN_CHANGE_FIELDS),
            key=f"design_change_fields_{link['id']}",
        )
        feedback = st.text_area(
            "Required Design Changes",
            max_chars=5000,
            key=f"design_feedback_{link['id']}",
            placeholder="Example: Keep the approved headline and CTA unchanged. Increase logo visibility and simplify the background.",
        )

    with st.form(f"design_review_form_{link['id']}"):
        reviewer_name = st.text_input("Senior Reviewer Name", max_chars=200)
        reviewer_email = st.text_input("Senior Reviewer Email", max_chars=320)
        submit = st.form_submit_button(
            "Approve Design" if decision_choice == "Approve Design" else "Submit Design Changes",
            use_container_width=True,
        )
    if not submit:
        return
    if not reviewer_name.strip() or not reviewer_email.strip():
        st.error("Senior reviewer name and email are required.")
        return
    decision = "approved" if decision_choice == "Approve Design" else "rejected"
    if decision == "rejected" and (not selected_fields or not feedback.strip()):
        st.error("Select at least one design area and describe the required changes.")
        return
    try:
        result = store.decide_design_review_link(
            token_hash,
            decision,
            reviewer_name.strip(),
            reviewer_email.strip(),
            feedback.strip() if decision == "rejected" else "",
            change_fields=selected_fields if decision == "rejected" else [],
        )
    except PERSISTENCE_EXCEPTIONS as error:
        st.error(f"The design decision could not be saved: {error}")
        return
    if result["approval"]["decision"] == "approved":
        st.success("Design approved. This creative version is now final for the post.")
    else:
        st.success(
            "Design change request saved. The marketing/design team can upload a new creative version."
        )
    st.info("This design review link is now consumed and cannot be reused.")


def render_creative_asset_controls(store, campaign, calendar, client, brief_record):
    """Show provider-neutral prompt, upload/versioning, and secure design review controls."""
    post_number = int(brief_record["post_number"])
    try:
        approved_post = content_post_by_number(
            calendar["headers"],
            calendar["rows"],
            post_number,
            week_heading_prefix=WEEK_HEADING_PREFIX,
        )
        prompt = build_ai_design_prompt(
            brief_record["brief"],
            approved_post,
            client_metadata={
                **dict(calendar.get("client_metadata") or {}),
                "client_name": client.get("name") if client else "",
                "language": (campaign.get("intake") or {}).get("language", ""),
            },
        )
    except (TypeError, ValueError) as error:
        st.warning(f"Creative production controls are unavailable: {error}")
        return

    st.markdown("#### Creative Production")
    st.caption(
        "The prompt is provider-neutral: paste it into Canva or another design AI, "
        "or create the design manually in any platform and upload the final file here."
    )
    st.markdown("**AI Design Prompt**")
    st.code(prompt, language=None)

    try:
        assets = store.list_latest_creative_assets(campaign["id"], calendar["id"])
    except PERSISTENCE_EXCEPTIONS as error:
        st.warning(f"Creative status could not be loaded: {error}")
        return
    latest_asset = next(
        (item for item in assets if int(item["post_number"]) == post_number), None
    )
    if latest_asset:
        st.markdown(f"**Creative Status:** {creative_status(latest_asset)}")
        render_creative_file(latest_asset, key_prefix=f"dashboard_{latest_asset['id']}")
        if latest_asset.get("latest_decision") == "rejected":
            fields = ", ".join(latest_asset.get("design_change_fields") or [])
            if fields:
                st.markdown(f"**Senior requested changes in:** {fields}")
            if latest_asset.get("design_feedback"):
                st.info(latest_asset["design_feedback"])
        elif latest_asset.get("latest_decision") == "approved":
            st.success("This creative version is Senior Design Approved.")

        if latest_asset.get("latest_decision") != "approved":
            if st.button(
                "Create / Replace Senior Design Review Link",
                key=f"create_design_review_{latest_asset['id']}",
                use_container_width=True,
            ):
                try:
                    raw_token = generate_review_token()
                    token_hash = hash_design_review_token(raw_token)
                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(hours=review_link_ttl_hours())
                    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    store.create_design_review_link(
                        latest_asset["id"], token_hash, expires_at
                    )
                    url = build_design_review_url(configured_public_base_url(), raw_token)
                    st.session_state[f"design_review_url_{latest_asset['id']}"] = url
                except PERSISTENCE_EXCEPTIONS as error:
                    st.error(f"Design review link could not be created: {error}")
                except ValueError as error:
                    st.error(str(error))
            saved_url = st.session_state.get(f"design_review_url_{latest_asset['id']}")
            if saved_url:
                st.markdown("**Senior Design Review Link**")
                st.code(saved_url, language=None)
                st.caption(
                    "Share this URL only with the intended Senior reviewer. Creating a replacement revokes the prior pending link."
                )

    upload_label = (
        "Upload Replacement Creative (new version)" if latest_asset else "Upload Creative"
    )
    uploaded = st.file_uploader(
        upload_label,
        type=["png", "jpg", "jpeg", "pdf"],
        key=f"creative_upload_{calendar['id']}_{post_number}",
        help="PNG, JPG/JPEG, or PDF up to 12 MB. The design can come from Canva, Figma, Photoshop, another AI tool, or a manual designer.",
    )
    if uploaded is not None and st.button(
        "Save Creative Version",
        key=f"save_creative_{calendar['id']}_{post_number}",
        use_container_width=True,
    ):
        raw = uploaded.getvalue()
        try:
            metadata = validate_creative_upload(uploaded.name, uploaded.type, raw)
            output_root = Path(
                get_app_setting("CREATIVE_OUTPUT_DIR", str(DEFAULT_CREATIVE_OUTPUT_DIR))
            )
            post_dir = output_root / campaign["id"] / f"post_{post_number:02d}"
            post_dir.mkdir(parents=True, exist_ok=True)
            storage_path = post_dir / f"{uuid4().hex}{metadata['extension']}"
            storage_path.write_bytes(raw)
            try:
                store.save_creative_asset(
                    campaign["id"],
                    calendar["id"],
                    calendar["content_hash"],
                    post_number,
                    file_name=metadata["file_name"],
                    mime_type=metadata["mime_type"],
                    storage_path=str(storage_path),
                    file_sha256=metadata["file_sha256"],
                    file_size=metadata["file_size"],
                    source_type="manual_upload",
                    design_prompt=prompt,
                )
            except Exception:
                storage_path.unlink(missing_ok=True)
                raise
        except (OSError, PERSISTENCE_EXCEPTIONS) as error:
            st.error(f"Creative could not be saved safely: {error}")
        else:
            st.success("Creative version saved. It is ready for Senior Design Review.")
            st.rerun()

'''
    text = replace_once(
        text,
        "\nif REVIEW_MODE_TOKEN:\n    render_senior_review_portal(campaign_store, REVIEW_MODE_TOKEN)\n    st.stop()\n",
        portal_and_controls
        + "\nif REVIEW_MODE_TOKEN and DESIGN_REVIEW_MODE_TOKEN:\n"
          "    st.error(\"Use only one review capability link at a time.\")\n"
          "    st.stop()\n"
          "if DESIGN_REVIEW_MODE_TOKEN:\n"
          "    render_design_review_portal(campaign_store, DESIGN_REVIEW_MODE_TOKEN)\n"
          "    st.stop()\n"
          "if REVIEW_MODE_TOKEN:\n"
          "    render_senior_review_portal(campaign_store, REVIEW_MODE_TOKEN)\n"
          "    st.stop()\n",
        "design review portal route",
    )

    text = replace_once(
        text,
        "    design_briefs = []\n",
        "    design_briefs = []\n    latest_creative_assets = []\n",
        "creative dashboard state",
    )

    design_load_block = '''    if campaign_store is not None and campaign_id and latest_calendar is not None:
        try:
            design_briefs = campaign_store.list_design_briefs(
                campaign_id, latest_calendar["id"]
            )
        except PERSISTENCE_EXCEPTIONS as design_load_error:
            st.warning(f"Design brief status could not be loaded: {design_load_error}")
            design_briefs = []
'''
    design_load_new = design_load_block + '''
    if campaign_store is not None and campaign_id and latest_calendar is not None:
        try:
            latest_creative_assets = campaign_store.list_latest_creative_assets(
                campaign_id, latest_calendar["id"]
            )
        except PERSISTENCE_EXCEPTIONS as creative_load_error:
            st.warning(f"Creative status could not be loaded: {creative_load_error}")
            latest_creative_assets = []
'''
    text = replace_once(text, design_load_block, design_load_new, "load creative assets")

    design_status_old = '''            design_status_by_post = {
                int(item["post_number"]): DESIGN_STATUS_BRIEF_READY
                for item in design_briefs
            }
'''
    design_status_new = '''            latest_asset_by_post = {
                int(item["post_number"]): item for item in latest_creative_assets
            }
            design_status_by_post = {
                int(item["post_number"]): creative_status(
                    latest_asset_by_post.get(int(item["post_number"]))
                )
                for item in design_briefs
            }
'''
    text = replace_once(text, design_status_old, design_status_new, "creative design status")

    ready_anchor = '''            st.success(
                f"Design briefs ready for {len(design_briefs)} approved post(s)."
            )
            for record in design_briefs:
'''
    ready_new = '''            st.success(
                f"Design briefs ready for {len(design_briefs)} approved post(s)."
            )
            current_publishing_status = publishing_status(
                latest_creative_assets, len(design_briefs)
            )
            if current_publishing_status == PUBLISHING_STATUS_READY:
                st.success("Publishing Gate: Ready — every latest creative is Senior Design Approved.")
            else:
                st.info("Publishing Gate: Locked until every latest creative is Senior Design Approved.")
            for record in design_briefs:
'''
    text = replace_once(text, ready_anchor, ready_new, "publishing gate status")

    brief_render_anchor = '''                        else:
                            st.write(section_value)
        elif campaign_store is not None and latest_calendar is not None:
'''
    brief_render_new = '''                        else:
                            st.write(section_value)
                    if campaign_store is not None and campaign_record is not None:
                        render_creative_asset_controls(
                            campaign_store,
                            campaign_record,
                            latest_calendar,
                            client_record,
                            record,
                        )
        elif campaign_store is not None and latest_calendar is not None:
'''
    text = replace_once(text, brief_render_anchor, brief_render_new, "creative controls in design briefs")

    APP_PATH.write_text(text, encoding="utf-8")
    print("patched app.py")


def main() -> None:
    patch_campaign_store()
    patch_app()
    print("Senior Design Approval transformation complete.")


if __name__ == "__main__":
    main()
