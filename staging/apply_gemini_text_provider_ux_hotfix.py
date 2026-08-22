"""Polish provider-neutral UI text after Gemini text support is applied.

Run from repository root after apply_gemini_text_provider.py:
    python staging/apply_gemini_text_provider_ux_hotfix.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "Gemini AI" in text and "AI provider returned" in text:
        print("app.py Gemini provider UX hotfix already applied")
        return
    if "from generation_router import generate_calendar_content" not in text:
        raise RuntimeError("Gemini text provider patch must be applied first.")

    old_label = 'provider_label = "n8n Automation" if configured_provider == "n8n" else "Groq Cloud AI"\n'
    new_label = '''provider_label = {
    "groq": "Groq Cloud AI",
    "gemini": "Gemini AI",
    "n8n": "n8n Automation",
}.get(configured_provider, "Configured AI")
'''
    count = text.count(old_label)
    if count != 1:
        raise RuntimeError(f"provider label: expected one anchor, found {count}.")
    text = text.replace(old_label, new_label, 1)

    text = text.replace(
        'f"Groq returned {len(model_rows)} content rows, but "',
        'f"AI provider returned {len(model_rows)} content rows, but "',
    )
    text = text.replace(
        "Only extracted text is sent to Groq; upload material you are allowed to share.",
        "Only extracted text is sent to the configured AI provider; upload material you are allowed to share.",
    )

    APP_PATH.write_text(text, encoding="utf-8")
    print("fixed Gemini provider labels and provider-neutral generation text")


if __name__ == "__main__":
    main()
