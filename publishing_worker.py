"""One-shot publication worker for queued Meta jobs.

Run this process from an always-on server scheduler (cron/systemd/cloud scheduler).
It never auto-retries jobs whose Meta outcome is uncertain, preventing duplicate
posts after ambiguous timeouts.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from typing import Any

from meta_publisher import (
    DEFAULT_META_GRAPH_API_VERSION,
    MetaPublishError,
    publish_facebook_photo,
    publish_instagram_image,
)
from publishing_store import PublishingStore

TokenResolver = Callable[[str], str]


def resolve_token_from_environment(credential_ref: str) -> str:
    """Resolve a token by secret-name reference without ever printing its value."""
    return str(os.getenv(str(credential_ref or "").strip(), "") or "").strip()


def dispatch_claimed_job(
    store: PublishingStore,
    job: Mapping[str, Any],
    *,
    token_resolver: TokenResolver = resolve_token_from_environment,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
    http_client: Any = None,
) -> dict[str, Any]:
    """Publish one job that has already been atomically claimed by the store."""

    if str(job.get("status") or "") != "publishing":
        raise ValueError("dispatch_claimed_job requires a claimed publishing job.")
    bundle = store.get_job_bundle(str(job["id"]))
    connection = bundle["connection"]
    credential_ref = str(connection.get("credential_ref") or "").strip()
    token = token_resolver(credential_ref)
    if not token:
        return store.mark_failed(
            str(job["id"]),
            error_code="META_TOKEN_MISSING",
            error_message=(
                "Meta credential reference is configured but its runtime secret is missing."
            ),
        )

    kwargs: dict[str, Any] = {
        "page_access_token": token,
        "image_url": str(job["public_media_url"]),
        "caption": str(job["caption"]),
        "request_id": str(job["id"]),
        "api_version": api_version,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client

    try:
        if job["platform"] == "facebook":
            result = publish_facebook_photo(
                page_id=str(connection["facebook_page_id"]),
                **kwargs,
            )
        elif job["platform"] == "instagram":
            result = publish_instagram_image(
                instagram_user_id=str(connection["instagram_user_id"]),
                **kwargs,
            )
        else:
            raise ValueError("Unsupported queued platform.")
    except MetaPublishError as error:
        if error.outcome_unknown:
            return store.mark_outcome_unknown(
                str(job["id"]),
                error_code=error.code,
                error_message=str(error),
                provider_request_id=error.request_id,
            )
        return store.mark_failed(
            str(job["id"]),
            error_code=error.code,
            error_message=str(error),
            provider_request_id=error.request_id,
        )
    except (TypeError, ValueError) as error:
        return store.mark_failed(
            str(job["id"]),
            error_code="PUBLISHING_CONFIGURATION_ERROR",
            error_message=str(error),
        )

    return store.mark_published(
        str(job["id"]),
        platform_post_id=result.platform_post_id,
        provider_request_id=result.request_id,
    )


def run_due_jobs(
    db_path: str,
    *,
    limit: int = 10,
    token_resolver: TokenResolver = resolve_token_from_environment,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
    http_client: Any = None,
) -> dict[str, int]:
    store = PublishingStore(db_path)
    summary = {
        "claimed": 0,
        "published": 0,
        "failed": 0,
        "outcome_unknown": 0,
    }
    try:
        jobs = store.claim_due_jobs(limit=limit)
        summary["claimed"] = len(jobs)
        for job in jobs:
            result = dispatch_claimed_job(
                store,
                job,
                token_resolver=token_resolver,
                api_version=api_version,
                http_client=http_client,
            )
            status = str(result.get("status") or "")
            if status in summary:
                summary[status] += 1
    finally:
        store.close()
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch due approved Meta publication jobs.")
    parser.add_argument("--db", required=True, help="Path to marketing_poc.sqlite3")
    parser.add_argument("--limit", type=int, default=10, help="Maximum jobs to claim per run")
    parser.add_argument(
        "--api-version",
        default=DEFAULT_META_GRAPH_API_VERSION,
        help="Pinned Meta Graph API version, e.g. v25.0",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    result = run_due_jobs(args.db, limit=args.limit, api_version=args.api_version)
    # Safe operational counts only; no tokens, captions, URLs, or client IDs.
    print(
        "Publishing run: "
        f"claimed={result['claimed']} published={result['published']} "
        f"failed={result['failed']} outcome_unknown={result['outcome_unknown']}"
    )


if __name__ == "__main__":
    main()
