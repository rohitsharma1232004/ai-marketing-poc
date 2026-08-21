"""Brand-aware wrapper around the stable design-brief prompt builder."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from brand_kit import brand_kit_prompt_context
from design_brief import build_design_brief_prompt


def build_brand_aware_design_brief_prompt(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    *,
    week_heading_prefix: str,
    client_metadata: Mapping[str, Any] | None = None,
    campaign_intake: Mapping[str, Any] | None = None,
    brand_kit: Mapping[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Append trusted Brand Kit direction without changing approved content."""

    prompt, posts = build_design_brief_prompt(
        headers,
        rows,
        week_heading_prefix=week_heading_prefix,
        client_metadata=client_metadata,
        campaign_intake=campaign_intake,
    )
    brand_context = brand_kit_prompt_context(brand_kit)
    if not brand_context:
        return prompt, posts
    prompt += (
        "\n\nCLIENT BRAND KIT (trusted visual constraints):\n"
        + brand_context
        + "\n\nBrand Kit rules:\n"
        "- Use these details only for visual direction, hierarchy, palette, typography preference, and brand placement.\n"
        "- Do not convert Brand Kit notes into new marketing claims.\n"
        "- If the Brand Kit conflicts with approved content, preserve approved content and use the Brand Kit only for styling.\n"
        "- Never invent or redraw the client logo."
    )
    return prompt, posts
