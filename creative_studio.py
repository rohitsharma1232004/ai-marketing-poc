"""Provider-neutral creative-studio helpers.

This layer keeps the app workflow independent of Gemini, Canva, Adobe, or a
manual designer. Provider-specific network code lives elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from brand_kit import brand_kit_prompt_context, normalize_brand_kit
from creative_workflow import build_ai_design_prompt

MAX_CREATIVE_GENERATION_PROMPT_CHARS = 12_000

CREATIVE_METHODS = (
    "gemini",
    "canva",
    "adobe",
    "manual_upload",
)

PROVIDER_CAPABILITIES = {
    "gemini": {
        "label": "Gemini AI",
        "available_in_app": True,
        "purpose": "Native prompt-to-image generation and image revision.",
        "credential": "GEMINI_API_KEY",
    },
    "canva": {
        "label": "Canva",
        "available_in_app": False,
        "purpose": "Template/design creation, editing handoff, autofill, and export through Canva Connect APIs.",
        "credential": "Canva OAuth connection",
    },
    "adobe": {
        "label": "Adobe / Photoshop",
        "available_in_app": False,
        "purpose": "Professional finishing and production automation through Firefly Services / Photoshop API v2.",
        "credential": "Adobe Firefly Services enterprise credentials",
    },
    "manual_upload": {
        "label": "Manual Upload",
        "available_in_app": True,
        "purpose": "Upload a finished creative made in any external design tool.",
        "credential": "None",
    },
}


def _require_prompt_size(prompt: str, label: str) -> str:
    value = str(prompt or "").strip()
    if not value:
        raise ValueError(f"{label} must not be empty.")
    if len(value) > MAX_CREATIVE_GENERATION_PROMPT_CHARS:
        raise ValueError(
            f"{label} is too large for the creative-generation pipeline. "
            "Reduce Brand Kit or design-brief detail."
        )
    return value


def recommended_aspect_ratio(format_name: str, platform: str = "") -> str:
    """Return a practical default ratio while allowing later UI override."""

    fmt = str(format_name or "").strip().casefold()
    platform_key = str(platform or "").strip().casefold()
    if fmt in {"reel", "video", "story"}:
        return "9:16"
    if "youtube" in platform_key:
        return "16:9"
    if fmt in {"image", "carousel"}:
        return "4:5"
    return "1:1"


def build_branded_design_prompt(
    brief: Mapping[str, Any],
    approved_post: Mapping[str, Any],
    *,
    client_metadata: Mapping[str, Any] | None = None,
    brand_kit: Mapping[str, Any] | None = None,
    generation_mode: str = "full_creative",
) -> str:
    """Build a brand-aware creative prompt without changing approved content."""

    base = build_ai_design_prompt(
        brief,
        approved_post,
        client_metadata=client_metadata,
    )
    kit = normalize_brand_kit(brand_kit)
    brand_context = brand_kit_prompt_context(kit)
    mode = str(generation_mode or "").strip().lower()
    if mode not in {"full_creative", "visual_only"}:
        raise ValueError("generation_mode must be 'full_creative' or 'visual_only'.")

    content = dict(approved_post.get("content") or {})
    approved_visible_copy = [
        str(content.get(field) or "").strip()
        for field in ("Content Idea", "CTA")
        if str(content.get(field) or "").strip()
    ]

    extra = [
        "",
        "BRAND KIT:",
        brand_context
        or "- No saved Brand Kit. Keep the visual neutral and do not invent brand identity.",
        "",
        "PRODUCTION SAFETY:",
        "- Never invent, redraw, or imitate a client logo. If a logo is available, leave a clean placement zone for the real logo asset.",
        "- Never add phone numbers, URLs, prices, offers, addresses, ratings, statistics, or claims unless they appear in the approved content.",
        "- Preserve spelling of every approved word exactly.",
        "- Avoid tiny text and crowded layouts.",
    ]
    if mode == "visual_only":
        extra.extend(
            [
                "- Generate the visual/background composition only.",
                "- Do not render the logo, headline, CTA, caption, or other marketing copy into the image.",
                "- Reserve clear negative space where approved text and the real logo can be overlaid later.",
            ]
        )
    else:
        extra.extend(
            [
                "- This is a full-creative draft. Only use approved consumer-facing copy; do not paraphrase it.",
                "- If text rendering is uncertain, prefer fewer words and keep the exact approved headline/CTA rather than inventing alternatives.",
            ]
        )
        if approved_visible_copy:
            extra.append(
                "- Approved visible-copy source: " + " | ".join(approved_visible_copy)
            )
    return _require_prompt_size(
        (base + "\n" + "\n".join(extra)).strip(), "Creative generation prompt"
    )


def build_design_revision_prompt(
    *,
    original_prompt: str,
    senior_feedback: str,
    change_fields: Sequence[str],
    approved_post: Mapping[str, Any],
    brand_kit: Mapping[str, Any] | None = None,
) -> str:
    """Create a constrained image-revision prompt for a rejected creative.

    Senior feedback and immutable approved content are always kept in full. The
    prior creative prompt is supporting context only and is clipped when needed
    so revisions remain within the same 12k boundary used by Gemini and storage.
    """

    feedback = str(senior_feedback or "").strip()
    fields = [str(item).strip() for item in change_fields if str(item).strip()]
    if not feedback or not fields:
        raise ValueError("Senior feedback and at least one change area are required.")
    if len(feedback) > 5000:
        raise ValueError("Senior feedback is too long.")

    content = dict(approved_post.get("content") or {})
    immutable = []
    for field in (
        "Platform",
        "Pillar",
        "Format",
        "Content Idea",
        "CTA",
        "Caption",
        "Reel Script",
    ):
        value = str(content.get(field) or "").strip()
        if value:
            immutable.append(f"- {field}: {value}")
    brand_context = brand_kit_prompt_context(brand_kit)

    prefix = "\n".join(
        [
            "Revise the previous creative using ONLY the Senior-requested design changes below.",
            "Do not rewrite or reinterpret the approved marketing content.",
            "",
            "SENIOR CHANGE AREAS:",
            "- " + "\n- ".join(fields),
            "",
            "SENIOR FEEDBACK:",
            feedback,
            "",
            "IMMUTABLE APPROVED CONTENT:",
            *immutable,
            "",
            "BRAND KIT:",
            brand_context or "- No saved Brand Kit. Do not invent a brand identity.",
            "",
            "ORIGINAL CREATIVE PROMPT:",
        ]
    )
    suffix = "\n".join(
        [
            "",
            "REVISION RULES:",
            "- Change only what is necessary to satisfy the Senior feedback.",
            "- Preserve every approved claim, CTA, message, platform, and format.",
            "- Never invent or redraw the client logo.",
            "- Return one revised production-ready visual.",
        ]
    )
    original = str(original_prompt or "").strip()
    fixed_length = len(prefix) + len(suffix) + 2
    available = MAX_CREATIVE_GENERATION_PROMPT_CHARS - fixed_length
    if available < 200:
        raise ValueError(
            "Senior feedback, approved content, and Brand Kit are too large for one revision request."
        )
    if len(original) > available:
        marker = "\n[Earlier creative prompt clipped to fit the revision request.]"
        keep = max(0, available - len(marker))
        original = original[:keep].rstrip() + marker
    return _require_prompt_size(
        f"{prefix}\n{original}\n{suffix}".strip(), "Creative revision prompt"
    )


def generated_image_extension(mime_type: str) -> str:
    mime = str(mime_type or "").strip().lower()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    if mime not in mapping:
        raise ValueError("Unsupported generated image MIME type.")
    return mapping[mime]


def provider_capability(provider: str) -> dict[str, Any]:
    key = str(provider or "").strip().lower()
    if key not in PROVIDER_CAPABILITIES:
        raise ValueError("Unknown creative provider.")
    return dict(PROVIDER_CAPABILITIES[key])
