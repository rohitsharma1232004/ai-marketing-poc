from io import BytesIO

import pytest
from PIL import Image

from publishing_media import prepare_image_for_approved_platforms


def _png_bytes(mode="RGBA"):
    image = Image.new(mode, (20, 20), (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_instagram_image_is_converted_to_jpeg_before_approval():
    result = prepare_image_for_approved_platforms(
        file_bytes=_png_bytes(),
        mime_type="image/png",
        file_name="creative.png",
        approved_platform_text="Instagram and Facebook",
        format_name="Image",
    )
    assert result.converted is True
    assert result.mime_type == "image/jpeg"
    assert result.file_name == "creative.jpg"
    assert result.file_bytes.startswith(b"\xff\xd8\xff")


def test_facebook_only_png_is_left_byte_identical():
    raw = _png_bytes("RGB")
    result = prepare_image_for_approved_platforms(
        file_bytes=raw,
        mime_type="image/png",
        file_name="creative.png",
        approved_platform_text="Facebook",
        format_name="Image",
    )
    assert result.converted is False
    assert result.file_bytes == raw
    assert result.mime_type == "image/png"


def test_non_image_format_is_not_silently_changed():
    raw = b"not-an-image-because-carousel-pipeline-is-separate"
    result = prepare_image_for_approved_platforms(
        file_bytes=raw,
        mime_type="application/pdf",
        file_name="carousel.pdf",
        approved_platform_text="Instagram",
        format_name="Carousel",
    )
    assert result.converted is False
    assert result.file_bytes == raw


def test_instagram_image_rejects_non_convertible_design_proof():
    with pytest.raises(ValueError, match="PNG/JPEG/WebP"):
        prepare_image_for_approved_platforms(
            file_bytes=b"%PDF-1.4 proof",
            mime_type="application/pdf",
            file_name="proof.pdf",
            approved_platform_text="Instagram",
            format_name="Image",
        )
