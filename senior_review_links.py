"""Opaque capability helpers for Senior content and design review links.

Raw tokens are shown only to the user who creates a link. The database stores
only domain-separated SHA-256 digests, so a database leak does not reveal active
review URLs.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,86}$")


def generate_review_token(num_bytes: int = 32) -> str:
    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an integer.")
    if not 32 <= num_bytes <= 64:
        raise ValueError("num_bytes must be between 32 and 64.")
    return secrets.token_urlsafe(num_bytes)


def normalize_review_token(token: str) -> str:
    if not isinstance(token, str):
        raise TypeError("review token must be text.")
    clean = token.strip()
    if _TOKEN_RE.fullmatch(clean) is None:
        raise ValueError("The Senior review link is invalid.")
    return clean


def hash_review_token(token: str) -> str:
    clean = normalize_review_token(token)
    return hashlib.sha256(
        b"senior-review-link\x00" + clean.encode("ascii")
    ).hexdigest()


def hash_design_review_token(token: str) -> str:
    """Hash a design-review capability in a separate security domain."""

    clean = normalize_review_token(token)
    return hashlib.sha256(
        b"senior-design-review-link\x00" + clean.encode("ascii")
    ).hexdigest()


def _build_capability_url(public_base_url: str, token: str, query_key: str) -> str:
    clean_token = normalize_review_token(token)
    if not isinstance(public_base_url, str):
        raise TypeError("public_base_url must be text.")
    base = public_base_url.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("APP_PUBLIC_BASE_URL must be a complete http(s) URL.")
    query = urlencode({query_key: clean_token})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def build_review_url(public_base_url: str, token: str) -> str:
    return _build_capability_url(public_base_url, token, "review")


def build_design_review_url(public_base_url: str, token: str) -> str:
    return _build_capability_url(public_base_url, token, "design_review")
