import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_store import CampaignStore, StoreConflict
from senior_review_links import (
    build_review_url,
    generate_review_token,
    hash_review_token,
)

HEADERS = [
    "Date",
    "Platform",
    "Pillar",
    "Format",
    "Content Idea",
    "SEO Keyword Focus",
    "CTA",
]
ROWS = [[
    "Mon, Aug 24",
    "Instagram",
    "Educational",
    "Image",
    "Helpful idea",
    "useful keyword",
    "Learn more",
]]


def future_iso(hours=2):
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_review_ready_store():
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    client = store.create_or_update_client("ABC Realty", {"industry": "Real Estate"})
    campaign = store.create_campaign(
        client["id"], {"goal": "Leads"}, request_id="request-review-link"
    )
    version = store.complete_generation(campaign["id"], HEADERS, ROWS)
    return temp, store, client, campaign, version


def test_token_is_opaque_hashed_and_url_safe():
    token = generate_review_token()
    assert len(token) >= 43
    assert len(hash_review_token(token)) == 64
    url = build_review_url("https://marketing.example.com", token)
    assert url.startswith("https://marketing.example.com/")
    assert "?review=" in url
    assert token in url


def test_create_open_and_approve_review_link():
    temp, store, _client, campaign, version = make_review_ready_store()
    try:
        token = generate_review_token()
        token_hash = hash_review_token(token)
        created = store.create_senior_review_link(
            campaign["id"], version["id"], token_hash, future_iso()
        )
        assert created["status"] == "pending"
        assert "token_hash" not in created

        bundle = store.get_senior_review_link_bundle(token_hash, mark_opened=True)
        assert bundle["campaign"]["id"] == campaign["id"]
        assert bundle["calendar"]["id"] == version["id"]
        assert bundle["link"]["opened_at"] is not None

        result = store.decide_senior_review_link(
            token_hash,
            "approved",
            "Senior Reviewer",
            "senior@example.com",
        )
        assert result["campaign"]["status"] == "fully_approved"
        assert result["approval"]["role"] == "senior"
        assert result["approval"]["decision"] == "approved"
        assert result["link"]["status"] == "decided"

        with pytest.raises(StoreConflict):
            store.get_senior_review_link_bundle(token_hash)
    finally:
        store.close()
        temp.cleanup()


def test_reject_requires_feedback_and_moves_to_revision_required():
    temp, store, _client, campaign, version = make_review_ready_store()
    try:
        token_hash = hash_review_token(generate_review_token())
        store.create_senior_review_link(
            campaign["id"], version["id"], token_hash, future_iso()
        )
        with pytest.raises(ValueError):
            store.decide_senior_review_link(
                token_hash,
                "rejected",
                "Senior Reviewer",
                "senior@example.com",
                "",
            )
        result = store.decide_senior_review_link(
            token_hash,
            "rejected",
            "Senior Reviewer",
            "senior@example.com",
            "Replace only the SEO keywords with buyer-intent keywords.",
            change_request={
                "scope": "specific_post",
                "post_number": 1,
                "row_index": 0,
                "fields": ["SEO Keyword Focus"],
            },
        )
        assert result["campaign"]["status"] == "revision_required"
        assert result["approval"]["feedback"] == (
            "Replace only the SEO keywords with buyer-intent keywords."
        )
        assert result["change_request"]["scope"] == "specific_post"
        assert result["change_request"]["post_number"] == 1
        assert result["change_request"]["fields"] == ["SEO Keyword Focus"]
        stored = store.get_senior_change_request(campaign["id"], version["id"])
        assert stored["id"] == result["change_request"]["id"]
    finally:
        store.close()
        temp.cleanup()


def test_new_link_revokes_previous_link_for_same_version():
    temp, store, _client, campaign, version = make_review_ready_store()
    try:
        first_hash = hash_review_token(generate_review_token())
        second_hash = hash_review_token(generate_review_token())
        store.create_senior_review_link(
            campaign["id"], version["id"], first_hash, future_iso()
        )
        store.create_senior_review_link(
            campaign["id"], version["id"], second_hash, future_iso()
        )
        with pytest.raises(StoreConflict):
            store.get_senior_review_link_bundle(first_hash)
        assert store.get_senior_review_link_bundle(second_hash)["link"]["status"] == "pending"
    finally:
        store.close()
        temp.cleanup()


def test_whole_calendar_change_request_is_structured():
    temp, store, _client, campaign, version = make_review_ready_store()
    try:
        token_hash = hash_review_token(generate_review_token())
        store.create_senior_review_link(
            campaign["id"], version["id"], token_hash, future_iso()
        )
        result = store.decide_senior_review_link(
            token_hash,
            "rejected",
            "Senior Reviewer",
            "senior@example.com",
            "Make every CTA lead-generation focused.",
            change_request={
                "scope": "whole_calendar",
                "post_number": None,
                "row_index": None,
                "fields": ["CTA"],
            },
        )
        request = result["change_request"]
        assert request["scope"] == "whole_calendar"
        assert request["post_number"] is None
        assert request["row_index"] is None
        assert request["fields"] == ["CTA"]
    finally:
        store.close()
        temp.cleanup()


def test_structured_change_rejects_unsupported_fields():
    temp, store, _client, campaign, version = make_review_ready_store()
    try:
        token_hash = hash_review_token(generate_review_token())
        store.create_senior_review_link(
            campaign["id"], version["id"], token_hash, future_iso()
        )
        with pytest.raises(ValueError):
            store.decide_senior_review_link(
                token_hash,
                "rejected",
                "Senior Reviewer",
                "senior@example.com",
                "Change platform.",
                change_request={
                    "scope": "specific_post",
                    "post_number": 1,
                    "row_index": 0,
                    "fields": ["Platform"],
                },
            )
    finally:
        store.close()
        temp.cleanup()
