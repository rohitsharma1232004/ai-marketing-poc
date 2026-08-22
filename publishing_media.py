"""Prepare creative image bytes before Senior Design Approval.

Instagram's current image publishing flow accepts JPEG images. Gemini normally
returns PNG, so an Instagram-bound Image creative must be converted *before* it
is saved/versioned and sent for Senior Design Approval. This module performs
that deterministic conversion so the exact JPEG bytes/hash the Senior approves
are the exact bytes later published.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from publishing_workflow import platform_is_allowed

MAX_PREPARED_IMAGE_BYTES = 12 * 1024 * 1024
SUPPORTED_INPUT_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)


@dataclass(frozen=True)
class PreparedCreative:
    file_bytes: bytes
    mime_type: str
    file_name: str
    converted: bool
    note: str = ""


def _jpeg_name(file_name: str) -> str:
    name = Path(str(file_name or "creative").strip()).name or "creative"
    stem = Path(name).stem.strip() or "creative"
    return f"{stem}.jpg"


def _convert_to_jpeg(file_bytes: bytes) -> bytes:
    try:
        with Image.open(BytesIO(file_bytes)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=95,
                optimize=True,
                progressive=False,
            )
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("Creative image could not be converted to publishing-ready JPEG.") from error
    result = output.getvalue()
    if not result or len(result) > MAX_PREPARED_IMAGE_BYTES:
        raise ValueError("Publishing-ready JPEG is empty or larger than 12 MB.")
    return result


def prepare_image_for_approved_platforms(
    *,
    file_bytes: bytes,
    mime_type: str,
    file_name: str,
    approved_platform_text: str,
    format_name: str,
) -> PreparedCreative:
    """Return bytes that are safe to save as the Senior-review creative version.

    Only single Image posts are normalized here. Reel/Video/Carousel assets need
    their own platform media pipeline and are intentionally left untouched.
    """

    if not isinstance(file_bytes, (bytes, bytearray)):
        raise TypeError("file_bytes must be bytes.")
    raw = bytes(file_bytes)
    if not raw or len(raw) > MAX_PREPARED_IMAGE_BYTES:
        raise ValueError("Creative image is empty or larger than 12 MB.")

    clean_mime = str(mime_type or "").strip().lower()
    clean_format = str(format_name or "").strip().casefold()
    clean_name = Path(str(file_name or "creative").strip()).name or "creative"
    if clean_format != "image":
        return PreparedCreative(raw, clean_mime, clean_name, False)

    instagram_target = platform_is_allowed(approved_platform_text, "instagram")
    if not instagram_target:
        return PreparedCreative(raw, clean_mime, clean_name, False)

    if clean_mime == "image/jpeg":
        return PreparedCreative(raw, clean_mime, clean_name, False)
    if clean_mime not in SUPPORTED_INPUT_IMAGE_MIME_TYPES:
        raise ValueError(
            "Instagram Image posts must use PNG/JPEG/WebP source artwork so the exact "
            "Senior-review creative can be made publishing-ready as JPEG."
        )

    jpeg = _convert_to_jpeg(raw)
    return PreparedCreative(
        jpeg,
        "image/jpeg",
        _jpeg_name(clean_name),
        True,
        "Converted to JPEG before Senior Design Approval for Instagram publishing.",
    )
