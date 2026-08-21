import pytest

from publishing_workflow import (
    PublishingEligibilityError,
    normalize_credential_ref,
    publication_dedupe_key,
    validate_publishable_image,
)


def _approved_post():
    return {
        "Platform": "Instagram and Facebook",
        "Format": "Image",
        "Caption": "Approved caption",
    }


def _creative():
    return {
        "id": "asset-1",
        "format": "Image",
        "mime_type": "image/png",
        "file_sha256": "a" * 64,
    }


def _approval():
    return {
        "creative_asset_id": "asset-1",
        "decision": "approved",
        "asset_hash": "a" * 64,
    }


def test_publishable_image_requires_hash_matched_design_approval():
    result = validate_publishable_image(
        approved_post=_approved_post(),
        creative_asset=_creative(),
        design_approval=_approval(),
        platform="instagram",
        public_media_url="https://cdn.example.com/creative.png",
    )
    assert result["caption"] == "Approved caption"
    assert result["platform"] == "instagram"


def test_rejected_or_wrong_hash_creative_cannot_publish():
    approval = _approval()
    approval["asset_hash"] = "b" * 64
    with pytest.raises(PublishingEligibilityError, match="file hash"):
        validate_publishable_image(
            approved_post=_approved_post(),
            creative_asset=_creative(),
            design_approval=approval,
            platform="facebook",
            public_media_url="https://cdn.example.com/creative.png",
        )


def test_phase1_blocks_reel_until_real_video_asset_pipeline_exists():
    post = _approved_post()
    post["Format"] = "Reel"
    creative = _creative()
    creative["format"] = "Reel"
    with pytest.raises(PublishingEligibilityError, match="Image posts only"):
        validate_publishable_image(
            approved_post=post,
            creative_asset=creative,
            design_approval=_approval(),
            platform="instagram",
            public_media_url="https://cdn.example.com/creative.png",
        )


def test_credential_ref_is_reference_not_token():
    assert normalize_credential_ref("meta_token_client_abc") == "META_TOKEN_CLIENT_ABC"
    with pytest.raises(PublishingEligibilityError):
        normalize_credential_ref("EAAB-real-token-looking-value")


def test_dedupe_key_is_stable_and_platform_specific():
    base = dict(
        campaign_id="campaign",
        calendar_version_id="calendar",
        content_hash="a" * 64,
        creative_asset_id="creative",
        creative_hash="b" * 64,
        connection_id="connection",
    )
    first = publication_dedupe_key(platform="instagram", **base)
    second = publication_dedupe_key(platform="instagram", **base)
    facebook = publication_dedupe_key(platform="facebook", **base)
    assert first == second
    assert first != facebook
