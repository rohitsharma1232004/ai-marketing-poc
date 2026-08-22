import hashlib

import pytest

from brand_kit import (
    brand_kit_prompt_context,
    normalize_brand_kit,
    validate_logo_upload,
)


def test_normalize_brand_kit_canonicalizes_colors_and_rules():
    kit = normalize_brand_kit(
        {
            "brand_name": " ABC Realty ",
            "primary_color": "#14213d",
            "secondary_color": "#fca311",
            "heading_font": "Montserrat",
            "brand_voice": "Premium but practical",
            "do_rules": "Use clean layouts\nUse real-estate imagery\nUse clean layouts",
            "dont_rules": ["Do not invent prices"],
        }
    )
    assert kit["brand_name"] == "ABC Realty"
    assert kit["primary_color"] == "#14213D"
    assert kit["secondary_color"] == "#FCA311"
    assert kit["do_rules"] == ["Use clean layouts", "Use real-estate imagery"]


def test_normalize_brand_kit_rejects_bad_color():
    with pytest.raises(ValueError, match="hex color"):
        normalize_brand_kit({"primary_color": "blue"})


def test_logo_upload_validates_real_signature_and_hash():
    raw = b"\x89PNG\r\n\x1a\n" + b"logo-data"
    result = validate_logo_upload("brand.png", "image/png", raw)
    assert result["logo_file_name"] == "brand.png"
    assert result["logo_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["extension"] == ".png"


def test_logo_upload_rejects_spoofed_png():
    with pytest.raises(ValueError, match="does not match"):
        validate_logo_upload("brand.png", "image/png", b"not-a-png")


def test_brand_kit_prompt_context_never_claims_logo_is_generated():
    context = brand_kit_prompt_context(
        {
            "brand_name": "ABC Realty",
            "primary_color": "#14213D",
            "visual_style": "Premium, minimal",
            "logo_file_name": "logo.png",
            "logo_mime_type": "image/png",
            "logo_storage_path": "generated_outputs/brand_assets/logo.png",
            "logo_sha256": "a" * 64,
            "logo_file_size": 123,
        }
    )
    assert "ABC Realty" in context
    assert "#14213D" in context
    assert "never redraw or invent the logo" in context
