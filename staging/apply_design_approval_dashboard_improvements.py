"""Improve the already-applied Senior Design Approval feature.

Run once from the repository root after apply_senior_design_approval_v2.py:
    python staging/apply_design_approval_dashboard_improvements.py

This patch makes Senior design decisions obvious on the marketing dashboard and
hardens creative version/review integrity. Exact anchors are used so the script
stops instead of guessing when the local source is unexpected.
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


def patch_campaign_store() -> None:
    text = STORE_PATH.read_text(encoding="utf-8")
    if "design_approver_name" in text and "This exact creative file is already the latest version" in text:
        print("campaign_store.py improvements already applied")
        return
    if "def save_creative_asset(" not in text or "def decide_design_review_link(" not in text:
        raise RuntimeError(
            "campaign_store.py does not contain the Senior Design Approval feature. "
            "Run staging/apply_senior_design_approval_v2.py first."
        )

    design_brief_anchor = '''            if design_brief is None:
                raise InvalidStatusTransition(
                    "Generate the approved post's Design Brief before uploading a creative."
                )
            next_version = connection.execute(
'''
    design_brief_new = '''            if design_brief is None:
                raise InvalidStatusTransition(
                    "Generate the approved post's Design Brief before uploading a creative."
                )
            latest_existing = connection.execute(
                "SELECT * FROM creative_assets WHERE campaign_id=? AND calendar_version_id=? "
                "AND post_number=? ORDER BY asset_version DESC LIMIT 1",
                (clean_campaign_id, clean_calendar_id, post_number),
            ).fetchone()
            if latest_existing is not None and latest_existing["file_sha256"] == clean_file_hash:
                raise StoreConflict(
                    "This exact creative file is already the latest version for this post."
                )
            # A replacement creative invalidates every still-pending link for the prior version.
            connection.execute(
                "UPDATE design_review_links SET status='revoked',revoked_at=? "
                "WHERE campaign_id=? AND calendar_version_id=? AND post_number=? "
                "AND status='pending'",
                (now, clean_campaign_id, clean_calendar_id, post_number),
            )
            next_version = connection.execute(
'''
    text = replace_once(
        text, design_brief_anchor, design_brief_new, "duplicate creative and stale link protection"
    )

    active_link_anchor = '''                active_link = connection.execute(
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
'''
    active_link_new = '''                active_link = connection.execute(
                    "SELECT expires_at FROM design_review_links WHERE creative_asset_id=? "
                    "AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                    (asset_row["id"], _utc_now()),
                ).fetchone()
                item = _creative_asset_from_row(asset_row)
                item["latest_decision"] = approval["decision"] if approval else None
                item["design_feedback"] = approval["feedback"] if approval else ""
                item["design_change_fields"] = (
                    _deserialize_json(approval["change_fields_json"]) if approval else []
                )
                item["design_approver_name"] = approval["approver_name"] if approval else ""
                item["design_approver_email"] = approval["approver_email"] if approval else ""
                item["design_decided_at"] = approval["decided_at"] if approval else ""
                item["active_review_link"] = active_link is not None
                item["active_review_expires_at"] = active_link["expires_at"] if active_link else ""
'''
    text = replace_once(
        text, active_link_anchor, active_link_new, "surface design decision metadata"
    )

    fields_anchor = '''        allowed_fields = (
            "Layout", "Image / Visual", "Colors", "Typography", "Text Placement",
            "Logo / Branding", "CTA Placement", "Carousel Slides", "Thumbnail", "Other",
        )
'''
    fields_new = '''        allowed_fields = (
            "Layout", "Image / Visual", "Colors", "Typography", "Text Placement",
            "Logo / Branding", "CTA Placement", "Carousel Slides", "Reel Scenes / B-roll",
            "Thumbnail", "Other",
        )
'''
    text = replace_once(text, fields_anchor, fields_new, "reel design change field")

    decision_integrity_anchor = '''            if latest_asset is None or latest_asset["id"] != asset["id"]:
                raise StoreConflict("A newer creative version has replaced this review link.")
            if link["asset_hash"] != asset["file_sha256"]:
                raise StoreConflict("The design review link does not match the creative file.")
'''
    decision_integrity_new = '''            if latest_asset is None or latest_asset["id"] != asset["id"]:
                raise StoreConflict("A newer creative version has replaced this review link.")
            latest_calendar = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? ORDER BY version DESC LIMIT 1",
                (link["campaign_id"],),
            ).fetchone()
            if latest_calendar is None or latest_calendar["id"] != link["calendar_version_id"]:
                raise StoreConflict("This design review link is for an older content version.")
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if calculated_hash != calendar["content_hash"] or asset["content_hash"] != calculated_hash:
                raise StoreConflict("The creative no longer matches the approved content version.")
            design_brief = connection.execute(
                "SELECT content_hash FROM design_briefs WHERE id=?", (asset["design_brief_id"],)
            ).fetchone()
            if design_brief is None or design_brief["content_hash"] != calculated_hash:
                raise StoreConflict("The creative no longer matches its approved Design Brief.")
            if link["asset_hash"] != asset["file_sha256"]:
                raise StoreConflict("The design review link does not match the creative file.")
'''
    text = replace_once(
        text, decision_integrity_anchor, decision_integrity_new, "design decision source integrity"
    )

    STORE_PATH.write_text(text, encoding="utf-8")
    print("improved campaign_store.py")


def patch_app() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "def render_design_approval_dashboard(" in text and "Refresh Senior Design Status" in text:
        print("app.py improvements already applied")
        return
    if "def render_design_review_portal(" not in text or "def render_creative_asset_controls(" not in text:
        raise RuntimeError(
            "app.py does not contain the Senior Design Approval feature. "
            "Run staging/apply_senior_design_approval_v2.py first."
        )

    import_anchor = '''    PUBLISHING_STATUS_READY,
    build_ai_design_prompt,
    content_post_by_number,
'''
    import_new = '''    PUBLISHING_STATUS_READY,
    build_ai_design_prompt,
    build_design_review_dashboard_rows,
    content_post_by_number,
'''
    text = replace_once(text, import_anchor, import_new, "design dashboard helper import")

    dashboard_function = r'''

def render_design_approval_dashboard(calendar, design_briefs, latest_assets):
    """Make every Senior design decision and required action visible at a glance."""
    if not design_briefs:
        return
    try:
        rows = build_design_review_dashboard_rows(design_briefs, latest_assets)
        gate = publishing_status(latest_assets, len(design_briefs))
    except (TypeError, ValueError) as error:
        st.warning(f"Design approval summary could not be prepared: {error}")
        return

    st.markdown("### Design Approval Status")
    st.caption(
        "Senior design decisions happen in a separate secure tab. Click refresh after the reviewer acts; "
        "the latest approval or change request will be loaded from the database."
    )
    if st.button(
        "Refresh Senior Design Status",
        key=f"refresh_design_status_{calendar['id']}",
        use_container_width=True,
    ):
        st.rerun()

    counts = {
        "approved": sum(1 for row in rows if row["status"] == "Design Approved"),
        "changes": sum(1 for row in rows if row["status"] == "Design Changes Requested"),
        "pending": sum(1 for row in rows if row["status"] == "Pending Senior Design Review"),
        "remaining": sum(
            1 for row in rows
            if row["status"] in {"Design Brief Ready", "Creative Uploaded"}
        ),
    }
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Approved", counts["approved"])
    col2.metric("Changes Requested", counts["changes"])
    col3.metric("Pending Review", counts["pending"])
    col4.metric("Not Sent / Missing", counts["remaining"])

    if counts["changes"]:
        st.error(
            f"Action required: Senior requested design changes on {counts['changes']} post(s). "
            "Review the feedback below, then upload a replacement creative version."
        )

    for row in rows:
        post_label = f"Post {row['post_number']} — {row['format'] or 'Creative'}"
        version_label = (
            f"Creative v{row['asset_version']}" if row["asset_version"] is not None
            else "No creative uploaded"
        )
        if row["status"] == "Design Approved":
            st.success(f"✅ {post_label} — Design Approved — {version_label}")
            reviewer = row.get("approver_name") or "Senior Reviewer"
            decided_at = row.get("decided_at") or ""
            detail = f"Approved by {reviewer}"
            if decided_at:
                detail += f" | {decided_at}"
            st.caption(detail)
        elif row["status"] == "Design Changes Requested":
            st.error(f"🔴 {post_label} — Design Changes Requested — {version_label}")
            if row.get("change_fields"):
                st.markdown("**Change Areas:** " + ", ".join(row["change_fields"]))
            if row.get("feedback"):
                st.info(f"Senior Feedback: {row['feedback']}")
            reviewer = row.get("approver_name") or "Senior Reviewer"
            decided_at = row.get("decided_at") or ""
            detail = f"Requested by {reviewer}"
            if decided_at:
                detail += f" | {decided_at}"
            st.caption(detail)
            st.caption(
                f"Open Post {row['post_number']} below → Creative Production → "
                "Upload Replacement Creative (new version)."
            )
        elif row["status"] == "Pending Senior Design Review":
            st.warning(f"🟡 {post_label} — Pending Senior Design Review — {version_label}")
            if row.get("active_review_expires_at"):
                st.caption(f"Active review link expires: {row['active_review_expires_at']}")
        elif row["status"] == "Creative Uploaded":
            st.info(f"🔵 {post_label} — Creative Uploaded, Review Link Not Generated — {version_label}")
            st.caption(
                f"Open Post {row['post_number']} below and generate the Senior Design Review Link."
            )
        else:
            st.caption(f"⚪ {post_label} — Design Brief Ready — upload the creative next.")

    if gate == PUBLISHING_STATUS_READY:
        st.success("Publishing Gate: READY — every latest creative is Senior Design Approved.")
    else:
        st.warning("Publishing Gate: LOCKED — every post needs an approved latest creative.")

'''
    text = replace_once(
        text,
        "\ndef render_creative_asset_controls(store, campaign, calendar, client, brief_record):\n",
        dashboard_function
        + "\ndef render_creative_asset_controls(store, campaign, calendar, client, brief_record):\n",
        "design approval dashboard function",
    )

    status_anchor = '''    if client_record is not None:
        st.caption(f"Client: {client_record['name']}")
'''
    status_new = '''    if latest_calendar is not None and design_briefs:
        render_design_approval_dashboard(
            latest_calendar, design_briefs, latest_creative_assets
        )

    if client_record is not None:
        st.caption(f"Client: {client_record['name']}")
'''
    text = replace_once(text, status_anchor, status_new, "dashboard placement")

    approval_anchor = '''        elif latest_asset.get("latest_decision") == "approved":
            st.success("This creative version is Senior Design Approved.")

        if latest_asset.get("latest_decision") != "approved":
            if st.button(
                "Create / Replace Senior Design Review Link",
                key=f"create_design_review_{latest_asset['id']}",
                use_container_width=True,
            ):
'''
    approval_new = '''        elif latest_asset.get("latest_decision") == "approved":
            st.success("This creative version is Senior Design Approved.")
            reviewer = latest_asset.get("design_approver_name") or "Senior Reviewer"
            decided_at = latest_asset.get("design_decided_at") or ""
            approval_text = f"Approved by {reviewer}"
            if decided_at:
                approval_text += f" | {decided_at}"
            st.caption(approval_text)

        if latest_asset.get("active_review_link") and not st.session_state.get(
            f"design_review_url_{latest_asset['id']}"
        ):
            st.warning(
                "A Senior Design Review link is active. For security the raw URL is not stored. "
                "If you no longer have the URL, generate a replacement link below."
            )

        if latest_asset.get("latest_decision") != "approved":
            review_button_label = (
                "Replace Senior Design Review Link"
                if latest_asset.get("active_review_link")
                else "Generate Senior Design Review Link"
            )
            if st.button(
                review_button_label,
                key=f"create_design_review_{latest_asset['id']}",
                use_container_width=True,
            ):
'''
    text = replace_once(text, approval_anchor, approval_new, "clear design review link action")

    rejected_anchor = '''        if latest_asset.get("latest_decision") == "rejected":
            fields = ", ".join(latest_asset.get("design_change_fields") or [])
            if fields:
                st.markdown(f"**Senior requested changes in:** {fields}")
            if latest_asset.get("design_feedback"):
                st.info(latest_asset["design_feedback"])
'''
    rejected_new = '''        if latest_asset.get("latest_decision") == "rejected":
            st.error("Senior Design Changes Requested — upload a revised creative as a new version.")
            fields = ", ".join(latest_asset.get("design_change_fields") or [])
            if fields:
                st.markdown(f"**Senior requested changes in:** {fields}")
            if latest_asset.get("design_feedback"):
                st.info(f"Senior Feedback: {latest_asset['design_feedback']}")
            reviewer = latest_asset.get("design_approver_name") or "Senior Reviewer"
            decided_at = latest_asset.get("design_decided_at") or ""
            rejection_text = f"Requested by {reviewer}"
            if decided_at:
                rejection_text += f" | {decided_at}"
            st.caption(rejection_text)
'''
    text = replace_once(text, rejected_anchor, rejected_new, "clear design change feedback")

    review_done_anchor = '''    st.info("This design review link is now consumed and cannot be reused.")
'''
    review_done_new = '''    st.info(
        "This design review link is now consumed and cannot be reused. "
        "The campaign owner should click Refresh Senior Design Status on the main dashboard to see this decision."
    )
'''
    text = replace_once(text, review_done_anchor, review_done_new, "review completion guidance")

    APP_PATH.write_text(text, encoding="utf-8")
    print("improved app.py")


def main() -> None:
    patch_campaign_store()
    patch_app()
    print("Design approval dashboard and workflow hardening complete.")


if __name__ == "__main__":
    main()
