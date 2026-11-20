"""Tests for SQLite database schema, migrations, and time-series snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from x_tracker.database import Database


def test_init_db_creates_tables_and_indexes(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    db.init_db()

    with sqlite3.connect(db_file) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        migrations = conn.execute("SELECT version FROM schema_migrations").fetchall()

    assert {"schema_migrations", "accounts", "profile_snapshots", "posts", "post_snapshots"} <= tables
    assert "idx_profile_snapshots_handle_time" in indexes
    assert "idx_posts_handle_published" in indexes
    assert "idx_post_snapshots_post_time" in indexes
    assert len(migrations) >= 1


def test_account_upsert_and_profile_snapshot_deltas(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()

    acc = db.upsert_account(
        handle="@AlexRivera_Dev",
        display_name="Alex Rivera",
        avatar_url="https://example.com/avatar.png",
        bio="Building local-first tools",
        followers_count=1000,
        following_count=250,
        total_posts=40,
        created_at="2023-01-15T10:00:00Z",
        last_synced_at="2026-11-19T09:00:00Z",
    )
    assert acc["handle"] == "alexrivera_dev"

    snap1 = db.record_profile_snapshot(
        handle="@AlexRivera_Dev",
        followers_count=1000,
        following_count=250,
        total_posts=40,
        captured_at="2026-11-19T09:00:00Z",
    )
    assert snap1["followers_delta"] == 0
    assert snap1["following_delta"] == 0
    assert snap1["posts_delta"] == 0

    snap2 = db.record_profile_snapshot(
        handle="alexrivera_dev",
        followers_count=1145,
        following_count=253,
        total_posts=42,
        captured_at="2026-11-20T09:00:00Z",
    )
    assert snap2["followers_delta"] == 145
    assert snap2["following_delta"] == 3
    assert snap2["posts_delta"] == 2

    snapshots = db.get_profile_snapshots("ALEXRIVERA_DEV")
    assert len(snapshots) == 2
    assert snapshots[1]["followers_count"] == 1145


def test_post_upsert_and_snapshot_views_delta_and_velocity(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()
    db.upsert_account(handle="quant_pulse", display_name="Quant Pulse")

    post_payload = {
        "post_id": "18880011223344",
        "handle": "@Quant_Pulse",
        "published_at": "Wed Nov 18 14:30:00 +0000 2026",
        "text": "Local-first analytics beat SaaS lock-in.",
        "media_type": "photo",
        "is_reply": False,
        "is_repost": False,
        "is_thread": True,
        "permalink": "https://x.com/quant_pulse/status/18880011223344",
        "views": 1200,
        "likes": 85,
        "replies": 12,
        "reposts": 19,
        "bookmarks": 44,
        "quotes": 3,
    }

    first = db.upsert_post_with_snapshot(post_payload, captured_at="2026-11-19T08:00:00Z")
    assert first["published_at"] == "2026-11-18T14:30:00Z"
    assert first["handle"] == "quant_pulse"

    # Second sync 24 hours later with updated counts
    post_payload_2 = {
        **post_payload,
        "views": 3050,
        "likes": 190,
        "replies": 25,
        "reposts": 40,
        "bookmarks": 112,
        "quotes": 8,
    }
    db.upsert_post_with_snapshot(post_payload_2, captured_at="2026-11-20T08:00:00Z")

    detail = db.get_post_detail("18880011223344")
    assert detail is not None
    assert detail["views"] == 3050
    assert detail["bookmarks"] == 112
    assert len(detail["snapshots"]) == 2
    assert detail["snapshots"][0]["views_delta"] == 0
    assert detail["snapshots"][1]["views_delta"] == 1850
    assert detail["velocity_24h"] == 1850


def test_get_posts_filters_and_delete_account(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()
    db.upsert_account(handle="designcraft_io")

    db.upsert_post_with_snapshot(
        {
            "post_id": "101",
            "handle": "designcraft_io",
            "published_at": "2026-11-10T12:00:00Z",
            "text": "Typography hierarchy deep dive",
            "media_type": "photo",
            "is_reply": False,
            "is_repost": False,
            "is_thread": False,
            "views": 4000,
        },
        captured_at="2026-11-11T12:00:00Z",
    )
    db.upsert_post_with_snapshot(
        {
            "post_id": "102",
            "handle": "designcraft_io",
            "published_at": "2026-11-15T12:00:00Z",
            "text": "Replying to feedback on spacing",
            "media_type": "none",
            "is_reply": True,
            "is_repost": False,
            "is_thread": False,
            "views": 500,
        },
        captured_at="2026-11-16T12:00:00Z",
    )

    media_posts = db.get_posts("designcraft_io", post_type="media")
    assert [p["post_id"] for p in media_posts] == ["101"]

    reply_posts = db.get_posts("designcraft_io", post_type="reply")
    assert [p["post_id"] for p in reply_posts] == ["102"]

    search_posts = db.get_posts("designcraft_io", search="Typography")
    assert [p["post_id"] for p in search_posts] == ["101"]

    assert db.delete_account("designcraft_io") is True
    assert db.get_account("designcraft_io") is None
    assert db.get_posts("designcraft_io") == []
