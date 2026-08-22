import json
import sqlite3

from publishing_analytics import normalize_insights_payload, recommend_next_action, summarize_performance
from publishing_store import PublishingStore


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
    connection.execute("INSERT INTO campaigns(id,client_id,status) VALUES ('campaign-1','client-1','fully_approved')")
    connection.execute(
        "INSERT INTO calendar_versions(id,campaign_id,version,headers_json,rows_json,content_hash) VALUES (?,?,?,?,?,?)",
        ("calendar-1", "campaign-1", 1, json.dumps(headers), json.dumps([row]), content_hash),
    )
    connection.execute("INSERT INTO approvals VALUES ('campaign-1','calendar-1','senior','approved',?)", (content_hash,))
    connection.execute("INSERT INTO design_briefs VALUES ('brief-1','campaign-1','calendar-1',?,1)", (content_hash,))
    connection.execute("INSERT INTO creative_assets VALUES ('asset-1','campaign-1','calendar-1',?,1,0,'Image',1,'image/jpeg',?)", (content_hash, creative_hash))
    connection.execute("INSERT INTO design_approvals VALUES ('asset-1','approved',?)", (creative_hash,))
    connection.commit()
    connection.close()


def test_metrics_are_normalized_and_recommended():
    payload = {
        "data": [
            {"name": "post_impressions_unique", "values": [{"value": 1000}]},
            {"name": "post_clicks_unique", "values": [{"value": 20}]},
            {"name": "post_saves", "values": [{"value": 10}]},
            {"name": "post_shares", "values": [{"value": 15}]},
            {"name": "post_comments_by_type_total", "values": [{"value": 5}]},
            {"name": "post_reactions_by_type_total", "values": [{"value": 25}]},
        ]
    }
    normalized = normalize_insights_payload(payload)
    assert normalized["impressions"] == 1000
    assert normalized["clicks"] == 20
    assert normalized["engagement_rate"] > 0

    summary = summarize_performance(payload)
    assert summary["engagement"] == 75
    assert "High engagement" in recommend_next_action(summary)


def test_store_persists_metrics_for_published_job(tmp_path):
    db = tmp_path / "marketing.sqlite3"
    _seed_approved_database(db)
    store = PublishingStore(db)
    meta = store.save_meta_connection(
        client_id="client-1",
        connection_name="Client Meta",
        credential_ref="META_TOKEN_CLIENT_1",
        instagram_user_id="987",
    )
    job = store.queue_image_publication(
        campaign_id="campaign-1",
        calendar_version_id="calendar-1",
        creative_asset_id="asset-1",
        connection_id=meta["id"],
        platform="instagram",
        public_media_url="https://cdn.example.com/publish.jpg",
    )
    store.claim_due_jobs(limit=1)
    store.mark_published(job["id"], platform_post_id="ig_123", provider_request_id="req_123")
    saved = store.save_job_insights(
        job["id"],
        metric_window="24h",
        metrics={
            "data": [
                {"name": "post_impressions_unique", "values": [{"value": 2000}]},
                {"name": "post_clicks_unique", "values": [{"value": 50}]},
                {"name": "post_saves", "values": [{"value": 20}]},
                {"name": "post_shares", "values": [{"value": 15}]},
                {"name": "post_comments_by_type_total", "values": [{"value": 10}]},
                {"name": "post_reactions_by_type_total", "values": [{"value": 25}]},
            ]
        },
    )
    assert saved[0]["metric_window"] == "24h"
    summary = store.get_campaign_metrics_summary("campaign-1", metric_window="24h")
    assert summary[0]["impressions"] == 2000
    assert summary[0]["engagement"] == 120
