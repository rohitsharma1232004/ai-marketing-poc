"""Make Gemini image generation request the MIME type accepted by the endpoint.

The Interactions image endpoint currently rejects ``image/png`` in
``response_format.mime_type`` and reports ``image/jpeg`` as the supported value.
This patch is deliberately narrow: it changes only the requested image output
format and the empty-mime fallback. Approval, hashing, publishing, and Groq
content generation are untouched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEMINI_PATH = ROOT / "gemini_api.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text and old not in text:
        print(f"{label}: already applied")
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    print(f"{label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    text = GEMINI_PATH.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '            "mime_type": "image/png",\n            "aspect_ratio": ratio,\n',
        '            "mime_type": "image/jpeg",\n            "aspect_ratio": ratio,\n',
        "Gemini JPEG response request",
    )
    text = replace_once(
        text,
        '    mime_type = str(image_block.get("mime_type") or "image/png").lower()\n',
        '    mime_type = str(image_block.get("mime_type") or "image/jpeg").lower()\n',
        "Gemini JPEG response fallback",
    )
    GEMINI_PATH.write_text(text, encoding="utf-8")
    print("Gemini JPEG image response format applied.")


if __name__ == "__main__":
    main()
