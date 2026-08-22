"""Validated client Brand Kit helpers for creative automation.

The Brand Kit is intentionally provider-neutral. Gemini, Canva, Adobe, manual
creative work, and future providers can all consume the same normalized data.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BRAND_KIT_SCHEMA_VERSION = 1
MAX_BRAND_TEXT_CHARS = 1200
MAX_BRAND_NOTES_CHARS = 4000
MAX_BRAND_RULES = 12
MAX_LOGO_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_LOGO_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
ALLOWED_LOGO_MIME_TYPES = frozenset({"image/png", "image/jpeg"})

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _clean_text(value: Any, label: str, *, max_chars: int = MAX_BRAND_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > max_chars:
        raise ValueError(f"{label} must be at most {max_chars} characters.")
    return text


def _clean_color(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _HEX_COLOR_RE.fullmatch(text):
        raise ValueError(f"{label} must be a hex color such as #14213D.")
    return text.upper()


def _clean_rules(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = [item for item in value.splitlines() if item.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = list(value)
    else:
        raise TypeError(f"{label} must be a list or newline-separated text.")
    if len(raw_items) > MAX_BRAND_RULES:
        raise ValueError(f"{label} can contain at most {MAX_BRAND_RULES} rules.")
    result: list[str] = []
    for raw in raw_items:
        item = _clean_text(raw, label, max_chars=500)
        if item and item not in result:
            result.append(item)
    return result


def normalize_brand_kit(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, predictable Brand Kit object.

    Brand identity is optional for a POC, so no visual field is mandatory. A
    client can start with only a name and progressively enrich the Brand Kit.
    """

    data = dict(value or {})
    allowed = {
        "brand_name",
        "primary_color",
        "secondary_color",
        "accent_color",
        "heading_font",
        "body_font",
        "brand_voice",
        "visual_style",
        "preferred_imagery",
        "website",
        "instagram_handle",
        "do_rules",
        "dont_rules",
        "notes",
        "logo_file_name",
        "logo_mime_type",
        "logo_storage_path",
        "logo_sha256",
        "logo_file_size",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Unsupported Brand Kit field(s): " + ", ".join(sorted(unknown)))

    normalized: dict[str, Any] = {
        "brand_name": _clean_text(data.get("brand_name"), "brand_name", max_chars=200),
        "primary_color": _clean_color(data.get("primary_color"), "primary_color"),
        "secondary_color": _clean_color(data.get("secondary_color"), "secondary_color"),
        "accent_color": _clean_color(data.get("accent_color"), "accent_color"),
        "heading_font": _clean_text(data.get("heading_font"), "heading_font", max_chars=160),
        "body_font": _clean_text(data.get("body_font"), "body_font", max_chars=160),
        "brand_voice": _clean_text(data.get("brand_voice"), "brand_voice"),
        "visual_style": _clean_text(data.get("visual_style"), "visual_style"),
        "preferred_imagery": _clean_text(data.get("preferred_imagery"), "preferred_imagery"),
        "website": _clean_text(data.get("website"), "website", max_chars=500),
        "instagram_handle": _clean_text(data.get("instagram_handle"), "instagram_handle", max_chars=200),
        "do_rules": _clean_rules(data.get("do_rules"), "do_rules"),
        "dont_rules": _clean_rules(data.get("dont_rules"), "dont_rules"),
        "notes": _clean_text(data.get("notes"), "notes", max_chars=MAX_BRAND_NOTES_CHARS),
        "logo_file_name": _clean_text(data.get("logo_file_name"), "logo_file_name", max_chars=300),
        "logo_mime_type": _clean_text(data.get("logo_mime_type"), "logo_mime_type", max_chars=120).lower(),
        "logo_storage_path": _clean_text(data.get("logo_storage_path"), "logo_storage_path", max_chars=1200),
        "logo_sha256": str(data.get("logo_sha256") or "").strip().lower(),
        "logo_file_size": int(data.get("logo_file_size") or 0),
    }
    if normalized["logo_sha256"] and not re.fullmatch(r"[0-9a-f]{64}", normalized["logo_sha256"]):
        raise ValueError("logo_sha256 must be a lowercase SHA-256 hash.")
    if normalized["logo_file_size"] < 0 or normalized["logo_file_size"] > MAX_LOGO_FILE_BYTES:
        raise ValueError("logo_file_size is outside the supported range.")
    logo_fields = (
        normalized["logo_file_name"],
        normalized["logo_mime_type"],
        normalized["logo_storage_path"],
        normalized["logo_sha256"],
        normalized["logo_file_size"],
    )
    if any(logo_fields) and not all(logo_fields):
        raise ValueError("Logo metadata must be complete when a logo is stored.")
    return normalized


def validate_logo_upload(file_name: str, mime_type: str, file_bytes: bytes) -> dict[str, Any]:
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise TypeError("file_bytes must be bytes.")
    raw = bytes(file_bytes)
    if not raw:
        raise ValueError("Logo file must not be empty.")
    if len(raw) > MAX_LOGO_FILE_BYTES:
        raise ValueError("Logo file must be 5 MB or smaller.")

    name = Path(str(file_name or "").strip()).name
    suffix = Path(name).suffix.lower()
    clean_mime = str(mime_type or "").strip().lower()
    if suffix not in ALLOWED_LOGO_EXTENSIONS:
        raise ValueError("Supported logo files are PNG and JPG/JPEG.")
    if clean_mime not in ALLOWED_LOGO_MIME_TYPES:
        raise ValueError("Unsupported logo MIME type.")
    if suffix == ".png":
        if clean_mime != "image/png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PNG logo content does not match its declared file type.")
    else:
        if clean_mime != "image/jpeg" or not raw.startswith(b"\xff\xd8\xff"):
            raise ValueError("JPEG logo content does not match its declared file type.")
    return {
        "logo_file_name": name,
        "logo_mime_type": clean_mime,
        "logo_sha256": hashlib.sha256(raw).hexdigest(),
        "logo_file_size": len(raw),
        "extension": suffix,
    }


def brand_kit_prompt_context(value: Mapping[str, Any] | None) -> str:
    """Render only useful Brand Kit fields into a provider-safe prompt block."""

    kit = normalize_brand_kit(value)
    lines: list[str] = []
    for key, label in (
        ("brand_name", "Brand"),
        ("primary_color", "Primary color"),
        ("secondary_color", "Secondary color"),
        ("accent_color", "Accent color"),
        ("heading_font", "Heading font preference"),
        ("body_font", "Body font preference"),
        ("brand_voice", "Brand voice"),
        ("visual_style", "Visual style"),
        ("preferred_imagery", "Preferred imagery"),
        ("website", "Website"),
        ("instagram_handle", "Instagram handle"),
    ):
        if kit[key]:
            lines.append(f"- {label}: {kit[key]}")
    if kit["do_rules"]:
        lines.append("- Brand DO rules: " + " | ".join(kit["do_rules"]))
    if kit["dont_rules"]:
        lines.append("- Brand DON'T rules: " + " | ".join(kit["dont_rules"]))
    if kit["notes"]:
        lines.append(f"- Additional brand notes: {kit['notes']}")
    if kit["logo_storage_path"]:
        lines.append("- Client logo is available as a separate reference asset; never redraw or invent the logo.")
    return "\n".join(lines)
