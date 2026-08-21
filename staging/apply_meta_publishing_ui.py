"""Wire the approval-bound Meta publishing queue into the transformed Streamlit app.

Prerequisites:
- Senior Design Approval + integrity hotfixes applied.
- Brand Kit / Gemini Creative Studio transforms applied.
- publishing_store.py, publishing_workflow.py, meta_publisher.py are present.

This UI never asks for or stores a raw Meta access token. It stores only a
credential reference (the name of a runtime secret/environment variable).

Run from repository root:
    python staging/apply_meta_publishing_ui.py
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
    if "def render_meta_publishing_panel(" in text and "publishing_store = PublishingStore" in text:
        print("app.py Meta publishing UI already applied")
        return
    if "AI Creative Studio (Gemini)" not in text:
        raise RuntimeError("Creative Studio UI must be applied before Meta publishing UI.")
    if "Publishing Gate: Ready" not in text:
        raise RuntimeError("Senior Design Approval publishing gate must exist first.")

    imports = '''from publishing_store import (
    PublishingConflict,
    PublishingNotFound,
    PublishingStore,
    PublishingStoreError,
)
from publishing_workflow import normalize_scheduled_for
'''
    text = replace_once(
        text,
        "from revision_logic import (\n",
        imports + "from revision_logic import (\n",
        "publishing imports",
    )

    store_anchor = '''def normalized_heading(value):
'''
    store_block = '''publishing_store = None
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
    text = replace_once(text, store_anchor, store_block, "publishing store initialization")

    panel = r'''

def render_meta_publishing_panel(
    store,
    campaign,
    calendar,
    client,
    design_briefs,
    latest_assets,
):
    """Configure client Meta destination and queue only fully approved image posts."""

    st.markdown("### Publishing")
    try:
        gate = publishing_status(latest_assets, len(design_briefs))
    except (TypeError, ValueError) as error:
        st.warning(f"Publishing status is unavailable: {error}")
        return
    if gate != PUBLISHING_STATUS_READY:
        st.info("Publishing Gate: Locked until every latest creative is Senior Design Approved.")
        return

    st.success("Publishing Gate: Ready — content and latest creatives are Senior approved.")
    st.caption(
        "Phase 1 publishes single-image PNG/JPEG posts only. Reel/Video and Carousel "
        "remain blocked until real platform-ready video/slide assets are stored."
    )

    try:
        active_connection = store.get_active_meta_connection(client["id"])
    except (PublishingStoreError, sqlite3.Error, ValueError) as error:
        st.warning(f"Meta connection status could not be loaded: {error}")
        active_connection = None

    with st.expander("Client Meta Connection", expanded=not bool(active_connection)):
        if active_connection:
            st.success(f"Active connection: {active_connection['connection_name']}")
            if active_connection.get("facebook_page_id"):
                st.caption(f"Facebook Page ID: {active_connection['facebook_page_id']}")
            if active_connection.get("instagram_user_id"):
                st.caption(
                    f"Instagram Professional ID: {active_connection['instagram_user_id']}"
                )
            credential_ref = str(active_connection.get("credential_ref") or "")
            secret_present = bool(get_app_setting(credential_ref)) if credential_ref else False
            st.caption(
                f"Credential reference: {credential_ref} — runtime secret "
                + ("found" if secret_present else "not configured yet")
            )

        with st.form(f"meta_connection_{client['id']}"):
            connection_name = st.text_input(
                "Connection Name",
                value=(active_connection or {}).get("connection_name") or f"{client['name']} Meta",
                max_chars=200,
            )
            meta_col1, meta_col2 = st.columns(2)
            with meta_col1:
                facebook_page_id = st.text_input(
                    "Facebook Page ID (optional)",
                    value=(active_connection or {}).get("facebook_page_id") or "",
                    max_chars=200,
                )
            with meta_col2:
                instagram_user_id = st.text_input(
                    "Instagram Professional ID (optional)",
                    value=(active_connection or {}).get("instagram_user_id") or "",
                    max_chars=200,
                )
            credential_ref = st.text_input(
                "Credential Secret Name",
                value=(active_connection or {}).get("credential_ref") or "",
                placeholder="META_TOKEN_CLIENT_ABC",
                max_chars=128,
                help=(
                    "Enter only the name of the environment/Streamlit secret containing the "
                    "Page access token. Never paste the actual Meta token into this form."
                ),
            )
            save_connection = st.form_submit_button(
                "Save / Replace Meta Connection", use_container_width=True
            )
        if save_connection:
            try:
                store.save_meta_connection(
                    client_id=client["id"],
                    connection_name=connection_name,
                    credential_ref=credential_ref,
                    facebook_page_id=facebook_page_id,
                    instagram_user_id=instagram_user_id,
                )
            except (PublishingStoreError, sqlite3.Error, ValueError) as error:
                st.error(f"Meta connection could not be saved: {error}")
            else:
                st.success("Meta connection reference saved. No raw token was stored in SQLite.")
                st.rerun()

    if not active_connection:
        st.info("Save the client Meta destination before queueing publication jobs.")
        return

    try:
        jobs = store.list_jobs(campaign["id"])
    except (PublishingStoreError, sqlite3.Error, ValueError) as error:
        st.warning(f"Publication history could not be loaded: {error}")
        jobs = []
    if jobs:
        st.markdown("#### Publication Queue / History")
        display_jobs = [
            {
                "Post": item["post_number"],
                "Platform": str(item["platform"]).title(),
                "Scheduled (UTC)": item["scheduled_for"],
                "Status": item["status"],
                "Platform Post ID": item.get("platform_post_id") or "",
                "Error": item.get("error_code") or "",
            }
            for item in jobs
        ]
        st.dataframe(display_jobs, use_container_width=True, hide_index=True)

    st.markdown("#### Queue Approved Image Posts")
    st.caption(
        "Instagram requires the approved creative to be on a public HTTPS URL at publish "
        "time. Production will use object storage/CDN; localhost or a laptop path cannot work."
    )

    asset_by_post = {int(item["post_number"]): item for item in latest_assets}
    for brief in design_briefs:
        post_number = int(brief["post_number"])
        asset = asset_by_post.get(post_number)
        if not asset or str(asset.get("latest_decision") or "").lower() != "approved":
            continue
        if str(asset.get("format") or "").strip().casefold() != "image":
            st.info(
                f"Post {post_number} ({asset.get('format')}): publishing waits for its "
                "real platform-ready media pipeline."
            )
            continue
        if str(asset.get("mime_type") or "").strip().lower() not in {"image/png", "image/jpeg"}:
            st.info(f"Post {post_number}: phase-1 requires an approved PNG/JPEG creative.")
            continue

        st.markdown(f"**Post {post_number} — Image**")
        destination_options = []
        if active_connection.get("facebook_page_id"):
            destination_options.append("Facebook")
        if active_connection.get("instagram_user_id"):
            destination_options.append("Instagram")
        selected_platforms = st.multiselect(
            "Destinations",
            destination_options,
            default=destination_options,
            key=f"publish_platforms_{calendar['id']}_{post_number}",
        )
        public_url = st.text_input(
            "Approved Creative Public HTTPS URL",
            key=f"publish_media_url_{calendar['id']}_{post_number}",
            placeholder="https://cdn.example.com/approved-creative.png",
        )
        timing = st.radio(
            "Timing",
            ("Queue now", "Schedule UTC"),
            horizontal=True,
            key=f"publish_timing_{calendar['id']}_{post_number}",
        )
        scheduled_for = None
        if timing == "Schedule UTC":
            date_col, time_col = st.columns(2)
            with date_col:
                scheduled_date = st.date_input(
                    "Publish Date (UTC)",
                    key=f"publish_date_{calendar['id']}_{post_number}",
                )
            with time_col:
                scheduled_time = st.time_input(
                    "Publish Time (UTC)",
                    key=f"publish_time_{calendar['id']}_{post_number}",
                )
            scheduled_for = datetime.combine(
                scheduled_date, scheduled_time, tzinfo=timezone.utc
            )

        if st.button(
            "Queue Approved Post",
            key=f"queue_publish_{calendar['id']}_{post_number}",
            use_container_width=True,
        ):
            if not selected_platforms:
                st.error("Select at least one destination.")
                continue
            queued = []
            try:
                for platform_label in selected_platforms:
                    job = store.queue_image_publication(
                        campaign_id=campaign["id"],
                        calendar_version_id=calendar["id"],
                        creative_asset_id=asset["id"],
                        connection_id=active_connection["id"],
                        platform=platform_label.lower(),
                        public_media_url=public_url,
                        scheduled_for=scheduled_for,
                    )
                    queued.append(
                        f"{platform_label}: {job['status']} ({job['scheduled_for']})"
                    )
            except (PublishingStoreError, sqlite3.Error, ValueError) as error:
                st.error(f"Post {post_number} could not be queued: {error}")
            else:
                st.success("Queued — " + "; ".join(queued))
                st.rerun()

    st.caption(
        "Queued jobs are dispatched by publishing_worker.py on an always-on server. "
        "A publish timeout becomes outcome_unknown and is never blindly retried."
    )

'''
    text = replace_once(
        text,
        "\ndef render_design_approval_dashboard(calendar, design_briefs, latest_assets):\n",
        panel + "\ndef render_design_approval_dashboard(calendar, design_briefs, latest_assets):\n",
        "publishing panel function",
    )

    call_anchor = '''                    if campaign_store is not None and campaign_record is not None:
                        render_creative_asset_controls(
                            campaign_store,
                            campaign_record,
                            latest_calendar,
                            client_record,
                            record,
                        )
        elif campaign_store is not None and latest_calendar is not None:
'''
    call_new = '''                    if campaign_store is not None and campaign_record is not None:
                        render_creative_asset_controls(
                            campaign_store,
                            campaign_record,
                            latest_calendar,
                            client_record,
                            record,
                        )
            if (
                publishing_store is not None
                and campaign_record is not None
                and client_record is not None
            ):
                render_meta_publishing_panel(
                    publishing_store,
                    campaign_record,
                    latest_calendar,
                    client_record,
                    design_briefs,
                    latest_creative_assets,
                )
        elif campaign_store is not None and latest_calendar is not None:
'''
    text = replace_once(text, call_anchor, call_new, "publishing panel dashboard call")

    APP_PATH.write_text(text, encoding="utf-8")
    print("Meta publishing UI transformation complete.")


if __name__ == "__main__":
    main()
