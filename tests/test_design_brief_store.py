import hashlib
import tempfile
from pathlib import Path

import pytest

from campaign_store import CampaignStore, InvalidStatusTransition, StoreConflict


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
ROWS = [
    [
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
    ],
    [
        "Wed, Aug 26",
        "Instagram",
        "Educational",
        "Reel",
        "Reel idea",
        "reel keyword",
        "Save this reel",
        "Reel caption",
        "Hook: Start; Scene 1: Explain; CTA: Save",
        "Ready for Senior Review",
    ],
]


def briefs():
    return [
        {
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
        },
        {
            "post_number": 2,
            "format": "Reel",
            "creative_objective": "Explain a site visit.",
            "main_headline": "Site Visit Checklist",
            "visual_concept": "Walkthrough with overlays.",
            "on_visual_text": "Save this checklist",
            "visual_style": "Fast and practical.",
            "brand_placement": "Small watermark.",
            "cta_placement": "End card.",
            "asset_guidance": "Generic property B-roll.",
            "format_details": {
                "scene_plan": ["Hook", "Checklist", "CTA"],
                "b_roll": "Entrance and common areas.",
                "transitions": "Simple hard cuts.",
                "thumbnail_idea": "Checklist over property image.",
            },
        },
    ]


def make_store():
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    client = store.upsert_client("ABC Realty")
    campaign = store.create_campaign(client["id"], {"goal": "Leads"})
    version = store.complete_generation(campaign["id"], HEADERS, ROWS)
    return temp, store, campaign, version


def test_design_briefs_require_final_senior_approval():
    temp, store, campaign, version = make_store()
    try:
        with pytest.raises(InvalidStatusTransition):
            store.save_design_briefs(
                campaign["id"], version["id"], version["content_hash"], briefs()
            )
    finally:
        store.close()
        temp.cleanup()


def test_design_briefs_are_version_and_hash_bound_and_listed_in_order():
    temp, store, campaign, version = make_store()
    try:
        store.record_approval(
            campaign["id"],
            version["id"],
            "senior",
            "approved",
            "Senior",
            "senior@example.com",
            senior_is_final=True,
        )
        saved = store.save_design_briefs(
            campaign["id"],
            version["id"],
            version["content_hash"],
            briefs(),
            generation_metadata={"provider": "groq", "model": "test", "request_id": "brief-1"},
        )
        assert [item["post_number"] for item in saved] == [1, 2]
        assert saved[1]["format"] == "Reel"
        assert saved[0]["brief"]["main_headline"] == "Buyer Checklist"
        assert saved[0]["content_hash"] == version["content_hash"]
        assert store.list_design_briefs(campaign["id"], version["id"]) == saved
        assert store.list_events(campaign["id"])[-1]["event_type"] == "design_briefs_generated"

        with pytest.raises(StoreConflict):
            store.save_design_briefs(
                campaign["id"], version["id"], version["content_hash"], briefs()
            )
    finally:
        store.close()
        temp.cleanup()


def test_wrong_hash_and_wrong_format_are_rejected():
    temp, store, campaign, version = make_store()
    try:
        store.record_approval(
            campaign["id"],
            version["id"],
            "senior",
            "approved",
            "Senior",
            "senior@example.com",
            senior_is_final=True,
        )
        wrong_hash = hashlib.sha256(b"wrong").hexdigest()
        with pytest.raises(StoreConflict):
            store.save_design_briefs(
                campaign["id"], version["id"], wrong_hash, briefs()
            )

        bad = briefs()
        bad[0]["format"] = "Reel"
        with pytest.raises(ValueError):
            store.save_design_briefs(
                campaign["id"], version["id"], version["content_hash"], bad
            )
    finally:
        store.close()
        temp.cleanup()
