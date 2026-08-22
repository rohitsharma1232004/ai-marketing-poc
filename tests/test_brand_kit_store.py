import tempfile
from pathlib import Path

from campaign_store import CampaignStore


def test_brand_kit_versioning_and_idempotency():
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    try:
        client = store.create_or_update_client("ABC Realty")
        kit = {
            "brand_name": "ABC Realty",
            "primary_color": "#14213D",
            "secondary_color": "#FCA311",
            "heading_font": "Montserrat",
            "visual_style": "Premium and clean",
        }
        first = store.save_brand_kit(client["id"], kit)
        same = store.save_brand_kit(client["id"], dict(kit))
        changed = store.save_brand_kit(
            client["id"], {**kit, "accent_color": "#FFFFFF"}
        )
        assert first["version"] == 1
        assert same["id"] == first["id"]
        assert changed["version"] == 2
        assert store.get_latest_brand_kit(client["id"])["id"] == changed["id"]
        assert [item["version"] for item in store.list_brand_kits(client["id"])] == [2, 1]
    finally:
        store.close()
        temp.cleanup()


def test_client_without_brand_kit_returns_none():
    temp = tempfile.TemporaryDirectory()
    store = CampaignStore(Path(temp.name) / "campaigns.sqlite3")
    try:
        client = store.create_or_update_client("No Kit Client")
        assert store.get_latest_brand_kit(client["id"]) is None
        assert store.list_brand_kits(client["id"]) == []
    finally:
        store.close()
        temp.cleanup()
