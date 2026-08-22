import hashlib

import pytest

from creative_workflow import (
    CREATIVE_STATUS_APPROVED,
    CREATIVE_STATUS_BRIEF_READY,
    CREATIVE_STATUS_CHANGES_REQUESTED,
    CREATIVE_STATUS_PENDING_REVIEW,
    CREATIVE_STATUS_UPLOADED,
    DESIGN_CHANGE_FIELDS,
    PUBLISHING_STATUS_LOCKED,
    PUBLISHING_STATUS_READY,
    build_ai_design_prompt,
    build_design_review_dashboard_rows,
    content_post_by_number,
    creative_status,
    list_content_posts,
    publishing_status,
    safe_asset_storage_name,
    validate_creative_upload,
)


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
    ["__WEEK_HEADING__:Week 1"],
    [
        "Mon, Aug 24",
        "Instagram",
        "Educational",
        "Image",
        "3 checks before buying your first home",
        "first home buyer",
        "Save this checklist",
        "Three simple checks before you shortlist your first home.",
        "Not applicable",
        "Senior Approved",
    ],
    [
        "Wed, Aug 26",
        "Instagram",
        "Product / Service",
        "Reel",
        "What site visits should reveal",
        "site visit checklist",
        "Book a site visit",
        "Use your site visit to verify the things that matter.",
        "Scene 1 hook. Scene 2 checklist. Scene 3 CTA.",
        "Senior Approved",
    ],
]

IMAGE_BRIEF = {
    "post_number": 1,
    "format": "Image",
    "creative_objective": "Help first-time buyers remember three checks.",
    "main_headline": "3 Checks Before Your First Home",
    "visual_concept": "Clean checklist card with a residential background.",
    "on_visual_text": "3 checks before you shortlist",
    "visual_style": "Professional, modern, minimal.",
    "brand_placement": "Logo in the lower-right corner.",
    "cta_placement": "CTA strip along the bottom.",
    "asset_guidance": "Use a generic modern residential exterior.",
    "format_details": {
        "image_direction": "Single 4:5 static composition with clear hierarchy."
    },
}


def test_list_content_posts_skips_week_heading():
    posts = list_content_posts(HEADERS, ROWS, week_heading_prefix="__WEEK_HEADING__:")
    assert [item["post_number"] for item in posts] == [1, 2]
    assert [item["row_index"] for item in posts] == [1, 2]
    assert posts[0]["content"]["Content Idea"].startswith("3 checks")
    assert "Content Status" not in posts[0]["content"]


def test_content_post_by_number_returns_exact_post():
    post = content_post_by_number(
        HEADERS, ROWS, 2, week_heading_prefix="__WEEK_HEADING__:"
    )
    assert post["format"] == "Reel"
    assert post["content"]["CTA"] == "Book a site visit"


def test_ai_design_prompt_preserves_approved_source_and_direction():
    post = content_post_by_number(
        HEADERS, ROWS, 1, week_heading_prefix="__WEEK_HEADING__:"
    )
    prompt = build_ai_design_prompt(
        IMAGE_BRIEF,
        post,
        client_metadata={
            "client_name": "ABC Realty",
            "business": "Real Estate",
            "location": "Faridabad",
            "audience": "First-time home buyers",
            "tone": "Professional",
        },
    )
    assert "3 checks before buying your first home" in prompt
    assert "Save this checklist" in prompt
    assert "3 Checks Before Your First Home" in prompt
    assert "immutable source copy" in prompt
    assert "Do not invent claims" in prompt
    assert "ABC Realty" in prompt


def test_ai_design_prompt_rejects_wrong_post_binding():
    post = content_post_by_number(
        HEADERS, ROWS, 2, week_heading_prefix="__WEEK_HEADING__:"
    )
    with pytest.raises(ValueError, match="numbers do not match"):
        build_ai_design_prompt(IMAGE_BRIEF, post)


def test_validate_creative_upload_accepts_real_file_signatures_and_hashes_bytes():
    samples = [
        ("design.png", "image/png", b"\x89PNG\r\n\x1a\nrest"),
        ("design.jpg", "image/jpeg", b"\xff\xd8\xffrest"),
        ("design.pdf", "application/pdf", b"%PDF-1.7\nrest"),
    ]
    for name, mime, raw in samples:
        result = validate_creative_upload(name, mime, raw)
        assert result["file_name"] == name
        assert result["file_size"] == len(raw)
        assert result["file_sha256"] == hashlib.sha256(raw).hexdigest()


