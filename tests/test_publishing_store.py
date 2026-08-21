import json
import sqlite3

import pytest

from publishing_store import PublishingConflict, PublishingStore


def _seed_approved_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE clients (id TEXT PRIMARY KEY);
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES clients(id)
        );
        CREATE TABLE calendar_versions (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            rows_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(campaign_id,id)
        );
        CREATE TABLE approvals (
            campaign_id TEXT NOT NULL,
            calendar_version_id TEXT NOT NULL,
            role TEXT NOT NULL,
            decision TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TABLE design_briefs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            calendar_version_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            post_number INTEGER NOT NULL
        );
        CREATE TABLE creative_assets (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            calendar_version_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            post_number INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            format TEXT NOT NULL,
            asset_version INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            file_sha256 TEXT NOT NULL
        );
        CREATE TABLE design_approvals (
            creative_asset_id TEXT PRIMARY KEY,
            decision TEXT NOT NULL,
            asset_hash TEXT NOT NULL
        );
        """
    )
    headers = ["Date", "Platform", "Pillar", "Format", "Content Idea", "SEO Keyword Focus", "CTA", "Caption", "Reel Script", "Content Status"]
    row = ["Mon", "Instagram and Facebook", "Educational", "Image", "Idea", "keyword", "Learn more", "Approved caption", "Not applicable", "Approved"]
    content_hash = "a" * 64
    creative_hash = "b" * 64
    connection.execute("INSERT INTO clients(id) VALUES ('client-1')")
    connection.execute(
        "INSERT INTO campaigns(id,client_id,status) VALUES ('campaign-1','client-1','fully_approved')"
    )
    connection.execute(
        "INSERT INTO calendar_versions(id,campaign_id,version,headers_json,rows_json,content_hash) VALUES (?,?,?,?,?,?)",
        ("calendar-1", "campaign-1", 1, json.dumps(headers), json.dumps([row]), content_hash),
    )
    connection.execute(
        "INSERT INTO approvals VALUES ('campaign-1','calendar-1','senior','approved',?)",
        (content_hash,),
    )
    connection.execute(
        "INSERT INTO design_briefs VALUES ('brief-1','campaign-1','calendar-1',?,1)",
        (content_hash,),
    )
    connection.execute(
        "INSERT INTO creative_assets VALUES ('asset-1','campaign-1','calendar-1',?,1,0,'Image',1,'image/png',?)",
        (content_hash, creative_hash),
    )
    connection.execute(
        "INSERT INTO design_approvals VALUES ('asset-1','approved',?)",
        (creative_hash,),
    )
    connection.commit()
    connection.close()


def test_queue_is_bound_to_exact_approved_content_and_creative(tmp_path):
    db = tmp_path / "marketing.sqlite3"
    _seed_approved_database(db)
    store = PublishingStore(db)
    meta = store.save_meta_connection(
        client_id="client-1",
        connection_name="Client Meta",
        credential_ref="META_TOKEN_CLIENT_1",
        facebook_page_id="123",
        instagram_user_id="987",
    )
    job = store.queue_image_publication(
        campaign_id="campaign-1",
        calendar_version_id="calendar-1",
        creative_asset_id="asset-1",
        connection_id=meta["id"],
        platform="instagram",
        public_media_url="https://cdn.example.com/post.png",
    )
    assert job["status"] == "queued"
    assert job["creative_hash"] == "b" * 64
    assert job["caption"] == "Approved caption"

    duplicate = store.queue_image_publication(
        campaign_id="campaign-1",
        calendar_version_id="calendar-1",
        creative_asset_id="asset-1",
        connection_id=meta["id"],
        platform="instagram",
        public_media_url="https://cdn.example.com/another-url.png",
    )
    assert duplicate["id"] == job["id"]


def test_design_gate_locks_when_latest_creative_is_not_approved(tmp_path):
    db = tmp_path / "marketing.sqlite3"
    _seed_approved_database(db)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE design_approvals SET decision='rejected'")
    connection.commit()
    connection.close()

    store = PublishingStore(db)
    meta = store.save_meta_connection(
        client_id="client-1",
        connection_name="Client Meta",
        credential_ref="META_TOKEN_CLIENT_1",
        instagram_user_id="987",
    )
    with pytest.raises(PublishingConflict, match="Publishing Gate is locked"):
        store.queue_image_publication(
            campaign_id="campaign-1",
            calendar_version_id="calendar-1",
            creative_asset_id="asset-1",
            connection_id=meta["id"],
            platform="instagram",
            public_media_url="https://cdn.example.com/post.png",
        )


def test_outcome_unknown_job_cannot_be_blindly_requeued(tmp_path):
    db = tmp_path / "marketing.sqlite3"
    _seed_approved_database(db)
    store = PublishingStore(db)
    meta = store.save_meta_connection(
        client_id="client-1",
        connection_name="Client Meta",
        credential_ref="META_TOKEN_CLIENT_1",
        facebook_page_id="123",
    )
    job = store.queue_image_publication(
        campaign_id="campaign-1",
        calendar_version_id="calendar-1",
        creative_asset_id="asset-1",
        connection_id=meta["id"],
        platform="facebook",
        public_media_url="https://cdn.example.com/post.png",
    )
    claimed = store.claim_due_jobs(limit=1)
    assert claimed[0]["id"] == job["id"]
    store.mark_outcome_unknown(
        job["id"],
        error_code="META_TIMEOUT",
        error_message="Ambiguous timeout",
    )
    with pytest.raises(PublishingConflict, match="verified on Meta"):
        store.requeue_failed(job["id"])
