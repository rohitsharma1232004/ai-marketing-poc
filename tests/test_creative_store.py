import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_store import CampaignStore, InvalidStatusTransition, StoreConflict
from senior_review_links import generate_review_token, hash_design_review_token


HEADERS = [
    "Date",
    "Platform",
    "Pillar",
    "Format",
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
    "Caption",
    "Reel Script",
    "Content Status",
]
ROWS = [[
    "Mon, Aug 24",
    "Instagram",
    "Educational",
    "Image",
    "Helpful idea",
    "useful keyword",
    "Learn more",
    "Helpful caption",
    "Not applicable",
    "Ready for Senior Review",
]]
BRIEF = [{
    "post_number": 1,
    "format": "Image",
    "creative_objective": "Educate buyers.",
    "main_headline": "Buyer Checklist",
    "visual_concept": "Couple with a checklist.",
    "on_visual_text": "3 checks before buying",
    "visual_style": "Clean and premium.",
    "brand_placement": "Logo bottom-right.",
    "cta_placement": "CTA at bottom.",
    "asset_guidance": "Generic residential exterior.",
    "format_details": {"image_direction": "Hero image plus checklist cards."},
}]


def future_iso(hours=2):
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_store(*, add_brief=True):
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    client = store.upsert_client("ABC Realty")
    campaign = store.create_campaign(client["id"], {"goal": "Leads"})
    version = store.complete_generation(campaign["id"], HEADERS, ROWS)
    store.record_approval(
        campaign["id"],
        version["id"],
        "senior",
        "approved",
        "Senior",
        "senior@example.com",
        senior_is_final=True,
    )
    if add_brief:
        store.save_design_briefs(
            campaign["id"], version["id"], version["content_hash"], BRIEF
        )
    return temp, store, campaign, version


def save_asset(store, campaign, version, *, suffix="one"):
    raw = f"creative-{suffix}".encode()
    return store.save_creative_asset(
        campaign["id"],
        version["id"],
        version["content_hash"],
        1,
        file_name=f"design-{suffix}.png",
        mime_type="image/png",
        storage_path=f"/tmp/design-{suffix}.png",
        file_sha256=hashlib.sha256(raw).hexdigest(),
        file_size=len(raw),
        design_prompt="Create the approved visual.",
    )


def test_creative_requires_existing_design_brief():
    temp, store, campaign, version = make_store(add_brief=False)
    try:
        with pytest.raises(InvalidStatusTransition, match="Design Brief"):
            save_asset(store, campaign, version)
    finally:
        store.close()
        temp.cleanup()


def test_creative_versions_are_append_only_and_latest_is_returned():
    temp, store, campaign, version = make_store()
    try:
        first = save_asset(store, campaign, version, suffix="one")
        second = save_asset(store, campaign, version, suffix="two")
        assert first["asset_version"] == 1
        assert second["asset_version"] == 2
        latest = store.list_latest_creative_assets(campaign["id"], version["id"])
        assert len(latest) == 1
        assert latest[0]["id"] == second["id"]
        assert latest[0]["latest_decision"] is None
        assert latest[0]["active_review_link"] is False
    finally:
        store.close()
        temp.cleanup()


def test_duplicate_latest_creative_is_rejected():
    temp, store, campaign, version = make_store()
    try:
        save_asset(store, campaign, version, suffix="same")
        with pytest.raises(StoreConflict, match="exact creative file"):
            save_asset(store, campaign, version, suffix="same")
    finally:
        store.close()
        temp.cleanup()


def test_design_review_link_rejects_old_creative_after_new_version_exists():
    temp, store, campaign, version = make_store()
    try:
        first = save_asset(store, campaign, version, suffix="one")
        save_asset(store, campaign, version, suffix="two")
        token_hash = hash_design_review_token(generate_review_token())
        with pytest.raises(StoreConflict, match="latest creative"):
            store.create_design_review_link(first["id"], token_hash, future_iso())
    finally:
        store.close()
        temp.cleanup()


def test_new_creative_revokes_pending_review_link_for_prior_version():
    temp, store, campaign, version = make_store()
    try:
        first = save_asset(store, campaign, version, suffix="one")
        token_hash = hash_design_review_token(generate_review_token())
        store.create_design_review_link(first["id"], token_hash, future_iso())
        assert store.list_latest_creative_assets(campaign["id"], version["id"])[0][
            "active_review_link"
        ] is True

        save_asset(store, campaign, version, suffix="two")
        with pytest.raises(StoreConflict, match="no longer active"):
            store.get_design_review_link_bundle(token_hash)
        latest = store.list_latest_creative_assets(campaign["id"], version["id"])[0]
        assert latest["asset_version"] == 2
        assert latest["active_review_link"] is False
    finally:
        store.close()
        temp.cleanup()


