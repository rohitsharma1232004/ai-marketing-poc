import hashlib
import tempfile
from pathlib import Path

from campaign_store import CampaignStore


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


def test_ai_creative_provenance_round_trips_with_latest_asset():
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    try:
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
        store.save_design_briefs(
            campaign["id"], version["id"], version["content_hash"], BRIEF
        )
        raw = b"gemini-generated-image"
        asset = store.save_creative_asset(
            campaign["id"],
            version["id"],
            version["content_hash"],
            1,
            file_name="gemini.png",
            mime_type="image/png",
            storage_path="/tmp/gemini.png",
            file_sha256=hashlib.sha256(raw).hexdigest(),
            file_size=len(raw),
            source_type="ai_generated",
            design_prompt="Create the approved visual.",
            source_provider="gemini",
            source_model="gemini-3.1-flash-lite-image",
            source_request_id="request-123",
            source_metadata={"aspect_ratio": "4:5", "image_size": "1K"},
        )
        assert asset["source_provider"] == "gemini"
        assert asset["source_model"] == "gemini-3.1-flash-lite-image"
        assert asset["source_request_id"] == "request-123"
        assert asset["source_metadata"] == {
            "aspect_ratio": "4:5",
            "image_size": "1K",
        }

        latest = store.list_latest_creative_assets(campaign["id"], version["id"])[0]
        assert latest["id"] == asset["id"]
        assert latest["source_provider"] == "gemini"
        assert latest["source_metadata"]["image_size"] == "1K"
    finally:
        store.close()
        temp.cleanup()
