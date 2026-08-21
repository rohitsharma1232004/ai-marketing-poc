from creative_studio import (
    build_branded_design_prompt,
    build_design_revision_prompt,
    provider_capability,
    recommended_aspect_ratio,
)


BRIEF = {
    "post_number": 1,
    "format": "Image",
    "creative_objective": "Educate first-home buyers.",
    "main_headline": "3 Checks Before Your First Home",
    "visual_concept": "Premium checklist layout with a modern residential visual.",
    "on_visual_text": "3 checks before you shortlist",
    "visual_style": "Clean and premium.",
    "brand_placement": "Reserve logo area at bottom-right.",
    "cta_placement": "CTA strip at the bottom.",
    "asset_guidance": "Use a generic modern residential exterior.",
    "format_details": {"image_direction": "Single 4:5 static composition."},
}
POST = {
    "post_number": 1,
    "format": "Image",
    "content": {
        "Platform": "Instagram",
        "Pillar": "Educational",
        "Format": "Image",
        "Content Idea": "3 checks before buying your first home",
        "CTA": "Save this checklist",
        "Caption": "Three simple checks before you shortlist your first home.",
        "Reel Script": "Not applicable",
    },
}
KIT = {
    "brand_name": "ABC Realty",
    "primary_color": "#14213D",
    "secondary_color": "#FCA311",
    "heading_font": "Montserrat",
    "visual_style": "Premium, clean, trustworthy",
    "do_rules": ["Use strong whitespace"],
    "dont_rules": ["Do not invent prices"],
}


def test_branded_prompt_keeps_approved_copy_and_brand_constraints():
    prompt = build_branded_design_prompt(
        BRIEF,
        POST,
        client_metadata={"client_name": "ABC Realty"},
        brand_kit=KIT,
    )
    assert "3 checks before buying your first home" in prompt
    assert "Save this checklist" in prompt
    assert "#14213D" in prompt
    assert "Montserrat" in prompt
    assert "Never invent, redraw, or imitate a client logo" in prompt


def test_visual_only_mode_explicitly_blocks_copy_rendering():
    prompt = build_branded_design_prompt(
        BRIEF,
        POST,
        brand_kit=KIT,
        generation_mode="visual_only",
    )
    assert "Generate the visual/background composition only" in prompt
    assert "Do not render the logo, headline, CTA" in prompt


def test_revision_prompt_preserves_content_and_scopes_changes():
    prompt = build_design_revision_prompt(
        original_prompt="Original creative direction",
        senior_feedback="Make the logo area more visible and simplify the background.",
        change_fields=["Logo / Branding", "Image / Visual"],
        approved_post=POST,
        brand_kit=KIT,
    )
    assert "Logo / Branding" in prompt
    assert "simplify the background" in prompt
    assert "Save this checklist" in prompt
    assert "Change only what is necessary" in prompt


def test_recommended_ratios_match_social_formats():
    assert recommended_aspect_ratio("Image", "Instagram") == "4:5"
    assert recommended_aspect_ratio("Carousel", "Facebook") == "4:5"
    assert recommended_aspect_ratio("Reel", "Instagram") == "9:16"
    assert recommended_aspect_ratio("Story", "Instagram") == "9:16"


def test_provider_registry_keeps_optional_enterprise_integrations_disabled():
    assert provider_capability("gemini")["available_in_app"] is True
    assert provider_capability("manual_upload")["available_in_app"] is True
    assert provider_capability("canva")["available_in_app"] is False
    assert provider_capability("adobe")["available_in_app"] is False
