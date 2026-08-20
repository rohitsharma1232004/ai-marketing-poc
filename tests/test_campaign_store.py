import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from campaign_store import (
    CampaignStore,
    SCHEMA_VERSION,
    CampaignStoreError,
    InvalidStatusTransition,
    RecordNotFound,
    StoreConflict,
)


HEADERS = [
    "Date",
    "Platform",
    "Pillar",
    "Format",
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
]
ROWS = [
    [
        "Mon, Aug 24",
        "Instagram",
        "Educational",
        "Image",
        "Helpful idea",
        "useful keyword",
        "Learn more",
    ]
]


def create_v1_database(db_path, *, user_version=1):
    """Create representative legacy data without v2 hashes or approvals."""
    client_id = str(uuid4())
    campaign_ids = {
        status: str(uuid4())
        for status in (
            "generating", "generation_unknown", "pending_review",
            "approved", "rejected", "generation_failed",
        )
    }
    version_id, event_id = str(uuid4()), str(uuid4())
    now = "2026-08-19T06:00:00.000Z"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE clients (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE campaigns (
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, external_id TEXT,
                request_id TEXT UNIQUE, status TEXT NOT NULL CHECK (status IN (
                    'generating', 'generation_unknown', 'pending_review',
                    'approved', 'rejected', 'generation_failed'
                )), intake_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
            );
            CREATE TABLE calendar_versions (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                headers_json TEXT NOT NULL, rows_json TEXT NOT NULL,
                client_metadata_json TEXT NOT NULL,
                generation_metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (campaign_id, version),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );
            CREATE TABLE workflow_events (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                event_type TEXT NOT NULL, from_status TEXT, to_status TEXT,
                details_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );
            """
        )
        connection.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, "Legacy Client", "legacy client", "{}", now, now),
        )
        for index, (status, campaign_id) in enumerate(campaign_ids.items()):
            connection.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, client_id, f"legacy-{index}", f"request-{index}",
                 status, json.dumps({"legacy_status": status}), now, now),
            )
        pending_id = campaign_ids["pending_review"]
        connection.execute(
            "INSERT INTO calendar_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, pending_id, 1, json.dumps(HEADERS), json.dumps(ROWS),
             '{"client_name":"Legacy Client"}', '{"provider":"ollama"}', now),
        )
        connection.execute(
            "INSERT INTO workflow_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, pending_id, "generation_succeeded", "generating",
             "pending_review", '{"version":1}', now),
        )
        connection.execute(f"PRAGMA user_version = {user_version}")
        connection.commit()
    return client_id, campaign_ids, version_id, event_id


class CampaignStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state" / "campaigns.sqlite3"
        self.store = CampaignStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def create_client_and_campaign(self, *, request_id=None):
        client = self.store.create_or_update_client(
            "ABC Realty", {"industry": "Real Estate"}
        )
        campaign = self.store.create_campaign(
            client["id"],
            {
                "goal": "Brand awareness",
                "document": {"id": "doc-123", "name": "brand.pdf"},
            },
            external_id="crm-campaign-42",
            request_id=request_id or f"request-{uuid4()}",
        )
        return client, campaign

    def create_review_ready_campaign(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        senior = self.store.upsert_review_recipient(
            campaign["id"], "senior", "Senior Reviewer", "+919876543210",
            "2026-08-20T00:00:00Z",
        )
        client = self.store.upsert_review_recipient(
            campaign["id"], "client", "Client Reviewer", "+919812345678",
            "2026-08-20T00:00:00Z",
        )
        return campaign, version, senior, client

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def test_schema_uses_wal_foreign_keys_and_busy_timeout(self):
        self.assertTrue(self.db_path.exists())
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("approvals", tables)
            self.assertTrue({
                "review_recipients", "review_requests", "review_sessions",
                "notification_outbox",
            }.issubset(tables))
            calendar_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(calendar_versions)")
            }
            self.assertIn("content_hash", calendar_columns)
            approval_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approvals)")
            }
            self.assertTrue({
                "approver_phone_e164", "identity_channel", "review_request_id"
            }.issubset(approval_columns))

        with self.store._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_v1_and_partial_v2_markers_migrate_without_losing_legacy_data(self):
        for old_marker in (1, 2):
            with self.subTest(old_marker=old_marker):
                path = Path(self.temp_dir.name) / f"legacy-{old_marker}.sqlite3"
                client_id, campaign_ids, version_id, event_id = create_v1_database(
                    path, user_version=old_marker
                )
                with CampaignStore(path) as migrated:
                    self.assertEqual(migrated.get_client(client_id)["name"], "Legacy Client")
                    self.assertEqual(
                        {
                            migrated.get_campaign(campaign_id)["status"]
                            for campaign_id in campaign_ids.values()
                        },
                        set(campaign_ids),
                    )
                    calendar = migrated.get_latest_calendar(
                        campaign_ids["pending_review"]
                    )
                    self.assertEqual(calendar["id"], version_id)
                    self.assertEqual(len(calendar["content_hash"]), 64)
                    self.assertEqual(
                        migrated.list_events(campaign_ids["pending_review"])[0]["id"],
                        event_id,
                    )
                    self.assertEqual(
                        migrated.list_approvals(campaign_ids["pending_review"]), []
                    )
                    migrated.record_approval(
                        campaign_ids["pending_review"], version_id,
                        "senior", "approved", "Senior", "senior@example.com",
                    )
                    self.assertEqual(
                        migrated.get_campaign(campaign_ids["pending_review"])["status"],
                        "pending_client_review",
                    )
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
                    )
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_missing_v2_approvals_table_is_recreated_idempotently(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TABLE approvals")
            connection.commit()
        with CampaignStore(self.db_path) as repaired:
            client = repaired.upsert_client("Repair Client")
            campaign = repaired.create_campaign(client["id"], {})
            version = repaired.complete_generation(campaign["id"], HEADERS, ROWS)
            repaired.record_approval(
                campaign["id"], version["id"], "senior", "approved",
                "Senior", "senior@example.com",
            )
            self.assertEqual(len(repaired.list_approvals(campaign["id"])), 1)

    def test_partial_v2_approval_migrates_without_losing_identity_or_triggers(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        original = self.store.record_approval(
            campaign["id"], version["id"], "senior", "approved",
            "Legacy Senior", "legacy@example.com", "Preserve this decision.",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript(
                """
                DROP TRIGGER IF EXISTS approvals_v3_no_update;
                DROP TRIGGER IF EXISTS approvals_v3_no_delete;
                DROP TRIGGER IF EXISTS approvals_no_update;
                DROP TRIGGER IF EXISTS approvals_no_delete;
                DROP TABLE IF EXISTS approvals_v2_archive;
                ALTER TABLE approvals RENAME TO approvals_v3_source;
                CREATE TABLE approvals (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    calendar_version_id TEXT NOT NULL, role TEXT NOT NULL,
                    decision TEXT NOT NULL, approver_name TEXT NOT NULL,
                    approver_email TEXT NOT NULL, feedback TEXT NOT NULL,
                    content_hash TEXT NOT NULL, decided_at TEXT NOT NULL
                );
                INSERT INTO approvals
                SELECT id,campaign_id,calendar_version_id,role,decision,
                       approver_name,approver_email,feedback,content_hash,decided_at
                FROM approvals_v3_source;
                PRAGMA user_version = 2;
                """
            )
            connection.commit()
        with CampaignStore(self.db_path) as migrated:
            approval = migrated.list_approvals(campaign["id"])[0]
            self.assertEqual(approval["id"], original["id"])
            self.assertEqual(approval["approver_email"], "legacy@example.com")
            self.assertIsNone(approval["approver_phone_e164"])
            self.assertEqual(approval["identity_channel"], "local_self_reported")
            self.assertIsNone(approval["review_request_id"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM approvals_v2_archive").fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE approvals SET feedback='changed' WHERE id=?", (original["id"],)
                )

    def test_newer_schema_version_is_rejected(self):
        path = Path(self.temp_dir.name) / "future.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaises(CampaignStoreError):
            CampaignStore(path)

    def test_upsert_client_normalizes_name_and_preserves_stable_id(self):
        first = self.store.create_or_update_client(
            "  Acme   WATER  ", {"industry": "Water", "location": "Delhi"}
        )
        second = self.store.create_or_update_client(
            "ACME water", {"location": "Faridabad"}
        )

        UUID(first["id"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["normalized_name"], "acme water")
        self.assertEqual(
            second["metadata"], {"industry": "Water", "location": "Faridabad"}
        )

    def test_explicit_client_id_can_update_and_conflicts_are_not_merged(self):
        stable_id = str(uuid4())
        client = self.store.upsert_client("Named Client", client_id=stable_id)
        updated = self.store.upsert_client(
            "Renamed Client", {"tone": "Professional"}, client_id=stable_id
        )
        self.assertEqual(client["id"], updated["id"])
        self.assertEqual(updated["name"], "Renamed Client")

        with self.assertRaises(StoreConflict):
            self.store.upsert_client(
                "Renamed Client", client_id=str(uuid4())
            )

    def test_create_and_retrieve_campaign_with_external_ids_and_event(self):
        client, campaign = self.create_client_and_campaign(request_id="req-unique")

        UUID(campaign["id"])
        self.assertEqual(campaign["client_id"], client["id"])
        self.assertEqual(campaign["external_id"], "crm-campaign-42")
        self.assertEqual(campaign["request_id"], "req-unique")
        self.assertEqual(campaign["status"], "generating")
        self.assertEqual(campaign["intake"]["document"]["id"], "doc-123")
        events = self.store.list_events(campaign["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "campaign_created")
        self.assertEqual(events[0]["to_status"], "generating")

    def test_request_id_is_unique(self):
        client = self.store.upsert_client("Client")
        self.store.create_campaign(client["id"], {}, request_id="same-request")
        with self.assertRaises(StoreConflict):
            self.store.create_campaign(client["id"], {}, request_id="same-request")

    def test_raw_document_bytes_are_rejected_at_every_json_boundary(self):
        client = self.store.upsert_client("Binary Test")
        with self.assertRaisesRegex(TypeError, "store a document ID"):
            self.store.create_campaign(
                client["id"], {"uploaded_document": b"raw PDF bytes"}
            )

        campaign = self.store.create_campaign(client["id"], {})
        with self.assertRaisesRegex(TypeError, "store a document ID"):
            self.store.save_calendar_version(
                campaign["id"], HEADERS, ROWS, client_metadata={"file": b"bytes"}
            )
        with self.assertRaisesRegex(TypeError, "store a document ID"):
            self.store.append_event(
                campaign["id"], "bad_event", {"provider_body": bytearray(b"bytes")}
            )

    def test_calendar_versions_are_json_round_tripped_and_preserve_history(self):
        client, campaign = self.create_client_and_campaign()
        first = self.store.save_calendar_version(
            campaign["id"],
            HEADERS,
            ROWS,
            client_metadata={"client_name": client["name"]},
            generation_metadata={"provider": "n8n", "model": "test-model"},
        )
        self.store.transition_campaign_status(campaign["id"], "pending_review")
        self.store.transition_campaign_status(
            campaign["id"], "rejected", details={"reason_code": "needs_revision"}
        )
        self.store.transition_campaign_status(
            campaign["id"], "generating", event_type="regeneration_started"
        )
        second_rows = [ROWS[0][:-1] + ["Book a demo"]]
        second = self.store.save_calendar_version(
            campaign["id"], HEADERS, second_rows
        )

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(first["headers"], HEADERS)
        self.assertEqual(first["rows"], ROWS)
        self.assertEqual(first["client_metadata"]["client_name"], "ABC Realty")
        self.assertEqual(first["generation_metadata"]["provider"], "n8n")
        self.assertEqual(
            self.store.get_latest_calendar(campaign["id"])["rows"], second_rows
        )
        self.assertEqual(
            [item["version"] for item in self.store.list_calendar_versions(campaign["id"])],
            [1, 2],
        )

    def test_valid_generation_failure_retry_review_and_approval_lifecycle(self):
        _, campaign = self.create_client_and_campaign()
        failed = self.store.transition_campaign_status(
            campaign["id"],
            "generation_failed",
            event_type="generation_failed",
            details={"code": "N8N_TIMEOUT", "retryable": True},
        )
        self.assertEqual(failed["status"], "generation_failed")
        retried = self.store.transition_campaign_status(
            campaign["id"], "generating", event_type="generation_retried"
        )
        self.assertEqual(retried["status"], "generating")
        self.store.save_calendar_version(campaign["id"], HEADERS, ROWS)
        self.store.transition_campaign_status(campaign["id"], "pending_review")
        approved = self.store.transition_campaign_status(campaign["id"], "approved")
        self.assertEqual(approved["status"], "approved")

        with self.assertRaises(InvalidStatusTransition):
            self.store.transition_campaign_status(campaign["id"], "generating")

    def test_ambiguous_generation_can_be_marked_unknown_then_retried(self):
        _, campaign = self.create_client_and_campaign()
        unknown = self.store.transition_campaign_status(
            campaign["id"],
            "generation_unknown",
            event_type="generation_outcome_unknown",
            details={"code": "N8N_TIMEOUT", "request_id": campaign["request_id"]},
        )
        self.assertEqual(unknown["status"], "generation_unknown")
        retried = self.store.transition_campaign_status(
            campaign["id"], "generating", event_type="generation_retried"
        )
        self.assertEqual(retried["status"], "generating")

    def test_single_senior_approval_can_be_terminal_for_active_poc(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        approval = self.store.record_approval(
            campaign["id"],
            version["id"],
            "senior",
            "approved",
            "Senior",
            "senior@example.com",
            senior_is_final=True,
        )
        self.assertEqual(approval["decision"], "approved")
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"], "fully_approved"
        )

    def test_complete_generation_saves_and_transitions_atomically(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        self.assertEqual(version["version"], 1)
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"],
            "pending_senior_review",
        )
        self.assertEqual(
            self.store.list_events(campaign["id"])[-1]["event_type"],
            "generation_succeeded",
        )

    def test_senior_then_client_approval_is_version_bound_and_audited(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)

        senior = self.store.record_approval(
            campaign["id"], version["id"], "senior", "approved",
            "Senior Reviewer", "senior@example.com", "Ready for the client.",
        )
        self.assertEqual(senior["content_hash"], version["content_hash"])
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"],
            "pending_client_review",
        )

        client = self.store.record_approval(
            campaign["id"], version["id"], "client", "approved",
            "Client Reviewer", "client@example.com", "Approved for publishing.",
        )
        self.assertEqual(client["content_hash"], version["content_hash"])
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"], "fully_approved"
        )
        approvals = self.store.list_approvals(campaign["id"])
        self.assertEqual([item["role"] for item in approvals], ["senior", "client"])
        bundle = self.store.get_campaign_review_bundle(campaign["id"])
        self.assertEqual(bundle["campaign"]["status"], "fully_approved")
        self.assertEqual(bundle["latest_calendar"]["id"], version["id"])
        self.assertEqual(bundle["approvals"], approvals)
        self.assertEqual(
            [event["event_type"] for event in self.store.list_events(campaign["id"])[-2:]],
            ["approval_recorded", "approval_recorded"],
        )
        approval_event = self.store.list_events(campaign["id"])[-1]
        self.assertNotIn("approver_email", approval_event["details"])
        self.assertNotIn("feedback", approval_event["details"])

        with closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE approvals SET feedback = 'changed' WHERE id = ?",
                    (senior["id"],),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM approvals WHERE id = ?", (senior["id"],))

    def test_client_cannot_decide_before_senior_and_duplicate_is_rejected(self):
        _, campaign = self.create_client_and_campaign()
        version = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        with self.assertRaises(InvalidStatusTransition):
            self.store.record_approval(
                campaign["id"], version["id"], "client", "approved",
                "Client", "client@example.com",
            )

        self.store.record_approval(
            campaign["id"], version["id"], "senior", "approved",
            "Senior", "senior@example.com",
        )
        with self.assertRaises(StoreConflict):
            self.store.record_approval(
                campaign["id"], version["id"], "senior", "approved",
                "Another Senior", "other@example.com",
            )
        self.assertEqual(len(self.store.list_approvals(campaign["id"])), 1)

    def test_decision_rejects_wrong_campaign_stale_version_and_tampering(self):
        _, first_campaign = self.create_client_and_campaign()
        first_version = self.store.save_calendar_version(
            first_campaign["id"], HEADERS, ROWS
        )
        latest_version = self.store.complete_generation(
            first_campaign["id"], HEADERS, [ROWS[0][:-1] + ["New CTA"]]
        )
        _, other_campaign = self.create_client_and_campaign()
        other_version = self.store.complete_generation(other_campaign["id"], HEADERS, ROWS)

        with self.assertRaises(StoreConflict):
            self.store.record_approval(
                first_campaign["id"], first_version["id"], "senior", "approved",
                "Senior", "senior@example.com",
            )
        with self.assertRaises(RecordNotFound):
            self.store.record_approval(
                first_campaign["id"], other_version["id"], "senior", "approved",
                "Senior", "senior@example.com",
            )

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE calendar_versions SET rows_json = ? WHERE id = ?",
                (json.dumps([["tampered"]]), latest_version["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(StoreConflict, "stored hash"):
            self.store.record_approval(
                first_campaign["id"], latest_version["id"], "senior", "approved",
                "Senior", "senior@example.com",
            )

    def test_rejection_requires_regeneration_before_a_new_review(self):
        _, campaign = self.create_client_and_campaign()
        first = self.store.complete_generation(campaign["id"], HEADERS, ROWS)
        senior_rejection = self.store.record_approval(
            campaign["id"], first["id"], "senior", "rejected",
            "Senior", "senior@example.com", "Revise the CTA.",
        )
        self.assertEqual(senior_rejection["decision"], "rejected")
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"], "revision_required"
        )
        with self.assertRaises(InvalidStatusTransition):
            self.store.record_approval(
                campaign["id"], first["id"], "client", "approved",
                "Client", "client@example.com",
            )

        self.store.transition_campaign_status(
            campaign["id"], "generating", event_type="revision_started"
        )
        second = self.store.complete_generation(
            campaign["id"], HEADERS, [ROWS[0][:-1] + ["Revised CTA"]]
        )
        self.store.record_approval(
            campaign["id"], second["id"], "senior", "approved",
            "Senior", "senior@example.com",
        )
        self.store.record_approval(
            campaign["id"], second["id"], "client", "rejected",
            "Client", "client@example.com", "Please revise again.",
        )
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"], "revision_required"
        )
        self.assertEqual(len(self.store.list_approvals(campaign["id"])), 3)

    def test_whatsapp_sessions_enforce_order_single_use_and_atomic_client_handoff(self):
        campaign, version, senior_recipient, client_recipient = (
            self.create_review_ready_campaign()
        )
        senior_token_hash = self.digest("senior-link-secret")
        created = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior_recipient["id"],
            senior_token_hash, "2099-01-01T00:00:00Z",
            outbox_dedupe_key=f"review:{campaign['id']}:senior:v1",
        )
        self.assertEqual(created["review_request"]["status"], "pending")
        self.assertNotIn("token_hash", created["review_request"])
        self.assertEqual(created["outbox"]["attempt_count"], 0)
        self.assertNotIn("phone_e164", created["outbox"])

        senior_session_hash = self.digest("senior-cookie-secret")
        opened = self.store.open_review_session(
            senior_token_hash, senior_session_hash, "2098-12-31T00:00:00Z"
        )
        self.assertEqual(opened["review_request"]["status"], "opened")
        self.assertNotIn("session_hash", opened["review_session"])
        with self.assertRaises(StoreConflict):
            self.store.open_review_session(
                senior_token_hash, self.digest("replay-session"),
                "2098-12-31T00:00:00Z",
            )

        client_token_hash = self.digest("client-link-secret")
        senior_decision = self.store.decide_review_session(
            senior_session_hash, "approved", "Ready for client review.",
            next_review_request={
                "recipient_id": client_recipient["id"],
                "token_hash": client_token_hash,
                "expires_at": "2099-01-01T00:00:00Z",
                "outbox_dedupe_key": f"review:{campaign['id']}:client:v1",
            },
        )
        self.assertEqual(
            senior_decision["campaign"]["status"], "pending_client_review"
        )
        self.assertEqual(
            senior_decision["next_review_request"]["role"], "client"
        )
        self.assertEqual(senior_decision["outbox"]["status"], "pending")
        senior_approval = senior_decision["approval"]
        self.assertIsNone(senior_approval["approver_email"])
        self.assertEqual(senior_approval["approver_phone_e164"], "+919876543210")
        self.assertEqual(senior_approval["identity_channel"], "whatsapp_link")
        self.assertEqual(
            senior_approval["review_request_id"], created["review_request"]["id"]
        )
        with self.assertRaises(StoreConflict):
            self.store.decide_review_session(senior_session_hash, "approved")

        client_session_hash = self.digest("client-cookie-secret")
        self.store.open_review_session(
            client_token_hash, client_session_hash, "2098-12-31T00:00:00Z"
        )
        client_decision = self.store.decide_review_session(
            client_session_hash, "approved", "Approved."
        )
        self.assertEqual(client_decision["campaign"]["status"], "fully_approved")
        self.assertIsNone(client_decision["next_review_request"])
        approvals = self.store.list_approvals(campaign["id"])
        self.assertEqual([item["role"] for item in approvals], ["senior", "client"])

        with closing(sqlite3.connect(self.db_path)) as connection:
            stored_hash = connection.execute(
                "SELECT token_hash FROM review_requests WHERE id=?",
                (created["review_request"]["id"],),
            ).fetchone()[0]
            self.assertEqual(stored_hash, senior_token_hash)
            self.assertNotEqual(stored_hash, "senior-link-secret")
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(notification_outbox)"
                )
            }
            self.assertFalse(
                columns.intersection({"payload", "payload_json", "phone", "token"})
            )
        events_json = json.dumps(self.store.list_events(campaign["id"]))
        self.assertNotIn("+919876543210", events_json)
        self.assertNotIn(senior_token_hash, events_json)
        self.assertNotIn(senior_session_hash, events_json)

    def test_notification_outbox_claim_retry_and_completion_are_atomic(self):
        campaign, version, senior, _ = self.create_review_ready_campaign()
        created = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            self.digest("delivery-token"), "2099-01-01T00:00:00Z",
            outbox_dedupe_key=f"review:{campaign['id']}:senior:delivery",
        )
        outbox_id = created["outbox"]["id"]
        self.assertEqual(self.store.get_notification_outbox(outbox_id)["status"], "pending")
        delivery = self.store.get_review_request_notification_bundle(
            created["review_request"]["id"]
        )
        self.assertEqual(delivery["recipient"]["phone_e164"], "+919876543210")
        claimed = self.store.claim_notification_outbox(limit=5)
        self.assertEqual([row["id"] for row in claimed], [outbox_id])
        self.assertEqual(claimed[0]["attempt_count"], 1)
        self.assertEqual(self.store.claim_notification_outbox(), [])
        retry = self.store.mark_notification_outbox(
            outbox_id, "pending", error_code="META_429",
            retry_at="2098-01-01T00:00:00Z",
        )
        self.assertEqual(retry["status"], "pending")
        self.assertEqual(retry["last_error_code"], "META_429")
        self.assertEqual(self.store.claim_notification_outbox(), [])
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE notification_outbox SET available_at=? WHERE id=?",
                ("2000-01-01T00:00:00.000Z", outbox_id),
            )
            connection.commit()
        claimed_again = self.store.claim_notification_outbox()
        self.assertEqual(claimed_again[0]["attempt_count"], 2)
        sent = self.store.mark_notification_outbox(
            outbox_id, "sent", provider_message_id="wamid.test-123"
        )
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(sent["provider_message_id"], "wamid.test-123")
        with self.assertRaises(StoreConflict):
            self.store.mark_notification_outbox(outbox_id, "sent")

    def test_review_inputs_expiry_revocation_rejection_and_event_privacy(self):
        campaign, version, senior, _ = self.create_review_ready_campaign()
        for bad_phone in ("9876543210", "+0123456789", "+123"):
            with self.subTest(phone=bad_phone), self.assertRaises(ValueError):
                self.store.upsert_review_recipient(
                    campaign["id"], "senior", "Bad Phone", bad_phone,
                    "2026-08-20T00:00:00Z",
                )
        with self.assertRaises(InvalidStatusTransition):
            self.store.create_review_request(
                campaign["id"], version["id"], "client",
                self.store.list_review_recipients(campaign["id"])[1]["id"],
                self.digest("early-client"), "2099-01-01T00:00:00Z",
            )
        created = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            self.digest("revoked-token"), "2099-01-01T00:00:00Z",
        )
        revoked = self.store.revoke_review_request(created["review_request"]["id"])
        self.assertEqual(revoked["status"], "revoked")
        with self.assertRaises(StoreConflict):
            self.store.open_review_session(
                self.digest("revoked-token"), self.digest("unused-session"),
                "2098-01-01T00:00:00Z",
            )
        expired = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            self.digest("expired-token"), "2099-01-01T00:00:00Z",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE review_requests SET expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00.000Z", expired["review_request"]["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(StoreConflict, "expired"):
            self.store.open_review_session(
                self.digest("expired-token"), self.digest("expired-session"),
                "2098-01-01T00:00:00Z",
            )
        with self.assertRaisesRegex(ValueError, "Sensitive review data"):
            self.store.append_event(campaign["id"], "unsafe", {"phone": "+919999999999"})

    def test_session_decision_rechecks_hash_latest_version_and_rejection_feedback(self):
        campaign, version, senior, _ = self.create_review_ready_campaign()
        token_hash = self.digest("tamper-token")
        self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            token_hash, "2099-01-01T00:00:00Z",
        )
        session_hash = self.digest("tamper-session")
        self.store.open_review_session(
            token_hash, session_hash, "2098-01-01T00:00:00Z"
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE calendar_versions SET rows_json=? WHERE id=?",
                (json.dumps([["tampered"]]), version["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(StoreConflict, "stored hash"):
            self.store.get_review_session_bundle(session_hash)
        with self.assertRaisesRegex(StoreConflict, "stored hash"):
            self.store.decide_review_session(session_hash, "approved")

        other_campaign, old_version, other_senior, _ = (
            self.create_review_ready_campaign()
        )
        stale_token = self.digest("stale-token")
        self.store.create_review_request(
            other_campaign["id"], old_version["id"], "senior", other_senior["id"],
            stale_token, "2099-01-01T00:00:00Z",
        )
        self.store.transition_campaign_status(
            other_campaign["id"], "generating", event_type="manual_revision_started"
        )
        self.store.complete_generation(
            other_campaign["id"], HEADERS, [ROWS[0][:-1] + ["New CTA"]]
        )
        with self.assertRaisesRegex(StoreConflict, "latest"):
            self.store.open_review_session(
                stale_token, self.digest("stale-session"),
                "2098-01-01T00:00:00Z",
            )

        reject_campaign, reject_version, reject_senior, _ = (
            self.create_review_ready_campaign()
        )
        reject_token = self.digest("reject-token")
        self.store.create_review_request(
            reject_campaign["id"], reject_version["id"], "senior",
            reject_senior["id"], reject_token, "2099-01-01T00:00:00Z",
        )
        reject_session = self.digest("reject-session")
        self.store.open_review_session(
            reject_token, reject_session, "2098-01-01T00:00:00Z"
        )
        with self.assertRaisesRegex(ValueError, "feedback is required"):
            self.store.decide_review_session(reject_session, "rejected")
        rejected = self.store.decide_review_session(
            reject_session, "rejected", "Please revise the claims."
        )
        self.assertEqual(rejected["campaign"]["status"], "revision_required")

    def test_failed_atomic_client_handoff_rolls_back_senior_decision(self):
        campaign, version, senior, client = self.create_review_ready_campaign()
        token_hash = self.digest("atomic-senior-token")
        request = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            token_hash, "2099-01-01T00:00:00Z",
        )["review_request"]
        session_hash = self.digest("atomic-senior-session")
        self.store.open_review_session(
            token_hash, session_hash, "2098-01-01T00:00:00Z"
        )
        with self.assertRaises(StoreConflict):
            self.store.decide_review_session(
                session_hash, "approved",
                next_review_request={
                    "recipient_id": senior["id"],
                    "token_hash": self.digest("invalid-client-token"),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "outbox_dedupe_key": f"review:{campaign['id']}:invalid",
                },
            )
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["status"],
            "pending_senior_review",
        )
        self.assertEqual(self.store.list_approvals(campaign["id"]), [])
        self.assertEqual(self.store.get_review_request(request["id"])["status"], "opened")
        self.assertIsNone(
            self.store.get_review_session_bundle(session_hash)["review_session"]["consumed_at"]
        )
        completed = self.store.decide_review_session(
            session_hash, "approved",
            next_review_request={
                "recipient_id": client["id"],
                "token_hash": self.digest("valid-client-token"),
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        self.assertEqual(completed["campaign"]["status"], "pending_client_review")

    def test_active_request_blocks_recipient_change_and_duplicate_stage_link(self):
        campaign, version, senior, _ = self.create_review_ready_campaign()
        first = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            self.digest("active-token"), "2099-01-01T00:00:00Z",
        )["review_request"]
        with self.assertRaises(StoreConflict):
            self.store.upsert_review_recipient(
                campaign["id"], "senior", "Changed Senior", "+919700000000",
                "2026-08-20T00:00:00Z",
            )
        with self.assertRaises(StoreConflict):
            self.store.create_review_request(
                campaign["id"], version["id"], "senior", senior["id"],
                self.digest("duplicate-stage-token"), "2099-01-01T00:00:00Z",
            )
        listed = self.store.list_review_requests(campaign["id"], "senior", limit=1)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["role"], "senior")
        self.assertNotIn("token_hash", listed[0])
        self.assertEqual(self.store.list_review_requests(campaign["id"], "client"), [])
        with self.assertRaises(ValueError):
            self.store.list_review_requests(campaign["id"], limit=0)
        with self.assertRaises(ValueError):
            self.store.list_review_requests(campaign["id"], "owner")
        self.store.revoke_review_request(first["id"])
        second = self.store.create_review_request(
            campaign["id"], version["id"], "senior", senior["id"],
            self.digest("replacement-token"), "2099-01-01T00:00:00Z",
        )["review_request"]
        self.assertEqual(
            [row["id"] for row in self.store.list_review_requests(campaign["id"])],
            [second["id"], first["id"]],
        )

    def test_invalid_status_transitions_and_save_outside_generation_are_rejected(self):
        _, campaign = self.create_client_and_campaign()
        with self.assertRaises(InvalidStatusTransition):
            self.store.transition_campaign_status(campaign["id"], "approved")
        self.store.transition_campaign_status(campaign["id"], "generation_failed")
        with self.assertRaises(InvalidStatusTransition):
            self.store.save_calendar_version(campaign["id"], HEADERS, ROWS)
        with self.assertRaises(ValueError):
            self.store.transition_campaign_status(campaign["id"], "unknown")

    def test_append_and_list_events_decode_structured_details(self):
        _, campaign = self.create_client_and_campaign()
        appended = self.store.append_event(
            campaign["id"],
            "provider_started",
            {"provider": "n8n", "attempt": 1},
        )
        UUID(appended["id"])
        events = self.store.list_events(campaign["id"])
        self.assertEqual(events[-1]["details"], {"attempt": 1, "provider": "n8n"})
        self.assertEqual(events[-1]["event_type"], "provider_started")

    def test_list_campaigns_is_bounded_and_includes_client_name(self):
        client = self.store.upsert_client("List Client")
        first = self.store.create_campaign(client["id"], {"sequence": 1})
        second = self.store.create_campaign(client["id"], {"sequence": 2})

        rows = self.store.list_campaigns(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], second["id"])
        self.assertEqual(rows[0]["client_name"], "List Client")
        self.assertNotIn("intake", rows[0])
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(ValueError):
            self.store.list_campaigns(limit=0)
        with self.assertRaises(ValueError):
            self.store.list_campaigns(limit=101)

    def test_missing_records_raise_clear_errors(self):
        unknown = str(uuid4())
        with self.assertRaises(RecordNotFound):
            self.store.get_client(unknown)
        with self.assertRaises(RecordNotFound):
            self.store.get_campaign(unknown)
        with self.assertRaises(RecordNotFound):
            self.store.get_latest_calendar(unknown)

    def test_parameterized_queries_handle_quotes_and_sql_text(self):
        malicious_looking_name = "Robert'); DROP TABLE clients;--"
        client = self.store.upsert_client(malicious_looking_name)
        campaign = self.store.create_campaign(
            client["id"], {"description": "'); DROP TABLE campaigns;--"}
        )
        self.assertEqual(self.store.get_client(client["id"])["name"], malicious_looking_name)
        self.assertEqual(
            self.store.get_campaign(campaign["id"])["intake"]["description"],
            "'); DROP TABLE campaigns;--",
        )

    def test_in_memory_store_keeps_state_across_operations(self):
        with CampaignStore(":memory:") as memory_store:
            client = memory_store.upsert_client("Memory Client")
            self.assertEqual(memory_store.get_client(client["id"])["name"], "Memory Client")


if __name__ == "__main__":
    unittest.main()
