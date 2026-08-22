"""Helpers for creative upload, prompt generation, and design-review status."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from content_package import require_supported_calendar_headers
from design_brief import normalize_design_brief

CREATIVE_STATUS_BRIEF_READY = "Design Brief Ready"
CREATIVE_STATUS_UPLOADED = "Creative Uploaded"
CREATIVE_STATUS_PENDING_REVIEW = "Pending Senior Design Review"
CREATIVE_STATUS_CHANGES_REQUESTED = "Design Changes Requested"
CREATIVE_STATUS_APPROVED = "Design Approved"

PUBLISHING_STATUS_LOCKED = "Publishing Locked — Design Approval Required"
PUBLISHING_STATUS_READY = "Publishing Ready"

DESIGN_CHANGE_FIELDS = (
    "Layout",
    "Image / Visual",
    "Colors",
    "Typography",
    "Text Placement",
    "Logo / Branding",
    "CTA Placement",
    "Carousel Slides",
    "Reel Scenes / B-roll",
    "Thumbnail",
    "Other",
)

ALLOWED_CREATIVE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".pdf"})
ALLOWED_CREATIVE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "application/pdf",
    }
)
MAX_CREATIVE_FILE_BYTES = 12 * 1024 * 1024
MAX_AI_DESIGN_PROMPT_CHARS = 12_000


def list_content_posts(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    *,
    week_heading_prefix: str,
) -> list[dict[str, Any]]:
    normalized_headers = list(require_supported_calendar_headers(headers))
    posts: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(rows):
        row = list(raw_row)
        if len(row) == 1 and str(row[0]).startswith(week_heading_prefix):
            continue
        if len(row) != len(normalized_headers):
            raise ValueError("A content row does not match the approved package headers.")
        post_number = len(posts) + 1
        content = {
            header: str(row[index]).strip()
            for index, header in enumerate(normalized_headers)
            if header != "Content Status"
        }
        posts.append(
            {
                "post_number": post_number,
                "row_index": row_index,
                "format": content.get("Format", ""),
                "content": content,
            }
        )
    if not posts:
        raise ValueError("The approved content package has no posts.")
    return posts


def content_post_by_number(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    post_number: int,
    *,
    week_heading_prefix: str,
) -> dict[str, Any]:
    if not isinstance(post_number, int) or isinstance(post_number, bool) or post_number < 1:
        raise ValueError("post_number must be a positive integer.")
    posts = list_content_posts(
        headers,
        rows,
        week_heading_prefix=week_heading_prefix,
    )
    if post_number > len(posts):
        raise ValueError("The selected post is outside this content package.")
    return posts[post_number - 1]


def build_ai_design_prompt(
    brief: Mapping[str, Any],
    approved_post: Mapping[str, Any],
    *,
    client_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Build a provider-neutral prompt from an approved post and its design brief.

    The prompt is deterministic and intentionally treats the approved content as
    immutable source copy. It can be pasted into Canva or another image/design AI,
    and later can be passed to an API-based creative provider without changing the
    approval workflow.
    """

    normalized_brief = normalize_design_brief(brief)
    post = dict(approved_post)
    content = dict(post.get("content") or {})
    post_number = post.get("post_number")
    if post_number != normalized_brief["post_number"]:
        raise ValueError("Design brief and approved post numbers do not match.")
    approved_format = str(content.get("Format") or post.get("format") or "").strip()
    if approved_format.casefold() != normalized_brief["format"].casefold():
        raise ValueError("Design brief format does not match the approved post.")

    client = dict(client_metadata or {})
    context_lines = []
    for key, label in (
        ("client_name", "Client"),
        ("business", "Business"),
        ("location", "Location"),
        ("audience", "Audience"),
        ("tone", "Brand tone"),
        ("language", "Language"),
    ):
        value = str(client.get(key) or "").strip()
        if value:
            context_lines.append(f"- {label}: {value}")

    approved_fields = (
        "Platform",
        "Pillar",
        "Format",
        "Content Idea",
        "CTA",
        "Caption",
        "Reel Script",
    )
    source_lines = [
        f"- {field}: {content[field]}"
        for field in approved_fields
        if str(content.get(field) or "").strip()
    ]

    brief_lines = [
        f"- Creative objective: {normalized_brief['creative_objective']}",
        f"- Main headline: {normalized_brief['main_headline']}",
        f"- Visual concept: {normalized_brief['visual_concept']}",
        f"- On-visual text: {normalized_brief['on_visual_text']}",
        f"- Visual style: {normalized_brief['visual_style']}",
        f"- Brand placement: {normalized_brief['brand_placement']}",
        f"- CTA placement: {normalized_brief['cta_placement']}",
        f"- Asset guidance: {normalized_brief['asset_guidance']}",
    ]
    for key, value in normalized_brief["format_details"].items():
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            brief_lines.append(f"- {label}: " + " | ".join(value))
        else:
            brief_lines.append(f"- {label}: {value}")

    prompt = "\n".join(
        [
            f"Create a polished {normalized_brief['format']} social-media creative for Post {post_number}.",
            "",
            "STRICT CONTENT RULES:",
            "- Treat the approved content below as immutable source copy.",
            "- Do not invent claims, offers, prices, statistics, testimonials, product details, or property details.",
            "- Do not change the approved message, CTA, caption, platform, pillar, or format.",
            "- Keep all visible text readable, brand-safe, and suitable for the target platform.",
            "- Do not add watermarks or third-party logos.",
            "",
            *(["CLIENT CONTEXT:", *context_lines, ""] if context_lines else []),
            "APPROVED CONTENT:",
            *source_lines,
            "",
            "DESIGN DIRECTION:",
            *brief_lines,
            "",
            "OUTPUT:",
            "Create the final visual composition only. Preserve the approved communication while executing the design direction above.",
        ]
    ).strip()
    if len(prompt) > MAX_AI_DESIGN_PROMPT_CHARS:
        raise ValueError("The AI design prompt is too large. Reduce design-brief detail.")
    return prompt


