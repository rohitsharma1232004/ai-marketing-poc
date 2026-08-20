"""Pure helpers for versioned, field-level calendar revisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

REVISION_FIELDS = (
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
)
_FIELD_INDEX = {name: index for index, name in enumerate(
    ("Date", "Platform", "Pillar", "Format", *REVISION_FIELDS)
)}


def list_reviewable_posts(
    rows: Sequence[Sequence[Any]],
    *,
    week_heading_prefix: str,
    expected_columns: int = 7,
) -> list[dict[str, Any]]:
    """Return stable row positions for actual posts, excluding week headings."""

    posts: list[dict[str, Any]] = []
    post_number = 0
    for row_index, raw_row in enumerate(rows):
        row = [str(value) for value in raw_row]
        if len(row) == 1 and row[0].startswith(week_heading_prefix):
            continue
        if len(row) != expected_columns:
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

    header_values = [str(value).strip() for value in headers]
    if len(header_values) != 7:
        raise ValueError("Field-level revision requires the seven-column calendar format.")

    row_values = [[str(value).strip() for value in row] for row in current_rows]
    if not row_values or any(len(row) != 7 for row in row_values):
        raise ValueError("Field-level revision requires one or more seven-column posts.")

    selected_fields = normalize_revision_fields(fields_to_change)
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

    return f"""
Revise the content-calendar row(s) below using the Senior's required changes.

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
Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA

Rules:
- Return exactly {len(row_values)} content row(s), in the same order as the input.
- Change ONLY these field(s): {selected_text}.
- Treat these fields as immutable: {', '.join(protected_fields)}.
- Even if the model thinks another field would be better, copy every immutable field exactly.
- Address the Senior's required changes first; use the team's additional instruction only as an add-on and never to contradict the Senior.
- Do not invent offers, prices, statistics, testimonials, property details, or unsupported claims.
- Keep the revision concise and suitable for the stated audience, tone, goal, and language.
- Do not use the `|` character inside a table cell.
- Return no commentary before or after the table.
""".strip()


def merge_revised_fields(
    rows: Sequence[Sequence[Any]],
    *,
    target_row_indices: Sequence[int],
    revised_rows: Sequence[Sequence[Any]],
    fields_to_change: Sequence[str],
    expected_columns: int = 7,
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

    merged = [list(map(str, row)) for row in rows]
    for row_index, raw_replacement in zip(target_indices, replacements):
        if not isinstance(row_index, int) or isinstance(row_index, bool):
            raise TypeError("target row indices must be integers.")
        if row_index < 0 or row_index >= len(merged):
            raise ValueError("A target row index is outside the calendar.")
        original = [str(value).strip() for value in merged[row_index]]
        replacement = [str(value).strip().replace("|", "/") for value in raw_replacement]
        if len(original) != expected_columns or len(replacement) != expected_columns:
            raise ValueError("Field-level revision requires seven columns.")
        for field in selected_fields:
            index = _FIELD_INDEX[field]
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
    """Backward-compatible wrapper that revises all three content fields."""

    return build_field_revision_prompt(
        headers=headers,
        current_rows=[current_row],
        fields_to_change=REVISION_FIELDS,
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
    return merge_revised_fields(
        rows,
        target_row_indices=[row_index],
        revised_rows=[revised_row],
        fields_to_change=REVISION_FIELDS,
        expected_columns=expected_columns,
    )
