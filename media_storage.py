"""Supabase Storage boundary for publishable marketing media.

This module keeps storage-specific code out of the Streamlit UI. It is intended
for server-side use with a Supabase secret key and a public bucket so generated
media can later be handed to publishing integrations through a stable URL.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from supabase import Client, create_client


DEFAULT_MEDIA_BUCKET = "publishing-media"
MAX_MEDIA_BYTES = 100 * 1024 * 1024
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class MediaStorageError(RuntimeError):
    """Raised when media cannot be validated, uploaded, or addressed."""


@dataclass(frozen=True)
class StoredMedia:
    """Normalized metadata for one object stored in Supabase Storage."""

    bucket: str
    object_path: str
    public_url: str
    content_type: str
    size_bytes: int


def create_storage_client(*, supabase_url: str, secret_key: str) -> Client:
    """Create a server-side Supabase client for Storage operations."""
    url = (supabase_url or "").strip()
    key = (secret_key or "").strip()
    if not url:
        raise MediaStorageError("SUPABASE_URL is missing from the server configuration.")
    if not key:
        raise MediaStorageError(
            "SUPABASE_SECRET_KEY is missing from the server configuration."
        )

    try:
        return create_client(url, key)
    except Exception as error:  # SDK exceptions vary by transport/version.
        raise MediaStorageError("Could not initialize Supabase Storage.") from error


def upload_media_bytes(
    *,
    client: Client,
    data: bytes,
    filename: str,
    bucket: str = DEFAULT_MEDIA_BUCKET,
    folder: str = "uploads",
    content_type: str | None = None,
    upsert: bool = False,
) -> StoredMedia:
    """Upload media bytes and return the public object URL.

    A UUID prefix avoids accidental filename collisions while preserving a safe,
    human-readable portion of the original filename.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise MediaStorageError("Media data must be bytes.")

    payload = bytes(data)
    if not payload:
        raise MediaStorageError("Media file is empty.")
    if len(payload) > MAX_MEDIA_BYTES:
        raise MediaStorageError("Media file exceeds the 100 MB POC upload limit.")

    bucket_name = _clean_bucket_name(bucket)
    object_path = build_media_object_path(filename=filename, folder=folder)
    mime_type = _resolve_content_type(filename=filename, content_type=content_type)

    try:
        client.storage.from_(bucket_name).upload(
            path=object_path,
            file=payload,
            file_options={
                "content-type": mime_type,
                "upsert": "true" if upsert else "false",
            },
        )
        public_url = client.storage.from_(bucket_name).get_public_url(object_path)
    except Exception as error:  # SDK/API exceptions are normalized for the UI.
        raise MediaStorageError("Supabase could not upload the media file.") from error

    if not public_url:
        raise MediaStorageError("Supabase uploaded the file but returned no public URL.")

    return StoredMedia(
        bucket=bucket_name,
        object_path=object_path,
        public_url=str(public_url),
        content_type=mime_type,
        size_bytes=len(payload),
    )


def build_media_object_path(*, filename: str, folder: str = "uploads") -> str:
    """Build a safe POSIX object path for Supabase Storage."""
    safe_name = _sanitize_filename(filename)
    safe_folder = _sanitize_folder(folder)
    unique_name = f"{uuid4().hex}_{safe_name}"
    return f"{safe_folder}/{unique_name}" if safe_folder else unique_name


def _clean_bucket_name(bucket: str) -> str:
    value = (bucket or "").strip()
    if not value:
        raise MediaStorageError("SUPABASE_BUCKET is missing from the server configuration.")
    if "/" in value or "\\" in value:
        raise MediaStorageError("Supabase bucket name is invalid.")
    return value


def _sanitize_filename(filename: str) -> str:
    original = PurePosixPath(str(filename or "").replace("\\", "/")).name.strip()
    if not original or original in {".", ".."}:
        original = "media.bin"

    safe = _SAFE_SEGMENT_RE.sub("-", original).strip(".-_")
    if not safe:
        safe = "media.bin"
    return safe[:180]


def _sanitize_folder(folder: str) -> str:
    raw_parts = str(folder or "").replace("\\", "/").split("/")
    safe_parts: list[str] = []
    for part in raw_parts:
        cleaned = _SAFE_SEGMENT_RE.sub("-", part.strip()).strip(".-_")
        if cleaned:
            safe_parts.append(cleaned[:80])
    return "/".join(safe_parts)


def _resolve_content_type(*, filename: str, content_type: str | None) -> str:
    explicit = (content_type or "").strip().lower()
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"
