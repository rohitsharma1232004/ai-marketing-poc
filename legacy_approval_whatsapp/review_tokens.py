"""Security helpers for external approval-review links.

Review capability tokens deliberately contain only a version, a review-request
UUID, and an expiry time.  Campaign, client, reviewer, and content data remain
server-side.  The token is deterministic so an outbox worker can reconstruct
the same link for a retry without storing the raw capability.

This module uses only Python's standard library.  Keep the signing secret in a
server-side secret store; it must never be included in a URL or database row.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union
from uuid import UUID


TOKEN_VERSION = "rv1"
SESSION_VERSION = "sv1"
CSRF_VERSION = "cv1"
MAX_REVIEW_TOKEN_LENGTH = 256
MAX_EXPIRY_TEXT_LENGTH = 64
MAX_UNIX_TIMESTAMP = 253_402_300_799  # 9999-12-31T23:59:59Z

_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_EXPIRY_RE = re.compile(r"^[1-9][0-9]{0,11}$")
_SESSION_RE = re.compile(r"^sv1\.([A-Za-z0-9_-]{43,86})$")
_CSRF_RE = re.compile(r"^cv1\.([A-Za-z0-9_-]{43})$")

ExpiryInput = Union[int, str, datetime]


class ReviewTokenError(ValueError):
    """Base class for review-token validation failures."""


class InvalidReviewToken(ReviewTokenError):
    """Raised when a review token is malformed or has an invalid signature."""


class ExpiredReviewToken(ReviewTokenError):
    """Raised when an otherwise valid review token has expired."""


@dataclass(frozen=True)
class ReviewTokenClaims:
    """Verified, non-sensitive claims carried by a review token."""

    version: str
    review_request_id: str
    expires_at: int

    @property
    def expires_at_utc(self) -> datetime:
        """Return the expiry as an aware UTC ``datetime``."""

        return datetime.fromtimestamp(self.expires_at, tz=timezone.utc)

    @property
    def expires_at_iso(self) -> str:
        """Return the expiry in the store-friendly ISO UTC ``Z`` form."""

        return self.expires_at_utc.isoformat().replace("+00:00", "Z")


def _secret_bytes(secret: Union[str, bytes]) -> bytes:
    if isinstance(secret, str):
        if len(secret) < 32:
            raise ValueError("The server signing secret must be at least 32 characters.")
        return secret.encode("utf-8")
    if isinstance(secret, bytes):
        if len(secret) < 32:
            raise ValueError("The server signing secret must be at least 32 bytes.")
        return secret
    raise TypeError("The server signing secret must be text or bytes.")


def _canonical_uuid(value: Union[str, UUID]) -> str:
    if isinstance(value, UUID):
        parsed = value
    elif isinstance(value, str):
        if len(value) != 36:
            raise ValueError("The review request ID must be a canonical UUID.")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError("The review request ID must be a canonical UUID.") from exc
        if str(parsed) != value:
            raise ValueError("The review request ID must be a canonical lowercase UUID.")
    else:
        raise TypeError("The review request ID must be UUID text or a UUID object.")
    if parsed.int == 0:
        raise ValueError("The review request ID cannot be the nil UUID.")
    return str(parsed)


def _datetime_to_timestamp(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expires_at must include a UTC offset.")
    try:
        timestamp = int(value.timestamp())
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("expires_at is outside the supported range.") from exc
    return _validate_timestamp(timestamp)


def _validate_timestamp(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expires_at must resolve to whole Unix seconds.")
    if value < 1 or value > MAX_UNIX_TIMESTAMP:
        raise ValueError("expires_at is outside the supported range.")
    return value


def normalize_expires_at(expires_at: ExpiryInput) -> int:
    """Normalize an expiry value to whole Unix seconds.

    Accepted inputs are an integer Unix timestamp, an aware ``datetime``, or an
    ISO-8601 string with an explicit offset (including the store's ``...Z``
    representation).  Fractional seconds are consistently truncated, so using
    either the saved ISO value or this returned integer reconstructs the same
    review token.
    """

    if isinstance(expires_at, bool):
        raise TypeError("expires_at must be an integer, aware datetime, or ISO text.")
    if isinstance(expires_at, int):
        return _validate_timestamp(expires_at)
    if isinstance(expires_at, datetime):
        return _datetime_to_timestamp(expires_at)
    if not isinstance(expires_at, str):
        raise TypeError("expires_at must be an integer, aware datetime, or ISO text.")
    if not expires_at or len(expires_at) > MAX_EXPIRY_TEXT_LENGTH:
        raise ValueError("expires_at must be a bounded ISO-8601 timestamp.")

    iso_value = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ValueError("expires_at must be valid ISO-8601 text.") from exc
    return _datetime_to_timestamp(parsed)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _review_signature(unsigned_token: str, secret: Union[str, bytes]) -> str:
    key = _secret_bytes(secret)
    message = b"review-capability\x00" + unsigned_token.encode("ascii")
    return _b64url(hmac.new(key, message, hashlib.sha256).digest())


def create_review_token(
    review_request_id: Union[str, UUID],
    expires_at: ExpiryInput,
    secret: Union[str, bytes],
) -> str:
    """Create a deterministic, URL-safe signed review capability token."""

    request_id = _canonical_uuid(review_request_id)
    expiry = normalize_expires_at(expires_at)
    unsigned = f"{TOKEN_VERSION}.{request_id}.{expiry}"
    return f"{unsigned}.{_review_signature(unsigned, secret)}"


def _invalid_token() -> InvalidReviewToken:
    # Never include the untrusted token in errors or logs.
    return InvalidReviewToken("The review link is invalid.")


def verify_review_token(
    token: str,
    secret: Union[str, bytes],
    *,
    now: ExpiryInput | None = None,
) -> ReviewTokenClaims:
    """Verify a review capability and return its non-sensitive claims.

    Signature comparison is constant-time.  ``now`` exists for deterministic
    testing; production callers should omit it.  A token is expired when the
    current time is equal to or later than its expiry.
    """

    key = _secret_bytes(secret)  # Fail fast for a server configuration error.
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_REVIEW_TOKEN_LENGTH
        or not token.isascii()
    ):
        raise _invalid_token()

    parts = token.split(".")
    if len(parts) != 4:
        raise _invalid_token()
    version, request_text, expiry_text, supplied_signature = parts
    if version != TOKEN_VERSION or not _SIGNATURE_RE.fullmatch(supplied_signature):
        raise _invalid_token()
    if not _EXPIRY_RE.fullmatch(expiry_text):
        raise _invalid_token()

    try:
        request_id = _canonical_uuid(request_text)
        expiry = _validate_timestamp(int(expiry_text))
    except (TypeError, ValueError) as exc:
        raise _invalid_token() from exc

    unsigned = f"{version}.{request_id}.{expiry}"
    expected_signature = _review_signature(unsigned, key)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise _invalid_token()

    current = (
        int(datetime.now(timezone.utc).timestamp())
        if now is None
        else normalize_expires_at(now)
    )
    if current >= expiry:
        raise ExpiredReviewToken("The review link has expired.")
    return ReviewTokenClaims(version, request_id, expiry)


def _bounded_ascii_token(token: str, *, maximum: int = 512) -> bytes:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > maximum
        or not token.isascii()
    ):
        raise ValueError("The token is invalid.")
    return token.encode("ascii")


def hash_token(token: str) -> str:
    """Return a domain-separated SHA-256 hash for storing a review token."""

    raw = _bounded_ascii_token(token, maximum=MAX_REVIEW_TOKEN_LENGTH)
    return hashlib.sha256(b"review-token-hash\x00" + raw).hexdigest()


def generate_session_token(num_bytes: int = 32) -> str:
    """Generate a versioned URL-safe session token with at least 256-bit entropy."""

    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an integer.")
    if num_bytes < 32 or num_bytes > 64:
        raise ValueError("num_bytes must be between 32 and 64.")
    return f"{SESSION_VERSION}.{_b64url(secrets.token_bytes(num_bytes))}"


def _validate_session_token(session_token: str) -> bytes:
    raw = _bounded_ascii_token(session_token, maximum=96)
    match = _SESSION_RE.fullmatch(session_token)
    if match is None:
        raise ValueError("The session token is invalid.")
    encoded = match.group(1)
    try:
        decoded = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("The session token is invalid.") from exc
    if len(decoded) < 32 or len(decoded) > 64 or _b64url(decoded) != encoded:
        raise ValueError("The session token is invalid.")
    return raw


def hash_session_token(session_token: str) -> str:
    """Return a domain-separated SHA-256 hash for session-token storage."""

    raw = _validate_session_token(session_token)
    return hashlib.sha256(b"review-session-hash\x00" + raw).hexdigest()


def derive_csrf_token(session_token: str, secret: Union[str, bytes]) -> str:
    """Derive a deterministic CSRF token bound to a review session."""

    raw_session = _validate_session_token(session_token)
    key = _secret_bytes(secret)
    signature = hmac.new(
        key, b"review-csrf\x00" + raw_session, hashlib.sha256
    ).digest()
    return f"{CSRF_VERSION}.{_b64url(signature)}"


def verify_csrf_token(
    csrf_token: str,
    session_token: str,
    secret: Union[str, bytes],
) -> bool:
    """Constant-time verify a CSRF token bound to ``session_token``.

    A malformed submitted CSRF token returns ``False``.  Invalid server-secret
    or session configuration raises a validation error so it is not silently
    mistaken for an ordinary rejected form submission.
    """

    _validate_session_token(session_token)
    _secret_bytes(secret)
    if (
        not isinstance(csrf_token, str)
        or len(csrf_token) > 64
        or _CSRF_RE.fullmatch(csrf_token) is None
    ):
        return False
    expected = derive_csrf_token(session_token, secret)
    return hmac.compare_digest(csrf_token, expected)


__all__ = [
    "CSRF_VERSION",
    "ExpiredReviewToken",
    "InvalidReviewToken",
    "MAX_REVIEW_TOKEN_LENGTH",
    "ReviewTokenClaims",
    "ReviewTokenError",
    "SESSION_VERSION",
    "TOKEN_VERSION",
    "create_review_token",
    "derive_csrf_token",
    "generate_session_token",
    "hash_session_token",
    "hash_token",
    "normalize_expires_at",
    "verify_csrf_token",
    "verify_review_token",
]
