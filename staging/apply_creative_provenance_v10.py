"""Add auditable creative-provider provenance to CampaignStore and Creative Studio.

Prerequisites:
- CampaignStore Brand Kit schema v9 source patch applied.
- Creative Studio UI source patch applied.

Run from repository root:
    python staging/apply_creative_provenance_v10.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "campaign_store.py"
APP_PATH = ROOT / "app.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}.")
    return text.replace(old, new, 1)


def patch_store() -> None:
    text = STORE_PATH.read_text(encoding="utf-8")
    if "_ensure_v10_creative_provenance_schema" in text and '"source_provider": row["source_provider"]' in text:
        print("campaign_store.py creative provenance already applied")
        return
    if "SCHEMA_VERSION = 9" not in text or "def save_brand_kit(" not in text:
        raise RuntimeError("CampaignStore Brand Kit schema v9 must be applied first.")

    text = replace_once(text, "SCHEMA_VERSION = 9", "SCHEMA_VERSION = 10", "schema version")

    signature_anchor = '''        source_type: str = "manual_upload",
        design_prompt: str = "",
    ) -> dict[str, Any]:
'''
    signature_new = '''        source_type: str = "manual_upload",
        design_prompt: str = "",
        source_provider: str = "",
        source_model: str = "",
        source_request_id: str = "",
        source_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
'''
    text = replace_once(text, signature_anchor, signature_new, "creative provenance signature")

    validation_anchor = '''        clean_prompt = _bounded_optional_text(
            design_prompt, "design_prompt", max_length=12_000
        )
        now = _utc_now()
'''
    validation_new = '''        clean_prompt = _bounded_optional_text(
            design_prompt, "design_prompt", max_length=12_000
        )
        clean_provider = _bounded_optional_text(
            source_provider, "source_provider", max_length=120
        ).lower()
        clean_model = _bounded_optional_text(
            source_model, "source_model", max_length=300
        )
        clean_request_id = _bounded_optional_text(
            source_request_id, "source_request_id", max_length=300
        )
        source_metadata_value = _require_mapping(source_metadata, "source_metadata")
        if clean_source == "manual_upload" and not clean_provider:
            clean_provider = "manual"
        elif clean_source == "ai_generated" and not clean_provider:
            clean_provider = "ai"
        now = _utc_now()
'''
    text = replace_once(text, validation_anchor, validation_new, "creative provenance validation")

    insert_anchor = '''                    mime_type,storage_path,file_sha256,file_size,design_prompt,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
'''
    insert_new = '''                    mime_type,storage_path,file_sha256,file_size,design_prompt,
                    source_provider,source_model,source_request_id,source_metadata_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
'''
    text = replace_once(text, insert_anchor, insert_new, "creative provenance insert columns")

    values_anchor = '''                    clean_mime, clean_storage, clean_file_hash, file_size, clean_prompt, now,
                ),
'''
    values_new = '''                    clean_mime, clean_storage, clean_file_hash, file_size, clean_prompt,
                    clean_provider, clean_model, clean_request_id,
                    _serialize_json(source_metadata_value, "source_metadata"), now,
                ),
'''
    text = replace_once(text, values_anchor, values_new, "creative provenance insert values")

    event_anchor = '''                    "source_type": clean_source,
                    "file_sha256": clean_file_hash,
'''
    event_new = '''                    "source_type": clean_source,
                    "source_provider": clean_provider,
                    "source_model": clean_model,
                    "source_request_id": clean_request_id,
                    "file_sha256": clean_file_hash,
'''
    text = replace_once(text, event_anchor, event_new, "creative provenance event")

    text = replace_once(
        text,
        "            self._ensure_v9_brand_kit_schema(connection)\n            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "            self._ensure_v9_brand_kit_schema(connection)\n"
        "            self._ensure_v10_creative_provenance_schema(connection)\n"
        "            connection.execute(f\"PRAGMA user_version = {SCHEMA_VERSION}\")",
        "v10 schema call",
    )

    schema = r'''
    @staticmethod
    def _ensure_v10_creative_provenance_schema(connection: sqlite3.Connection) -> None:
        """Add provider/model/request metadata to immutable creative versions."""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(creative_assets)").fetchall()
        }
        additions = (
            ("source_provider", "TEXT NOT NULL DEFAULT ''"),
            ("source_model", "TEXT NOT NULL DEFAULT ''"),
            ("source_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        )
        for column, definition in additions:
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE creative_assets ADD COLUMN {column} {definition}"
                )

'''
    text = replace_once(
        text,
        "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        schema
        + "    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n",
        "v10 creative provenance schema",
    )

    converter_anchor = '''        "design_prompt": row["design_prompt"],
        "created_at": row["created_at"],
'''
    converter_new = '''        "design_prompt": row["design_prompt"],
        "source_provider": row["source_provider"],
        "source_model": row["source_model"],
        "source_request_id": row["source_request_id"],
        "source_metadata": _deserialize_json(row["source_metadata_json"]),
        "created_at": row["created_at"],
'''
    text = replace_once(text, converter_anchor, converter_new, "creative provenance converter")

    STORE_PATH.write_text(text, encoding="utf-8")
    print("added CampaignStore schema v10 creative provenance")


def patch_app() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if 'source_provider="gemini"' in text and "Creative source:" in text:
        print("app.py creative provenance already applied")
        return
    if "AI Creative Studio (Gemini)" not in text:
        raise RuntimeError("Creative Studio UI must be applied first.")

    save_anchor = '''                            source_type="ai_generated",
                            design_prompt=draft["prompt"],
                        )
'''
    save_new = '''                            source_type="ai_generated",
                            design_prompt=draft["prompt"],
                            source_provider="gemini",
                            source_model=draft["model"],
                            source_request_id=draft["request_id"],
                            source_metadata={
                                "aspect_ratio": draft["aspect_ratio"],
                                "image_size": draft["image_size"],
                            },
                        )
'''
    text = replace_once(text, save_anchor, save_new, "Gemini creative provenance save")

    display_anchor = '''        st.markdown(f"**Creative Status:** {creative_status(latest_asset)}")
        creative_file_ok = render_creative_file(
'''
    display_new = '''        st.markdown(f"**Creative Status:** {creative_status(latest_asset)}")
        source_provider = str(latest_asset.get("source_provider") or "").strip()
        source_model = str(latest_asset.get("source_model") or "").strip()
        if latest_asset.get("source_type") == "ai_generated":
            source_text = source_provider.title() if source_provider else "AI"
            if source_model:
                source_text += f" ({source_model})"
            st.caption(f"Creative source: {source_text}")
        elif source_provider:
            st.caption(f"Creative source: {source_provider.title()}")
        creative_file_ok = render_creative_file(
'''
    text = replace_once(text, display_anchor, display_new, "creative provenance display")

    APP_PATH.write_text(text, encoding="utf-8")
    print("added Creative Studio provider provenance display/save")


def main() -> None:
    patch_store()
    patch_app()
    print("Creative provenance v10 transformation complete.")


if __name__ == "__main__":
    main()
