"""Minimal Supabase Storage uploader for approved social creatives.

The service-role key is accepted only at call time and is never returned,
logged, persisted, or included in exceptions. Object names are content-addressed
so repeated uploads of the same approved creative are deterministic.

This module intentionally uses the Storage REST API through ``requests`` instead
of adding the full Supabase SDK dependency to the Streamlit POC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import requests

MAX_MEDIA_BYTES = 12 * 1024 * 1024
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
SAFE_OBJECT_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")
SUPPORTED_MIME_TYPES = frozenset({"image/jpeg", "image/png"})


@dataclass(frozen=True)
class SupabaseMediaResult:
    object_path: str
    public_url: str
    mime_type: str
    file_size: int


class SupabaseMediaError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    parts = urlsplit(base)
    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not hostname.endswith(".supabase.co")
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "SUPABASE_URL must be the project's standard HTTPS *.supabase.co URL."
        )
    return base


def _service_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise SupabaseMediaError(
            "Supabase Storage service key is missing from server configuration.",
            code="SUPABASE_KEY_MISSING",
        )
    return key


def _bucket(value: str) -> str:
    bucket = str(value or "").strip().lower()
    if not BUCKET_RE.fullmatch(bucket):
        raise ValueError("Supabase media bucket name is invalid.")
    return bucket


def _safe_part(value: str, fallback: str) -> str:
    result = SAFE_OBJECT_PART_RE.sub("_", str(value or "").strip()).strip("._-")
    return (result[:100] or fallback)


def _object_path(
    *,
    campaign_id: str,
    post_number: int,
    creative_asset_id: str,
    creative_hash: str,
    mime_type: str,
) -> str:
    if not isinstance(post_number, int) or isinstance(post_number, bool) or post_number < 1:
        raise ValueError("post_number must be a positive integer.")
    clean_hash = str(creative_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean_hash):
        raise ValueError("creative_hash must be a SHA-256 hex digest.")
    extension = ".jpg" if mime_type == "image/jpeg" else ".png"
    campaign = _safe_part(campaign_id, "campaign")
    asset = _safe_part(creative_asset_id, "asset")
    return (
        f"campaigns/{campaign}/post_{post_number:02d}/"
        f"{asset}_{clean_hash[:16]}{extension}"
    )


def _quoted_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def public_object_url(project_url: str, bucket: str, object_path: str) -> str:
    base = _base_url(project_url)
    clean_bucket = _bucket(bucket)
    return (
        f"{base}/storage/v1/object/public/{quote(clean_bucket, safe='')}/"
        f"{_quoted_path(object_path)}"
    )


def upload_public_creative(
    *,
    project_url: str,
    service_role_key: str,
    bucket: str,
    campaign_id: str,
    post_number: int,
    creative_asset_id: str,
    creative_hash: str,
    file_bytes: bytes,
    mime_type: str,
    http_client: Any = requests,
    verify_public: bool = True,
) -> SupabaseMediaResult:
    """Upload one immutable approved creative and return its public CDN URL.

    The configured bucket must be public. The object path contains the creative
    hash, so overwriting the same path is safe and idempotent for identical bytes.
    """

    if not isinstance(file_bytes, (bytes, bytearray)):
        raise TypeError("file_bytes must be bytes.")
    raw = bytes(file_bytes)
    if not raw or len(raw) > MAX_MEDIA_BYTES:
        raise ValueError("Publishing creative is empty or larger than 12 MB.")
    clean_mime = str(mime_type or "").strip().lower()
    if clean_mime not in SUPPORTED_MIME_TYPES:
        raise ValueError("Supabase publishing media must be JPEG or PNG.")

    base = _base_url(project_url)
    key = _service_key(service_role_key)
    clean_bucket = _bucket(bucket)
    path = _object_path(
        campaign_id=campaign_id,
        post_number=post_number,
        creative_asset_id=creative_asset_id,
        creative_hash=creative_hash,
        mime_type=clean_mime,
    )
    upload_url = (
        f"{base}/storage/v1/object/{quote(clean_bucket, safe='')}/{_quoted_path(path)}"
    )
    try:
        response = http_client.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {key}",
                "apikey": key,
                "Content-Type": clean_mime,
                "x-upsert": "true",
                "cache-control": "3600",
            },
            data=raw,
            timeout=(5, 60),
        )
    except requests.exceptions.Timeout as error:
        raise SupabaseMediaError(
            "Supabase Storage upload timed out.",
            code="SUPABASE_TIMEOUT",
            retryable=True,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise SupabaseMediaError(
            "Could not connect to Supabase Storage.",
            code="SUPABASE_CONNECTION_ERROR",
            retryable=True,
        ) from error
    except requests.exceptions.RequestException as error:
        raise SupabaseMediaError(
            "Supabase Storage upload request failed.",
            code="SUPABASE_REQUEST_ERROR",
            retryable=True,
        ) from error

    status = int(getattr(response, "status_code", 0))
    if status in {401, 403}:
        raise SupabaseMediaError(
            "Supabase Storage authorization failed. Check the server-side service-role key.",
            code="SUPABASE_AUTH_ERROR",
        )
    if status == 404:
        raise SupabaseMediaError(
            "Supabase media bucket was not found. Create the configured bucket first.",
            code="SUPABASE_BUCKET_NOT_FOUND",
        )
    if status == 413:
        raise SupabaseMediaError(
            "Supabase rejected the creative because it is too large.",
            code="SUPABASE_FILE_TOO_LARGE",
        )
    if status < 200 or status >= 300:
        raise SupabaseMediaError(
            "Supabase Storage rejected the creative upload.",
            code="SUPABASE_UPLOAD_REJECTED",
            retryable=status >= 500,
        )

    url = public_object_url(base, clean_bucket, path)
    if verify_public:
        try:
            public_response = http_client.head(
                url,
                timeout=(5, 20),
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as error:
            raise SupabaseMediaError(
                "Creative uploaded, but its public URL could not be verified.",
                code="SUPABASE_PUBLIC_VERIFY_ERROR",
                retryable=True,
            ) from error
        public_status = int(getattr(public_response, "status_code", 0))
        if public_status < 200 or public_status >= 400:
            raise SupabaseMediaError(
                "Creative uploaded, but the Supabase bucket is not publicly readable. "
                "Make the publishing-media bucket public before Meta publishing.",
                code="SUPABASE_BUCKET_NOT_PUBLIC",
            )

    return SupabaseMediaResult(
        object_path=path,
        public_url=url,
        mime_type=clean_mime,
        file_size=len(raw),
    )
