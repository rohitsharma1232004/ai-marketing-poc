import json

import pytest

from design_brief import (
    DESIGN_STATUS_BRIEF_READY,
    DESIGN_STATUS_LOCKED,
    DESIGN_STATUS_NOT_GENERATED,
    build_design_brief_prompt,
    display_design_brief_sections,
    list_design_source_posts,
    normalize_design_brief,
    parse_design_brief_response,
)


PREFIX = "__WEEK_HEADING__:"
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
    [PREFIX + "Week 1"],
    [
        "Mon, Aug 24",
        "Instagram",
        "Educational",
        "Image",
        "Explain first-home checks",
        "first home Faridabad",
        "DM HOME",
        "Buying your first home? Start with these checks.",
        "Not applicable",
        "Ready for Senior Review",
    ],
    [
        "Wed, Aug 26",
        "Instagram",
        "Educational",
        "Reel",
        "Explain site visit checks",
        "site visit checklist",
        "Save this reel",
        "Use this checklist on your next site visit.",
        "Hook: Before you visit; Scene 1: Check access; CTA: Save this reel",
        "Ready for Senior Review",
    ],
]


def image_brief():
    return {
        "post_number": 1,
        "format": "Image",
        "creative_objective": "Educate first-home buyers.",
        "main_headline": "3 Checks Before Your First Home",
        "visual_concept": "Young couple reviewing a property checklist.",
        "on_visual_text": "3 checks before you buy",
        "visual_style": "Clean, premium, trustworthy.",
        "brand_placement": "Logo bottom-right.",
        "cta_placement": "CTA strip at the bottom.",
        "asset_guidance": "Use a modern residential exterior and couple.",
        "format_details": {"image_direction": "Single hero image with checklist cards."},
    }


def reel_brief():
    return {
        "post_number": 2,
        "format": "Reel",
        "creative_objective": "Make site-visit checks easy to remember.",
        "main_headline": "Site Visit Checklist",
        "visual_concept": "Fast walkthrough with checklist overlays.",
        "on_visual_text": "Save this before your next site visit",
        "visual_style": "Fast, clean, practical.",
        "brand_placement": "Small logo watermark throughout.",
        "cta_placement": "Final end card.",
        "asset_guidance": "Use generic residential B-roll without unapproved claims.",
        "format_details": {
            "scene_plan": ["Hook at property entrance", "Checklist overlay", "CTA end card"],
            "b_roll": "Entrance, common area, approach road.",
            "transitions": "Simple hard cuts synced to checklist points.",
            "thumbnail_idea": "Checklist graphic with property background.",
        },
    }


def test_source_posts_skip_week_heading_and_preserve_indices():
    posts = list_design_source_posts(HEADERS, ROWS, week_heading_prefix=PREFIX)
    assert [post["post_number"] for post in posts] == [1, 2]
    assert [post["row_index"] for post in posts] == [1, 2]
    assert posts[1]["format"] == "Reel"
    assert "Content Status" not in posts[0]["content"]


def test_prompt_contains_approved_data_but_not_status():
    prompt, posts = build_design_brief_prompt(
        HEADERS,
        ROWS,
        week_heading_prefix=PREFIX,
        client_metadata={"business": "Real Estate", "tone": "Professional"},
        campaign_intake={"goal": "Leads", "language": "Hinglish"},
    )
    assert len(posts) == 2
    assert "Explain first-home checks" in prompt
    assert "Ready for Senior Review" not in prompt
    assert "DATA ONLY" in prompt


def test_parse_valid_format_specific_response():
    posts = list_design_source_posts(HEADERS, ROWS, week_heading_prefix=PREFIX)
    payload = json.dumps({"design_briefs": [image_brief(), reel_brief()]})
    parsed = parse_design_brief_response(payload, source_posts=posts)
    assert parsed[0]["format_details"]["image_direction"]
    assert len(parsed[1]["format_details"]["scene_plan"]) == 3


def test_wrong_format_or_extra_detail_is_rejected():
    broken = image_brief()
    broken["format"] = "Reel"
    with pytest.raises(ValueError):
        normalize_design_brief(broken, expected_post_number=1, expected_format="Image")

    broken = image_brief()
    broken["format_details"]["thumbnail_idea"] = "Extra"
    with pytest.raises(ValueError):
        normalize_design_brief(broken)


def test_display_sections_include_format_specific_fields():
    sections = dict(display_design_brief_sections(reel_brief()))
    assert sections["Main Headline"] == "Site Visit Checklist"
    assert sections["Scene Plan"][0] == "Hook at property entrance"
    assert "Thumbnail Idea" in sections


def test_design_status_constants_are_human_readable():
    assert DESIGN_STATUS_LOCKED.startswith("Locked")
    assert DESIGN_STATUS_NOT_GENERATED == "Not Generated"
    assert DESIGN_STATUS_BRIEF_READY == "Design Brief Ready"