def _validate_file_signature(extension: str, file_bytes: bytes) -> None:
    """Reject obvious extension/MIME spoofing before a creative reaches storage."""

    raw = bytes(file_bytes)
    if extension == ".png":
        valid = raw.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = raw.startswith(b"\xff\xd8\xff")
    elif extension == ".pdf":
        valid = raw[:1024].lstrip().startswith(b"%PDF-")
    else:
        valid = False
    if not valid:
        raise ValueError("The creative file content does not match its declared file type.")


def validate_creative_upload(file_name: str, mime_type: str, file_bytes: bytes) -> dict[str, Any]:
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise TypeError("file_bytes must be bytes.")
    size = len(file_bytes)
    if size < 1:
        raise ValueError("Creative file must not be empty.")
    if size > MAX_CREATIVE_FILE_BYTES:
        raise ValueError("Creative file must be 12 MB or smaller.")

    name = Path(str(file_name or "").strip()).name
    if not name:
        raise ValueError("Creative file name is required.")
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_CREATIVE_EXTENSIONS:
        raise ValueError("Supported creative files are PNG, JPG/JPEG, and PDF.")
    clean_mime = str(mime_type or "").strip().lower()
    if clean_mime not in ALLOWED_CREATIVE_MIME_TYPES:
        raise ValueError("The creative file type is not supported.")
    if extension == ".png" and clean_mime != "image/png":
        raise ValueError("PNG file type does not match its MIME type.")
    if extension in {".jpg", ".jpeg"} and clean_mime != "image/jpeg":
        raise ValueError("JPEG file type does not match its MIME type.")
    if extension == ".pdf" and clean_mime != "application/pdf":
        raise ValueError("PDF file type does not match its MIME type.")
    _validate_file_signature(extension, bytes(file_bytes))

    return {
        "file_name": name,
        "mime_type": clean_mime,
        "file_size": size,
        "file_sha256": hashlib.sha256(bytes(file_bytes)).hexdigest(),
        "extension": extension,
    }


def safe_asset_storage_name(
    *, campaign_id: str, post_number: int, asset_version: int, original_name: str
) -> str:
    suffix = Path(original_name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(original_name).stem).strip("_")
    stem = stem[:60] or "creative"
    return (
        f"{campaign_id[:8]}_post{int(post_number):02d}_v{int(asset_version):02d}_"
        f"{stem}{suffix}"
    )


def creative_status(latest_asset: Mapping[str, Any] | None) -> str:
    if not latest_asset:
        return CREATIVE_STATUS_BRIEF_READY
    decision = str(latest_asset.get("latest_decision") or "").strip().lower()
    if decision == "approved":
        return CREATIVE_STATUS_APPROVED
    if decision == "rejected":
        return CREATIVE_STATUS_CHANGES_REQUESTED
    if latest_asset.get("active_review_link"):
        return CREATIVE_STATUS_PENDING_REVIEW
    return CREATIVE_STATUS_UPLOADED


def publishing_status(latest_assets: Sequence[Mapping[str, Any]], expected_posts: int) -> str:
    if expected_posts < 1:
        raise ValueError("expected_posts must be positive.")
    approved_posts = {
        int(item["post_number"])
        for item in latest_assets
        if str(item.get("latest_decision") or "").strip().lower() == "approved"
    }
    return (
        PUBLISHING_STATUS_READY
        if approved_posts == set(range(1, expected_posts + 1))
        else PUBLISHING_STATUS_LOCKED
    )


def build_design_review_dashboard_rows(
    design_briefs: Sequence[Mapping[str, Any]],
    latest_assets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one clear dashboard row per approved post/design brief."""

    asset_by_post: dict[int, Mapping[str, Any]] = {}
    for asset in latest_assets:
        post_number = int(asset["post_number"])
        if post_number in asset_by_post:
            raise ValueError("latest_assets must contain at most one asset per post.")
        asset_by_post[post_number] = asset

    rows: list[dict[str, Any]] = []
    seen_posts: set[int] = set()
    for brief_record in design_briefs:
        post_number = int(brief_record["post_number"])
        if post_number < 1 or post_number in seen_posts:
            raise ValueError("design_briefs must contain unique positive post numbers.")
        seen_posts.add(post_number)
        asset = asset_by_post.get(post_number)
        status = creative_status(asset)
        rows.append(
            {
                "post_number": post_number,
                "format": str(brief_record.get("format") or "").strip(),
                "status": status,
                "asset_version": int(asset["asset_version"]) if asset else None,
                "file_name": str(asset.get("file_name") or "") if asset else "",
                "active_review_link": bool(asset.get("active_review_link")) if asset else False,
                "active_review_expires_at": str(asset.get("active_review_expires_at") or "") if asset else "",
                "latest_decision": str(asset.get("latest_decision") or "") if asset else "",
                "approver_name": str(asset.get("design_approver_name") or "") if asset else "",
                "approver_email": str(asset.get("design_approver_email") or "") if asset else "",
                "decided_at": str(asset.get("design_decided_at") or "") if asset else "",
                "change_fields": list(asset.get("design_change_fields") or []) if asset else [],
                "feedback": str(asset.get("design_feedback") or "") if asset else "",
                "action_required": status == CREATIVE_STATUS_CHANGES_REQUESTED,
            }
        )
    return rows