def test_validate_creative_upload_rejects_mismatch_unsupported_and_spoofed_content():
    with pytest.raises(ValueError, match="does not match"):
        validate_creative_upload("design.png", "image/jpeg", b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ValueError, match="Supported creative files"):
        validate_creative_upload("design.psd", "image/png", b"abc")
    with pytest.raises(ValueError, match="content does not match"):
        validate_creative_upload("design.png", "image/png", b"not-a-png")


def test_safe_asset_storage_name_is_scoped_and_sanitized():
    value = safe_asset_storage_name(
        campaign_id="12345678-aaaa-bbbb-cccc-123456789012",
        post_number=3,
        asset_version=2,
        original_name="Final Design (client)!!.jpg",
    )
    assert value.startswith("12345678_post03_v02_")
    assert value.endswith(".jpg")
    assert " " not in value


def test_creative_status_and_publishing_status_are_derived():
    assert creative_status(None) == CREATIVE_STATUS_BRIEF_READY
    assert creative_status({"post_number": 1}) == CREATIVE_STATUS_UPLOADED
    assert (
        creative_status({"post_number": 1, "active_review_link": True})
        == CREATIVE_STATUS_PENDING_REVIEW
    )
    assert (
        creative_status({"post_number": 1, "latest_decision": "rejected"})
        == CREATIVE_STATUS_CHANGES_REQUESTED
    )
    assert (
        creative_status({"post_number": 1, "latest_decision": "approved"})
        == CREATIVE_STATUS_APPROVED
    )
    assert (
        publishing_status(
            [
                {"post_number": 1, "latest_decision": "approved"},
                {"post_number": 2, "latest_decision": "approved"},
            ],
            2,
        )
        == PUBLISHING_STATUS_READY
    )
    assert (
        publishing_status(
            [{"post_number": 1, "latest_decision": "approved"}], 2
        )
        == PUBLISHING_STATUS_LOCKED
    )


def test_design_dashboard_rows_surface_approval_and_change_feedback():
    briefs = [
        {"post_number": 1, "format": "Image"},
        {"post_number": 2, "format": "Reel"},
        {"post_number": 3, "format": "Carousel"},
    ]
    assets = [
        {
            "post_number": 1,
            "asset_version": 2,
            "file_name": "approved.png",
            "latest_decision": "approved",
            "design_approver_name": "Senior One",
            "design_approver_email": "senior@example.com",
            "design_decided_at": "2026-08-21T10:00:00.000Z",
        },
        {
            "post_number": 2,
            "asset_version": 1,
            "file_name": "reel-cover.png",
            "latest_decision": "rejected",
            "design_approver_name": "Senior Two",
            "design_decided_at": "2026-08-21T10:05:00.000Z",
            "design_change_fields": ["Colors", "Reel Scenes / B-roll"],
            "design_feedback": "Use warmer colors and simplify scene 2.",
        },
    ]
    rows = build_design_review_dashboard_rows(briefs, assets)
    assert rows[0]["status"] == CREATIVE_STATUS_APPROVED
    assert rows[0]["approver_name"] == "Senior One"
    assert rows[1]["status"] == CREATIVE_STATUS_CHANGES_REQUESTED
    assert rows[1]["action_required"] is True
    assert rows[1]["change_fields"] == ["Colors", "Reel Scenes / B-roll"]
    assert rows[1]["feedback"].startswith("Use warmer")
    assert rows[2]["status"] == CREATIVE_STATUS_BRIEF_READY
    assert "Reel Scenes / B-roll" in DESIGN_CHANGE_FIELDS


def test_design_dashboard_rejects_duplicate_latest_assets():
    briefs = [{"post_number": 1, "format": "Image"}]
    assets = [
        {"post_number": 1, "asset_version": 1},
        {"post_number": 1, "asset_version": 2},
    ]
    with pytest.raises(ValueError, match="at most one"):
        build_design_review_dashboard_rows(briefs, assets)