def test_design_rejection_is_bound_to_asset_and_new_upload_resets_latest_status():
    temp, store, campaign, version = make_store()
    try:
        first = save_asset(store, campaign, version, suffix="one")
        token_hash = hash_design_review_token(generate_review_token())
        store.create_design_review_link(first["id"], token_hash, future_iso())
        bundle = store.get_design_review_link_bundle(token_hash, mark_opened=True)
        assert bundle["asset"]["id"] == first["id"]
        assert bundle["design_brief"]["post_number"] == 1

        result = store.decide_design_review_link(
            token_hash,
            "rejected",
            "Senior Reviewer",
            "senior@example.com",
            "Increase logo visibility but keep approved copy unchanged.",
            change_fields=["Logo / Branding"],
        )
        assert result["approval"]["decision"] == "rejected"
        assert result["approval"]["change_fields"] == ["Logo / Branding"]
        latest = store.list_latest_creative_assets(campaign["id"], version["id"])[0]
        assert latest["latest_decision"] == "rejected"
        assert latest["design_approver_name"] == "Senior Reviewer"
        assert latest["design_approver_email"] == "senior@example.com"
        assert latest["design_decided_at"]
        assert latest["design_feedback"].startswith("Increase logo")

        second = save_asset(store, campaign, version, suffix="two")
        latest = store.list_latest_creative_assets(campaign["id"], version["id"])[0]
        assert latest["id"] == second["id"]
        assert latest["latest_decision"] is None
    finally:
        store.close()
        temp.cleanup()


def test_design_approval_consumes_link_and_marks_latest_asset_approved():
    temp, store, campaign, version = make_store()
    try:
        asset = save_asset(store, campaign, version)
        token_hash = hash_design_review_token(generate_review_token())
        store.create_design_review_link(asset["id"], token_hash, future_iso())
        result = store.decide_design_review_link(
            token_hash,
            "approved",
            "Senior Reviewer",
            "senior@example.com",
        )
        assert result["approval"]["decision"] == "approved"
        latest = store.list_latest_creative_assets(campaign["id"], version["id"])[0]
        assert latest["latest_decision"] == "approved"
        assert latest["design_approver_name"] == "Senior Reviewer"
        assert latest["design_approver_email"] == "senior@example.com"
        assert latest["design_decided_at"]
        assert latest["active_review_link"] is False
        with pytest.raises(StoreConflict):
            store.get_design_review_link_bundle(token_hash)
    finally:
        store.close()
        temp.cleanup()


def test_design_rejection_requires_feedback_and_change_field():
    temp, store, campaign, version = make_store()
    try:
        asset = save_asset(store, campaign, version)
        token_hash = hash_design_review_token(generate_review_token())
        store.create_design_review_link(asset["id"], token_hash, future_iso())
        with pytest.raises(ValueError):
            store.decide_design_review_link(
                token_hash,
                "rejected",
                "Senior Reviewer",
                "senior@example.com",
                "",
                change_fields=["Layout"],
            )
        with pytest.raises(ValueError):
            store.decide_design_review_link(
                token_hash,
                "rejected",
                "Senior Reviewer",
                "senior@example.com",
                "Change the layout.",
                change_fields=[],
            )
    finally:
        store.close()
        temp.cleanup()


def test_reel_scene_change_field_is_supported():
    temp, store, campaign, version = make_store()
    try:
        asset = save_asset(store, campaign, version)
        token_hash = hash_design_review_token(generate_review_token())
        store.create_design_review_link(asset["id"], token_hash, future_iso())
        result = store.decide_design_review_link(
            token_hash,
            "rejected",
            "Senior Reviewer",
            "senior@example.com",
            "Simplify the visual sequence and B-roll.",
            change_fields=["Reel Scenes / B-roll"],
        )
        assert result["approval"]["change_fields"] == ["Reel Scenes / B-roll"]
    finally:
        store.close()
        temp.cleanup()


def test_creative_upload_is_content_hash_bound():
    temp, store, campaign, version = make_store()
    try:
        raw = b"wrong-hash-test"
        with pytest.raises(StoreConflict):
            store.save_creative_asset(
                campaign["id"],
                version["id"],
                hashlib.sha256(b"wrong-content").hexdigest(),
                1,
                file_name="design.png",
                mime_type="image/png",
                storage_path="/tmp/design.png",
                file_sha256=hashlib.sha256(raw).hexdigest(),
                file_size=len(raw),
            )
    finally:
        store.close()
        temp.cleanup()
