"""Shared content-package structure and format-aware helpers.

This module is intentionally UI- and persistence-free so the Streamlit app,
revision logic, and storage layer can enforce the same column contract.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

LEGACY_CALENDAR_HEADERS = (
    "Date",
    "Platform",
    "Pillar",
    "Format",
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
)
GENERATION_HEADERS = LEGACY_CALENDAR_HEADERS + (
    "Caption",
    "Reel Script",
)
CONTENT_PACKAGE_HEADERS = GENERATION_HEADERS + ("Content Status",)
SUPPORTED_CALENDAR_HEADERS = (
    LEGACY_CALENDAR_HEADERS,
    CONTENT_PACKAGE_HEADERS,
)
REVISION_FIELDS = (
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
    "Caption",
    "Reel Script",
)
REEL_SCRIPT_FORMATS = frozenset({"reel", "video"})
REEL_SCRIPT_NOT_APPLICABLE = "Not applicable"
CONTENT_STATUS_READY = "Ready for Senior Review"
CONTENT_STATUS_NEEDS_CHANGES = "Needs Changes"
CONTENT_STATUS_APPROVED = "Senior Approved"
CONTENT_STATUS_DRAFT = "Draft"


def normalize_headers(headers: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(headers, (str, bytes, bytearray)) or not isinstance(headers, Sequence):
        raise TypeError("headers must be a sequence.")
    return tuple(str(value).strip() for value in headers)


def require_supported_calendar_headers(headers: Sequence[Any]) -> tuple[str, ...]:
    normalized = normalize_headers(headers)
    if normalized not in SUPPORTED_CALENDAR_HEADERS:
        raise ValueError(
            "Calendar headers must use either the legacy seven-column format or "
            "the current content-package format."
        )
    return normalized


def row_format(headers: Sequence[Any], row: Sequence[Any]) -> str:
    normalized = require_supported_calendar_headers(headers)
    if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
        raise TypeError("row must be a sequence.")
    if len(row) != len(normalized):
        raise ValueError("The content row does not match the calendar headers.")
    return str(row[normalized.index("Format")]).strip()


def is_reel_script_format(value: Any) -> bool:
    return str(value or "").strip().casefold() in REEL_SCRIPT_FORMATS


def revision_fields_for_row(
    headers: Sequence[Any], row: Sequence[Any]
) -> tuple[str, ...]:
    normalized = require_supported_calendar_headers(headers)
    available = [field for field in REVISION_FIELDS if field in normalized]
    if "Reel Script" in available and not is_reel_script_format(row_format(normalized, row)):
        available.remove("Reel Script")
    return tuple(available)


def revision_fields_for_rows(
    headers: Sequence[Any], rows: Sequence[Sequence[Any]]
) -> tuple[str, ...]:
    normalized = require_supported_calendar_headers(headers)
    requested: set[str] = set()
    for row in rows:
        requested.update(revision_fields_for_row(normalized, row))
    return tuple(field for field in REVISION_FIELDS if field in requested)


def normalize_generated_content_row(
    row: Sequence[Any], *, date_label: str
) -> list[str]:
    """Normalize one nine-column AI row and append app-controlled status."""

    if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
        raise TypeError("Generated content row must be a sequence.")
    if len(row) != len(GENERATION_HEADERS):
        raise ValueError(
            f"A generated content row must have {len(GENERATION_HEADERS)} columns."
        )

    cleaned = [
        re.sub(r"\s+", " ", str(value)).strip().replace("|", "/")
        for value in row
    ]
    if any(not value for value in cleaned):
        raise ValueError("Generated content-package cells must not be blank.")

    cleaned[0] = str(date_label).strip()
    format_index = GENERATION_HEADERS.index("Format")
    caption_index = GENERATION_HEADERS.index("Caption")
    script_index = GENERATION_HEADERS.index("Reel Script")

    if not cleaned[caption_index]:
        raise ValueError("Every post requires a Caption.")

    if is_reel_script_format(cleaned[format_index]):
        invalid_scripts = {"n/a", "na", "-", REEL_SCRIPT_NOT_APPLICABLE.casefold()}
        if cleaned[script_index].casefold() in invalid_scripts:
            raise ValueError("Reel and Video posts require a usable Reel Script.")
    else:
        cleaned[script_index] = REEL_SCRIPT_NOT_APPLICABLE

    return cleaned + [CONTENT_STATUS_READY]


def apply_content_status(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    status: str,
    *,
    week_heading_prefix: str,
) -> list[list[str]]:
    """Return a display/export copy with a derived status; stored rows stay immutable."""

    normalized = require_supported_calendar_headers(headers)
    clean_status = str(status or "").strip()
    if not clean_status:
        raise ValueError("Content status must not be empty.")

    result: list[list[str]] = []
    status_index = (
        normalized.index("Content Status") if "Content Status" in normalized else None
    )
    for raw_row in rows:
        row = [str(value) for value in raw_row]
        if len(row) == 1 and row[0].startswith(week_heading_prefix):
            result.append(row)
            continue
        if len(row) != len(normalized):
            raise ValueError("A calendar content row has an unexpected column count.")
        if status_index is not None:
            row[status_index] = clean_status
        result.append(row)
    return result
