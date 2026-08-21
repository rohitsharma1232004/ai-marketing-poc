"""Pure helpers for versioned, field-level calendar revisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from content_package import (
    CONTENT_PACKAGE_HEADERS,
    LEGACY_CALENDAR_HEADERS,
    REEL_SCRIPT_FORMATS,
    REVISION_FIELDS,
    require_supported_calendar_headers,
)


def list_reviewable_posts(
    rows: Sequence[Sequence[Any]],
    *,
    week_heading_prefix: str,
    expected_columns: int | None = None,
) -> list[dict[str, Any]]:
    """Return stable row positions for actual posts, excluding week headings."""

    allowed_counts = (
        {int(expected_columns)}
        if expected_columns is not None
        else {len(LEGACY_CALENDAR_HEADERS), len(CONTENT_PACKAGE_HEADERS)}
    )
    posts: list[dict[str, Any]] = []
    post_number = 0
    for row_index, raw_row in enumerate(rows):
        row = [str(value) for value in raw_row]
        if len(row) == 1 and row[0].startswith(week_heading_prefix):
            continue
        if len(row) not in allowed_counts:
            raise ValueError("A calendar content row has an unexpected column count.")
        post_number += 1
        idea = row[4].strip() if len(row) > 4 else ""
        short_idea = idea if len(idea) <= 72 else idea[:69].rstrip() + "..."
        posts.append(
            {
                "post_number": post_number,
                "row_index": row_index,
                "row": row,
                "label": f"Post {post_number}: {row[0]} | {row[1]} | {short_idea}",
            }
        )
    return posts


def normalize_revision_fields(fields: Sequence[str]) -> tuple[str, ...]:
    """Validate and preserve the canonical order of editable content fields."""

    requested = {str(value).strip() for value in fields if str(value).strip()}
    unknown = requested.difference(REVISION_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported revision field(s): {', '.join(sorted(unknown))}.")
    ordered = tuple(field for field in REVISION_FIELDS if field in requested)
    if not ordered:
        raise ValueError("Select at least one field to regenerate.")
    return ordered


def build_field_revision_prompt(
    *,
    headers: Sequence[str],
    current_rows: Sequence[Sequence[Any]],
    fields_to_change: Sequence[str],
    senior_feedback: str,
    user_instructions: str = "",
    client_metadata: Mapping[str, Any] | None = None,
    campaign_intake: Mapping[str, Any] | None = None,
) -> str:
    """Build a prompt that may change only explicitly requested content fields."""

    header_values = list(require_supported_calendar_headers(headers))
    row_values = [[str(value).strip() for value in row] for row in current_rows]
    if not row_values or any(len(row) != len(header_values) for row in row_values):
        raise ValueError("Field-level revision rows must match the calendar headers.")

    selected_fields = normalize_revision_fields(fields_to_change)
    unavailable = [field for field in selected_fields if field not in header_values]
    if unavailable:
        raise ValueError(
            "This calendar version does not contain: " + ", ".join(unavailable) + "."
        )

    feedback = str(senior_feedback or "").strip()
    if not feedback:
        raise ValueError("Senior required-changes description is required.")
    additional = str(user_instructions or "").strip()

    client = dict(client_metadata or {})
    intake = dict(campaign_intake or {})
    client_context = {
        "client_name": client.get("client_name", ""),
        "business": client.get("business", ""),
        "location": client.get("location", ""),
        "audience": client.get("audience", ""),
        "platforms": client.get("platforms", ""),
        "tone": client.get("tone", ""),
        "goal": client.get("goal", intake.get("goal", "")),
        "language": intake.get("language", "English"),
    }
    context_lines = "\n".join(
        f"- {key.replace('_', ' ').title()}: {value or 'Not provided'}"
        for key, value in client_context.items()
    )
    current_table_lines = [
        "| " + " | ".join(header_values) + " |",
        "| " + " | ".join(["---"] * len(header_values)) + " |",
    ]
    current_table_lines.extend(
        "| " + " | ".join(value.replace("|", "/") for value in row) + " |"
        for row in row_values
    )
    current_table = "\n".join(current_table_lines)
    selected_text = ", ".join(selected_fields)
    protected_fields = [field for field in header_values if field not in selected_fields]
    additional_section = additional or "No additional team instruction."
    exact_header = " | ".join(header_values)

    format_rules = ""
    if "Caption" in selected_fields:
        format_rules += (
            "\n- Caption must remain publish-ready, natural for the platform, and consistent "
            "with the approved idea, CTA, audience, tone, and language."
        )
    if "Reel Script" in selected_fields:
        format_rules += (
            "\n- Change Reel Script only for rows whose Format is Reel or Video. For every "
            "other format, copy the existing Reel Script value exactly."
            "\n- Keep Reel/Video scripts concise and production-friendly with a hook, short "
            "scene/beat sequence, and closing CTA in one table cell."
        )

    return f"""
Revise the content-package row(s) below using the Senior's required changes.

Client context:
{context_lines}

Fields that are ALLOWED to change:
{selected_text}

Senior required changes:
{feedback}

Additional instruction from the marketing team:
{additional_section}

Current row(s):
{current_table}

Return exactly one Markdown table with this exact header:
{exact_header}

