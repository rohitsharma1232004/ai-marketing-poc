"""Provider-neutral publishing rules after final Senior Design Approval.

This module contains no Meta credentials and performs no network calls. It keeps
publication eligibility deterministic so the Streamlit UI, a future web API, and
a background worker all enforce the same gate.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

SUPPORTED_PLATFORMS = ("facebook", "instagram")
PHASE1_SUPPORTED_FORMATS = frozenset({"image"})
FACEBOOK_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg"})
# Meta's current Instagram publishing documentation specifies JPEG for image posts.
INSTAGRAM_IMAGE_MIME_TYPES = frozenset({"image/jpeg"})
MAX_CAPTION_CHARS = 2_200
MAX_PUBLIC_MEDIA_URL_CHARS = 2_000
META_CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class PublishingEligibilityError(ValueError):
    """Raised when an item is not eligible to cross the publishing gate."""


def normalize_platform(value: str) -> str:
    platform = str(value or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise PublishingEligibilityError(
            "Publishing platform must be Facebook or Instagram."
        )
    return platform


def normalize_credential_ref(value: str) -> str:
    ref = str(value or "").strip().upper()
    if not META_CREDENTIAL_REF_RE.fullmatch(ref):
        raise PublishingEligibilityError(
            "Credential reference must be an environment/secret name such as "
            "META_TOKEN_CLIENT_ABC. Never store the access token itself here."
        )
    return ref


def validate_public_media_url(value: str) -> str:
    url = str(value or "").strip()
    if not url or len(url) > MAX_PUBLIC_MEDIA_URL_CHARS:
        raise PublishingEligibilityError("A bounded public media URL is required.")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise PublishingEligibilityError(
            "Publishing media must use a publicly accessible HTTPS URL."
        )
    if parts.username or parts.password:
        raise PublishingEligibilityError("Public media URLs must not contain credentials.")
    return url


def normalize_scheduled_for(value: str | datetime | None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise PublishingEligibilityError("scheduled_for must not be empty.")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise PublishingEligibilityError(
                "scheduled_for must be an ISO-8601 timestamp."
            ) from error
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PublishingEligibilityError("scheduled_for must include a timezone.")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def platform_is_allowed(approved_platform_text: str, platform: str) -> bool:
    target = normalize_platform(platform)
    text = str(approved_platform_text or "").strip().lower()
    if not text:
        return False
    if target == "facebook":
        return "facebook" in text or text in {"fb", "both", "all"}
    return "instagram" in text or text in {"ig", "both", "all"}


def approved_post_from_row(
    headers: Sequence[Any], row: Sequence[Any]
) -> dict[str, str]:
    header_values = [str(item).strip() for item in headers]
    row_values = list(row)
    if len(header_values) != len(row_values):
        raise PublishingEligibilityError(
            "Approved content row does not match its stored headers."
        )
    content = {
        header: str(row_values[index] if row_values[index] is not None else "").strip()
        for index, header in enumerate(header_values)
    }
    for required in ("Platform", "Format", "Caption"):
        if required not in content:
            raise PublishingEligibilityError(
                f"Approved content is missing the {required} field."
            )
    return content


def validate_publishable_image(
    *,
    approved_post: Mapping[str, Any],
    creative_asset: Mapping[str, Any],
    design_approval: Mapping[str, Any],
    platform: str,
    public_media_url: str,
) -> dict[str, str]:
    """Validate one exact approved post/creative for phase-1 image publishing."""

    target = normalize_platform(platform)
    post = {str(key): str(value or "").strip() for key, value in approved_post.items()}
    creative = dict(creative_asset)
    approval = dict(design_approval)

    if str(approval.get("decision") or "").strip().lower() != "approved":
        raise PublishingEligibilityError(
            "Senior Design Approval is required before publishing."
        )
    if str(approval.get("creative_asset_id") or "") != str(creative.get("id") or ""):
        raise PublishingEligibilityError(
            "Design approval does not belong to this creative version."
        )
    if str(approval.get("asset_hash") or "") != str(creative.get("file_sha256") or ""):
        raise PublishingEligibilityError(
            "Design approval does not match the creative file hash."
        )

    approved_format = post.get("Format", "").casefold()
    creative_format = str(creative.get("format") or "").strip().casefold()
    if approved_format != creative_format:
        raise PublishingEligibilityError(
            "Approved content format and creative format do not match."
        )
    if approved_format not in PHASE1_SUPPORTED_FORMATS:
        raise PublishingEligibilityError(
            "Phase-1 publishing safely supports Image posts only. Reel/Video and "
            "Carousel require their final platform-ready media assets first."
        )

    mime_type = str(creative.get("mime_type") or "").strip().lower()
    allowed_mime_types = (
        INSTAGRAM_IMAGE_MIME_TYPES if target == "instagram" else FACEBOOK_IMAGE_MIME_TYPES
    )
    if mime_type not in allowed_mime_types:
        if target == "instagram":
            raise PublishingEligibilityError(
                "Instagram image publishing requires a Senior-approved JPEG creative. "
                "Convert/export to JPEG before Senior Design Approval; do not convert an "
                "already-approved file after approval."
            )
        raise PublishingEligibilityError(
            "Facebook image publishing requires a Senior-approved PNG or JPEG creative."
        )
    if not platform_is_allowed(post.get("Platform", ""), target):
        raise PublishingEligibilityError(
            f"The approved post is not assigned to {target.title()}."
        )

    caption = post.get("Caption", "").strip()
    if not caption:
        raise PublishingEligibilityError("The approved post has no caption.")
    if len(caption) > MAX_CAPTION_CHARS:
        raise PublishingEligibilityError(
            f"Caption exceeds the phase-1 safety limit of {MAX_CAPTION_CHARS} characters."
        )

    return {
        "platform": target,
        "caption": caption,
        "public_media_url": validate_public_media_url(public_media_url),
    }


def publication_dedupe_key(
    *,
    campaign_id: str,
    calendar_version_id: str,
    content_hash: str,
    creative_asset_id: str,
    creative_hash: str,
    connection_id: str,
    platform: str,
) -> str:
    target = normalize_platform(platform)
    material = "\n".join(
        str(item or "").strip()
        for item in (
            campaign_id,
            calendar_version_id,
            content_hash,
            creative_asset_id,
            creative_hash,
            connection_id,
            target,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
