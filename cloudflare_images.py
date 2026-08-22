"""Small Cloudflare Workers AI image-generation client for the Creative Studio.

The client intentionally supports one stable, free-tier-friendly Cloudflare-hosted
model first: FLUX.1 Schnell. It uses Cloudflare's official REST endpoint and
returns a provider-neutral result that the existing creative approval workflow
can store without changing approved marketing content.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping
from uuid import uuid4

import requests
from PIL import Image

DEFAULT_CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
SUPPORTED_CLOUDFLARE_IMAGE_MODELS = (DEFAULT_CLOUDFLARE_IMAGE_MODEL,)
MAX_CLOUDFLARE_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_CLOUDFLARE_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CLOUDFLARE_PROMPT_CHARS = 2048


@dataclass(frozen=True)
class CloudflareImageResult:
    image_bytes: bytes
    mime_type: str
    request_id: str
    model: str
    aspect_ratio: str
    image_size: str
    steps: int
    width: int
    height: int
    prompt_compacted: bool
    provider_prompt_chars: int


class CloudflareImageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code
        self.retryable = retryable


def _clean_account_id(value: str) -> str:
    account_id = str(value or "").strip()
    if not account_id:
        raise ValueError("CLOUDFLARE_ACCOUNT_ID is missing.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", account_id):
        raise ValueError("CLOUDFLARE_ACCOUNT_ID has an invalid format.")
    return account_id


def _clean_token(value: str, request_id: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise CloudflareImageError(
            "CLOUDFLARE_API_TOKEN is missing from server configuration.",
            request_id=request_id,
            code="CLOUDFLARE_KEY_MISSING",
        )
    return token


def _clean_model(value: str) -> str:
    model = str(value or "").strip()
    if model not in SUPPORTED_CLOUDFLARE_IMAGE_MODELS:
        raise ValueError(
            "Unsupported Cloudflare image model. Use: "
            + ", ".join(SUPPORTED_CLOUDFLARE_IMAGE_MODELS)
        )
    return model


def _safe_error_detail(response: Any, token: str = "") -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    errors = payload.get("errors")
    messages: list[str] = []
    if isinstance(errors, list):
        for item in errors[:3]:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip()
            message = re.sub(r"\s+", " ", str(item.get("message") or "")).strip()
            if code and message:
                messages.append(f"{code}: {message}")
            elif message:
                messages.append(message)
    detail = " | ".join(messages)
    if token and token in detail:
        detail = detail.replace(token, "[redacted]")
    return detail[:600]


def _prepare_prompt(prompt: str, aspect_ratio: str) -> tuple[str, bool]:
    clean = re.sub(r"[ \t]+", " ", str(prompt or "").strip())
    if not clean:
        raise ValueError("Creative prompt must not be empty.")
    ratio = str(aspect_ratio or "").strip() or "4:5"
    ratio_instruction = (
        f"Compose the visual for a {ratio} social-media layout. Keep the main subject, "
        "headline-safe space, brand elements and CTA comfortably inside the frame.\n"
    )
    available = MAX_CLOUDFLARE_PROMPT_CHARS - len(ratio_instruction)
    if available < 200:
        raise ValueError("Internal Cloudflare prompt budget is invalid.")
    if len(clean) <= available:
        return ratio_instruction + clean, False

    # Keep both the concept at the beginning and the constraints/brand rules that
    # usually appear near the end of our provider-neutral design prompt.
    tail_size = min(560, max(240, available // 3))
    head_size = available - tail_size - len("\n[...condensed...]\n")
    compacted = (
        clean[:head_size].rstrip()
        + "\n[...condensed...]\n"
        + clean[-tail_size:].lstrip()
    )
    return ratio_instruction + compacted, True


def generate_image(
    *,
    prompt: str,
    account_id: str,
    api_token: str,
    model: str = DEFAULT_CLOUDFLARE_IMAGE_MODEL,
    aspect_ratio: str = "4:5",
    steps: int = 4,
    request_id: str | None = None,
    http_client: Any = requests,
) -> CloudflareImageResult:
    """Generate one JPEG creative through Cloudflare Workers AI FLUX.1 Schnell.

    Cloudflare's current REST validation for this model rejects ``seed`` even
    though some Workers binding/docs examples still show it. Keep the REST
    payload to the model's accepted prompt + steps schema.
    """

    correlation_id = request_id or str(uuid4())
    clean_account = _clean_account_id(account_id)
    token = _clean_token(api_token, correlation_id)
    chosen_model = _clean_model(model)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 8:
        raise ValueError("Cloudflare FLUX steps must be an integer from 1 to 8.")

    provider_prompt, prompt_compacted = _prepare_prompt(prompt, aspect_ratio)
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{clean_account}/ai/run/{chosen_model}"
    )
    try:
        response = http_client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Request-ID": correlation_id,
            },
            json={
                "prompt": provider_prompt,
                "steps": steps,
            },
            timeout=(5, 180),
        )
    except requests.exceptions.Timeout as error:
        raise CloudflareImageError(
            "Cloudflare Workers AI took too long to respond.",
            request_id=correlation_id,
            code="CLOUDFLARE_TIMEOUT",
            retryable=True,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise CloudflareImageError(
            "Could not connect to Cloudflare Workers AI.",
            request_id=correlation_id,
            code="CLOUDFLARE_CONNECTION_ERROR",
            retryable=True,
        ) from error
    except requests.exceptions.RequestException as error:
        raise CloudflareImageError(
            "The Cloudflare request failed before a valid response was received.",
            request_id=correlation_id,
            code="CLOUDFLARE_REQUEST_ERROR",
            retryable=True,
        ) from error

    body = getattr(response, "content", b"") or b""
    if len(body) > MAX_CLOUDFLARE_RESPONSE_BYTES:
        raise CloudflareImageError(
            "Cloudflare returned a response that is too large.",
            request_id=correlation_id,
            code="CLOUDFLARE_RESPONSE_TOO_LARGE",
        )

    status_code = int(getattr(response, "status_code", 0))
    detail = _safe_error_detail(response, token)
    suffix = f" Cloudflare response: {detail}" if detail else ""
    if status_code in {401, 403}:
        raise CloudflareImageError(
            "Cloudflare rejected the account/token or Workers AI permission." + suffix,
            request_id=correlation_id,
            code="CLOUDFLARE_AUTH_ERROR",
        )
    if status_code == 429:
        raise CloudflareImageError(
            "Cloudflare Workers AI quota or rate limit was reached." + suffix,
            request_id=correlation_id,
            code="CLOUDFLARE_RATE_LIMIT",
            retryable=True,
        )
    if status_code == 400:
        raise CloudflareImageError(
            "Cloudflare rejected the image request parameters." + suffix,
            request_id=correlation_id,
            code="CLOUDFLARE_INVALID_REQUEST",
        )
    if status_code < 200 or status_code >= 300:
        raise CloudflareImageError(
            "Cloudflare Workers AI returned an upstream error." + suffix,
            request_id=correlation_id,
            code="CLOUDFLARE_UPSTREAM_ERROR",
            retryable=status_code >= 500,
        )

    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise CloudflareImageError(
            "Cloudflare returned a non-JSON response.",
            request_id=correlation_id,
            code="CLOUDFLARE_INVALID_JSON",
            retryable=True,
        ) from error
    if not isinstance(payload, Mapping):
        raise CloudflareImageError(
            "Cloudflare returned an invalid response object.",
            request_id=correlation_id,
            code="CLOUDFLARE_INVALID_RESPONSE",
            retryable=True,
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        result = payload
    encoded = result.get("image") if isinstance(result, Mapping) else None
    if not isinstance(encoded, str) or not encoded.strip():
        raise CloudflareImageError(
            "Cloudflare completed without returning an image.",
            request_id=correlation_id,
            code="CLOUDFLARE_IMAGE_MISSING",
            retryable=True,
        )
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as error:
        raise CloudflareImageError(
            "Cloudflare returned invalid base64 image data.",
            request_id=correlation_id,
            code="CLOUDFLARE_IMAGE_DATA",
            retryable=True,
        ) from error
    if not image_bytes or len(image_bytes) > MAX_CLOUDFLARE_IMAGE_BYTES:
        raise CloudflareImageError(
            "Cloudflare returned an empty or oversized image.",
            request_id=correlation_id,
            code="CLOUDFLARE_IMAGE_SIZE",
        )
    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise CloudflareImageError(
            "Cloudflare returned an unexpected image type; JPEG was expected.",
            request_id=correlation_id,
            code="CLOUDFLARE_IMAGE_TYPE",
        )
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
    except OSError as error:
        raise CloudflareImageError(
            "Cloudflare returned an unreadable JPEG image.",
            request_id=correlation_id,
            code="CLOUDFLARE_IMAGE_DATA",
        ) from error

    return CloudflareImageResult(
        image_bytes=image_bytes,
        mime_type="image/jpeg",
        request_id=correlation_id,
        model=chosen_model,
        aspect_ratio=str(aspect_ratio or "4:5"),
        image_size=f"{width}x{height}",
        steps=steps,
        width=width,
        height=height,
        prompt_compacted=prompt_compacted,
        provider_prompt_chars=len(provider_prompt),
    )