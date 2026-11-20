"""Tests for 120-day demo seeder and background sync scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from x_tracker.analytics import AnalyticsEngine
from x_tracker.database import Database
from x_tracker.fetcher import MultiSourceFetcher
from x_tracker.scheduler import BackgroundSyncScheduler, parse_interval_seconds
from x_tracker.seeder import seed_demo_data


def test_seed_demo_data_populates_120_days_and_all_windows(tmp_path: Path) -> None:
    db = Database(tmp_path / "demo.db")
    db.init_db()
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    summary = seed_demo_data(db, days=120, reference_now=now)
    assert summary["accounts_seeded"] == 3
    assert summary["profile_snapshots_seeded"] >= 360
    assert summary["posts_seeded"] >= 270

    accounts = db.list_accounts()
    handles = [a["handle"] for a in accounts]
    assert set(handles) == {"alexrivera_dev", "quant_pulse", "designcraft_io"}

    for handle in handles:
        prof_snaps = db.get_profile_snapshots(handle)
        assert len(prof_snaps) >= 120

    engine = AnalyticsEngine(db)
    w7 = engine.compute_dashboard_analytics("alexrivera_dev", timeframe="7d", reference_now=now)
    w30 = engine.compute_dashboard_analytics("alexrivera_dev", timeframe="30d", reference_now=now)
    w7w = engine.compute_dashboard_analytics("alexrivera_dev", timeframe="7w", reference_now=now)
    w90 = engine.compute_dashboard_analytics("alexrivera_dev", timeframe="90d", reference_now=now)
    wlife = engine.compute_dashboard_analytics("alexrivera_dev", timeframe="lifetime", reference_now=now)

    assert 0 < w7["kpis"]["total_posts"] < w30["kpis"]["total_posts"]
    assert w30["kpis"]["total_posts"] < w7w["kpis"]["total_posts"]
    assert w7w["kpis"]["total_posts"] < w90["kpis"]["total_posts"]
    assert w90["kpis"]["total_posts"] < wlife["kpis"]["total_posts"]
    assert w7["kpis"]["velocity_impressions"] > w7["kpis"]["cohort_impressions"]

    # Check post detail has >= 5 snapshots
    sample_post = w30["posts"][0]
    detail = db.get_post_detail(sample_post["post_id"])
    assert detail is not None
    assert len(detail["snapshots"]) >= 5


def test_parse_interval_seconds() -> None:
    assert parse_interval_seconds("6h") == 21600
    assert parse_interval_seconds("30m") == 1800
    assert parse_interval_seconds("45s") == 45
    assert parse_interval_seconds("1d") == 86400
    assert parse_interval_seconds("120") == 120
    with pytest.raises(ValueError):
        parse_interval_seconds("0h")
    with pytest.raises(ValueError):
        parse_interval_seconds("invalid")


def test_background_scheduler_run_once_and_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "sched.db")
    db.init_db()
    db.upsert_account("alexrivera_dev", display_name="Alex Rivera")

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.fxtwitter.com/alexrivera_dev" == str(request.url).replace("https://", ""):
            return httpx.Response(
                200,
                json={
                    "user": {
                        "screen_name": "alexrivera_dev",
                        "name": "Alex Rivera",
                        "followers": 25010,
                        "following": 410,
                        "tweets": 900,
                    }
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    fetcher = MultiSourceFetcher(
        db=db,
        max_retries=1,
        base_backoff=0.01,
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )
    scheduler = BackgroundSyncScheduler(db=db, fetcher=fetcher, interval_seconds=21600)
    results = scheduler.run_once()
    assert len(results) == 1
    assert results[0].handle == "alexrivera_dev"
    assert results[0].profile_updated is True

    status = scheduler.get_status()
    assert status["runs_completed"] == 1
    assert status["interval_seconds"] == 21600
