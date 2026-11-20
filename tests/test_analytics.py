"""Tests for time-window analytics, Cohort vs Velocity modes, KPIs, heatmaps, and bucketing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from x_tracker.analytics import AnalyticsEngine
from x_tracker.database import Database


def _utc_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_resolve_all_time_windows() -> None:
    db = Database(":memory:")
    engine = AnalyticsEngine(db)
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    for tf, expected_days in [("7d", 7), ("30d", 30), ("7w", 49), ("90d", 90)]:
        start, end, prev_start, prev_end, days = engine.resolve_window(tf, reference_now=now)
        assert days == expected_days
        assert end == now
        assert (end - start).days == expected_days
        assert prev_end == start
        assert prev_start is not None and (prev_end - prev_start).days == expected_days

    start, end, prev_start, prev_end, days = engine.resolve_window(
        "custom",
        start_date="2026-10-01",
        end_date="2026-10-15",
        reference_now=now,
    )
    assert start == datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 15, 23, 59, 59, tzinfo=timezone.utc)
    assert days == 15
    assert prev_start is not None and prev_end == start


def test_cohort_vs_velocity_mode_and_kpi_formulas(tmp_path: Path) -> None:
    db = Database(tmp_path / "analytics.db")
    db.init_db()
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    db.upsert_account("alexrivera_dev", display_name="Alex Rivera", followers_count=5500)
    # Profile snapshots: baseline before 7d window (5000) -> inside 7d window (5280 -> 5500)
    db.record_profile_snapshot("alexrivera_dev", 5000, 200, 10, captured_at=_utc_str(now - timedelta(days=10)))
    db.record_profile_snapshot("alexrivera_dev", 5280, 202, 11, captured_at=_utc_str(now - timedelta(days=3)))
    db.record_profile_snapshot("alexrivera_dev", 5500, 205, 12, captured_at=_utc_str(now - timedelta(hours=2)))

    # Post A: Published 60 days ago (OUTSIDE 7d cohort), gained +5,000 views INSIDE 7d window
    db.upsert_post_with_snapshot(
        {
            "post_id": "evergreen_60d",
            "handle": "alexrivera_dev",
            "published_at": _utc_str(now - timedelta(days=60)),
            "text": "Evergreen architecture thread",
            "media_type": "photo",
            "is_thread": True,
            "views": 2000,
            "likes": 100,
            "replies": 10,
            "reposts": 20,
            "bookmarks": 50,
            "quotes": 2,
        },
        captured_at=_utc_str(now - timedelta(days=10)),
    )
    db.insert_post_snapshot(
        post_id="evergreen_60d",
        captured_at=_utc_str(now - timedelta(days=1)),
        views=7000,
        likes=300,
        replies=30,
        reposts=70,
        bookmarks=150,
        quotes=12,
    )

    # Post B: Published 2 days ago (INSIDE 7d cohort) with exact metrics
    # views=1000, likes=40, replies=10, reposts=10, bookmarks=40, quotes=4
    # ER = ((40 + 10 + 10 + 40) / 1000) * 100 = 10.0%
    # High-Signal = ((4*40 + 3*10 + 2*10 + 2.5*4 + 1*40) / 1000) * 1000 = 160+30+20+10+40 = 260.0
    # Bookmark/Like = (40 / 40) * 100 = 100.0%
    # Virality = (10 / 1000) * 100 = 1.0%
    db.upsert_post_with_snapshot(
        {
            "post_id": "recent_2d",
            "handle": "alexrivera_dev",
            "published_at": _utc_str(now - timedelta(days=2)),
            "text": "Text-only release notes",
            "media_type": "none",
            "views": 1000,
            "likes": 40,
            "replies": 10,
            "reposts": 10,
            "bookmarks": 40,
            "quotes": 4,
        },
        captured_at=_utc_str(now - timedelta(hours=1)),
    )

    engine = AnalyticsEngine(db)
    cohort = engine.compute_dashboard_analytics(
        "alexrivera_dev", timeframe="7d", mode="cohort", reference_now=now
    )
    velocity = engine.compute_dashboard_analytics(
        "alexrivera_dev", timeframe="7d", mode="velocity", reference_now=now
    )

    # Cohort 7d includes only Post B
    assert cohort["kpis"]["impressions"] == 1000
    assert cohort["kpis"]["total_posts"] == 1
    assert cohort["kpis"]["engagement_rate"] == 10.0
    assert cohort["kpis"]["high_signal_score"] == 260.0
    assert cohort["kpis"]["bookmark_to_like_ratio"] == 100.0
    assert cohort["kpis"]["virality_ratio"] == 1.0
    assert cohort["kpis"]["followers_gained"] == 500
    assert round(cohort["kpis"]["follower_daily_velocity"], 2) == round(500 / 7, 2)

    # Velocity 7d includes Post A's +5,000 gain + Post B's 1,000 new views = 6,000
    assert velocity["kpis"]["impressions"] == 6000
    assert cohort["kpis"]["velocity_impressions"] == 6000
    assert velocity["kpis"]["cohort_impressions"] == 1000


def test_zero_division_and_empty_account_safety(tmp_path: Path) -> None:
    db = Database(tmp_path / "empty.db")
    db.init_db()
    db.upsert_account("zero_views")
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    db.upsert_post_with_snapshot(
        {
            "post_id": "p0",
            "handle": "zero_views",
            "published_at": _utc_str(now - timedelta(hours=2)),
            "text": "Zero impressions post",
            "views": 0,
            "likes": 0,
            "replies": 0,
            "reposts": 0,
            "bookmarks": 0,
        },
        captured_at=_utc_str(now),
    )

    engine = AnalyticsEngine(db)
    res = engine.compute_dashboard_analytics("zero_views", timeframe="30d", reference_now=now)
    assert res["kpis"]["engagement_rate"] == 0.0
    assert res["kpis"]["high_signal_score"] == 0.0
    assert res["kpis"]["bookmark_to_like_ratio"] == 0.0
    assert res["kpis"]["virality_ratio"] == 0.0

    missing = engine.compute_dashboard_analytics("nobody", timeframe="30d", reference_now=now)
    assert missing["kpis"]["impressions"] == 0
    assert missing["posts"] == []


def test_granularity_heatmap_media_breakdown_and_sorting(tmp_path: Path) -> None:
    db = Database(tmp_path / "charts.db")
    db.init_db()
    db.upsert_account("designcraft_io")
    # Pick fixed dates on Tuesday 14:00 UTC and Friday 09:00 UTC
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    tuesday_14 = datetime(2026, 11, 17, 14, 0, 0, tzinfo=timezone.utc)  # Tue
    friday_09 = datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc)  # Fri

    db.upsert_post_with_snapshot(
        {
            "post_id": "m1",
            "handle": "designcraft_io",
            "published_at": _utc_str(tuesday_14),
            "text": "Visual showcase",
            "media_type": "photo",
            "views": 9000,
            "likes": 450,
            "replies": 30,
            "reposts": 90,
            "bookmarks": 220,
        },
        captured_at=_utc_str(now - timedelta(hours=2)),
    )
    db.upsert_post_with_snapshot(
        {
            "post_id": "t1",
            "handle": "designcraft_io",
            "published_at": _utc_str(friday_09),
            "text": "Quick text tip",
            "media_type": "none",
            "views": 1500,
            "likes": 60,
            "replies": 5,
            "reposts": 4,
            "bookmarks": 15,
        },
        captured_at=_utc_str(now - timedelta(hours=2)),
    )

    engine = AnalyticsEngine(db)
    daily = engine.compute_dashboard_analytics(
        "designcraft_io", timeframe="30d", granularity="daily", sort_by="bookmarks", reference_now=now
    )
    weekly = engine.compute_dashboard_analytics(
        "designcraft_io", timeframe="90d", granularity="weekly", reference_now=now
    )
    monthly = engine.compute_dashboard_analytics(
        "designcraft_io", timeframe="lifetime", granularity="monthly", reference_now=now
    )

    assert len(daily["charts"]["over_time"]) >= 30
    assert len(weekly["charts"]["over_time"]) >= 12
    assert len(monthly["charts"]["over_time"]) >= 1
    assert daily["charts"]["heatmap"]["best_day"] == "Tue"
    assert daily["charts"]["heatmap"]["best_hour"] == 14
    assert [p["post_id"] for p in daily["posts"]] == ["m1", "t1"]

    media_cats = {row["category"]: row for row in daily["charts"]["media_breakdown"]}
    assert media_cats["photo"]["avg_views"] == 9000.0
    assert media_cats["text"]["avg_views"] == 1500.0
