"""Tests for multi-source fetcher, retries, fallback chain, pagination, and CSV/JSON import/export."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from x_tracker.database import Database
from x_tracker.fetcher import MultiSourceFetcher


def test_retry_with_exponential_backoff_and_ua_rotation(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()

    observed_uas: list[str] = []
    sleeps: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        observed_uas.append(request.headers.get("User-Agent", ""))
        if attempts == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        if attempts == 2:
            return httpx.Response(503, json={"error": "upstream unavailable"})
        return httpx.Response(200, json={"ok": True})

    fetcher = MultiSourceFetcher(
        db=db,
        max_retries=4,
        base_backoff=0.2,
        transport=httpx.MockTransport(handler),
        sleep_fn=sleeps.append,
    )

    resp = fetcher.request_with_retry("https://api.fxtwitter.com/test")
    assert resp.status_code == 200
    assert attempts == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]
    assert len(set(observed_uas)) >= 2


def test_fallback_from_fxtwitter_to_vxtwitter_and_syndication(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.fxtwitter.com" in url:
            return httpx.Response(502, text="Bad Gateway")
        if "api.vxtwitter.com" in url and "/status/" in url:
            return httpx.Response(
                200,
                json={
                    "tweetID": "99001122",
                    "user_screen_name": "alexrivera_dev",
                    "text": "Fallback from VxTwitter works cleanly.",
                    "date_epoch": 1795000000,
                    "views": 5400,
                    "likes": 220,
                    "replies": 14,
                    "retweets": 31,
                    "bookmarks": 88,
                    "media_extended": [{"type": "image"}],
                    "tweetURL": "https://x.com/alexrivera_dev/status/99001122",
                },
            )
        if "cdn.syndication.twimg.com" in url:
            next_data = {
                "props": {
                    "pageProps": {
                        "timeline": {
                            "entries": [
                                {
                                    "content": {
                                        "tweet": {
                                            "id_str": "99001123",
                                            "full_text": "Syndication timeline fallback post!",
                                            "created_at": "Wed Nov 18 15:00:00 +0000 2026",
                                            "favorite_count": 95,
                                            "reply_count": 9,
                                            "retweet_count": 12,
                                            "bookmark_count": 30,
                                            "views_count": 2100,
                                            "user": {
                                                "screen_name": "alexrivera_dev",
                                                "name": "Alex Rivera",
                                                "followers_count": 14200,
                                                "friends_count": 410,
                                                "statuses_count": 1820,
                                                "description": "Local-first builder",
                                                "profile_image_url_https": "https://example.com/a.jpg",
                                            },
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
            html = (
                '<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(next_data)
                + "</script></html>"
            )
            return httpx.Response(200, text=html)
        return httpx.Response(404, text="Not found")

    fetcher = MultiSourceFetcher(
        db=db,
        max_retries=1,
        base_backoff=0.01,
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )

    result = fetcher.sync_handle("@alexrivera_dev", post_ids=["99001122"])
    assert result.success is True
    assert result.posts_synced >= 2
    assert "fxtwitter" in result.fallback_chain
    assert any(src in result.fallback_chain for src in ("syndication", "vxtwitter"))

    acc = db.get_account("alexrivera_dev")
    assert acc is not None
    assert acc["followers_count"] == 14200

    p1 = db.get_post_detail("99001122")
    p2 = db.get_post_detail("99001123")
    assert p1 is not None and p1["views"] == 5400 and p1["media_type"] == "photo"
    assert p2 is not None and p2["views"] == 2100


def test_partial_malformed_json_graceful_degradation(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()
    fetcher = MultiSourceFetcher(db=db, sleep_fn=lambda _: None)

    partial_fx = {
        "tweet": {
            "id": "555001",
            "text": None,
            "views": None,
            "likes": "not-a-number",
            "media": None,
            "created_timestamp": None,
        }
    }
    parsed = fetcher.parse_fxtwitter_tweet(partial_fx, "partial_user")
    assert parsed is not None
    assert parsed["post_id"] == "555001"
    assert parsed["views"] == 0
    assert parsed["likes"] == 0
    assert parsed["bookmarks"] == 0
    assert parsed["media_type"] == "none"
    assert parsed["text"] == ""


def test_pagination_cursor_handling_and_cycle_termination(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()
    fetcher = MultiSourceFetcher(db=db, sleep_fn=lambda _: None)

    calls: list[str | None] = []

    def fake_page_fetcher(handle: str, cursor: str | None) -> tuple[list[dict], str | None]:
        calls.append(cursor)
        if cursor is None:
            return ([{"post_id": "1", "handle": handle, "text": "p1"}], "cur_2")
        if cursor == "cur_2":
            return ([{"post_id": "2", "handle": handle, "text": "p2"}], "cur_3")
        if cursor == "cur_3":
            # Duplicate cursor loop must terminate immediately
            return ([{"post_id": "3", "handle": handle, "text": "p3"}], "cur_2")
        return ([], None)

    posts = fetcher.fetch_timeline_paginated(
        "alexrivera_dev", max_pages=10, page_fetcher=fake_page_fetcher
    )
    assert [p["post_id"] for p in posts] == ["1", "2", "3"]
    assert calls == [None, "cur_2", "cur_3"]


def test_import_official_csv_and_archive_json_and_roundtrip_export(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_db()
    fetcher = MultiSourceFetcher(db=db, sleep_fn=lambda _: None)

    official_csv = "\n".join(
        [
            "Tweet id,Tweet permalink,Tweet text,time,impressions,engagements,retweets,replies,likes,bookmarks",
            '1900001,https://x.com/alexrivera_dev/status/1900001,"Shipped SQLite WAL snapshots today",2026-11-10 14:30 +0000,12400,640,48,22,450,120',
            '1900002,https://x.com/alexrivera_dev/status/1900002,"Thread: why local-first wins",2026-11-12 09:15 +0000,8200,410,30,15,280,85',
        ]
    )
    csv_res = fetcher.import_csv(official_csv)
    assert csv_res.posts_imported == 2
    assert csv_res.snapshots_created == 2
    assert csv_res.handle == "alexrivera_dev"

    p = db.get_post_detail("1900001")
    assert p is not None
    assert p["published_at"] == "2026-11-10T14:30:00Z"
    assert p["views"] == 12400
    assert p["bookmarks"] == 120

    # Export native JSON and CSV, then re-import into a fresh handle/db
    exported_json = fetcher.export_json("alexrivera_dev")
    exported_csv = fetcher.export_csv("alexrivera_dev")
    assert "1900001" in exported_json
    assert "1900001" in exported_csv

    # Official X Archive tweets.js format
    archive_js = 'window.YTD.tweet.part0 = ' + json.dumps(
        [
            {
                "tweet": {
                    "id_str": "1900003",
                    "created_at": "Thu Nov 14 16:00:00 +0000 2026",
                    "full_text": "Archive import test post with photo",
                    "favorite_count": "150",
                    "retweet_count": "20",
                    "reply_count": "11",
                    "bookmark_count": "45",
                    "entities": {"media": [{"type": "photo"}]},
                }
            }
        ]
    )
    json_res = fetcher.import_json(archive_js, default_handle="@alexrivera_dev")
    assert json_res.posts_imported == 1
    p3 = db.get_post_detail("1900003")
    assert p3 is not None
    assert p3["media_type"] == "photo"
    assert p3["published_at"] == "2026-11-14T16:00:00Z"
