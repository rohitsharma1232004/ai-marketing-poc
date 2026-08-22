"""Direct Meta Graph API publisher for approved social content.

The module intentionally accepts credentials only at call time. Tokens are never
stored, logged, returned, or included in exception messages. Phase 1 supports
single-image Facebook Page and Instagram Professional feed publishing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from publishing_analytics import normalize_insights_payload, summarize_performance

DEFAULT_META_GRAPH_BASE_URL = "https://graph.facebook.com"
DEFAULT_META_GRAPH_API_VERSION = "v25.0"
META_API_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+$")
MAX_META_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MetaPublishResult:
    platform: str
    platform_post_id: str
    request_id: str
    container_id: str = ""


class MetaPublishError(RuntimeError):
    """Safe publishing error with retry and duplicate-risk semantics."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        code: str,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


class MetaInsightsError(RuntimeError):
    """Raised when a Meta insight request cannot be completed safely."""


def _clean_id(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 200 or not clean.isdigit():
        raise ValueError(f"{label} must be a numeric Meta object ID.")
    return clean


def _clean_token(value: str, request_id: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise MetaPublishError(
            "Meta access token is missing from the runtime credential store.",
            request_id=request_id,
            code="META_TOKEN_MISSING",
        )
    return token


def _clean_version(value: str) -> str:
    version = str(value or DEFAULT_META_GRAPH_API_VERSION).strip()
    if not META_API_VERSION_RE.fullmatch(version):
        raise ValueError("Meta Graph API version must look like v25.0.")
    return version


def _clean_base_url(value: str) -> str:
    base = str(value or DEFAULT_META_GRAPH_BASE_URL).strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or parts.hostname != "graph.facebook.com":
        raise ValueError("Meta Graph API base URL must be https://graph.facebook.com.")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("Meta Graph API base URL must not include a path or query.")
    return base


def _public_https_url(value: str) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    if not url or len(url) > 2_000 or parts.scheme != "https" or not parts.hostname:
        raise ValueError("Meta publishing media must use a public HTTPS URL.")
    if parts.username or parts.password:
        raise ValueError("Media URLs must not contain credentials.")
    return url


def _safe_json(response: Any, request_id: str) -> Mapping[str, Any]:
    raw = getattr(response, "content", b"") or b""
    if len(raw) > MAX_META_RESPONSE_BYTES:
        raise MetaPublishError(
            "Meta returned a response that is too large.",
            request_id=request_id,
            code="META_RESPONSE_TOO_LARGE",
        )
    try:
        data = response.json()
    except (TypeError, ValueError) as error:
        raise MetaPublishError(
            "Meta returned a non-JSON response.",
            request_id=request_id,
            code="META_INVALID_RESPONSE",
            retryable=True,
        ) from error
    if not isinstance(data, Mapping):
        raise MetaPublishError(
            "Meta returned an invalid response object.",
            request_id=request_id,
            code="META_INVALID_RESPONSE",
            retryable=True,
        )
    return data


def _error_code_from_body(data: Mapping[str, Any]) -> str:
    raw_error = data.get("error")
    if not isinstance(raw_error, Mapping):
        return ""
    code = raw_error.get("code")
    subcode = raw_error.get("error_subcode")
    pieces = []
    if isinstance(code, int):
        pieces.append(str(code))
    if isinstance(subcode, int):
        pieces.append(str(subcode))
    return "_".join(pieces)


def _raise_for_status(
    response: Any,
    *,
    request_id: str,
    action: str,
    outcome_unknown_on_timeout_equivalent: bool = False,
) -> Mapping[str, Any]:
    status = int(getattr(response, "status_code", 0))
    data = _safe_json(response, request_id)
    if 200 <= status < 300:
        return data

    provider_code = _error_code_from_body(data)
    suffix = f" ({provider_code})" if provider_code else ""
    if status in {401, 403}:
        raise MetaPublishError(
            f"Meta authorization failed while {action}{suffix}.",
            request_id=request_id,
            code="META_AUTH_ERROR",
        )
    if status == 429:
        retry_after = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
        note = f" Retry after {retry_after}." if retry_after else ""
        raise MetaPublishError(
            f"Meta rate limit reached while {action}.{note}",
            request_id=request_id,
            code="META_RATE_LIMIT",
            retryable=True,
        )
    if 500 <= status <= 599:
        raise MetaPublishError(
            f"Meta had a temporary server error while {action}{suffix}.",
            request_id=request_id,
            code="META_UPSTREAM_ERROR",
            retryable=True,
            outcome_unknown=outcome_unknown_on_timeout_equivalent,
        )
    raise MetaPublishError(
        f"Meta rejected the request while {action}{suffix}.",
        request_id=request_id,
        code="META_REQUEST_REJECTED",
    )


def _post(
    *,
    url: str,
    token: str,
    data: Mapping[str, Any],
    request_id: str,
    action: str,
    http_client: Any,
    outcome_unknown_on_timeout: bool,
) -> Mapping[str, Any]:
    try:
        response = http_client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "AI-Marketing-POC/1.0",
                "X-Request-ID": request_id,
            },
            data=dict(data),
            timeout=(5, 90),
        )
    except requests.exceptions.Timeout as error:
        raise MetaPublishError(
            f"Meta timed out while {action}.",
            request_id=request_id,
            code="META_TIMEOUT",
            retryable=not outcome_unknown_on_timeout,
            outcome_unknown=outcome_unknown_on_timeout,
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise MetaPublishError(
            f"Could not connect to Meta while {action}.",
            request_id=request_id,
            code="META_CONNECTION_ERROR",
            retryable=not outcome_unknown_on_timeout,
            outcome_unknown=outcome_unknown_on_timeout,
        ) from error
    except requests.exceptions.RequestException as error:
        raise MetaPublishError(
            f"Meta request failed while {action}.",
            request_id=request_id,
            code="META_REQUEST_ERROR",
            retryable=not outcome_unknown_on_timeout,
            outcome_unknown=outcome_unknown_on_timeout,
        ) from error
    return _raise_for_status(
        response,
        request_id=request_id,
        action=action,
        outcome_unknown_on_timeout_equivalent=outcome_unknown_on_timeout,
    )


def publish_facebook_photo(
    *,
    page_id: str,
    page_access_token: str,
    image_url: str,
    caption: str,
    request_id: str | None = None,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
    base_url: str = DEFAULT_META_GRAPH_BASE_URL,
    http_client: Any = requests,
) -> MetaPublishResult:
    """Publish one public image to a Facebook Page."""

    correlation_id = request_id or str(uuid4())
    clean_page_id = _clean_id(page_id, "page_id")
    token = _clean_token(page_access_token, correlation_id)
    media_url = _public_https_url(image_url)
    version = _clean_version(api_version)
    base = _clean_base_url(base_url)
    clean_caption = str(caption or "").strip()
    if not clean_caption:
        raise ValueError("Facebook photo caption must not be empty.")

    data = _post(
        url=f"{base}/{version}/{clean_page_id}/photos",
        token=token,
        data={"url": media_url, "caption": clean_caption},
        request_id=correlation_id,
        action="publishing the Facebook photo",
        http_client=http_client,
        # A timeout can occur after Meta accepted the post, so never auto-retry.
        outcome_unknown_on_timeout=True,
    )
    post_id = str(data.get("post_id") or data.get("id") or "").strip()
    if not post_id:
        raise MetaPublishError(
            "Meta accepted the Facebook request but returned no post ID.",
            request_id=correlation_id,
            code="META_PUBLISH_ID_MISSING",
            outcome_unknown=True,
        )
    return MetaPublishResult(
        platform="facebook",
        platform_post_id=post_id,
        request_id=correlation_id,
    )


def publish_instagram_image(
    *,
    instagram_user_id: str,
    page_access_token: str,
    image_url: str,
    caption: str,
    request_id: str | None = None,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
    base_url: str = DEFAULT_META_GRAPH_BASE_URL,
    http_client: Any = requests,
) -> MetaPublishResult:
    """Create an Instagram image container and publish it to a Professional account."""

    correlation_id = request_id or str(uuid4())
    ig_id = _clean_id(instagram_user_id, "instagram_user_id")
    token = _clean_token(page_access_token, correlation_id)
    media_url = _public_https_url(image_url)
    version = _clean_version(api_version)
    base = _clean_base_url(base_url)
    clean_caption = str(caption or "").strip()
    if not clean_caption:
        raise ValueError("Instagram caption must not be empty.")

    container = _post(
        url=f"{base}/{version}/{ig_id}/media",
        token=token,
        data={"image_url": media_url, "caption": clean_caption},
        request_id=correlation_id,
        action="creating the Instagram media container",
        http_client=http_client,
        # Container creation cannot make a live post by itself.
        outcome_unknown_on_timeout=False,
    )
    container_id = str(container.get("id") or "").strip()
    if not container_id:
        raise MetaPublishError(
            "Meta returned no Instagram media container ID.",
            request_id=correlation_id,
            code="META_CONTAINER_ID_MISSING",
            retryable=True,
        )

    published = _post(
        url=f"{base}/{version}/{ig_id}/media_publish",
        token=token,
        data={"creation_id": container_id},
        request_id=correlation_id,
        action="publishing the Instagram media container",
        http_client=http_client,
        # A timeout here may mean the post went live. Automatic retry risks duplicates.
        outcome_unknown_on_timeout=True,
    )
    post_id = str(published.get("id") or "").strip()
    if not post_id:
        raise MetaPublishError(
            "Meta accepted the Instagram publish request but returned no media ID.",
            request_id=correlation_id,
            code="META_PUBLISH_ID_MISSING",
            outcome_unknown=True,
        )
    return MetaPublishResult(
        platform="instagram",
        platform_post_id=post_id,
        request_id=correlation_id,
        container_id=container_id,
    )


def fetch_post_insights(
    *,
    object_id: str,
    page_access_token: str,
    metric_names: Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    request_id: str | None = None,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
    base_url: str = DEFAULT_META_GRAPH_BASE_URL,
    http_client: Any = requests,
) -> dict[str, float]:
    """Fetch and normalize a single Meta post's insight summary."""

    correlation_id = request_id or str(uuid4())
    clean_object_id = _clean_id(object_id, "object_id")
    token = _clean_token(page_access_token, correlation_id)
    version = _clean_version(api_version)
    base = _clean_base_url(base_url)
    metrics = tuple(str(item).strip() for item in (metric_names or (
        "post_impressions_unique",
        "post_engaged_users",
        "post_clicks_unique",
        "post_reactions_by_type_total",
        "post_comments_by_type_total",
        "post_shares",
        "post_saves",
    ))) if item else ()
    if not metrics:
        raise MetaInsightsError("At least one Meta insight metric is required.",)

    params = {"access_token": token, "metric": ",".join(metrics)}
    if since:
        params["since"] = str(since)
    if until:
        params["until"] = str(until)

    url = f"{base}/{version}/{clean_object_id}/insights"
    try:
        response = http_client.get(
            url,
            params=params,
            headers={
                "User-Agent": "AI-Marketing-POC/1.0",
                "X-Request-ID": correlation_id,
            },
            timeout=(5, 60),
        )
    except requests.exceptions.RequestException as error:
        raise MetaInsightsError(
            f"Meta insight lookup failed for object {clean_object_id}.",
        ) from error

    data = _raise_for_status(response, request_id=correlation_id, action="fetching Meta insights")
    normalized = normalize_insights_payload(data)
    return summarize_performance(normalized)
