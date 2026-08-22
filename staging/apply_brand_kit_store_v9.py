"""Add versioned client Brand Kits to the locally transformed CampaignStore.

Run from repository root after Senior Design Approval schema v8 is present:
    python staging/apply_brand_kit_store_v9.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "campaign_store.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}.")
    return text.replace(old, new, 1)


def main() -> None:
    text = STORE_PATH.read_text(encoding="utf-8")
    if "def save_brand_kit(" in text and "_ensure_v9_brand_kit_schema" in text:
        print("campaign_store.py Brand Kit schema already applied")
        return
    if "SCHEMA_VERSION = 8" not in text or "_ensure_v8_creative_review_schema" not in text:
        raise RuntimeError(
            "campaign_store.py must already contain the Senior Design Approval v8 schema."
        )

    text = replace_once(text, "SCHEMA_VERSION = 8", "SCHEMA_VERSION = 9", "schema version")

    methods = r'''
    def save_brand_kit(self, client_id: str, brand_kit: Mapping[str, Any]) -> dict[str, Any]:
        """Append one immutable Brand Kit version for a client.

        Saving an unchanged normalized kit is idempotent and returns the current
        latest version instead of creating duplicate history.
        """
        from brand_kit import normalize_brand_kit

        clean_client_id = _canonical_uuid(client_id, "client_id")
        normalized = normalize_brand_kit(brand_kit)
        serialized = _serialize_json(normalized, "brand_kit")
        now = _utc_now()
        record_id = str(uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            client = connection.execute(
                "SELECT id FROM clients WHERE id=?", (clean_client_id,)
            ).fetchone()
            if client is None:
                raise RecordNotFound(f"Client {clean_client_id} was not found.")
            latest = connection.execute(
                "SELECT * FROM client_brand_kits WHERE client_id=? "
                "ORDER BY version DESC LIMIT 1",
                (clean_client_id,),
            ).fetchone()
            if latest is not None and latest["kit_json"] == serialized:
                connection.commit()
                return {
                    "id": latest["id"],
                    "client_id": latest["client_id"],
                    "version": latest["version"],
                    "kit": _deserialize_json(latest["kit_json"]),
                    "created_at": latest["created_at"],
                }
            next_version = 1 if latest is None else int(latest["version"]) + 1
            connection.execute(
                "INSERT INTO client_brand_kits (id,client_id,version,kit_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (record_id, clean_client_id, next_version, serialized, now),
            )
            row = connection.execute(
                "SELECT * FROM client_brand_kits WHERE id=?", (record_id,)
            ).fetchone()
            connection.commit()
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "version": row["version"],
            "kit": _deserialize_json(row["kit_json"]),
            "created_at": row["created_at"],
        }

    def get_latest_brand_kit(self, client_id: str) -> dict[str, Any] | None:
        clean_client_id = _canonical_uuid(client_id, "client_id")
        with self._connection() as connection:
            client = connection.execute(
                "SELECT id FROM clients WHERE id=?", (clean_client_id,)
            ).fetchone()
            if client is None:
                raise RecordNotFound(f"Client {clean_client_id} was not found.")
            row = connection.execute(
                "SELECT * FROM client_brand_kits WHERE client_id=? "
                "ORDER BY version DESC LIMIT 1",
                (clean_client_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "version": row["version"],
            "kit": _deserialize_json(row["kit_json"]),
            "created_at": row["created_at"],
        }

    def list_brand_kits(self, client_id: str) -> list[dict[str, Any]]:
        clean_client_id = _canonical_uuid(client_id, "client_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM client_brand_kits WHERE client_id=? "
                "ORDER BY version DESC",
                (clean_client_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "client_id": row["client_id"],
                "version": row["version"],
                "kit": _deserialize_json(row["kit_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

'''
    text = replace_once(
        text,
        "    # Manual client-share audit APIs appear before the automated review APIs.\n",
        methods + "    # Manual client-share audit APIs appear before the automated review APIs.\n",
        "Brand Kit store methods",
    )

    text = replace_once(
        text,
        "            self._ensure_v8_creative_review_schema(connection)\n            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "            self._ensure_v8_creative_review_schema(connection)\n"
        "            self._ensure_v9_brand_kit_schema(connection)\n"
        "            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "v9 schema call",
    )

    schema = r'''
    @staticmethod
    def _ensure_v9_brand_kit_schema(connection: sqlite3.Connection) -> None:
        """Install immutable, versioned client Brand Kits."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS client_brand_kits (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                kit_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (client_id, version),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS client_brand_kits_latest_idx "
            "ON client_brand_kits(client_id,version DESC)"
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS client_brand_kits_no_update
            BEFORE UPDATE ON client_brand_kits
            BEGIN
                SELECT RAISE(ABORT, 'Brand Kit versions are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS client_brand_kits_no_delete
            BEFORE DELETE ON client_brand_kits
            BEGIN
                SELECT RAISE(ABORT, 'Brand Kit versions are append-only');
            END
            """
        )

'''
    text = replace_once(
        text,
        "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        schema
        + "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        "v9 Brand Kit schema",
    )

    STORE_PATH.write_text(text, encoding="utf-8")
    print("added CampaignStore schema v9 Brand Kit persistence")


if __name__ == "__main__":
    main()
