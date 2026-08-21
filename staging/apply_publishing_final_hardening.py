"""Finish deployable image publishing on the already-transformed local app.

Prerequisites:
- Brand Kit + Gemini Creative Studio applied.
- Senior Design Approval/integrity fixes applied.
- Meta publishing UI applied.

Adds:
- pre-approval JPEG normalization for Instagram Image creatives,
- Supabase Storage upload/public URL automation,
- real Publish Now dispatch from the Streamlit app,
- optional single-process scheduled worker for Railway/POC deployment.

Run from repository root:
    python staging/apply_publishing_final_hardening.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text and old not in text:
        print(f"{label}: already applied")
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    print(f"{label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if (
        "prepare_image_for_approved_platforms" in text
        and "upload_public_creative" in text
        and "start_background_publishing_worker" in text
        and '"Publish now", "Schedule UTC"' in text
    ):
        print("app.py publishing final hardening already applied")
        return
    if "def render_meta_publishing_panel(" not in text:
        raise RuntimeError("Apply staging/apply_meta_publishing_ui.py first.")
    if "AI Creative Studio (Gemini)" not in text:
        raise RuntimeError("Creative Studio must be applied before publishing hardening.")

    imports = '''from meta_publisher import DEFAULT_META_GRAPH_API_VERSION
from publishing_media import prepare_image_for_approved_platforms
from publishing_runtime import (
    configured_auto_worker_enabled,
    start_background_publishing_worker,
)
from publishing_worker import run_due_jobs
from supabase_media import SupabaseMediaError, upload_public_creative
'''
    text = replace_once(
        text,
        "from revision_logic import (\n",
        imports + "from revision_logic import (\n",
        "publishing hardening imports",
    )

    # Gemini returns PNG. Convert Instagram-bound Image drafts before saving so
    # Senior reviews the exact JPEG bytes that Meta will later publish.
    generated_anchor = '''                        else:
                            st.session_state[draft_key] = {
                                "image_bytes": generated.image_bytes,
                                "mime_type": generated.mime_type,
                                "prompt": editable_prompt,
                                "model": generated.model,
                                "request_id": generated.request_id,
                                "aspect_ratio": generated.aspect_ratio,
                                "image_size": generated.image_size,
                            }
                            st.rerun()
'''
    generated_new = '''                        else:
                            try:
                                prepared_generated = prepare_image_for_approved_platforms(
                                    file_bytes=generated.image_bytes,
                                    mime_type=generated.mime_type,
                                    file_name=f"gemini_post_{post_number}_creative.png",
                                    approved_platform_text=approved_post.get("content", {}).get("Platform", ""),
                                    format_name=brief_record.get("format", ""),
                                )
                            except (TypeError, ValueError) as error:
                                st.error(f"Generated creative is not publishing-ready: {error}")
                            else:
                                st.session_state[draft_key] = {
                                    "image_bytes": prepared_generated.file_bytes,
                                    "mime_type": prepared_generated.mime_type,
                                    "prompt": editable_prompt,
                                    "model": generated.model,
                                    "request_id": generated.request_id,
                                    "aspect_ratio": generated.aspect_ratio,
                                    "image_size": generated.image_size,
                                    "media_note": prepared_generated.note,
                                }
                                st.rerun()
'''
    text = replace_once(
        text,
        generated_anchor,
        generated_new,
        "Gemini Instagram JPEG normalization",
    )

    preview_anchor = '''                st.caption(
                    f"{draft['model']} | {draft['aspect_ratio']} | {draft['image_size']} | "
                    f"Request ID: {draft['request_id']}"
                )
'''
    preview_new = preview_anchor + '''                if draft.get("media_note"):
                    st.caption(draft["media_note"])
'''
    text = replace_once(text, preview_anchor, preview_new, "creative conversion preview note")

    # Manual Image uploads targeting Instagram are normalized before the asset
    # hash/version is created, never after approval.
    manual_anchor = '''            metadata = validate_creative_upload(uploaded.name, uploaded.type, raw)
'''
    manual_new = '''            prepared_upload = prepare_image_for_approved_platforms(
                file_bytes=raw,
                mime_type=uploaded.type,
                file_name=uploaded.name,
                approved_platform_text=approved_post.get("content", {}).get("Platform", ""),
                format_name=brief_record.get("format", ""),
            )
            raw = prepared_upload.file_bytes
            metadata = validate_creative_upload(
                prepared_upload.file_name, prepared_upload.mime_type, raw
            )
            if prepared_upload.converted:
                st.info(prepared_upload.note)
'''
    text = replace_once(text, manual_anchor, manual_new, "manual Instagram JPEG normalization")

    # Start the optional single-instance POC scheduler only when explicitly
    # enabled. Railway injects secrets as environment variables, which the
    # background worker resolves without touching Streamlit session state.
    worker_anchor = '''publishing_store = None
if campaign_store is not None:
    try:
        publishing_store = PublishingStore(
            get_app_setting("CAMPAIGN_DB_PATH", str(DEFAULT_CAMPAIGN_DB_PATH))
        )
    except (PublishingStoreError, sqlite3.Error, OSError, ValueError) as error:
        st.warning(
            "Publishing queue is unavailable, but content/design work can continue. "
            f"Details: {error}"
        )


def normalized_heading(value):
'''
    worker_new = '''publishing_store = None
if campaign_store is not None:
    try:
        publishing_store = PublishingStore(
            get_app_setting("CAMPAIGN_DB_PATH", str(DEFAULT_CAMPAIGN_DB_PATH))
        )
    except (PublishingStoreError, sqlite3.Error, OSError, ValueError) as error:
        st.warning(
            "Publishing queue is unavailable, but content/design work can continue. "
            f"Details: {error}"
        )

if publishing_store is not None:
    try:
        auto_worker_enabled = configured_auto_worker_enabled(
            get_app_setting("AUTO_PUBLISH_WORKER", "false")
        )
        if auto_worker_enabled:
            start_background_publishing_worker(
                get_app_setting("CAMPAIGN_DB_PATH", str(DEFAULT_CAMPAIGN_DB_PATH)),
                interval_seconds=get_app_setting("PUBLISHING_WORKER_INTERVAL_SECONDS", "60"),
                api_version=get_app_setting(
                    "META_GRAPH_API_VERSION", DEFAULT_META_GRAPH_API_VERSION
                ),
            )
    except (TypeError, ValueError, RuntimeError) as error:
        st.warning(f"Automatic publishing worker is disabled: {error}")


def normalized_heading(value):
'''
    text = replace_once(text, worker_anchor, worker_new, "optional background publishing worker")

    url_anchor = '''        public_url = st.text_input(
            "Approved Creative Public HTTPS URL",
            key=f"publish_media_url_{calendar['id']}_{post_number}",
            placeholder="https://cdn.example.com/approved-creative.png",
        )
'''
    url_new = '''        supabase_url = get_app_setting("SUPABASE_URL")
        supabase_service_key = get_app_setting("SUPABASE_SERVICE_ROLE_KEY")
        supabase_bucket = get_app_setting("SUPABASE_MEDIA_BUCKET", "publishing-media")
        supabase_ready = bool(supabase_url and supabase_service_key and supabase_bucket)
        if supabase_ready:
            st.caption(
                "Media storage: Supabase configured. The exact Senior-approved creative "
                "will be uploaded automatically when you publish/queue."
            )
            public_url = ""
        else:
            st.warning(
                "Supabase media storage is not configured. For local testing you can "
                "provide another public HTTPS URL manually."
            )
            public_url = st.text_input(
                "Approved Creative Public HTTPS URL",
                key=f"publish_media_url_{calendar['id']}_{post_number}",
                placeholder="https://cdn.example.com/approved-creative.jpg",
            )
'''
    text = replace_once(text, url_anchor, url_new, "Supabase automatic media URL")

    text = replace_once(
        text,
        '            ("Queue now", "Schedule UTC"),\n',
        '            ("Publish now", "Schedule UTC"),\n',
        "Publish Now timing option",
    )

    queue_anchor = '''            if not selected_platforms:
                st.error("Select at least one destination.")
                continue
            queued = []
            try:
                for platform_label in selected_platforms:
'''
    queue_new = '''            if not selected_platforms:
                st.error("Select at least one destination.")
                continue

            effective_public_url = str(public_url or "").strip()
            if supabase_ready:
                creative_ok, _creative_path, creative_raw, creative_error = verify_creative_file_integrity(asset)
                if not creative_ok:
                    st.error(
                        "The exact Senior-approved creative file is unavailable or changed, "
                        f"so publishing is blocked. Details: {creative_error}"
                    )
                    continue
                try:
                    media_result = upload_public_creative(
                        project_url=supabase_url,
                        service_role_key=supabase_service_key,
                        bucket=supabase_bucket,
                        campaign_id=campaign["id"],
                        post_number=post_number,
                        creative_asset_id=asset["id"],
                        creative_hash=asset["file_sha256"],
                        file_bytes=creative_raw,
                        mime_type=asset["mime_type"],
                    )
                except (SupabaseMediaError, TypeError, ValueError) as error:
                    st.error(f"Approved creative could not be prepared for Meta: {error}")
                    continue
                effective_public_url = media_result.public_url

            if not effective_public_url:
                st.error("A public HTTPS creative URL is required before publishing.")
                continue

            queued = []
            try:
                for platform_label in selected_platforms:
'''
    text = replace_once(text, queue_anchor, queue_new, "upload approved creative before queue")

    text = replace_once(
        text,
        "                        public_media_url=public_url,\n",
        "                        public_media_url=effective_public_url,\n",
        "queue generated public media URL",
    )

    success_anchor = '''            else:
                st.success("Queued — " + "; ".join(queued))
                st.rerun()
'''
    success_new = '''            else:
                if timing == "Publish now":
                    try:
                        publish_summary = run_due_jobs(
                            get_app_setting("CAMPAIGN_DB_PATH", str(DEFAULT_CAMPAIGN_DB_PATH)),
                            limit=20,
                            token_resolver=get_app_setting,
                            api_version=get_app_setting(
                                "META_GRAPH_API_VERSION", DEFAULT_META_GRAPH_API_VERSION
                            ),
                        )
                    except (PublishingStoreError, sqlite3.Error, OSError, TypeError, ValueError) as error:
                        st.error(f"Queued successfully, but immediate dispatch failed: {error}")
                    else:
                        st.success(
                            "Publish run complete — "
                            f"published={publish_summary['published']}, "
                            f"failed={publish_summary['failed']}, "
                            f"outcome_unknown={publish_summary['outcome_unknown']}."
                        )
                else:
                    st.success("Scheduled — " + "; ".join(queued))
                st.rerun()
'''
    text = replace_once(text, success_anchor, success_new, "immediate Meta dispatch")

    caption_anchor = '''    st.caption(
        "Queued jobs are dispatched by publishing_worker.py on an always-on server. "
        "A publish timeout becomes outcome_unknown and is never blindly retried."
    )
'''
    caption_new = '''    st.caption(
        "Publish now is dispatched immediately by this app. Scheduled jobs are processed "
        "by publishing_worker.py or by AUTO_PUBLISH_WORKER=true on a single-instance POC "
        "deployment. A publish timeout becomes outcome_unknown and is never blindly retried."
    )
'''
    text = replace_once(text, caption_anchor, caption_new, "publishing worker guidance")

    APP_PATH.write_text(text, encoding="utf-8")
    print("Publishing final hardening complete.")


if __name__ == "__main__":
    main()
