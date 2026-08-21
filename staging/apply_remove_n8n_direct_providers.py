"""Remove n8n from the active local Streamlit generation path.

This patch is designed for the already-transformed local app that has Gemini,
Brand Kit, Creative Studio, and Senior Design Approval applied.

After this patch the UI accepts only direct providers:
    - groq
    - gemini

The low-level historical provider module may still contain legacy n8n code for
repository history/backward compatibility, but the application cannot select,
configure, or call n8n.

Run from repository root:
    python staging/apply_remove_n8n_direct_providers.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def _remove_n8n_elif_blocks(lines: list[str]) -> list[str]:
    """Drop complete `elif <provider> == "n8n"` validation blocks safely."""
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("elif ") and '== "n8n"' in stripped:
            index += 1
            while index < len(lines):
                candidate = lines[index]
                candidate_stripped = candidate.lstrip()
                candidate_indent = len(candidate) - len(candidate_stripped)
                if (
                    candidate_indent == indent
                    and (
                        candidate_stripped.startswith("elif ")
                        or candidate_stripped.startswith("else:")
                    )
                ):
                    break
                index += 1
            continue
        output.append(line)
        index += 1
    return output


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")

    # Provider allow-lists used in initial generation, design brief generation,
    # and content revision generation.
    text = text.replace(
        'not in {"groq", "gemini", "n8n"}',
        'not in {"groq", "gemini"}',
    )
    text = text.replace(
        "CALENDAR_GENERATION_PROVIDER must be 'groq', 'gemini', or 'n8n'.",
        "CALENDAR_GENERATION_PROVIDER must be 'groq' or 'gemini'.",
    )
    text = text.replace(
        "CALENDAR_GENERATION_PROVIDER must be either 'groq' or 'n8n'.",
        "CALENDAR_GENERATION_PROVIDER must be 'groq' or 'gemini'.",
    )

    # Human-readable provider labels.
    text = text.replace(
        '{"groq": "Groq", "gemini": "Gemini", "n8n": "n8n"}',
        '{"groq": "Groq", "gemini": "Gemini"}',
    )
    text = text.replace(
        'generation_label = "n8n" if generation_provider == "n8n" else "Groq"',
        'generation_label = "Groq"',
    )

    lines = text.splitlines(keepends=True)

    # Remove n8n setting reads and call arguments. These names cover the main,
    # design-brief, and revision paths created by the Gemini provider patch.
    drop_fragments = (
        'n8n_webhook_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")',
        'n8n_webhook_secret = get_app_setting("N8N_WEBHOOK_SECRET")',
        'brief_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")',
        'brief_n8n_secret = get_app_setting("N8N_WEBHOOK_SECRET")',
        'revision_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")',
        'revision_n8n_secret = get_app_setting("N8N_WEBHOOK_SECRET")',
        'n8n_webhook_url=n8n_webhook_url,',
        'n8n_webhook_secret=n8n_webhook_secret,',
        'n8n_webhook_url=brief_n8n_url,',
        'n8n_webhook_secret=brief_n8n_secret,',
        'n8n_webhook_url=revision_n8n_url,',
        'n8n_webhook_secret=revision_n8n_secret,',
        '"N8N_TIMEOUT",',
    )
    lines = [
        line for line in lines
        if not any(fragment in line for fragment in drop_fragments)
    ]
    lines = _remove_n8n_elif_blocks(lines)
    text = "".join(lines)

    # A transformed app should now have no active n8n provider branch/call.
    forbidden = (
        'generation_provider == "n8n"',
        'brief_provider == "n8n"',
        'revision_provider == "n8n"',
        'n8n_webhook_url=',
        'n8n_webhook_secret=',
        'not in {"groq", "gemini", "n8n"}',
    )
    leftovers = [item for item in forbidden if item in text]
    if leftovers:
        raise RuntimeError(
            "n8n cleanup incomplete; active references remain: " + ", ".join(leftovers)
        )

    APP_PATH.write_text(text, encoding="utf-8")
    print("Removed n8n from active app generation. Direct Groq/Gemini only.")


if __name__ == "__main__":
    main()
