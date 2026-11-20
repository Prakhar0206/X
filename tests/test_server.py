"""Tests for FastAPI REST server, dashboard routes, sync status, and import/export endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from x_tracker.server import create_app


def test_dashboard_and_seeded_analytics_endpoints(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "api.db"))
    client = TestClient(app)

    # 1. Root page renders HTML
    home = client.get("/")
    assert home.status_code == 200
    assert "X Telemetry" in home.text

    # 2. Seed demo data
    seed_resp = client.post("/api/seed-demo")
    assert seed_resp.status_code == 200
    assert seed_resp.json()["accounts_seeded"] == 3

    # 3. List accounts
    acc_resp = client.get("/api/accounts")
    assert acc_resp.status_code == 200
    handles = [a["handle"] for a in acc_resp.json()["accounts"]]
    assert "alexrivera_dev" in handles

    # 4. Analytics across timeframes, modes, and granularities
    for tf in ("7d", "30d", "7w", "90d", "lifetime"):
        for mode in ("cohort", "velocity"):
            res = client.get(
                f"/api/analytics?handle=alexrivera_dev&timeframe={tf}&mode={mode}&granularity=weekly"
            )
            assert res.status_code == 200
            body = res.json()
            assert body["window"]["timeframe"] == tf
            assert body["window"]["mode"] == mode
            assert body["kpis"]["impressions"] > 0
            assert len(body["charts"]["heatmap"]["matrix"]) == 7
            assert len(body["charts"]["media_breakdown"]) == 4

    # 5. Post detail drawer endpoint
    analytics_30d = client.get("/api/analytics?handle=alexrivera_dev&timeframe=30d").json()
    first_post_id = analytics_30d["posts"][0]["post_id"]
    detail_resp = client.get(f"/api/posts/{first_post_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["post_id"] == first_post_id
    assert len(detail["snapshots"]) >= 5


def test_import_export_accounts_and_sync_endpoints(tmp_path: Path) -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "api.fxtwitter.com/new_creator" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "user": {
                        "screen_name": "new_creator",
                        "name": "New Creator",
                        "followers": 3400,
                        "following": 180,
                        "tweets": 95,
                    }
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    app = create_app(
        db_path=str(tmp_path / "api_ops.db"),
        transport=httpx.MockTransport(mock_handler),
    )
    client = TestClient(app)

    # Add account
    add_resp = client.post("/api/accounts", json={"handle": "@New_Creator", "sync": False})
    assert add_resp.status_code == 200
    assert add_resp.json()["account"]["handle"] == "new_creator"

    # Trigger sync
    sync_resp = client.post("/api/sync", json={"handle": "new_creator"})
    assert sync_resp.status_code == 200
    assert sync_resp.json()["result"]["profile_updated"] is True

    status_resp = client.get("/api/sync/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["progress_pct"] == 100

    # Import CSV multipart file
    csv_bytes = (
        "Tweet id,Tweet permalink,Tweet text,time,impressions,retweets,replies,likes,bookmarks\n"
        "88001,https://x.com/new_creator/status/88001,First tracked CSV post,2026-11-18 10:00 +0000,4500,25,8,190,42\n"
    ).encode("utf-8")
    imp_resp = client.post(
        "/api/import",
        files={"file": ("analytics.csv", csv_bytes, "text/csv")},
        data={"handle": "new_creator"},
    )
    assert imp_resp.status_code == 200
    assert imp_resp.json()["posts_imported"] == 1

    # Export CSV and JSON
    exp_csv = client.get("/api/export?handle=new_creator&format=csv")
    assert exp_csv.status_code == 200
    assert "88001" in exp_csv.text

    exp_json = client.get("/api/export?handle=new_creator&format=json")
    assert exp_json.status_code == 200
    parsed = json.loads(exp_json.text)
    assert parsed["posts"][0]["post_id"] == "88001"

    # Delete account
    del_resp = client.delete("/api/accounts/new_creator")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True
