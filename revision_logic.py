"""Pure helpers for versioned, selected-post calendar revisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


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


def build_selected_post_revision_prompt(
    *,
    headers: Sequence[str],
    current_row: Sequence[Any],
    senior_feedback: str,
    client_metadata: Mapping[str, Any] | None = None,
    campaign_intake: Mapping[str, Any] | None = None,
) -> str:
    """Build a narrow prompt that rewrites content without changing schedule/mix."""

    header_values = [str(value).strip() for value in headers]
    row_values = [str(value).strip() for value in current_row]
    if len(header_values) != 7 or len(row_values) != 7:
        raise ValueError("Selected-post revision requires the seven-column calendar format.")

    feedback = str(senior_feedback or "").strip()
    if not feedback:
        raise ValueError("Senior feedback is required before regenerating a post.")

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
    current_table = (
        "| " + " | ".join(header_values) + " |\n"
        + "| " + " | ".join(["---"] * len(header_values)) + " |\n"
        + "| " + " | ".join(value.replace("|", "/") for value in row_values) + " |"
    )

    return f"""
Revise exactly ONE content-calendar post using the Senior review feedback below.

Client context:
{context_lines}

Senior feedback:
{feedback}

Current post:
{current_table}

Return exactly one Markdown table with this exact header:
Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA

Rules:
- Keep Date, Platform, Pillar, and Format unchanged from the current post.
- Rewrite only Content Idea, SEO Keyword Focus, and CTA so the Senior feedback is addressed.
- Do not invent offers, prices, statistics, testimonials, property details, or unsupported claims.
- Keep the revision concise and suitable for the stated audience, tone, goal, and language.
- Do not use the `|` character inside a table cell.
- Return exactly one content row and no commentary before or after the table.
""".strip()


def merge_revised_post(
    rows: Sequence[Sequence[Any]],
    *,
    row_index: int,
    revised_row: Sequence[Any],
    expected_columns: int = 7,
    preserved_columns: int = 4,
) -> list[list[str]]:
    """Replace one post while preserving its schedule/mix columns."""

    if not isinstance(row_index, int) or isinstance(row_index, bool):
        raise TypeError("row_index must be an integer.")
    if row_index < 0 or row_index >= len(rows):
        raise ValueError("row_index is outside the calendar.")

    original = [str(value).strip() for value in rows[row_index]]
    replacement = [str(value).strip().replace("|", "/") for value in revised_row]
    if len(original) != expected_columns or len(replacement) != expected_columns:
        raise ValueError("Selected-post revision requires seven columns.")
    if any(not value for value in replacement[preserved_columns:]):
        raise ValueError("The regenerated content fields must not be empty.")

    merged = [list(map(str, row)) for row in rows]
    merged[row_index] = original[:preserved_columns] + replacement[preserved_columns:]
    return merged