Rules:
- Return exactly {len(row_values)} content row(s), in the same order as the input.
- Change ONLY these field(s): {selected_text}.
- Treat these fields as immutable: {', '.join(protected_fields)}.
- Even if the model thinks another field would be better, copy every immutable field exactly.
- Address the Senior's required changes first; use the team's additional instruction only as an add-on and never to contradict the Senior.
- Do not invent offers, prices, statistics, testimonials, property details, or unsupported claims.
- Keep the revision concise and suitable for the stated audience, tone, goal, and language.
- Do not use the `|` character inside a table cell.
- Return no commentary before or after the table.{format_rules}
""".strip()


def _infer_headers_for_target_rows(
    rows: Sequence[Sequence[Any]], target_row_indices: Sequence[int]
) -> tuple[str, ...]:
    lengths = {
        len(rows[index])
        for index in target_row_indices
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rows)
    }
    if lengths == {len(LEGACY_CALENDAR_HEADERS)}:
        return LEGACY_CALENDAR_HEADERS
    if lengths == {len(CONTENT_PACKAGE_HEADERS)}:
        return CONTENT_PACKAGE_HEADERS
    raise ValueError("Could not infer a supported calendar format for the revision.")


def merge_revised_fields(
    rows: Sequence[Sequence[Any]],
    *,
    target_row_indices: Sequence[int],
    revised_rows: Sequence[Sequence[Any]],
    fields_to_change: Sequence[str],
    headers: Sequence[str] | None = None,
    expected_columns: int | None = None,
) -> list[list[str]]:
    """Merge only requested fields into targeted posts and preserve everything else."""

    selected_fields = normalize_revision_fields(fields_to_change)
    target_indices = list(target_row_indices)
    replacements = list(revised_rows)
    if not target_indices:
        raise ValueError("At least one target post is required.")
    if len(target_indices) != len(replacements):
        raise ValueError("Regenerated row count does not match the requested target posts.")
    if len(set(target_indices)) != len(target_indices):
        raise ValueError("Target post rows must be unique.")

    for row_index in target_indices:
        if not isinstance(row_index, int) or isinstance(row_index, bool):
            raise TypeError("target row indices must be integers.")
        if row_index < 0 or row_index >= len(rows):
            raise ValueError("A target row index is outside the calendar.")

    header_values = (
        require_supported_calendar_headers(headers)
        if headers is not None
        else _infer_headers_for_target_rows(rows, target_indices)
    )
    if expected_columns is not None and int(expected_columns) != len(header_values):
        raise ValueError("expected_columns does not match the calendar headers.")
    unavailable = [field for field in selected_fields if field not in header_values]
    if unavailable:
        raise ValueError(
            "This calendar version does not contain: " + ", ".join(unavailable) + "."
        )

    field_indices = {field: header_values.index(field) for field in selected_fields}
    format_index = header_values.index("Format")
    merged = [list(map(str, row)) for row in rows]
    for row_index, raw_replacement in zip(target_indices, replacements):
        original = [str(value).strip() for value in merged[row_index]]
        replacement = [str(value).strip().replace("|", "/") for value in raw_replacement]
        if len(original) != len(header_values) or len(replacement) != len(header_values):
            raise ValueError("Field-level revision rows must match the calendar headers.")
        for field in selected_fields:
            if (
                field == "Reel Script"
                and original[format_index].strip().casefold() not in REEL_SCRIPT_FORMATS
            ):
                continue
            index = field_indices[field]
            if not replacement[index]:
                raise ValueError(f"The regenerated {field} field must not be empty.")
            original[index] = replacement[index]
        merged[row_index] = original
    return merged


def build_selected_post_revision_prompt(
    *,
    headers: Sequence[str],
    current_row: Sequence[Any],
    senior_feedback: str,
    client_metadata: Mapping[str, Any] | None = None,
    campaign_intake: Mapping[str, Any] | None = None,
) -> str:
    """Backward-compatible wrapper that revises all fields present in that version."""

    header_values = require_supported_calendar_headers(headers)
    fields = [field for field in REVISION_FIELDS if field in header_values]
    return build_field_revision_prompt(
        headers=header_values,
        current_rows=[current_row],
        fields_to_change=fields,
        senior_feedback=senior_feedback,
        client_metadata=client_metadata,
        campaign_intake=campaign_intake,
    )


def merge_revised_post(
    rows: Sequence[Sequence[Any]],
    *,
    row_index: int,
    revised_row: Sequence[Any],
    expected_columns: int = 7,
    preserved_columns: int = 4,
) -> list[list[str]]:
    """Backward-compatible selected-post merge used by older tests/callers."""

    if preserved_columns != 4:
        raise ValueError("Only the first four calendar columns may be preserved here.")
    if expected_columns == len(LEGACY_CALENDAR_HEADERS):
        headers = LEGACY_CALENDAR_HEADERS
    elif expected_columns == len(CONTENT_PACKAGE_HEADERS):
        headers = CONTENT_PACKAGE_HEADERS
    else:
        raise ValueError("Unsupported expected column count.")
    fields = [field for field in REVISION_FIELDS if field in headers]
    return merge_revised_fields(
        rows,
        target_row_indices=[row_index],
        revised_rows=[revised_row],
        fields_to_change=fields,
        headers=headers,
        expected_columns=expected_columns,
    )
