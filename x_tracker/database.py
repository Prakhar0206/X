"""SQLite database storage and time-series snapshot management."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from x_tracker.models import (
    normalize_handle,
    normalize_utc_iso,
    parse_utc_datetime,
    safe_int,
    utc_now_iso,
)

DEFAULT_DB_PATH = os.environ.get("X_TRACKER_DB", "x_analytics.db")

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial_schema",
        """
        CREATE TABLE IF NOT EXISTS accounts (
            handle TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            avatar_url TEXT NOT NULL DEFAULT '',
            bio TEXT NOT NULL DEFAULT '',
            followers_count INTEGER NOT NULL DEFAULT 0,
            following_count INTEGER NOT NULL DEFAULT 0,
            total_posts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profile_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            followers_count INTEGER NOT NULL DEFAULT 0,
            following_count INTEGER NOT NULL DEFAULT 0,
            total_posts INTEGER NOT NULL DEFAULT 0,
            followers_delta INTEGER NOT NULL DEFAULT 0,
            following_delta INTEGER NOT NULL DEFAULT 0,
            posts_delta INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (handle) REFERENCES accounts(handle) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            handle TEXT NOT NULL,
            published_at TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT 'none',
            is_reply INTEGER NOT NULL DEFAULT 0,
            is_repost INTEGER NOT NULL DEFAULT 0,
            is_thread INTEGER NOT NULL DEFAULT 0,
            permalink TEXT NOT NULL DEFAULT '',
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            reposts INTEGER NOT NULL DEFAULT 0,
            bookmarks INTEGER NOT NULL DEFAULT 0,
            quotes INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (handle) REFERENCES accounts(handle) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS post_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            reposts INTEGER NOT NULL DEFAULT 0,
            bookmarks INTEGER NOT NULL DEFAULT 0,
            quotes INTEGER NOT NULL DEFAULT 0,
            views_delta INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_profile_snapshots_handle_time
            ON profile_snapshots(handle, captured_at);

        CREATE INDEX IF NOT EXISTS idx_posts_handle_published
            ON posts(handle, published_at);

        CREATE INDEX IF NOT EXISTS idx_post_snapshots_post_time
            ON post_snapshots(post_id, captured_at);

        CREATE INDEX IF NOT EXISTS idx_post_snapshots_captured
            ON post_snapshots(captured_at);
        """,
    ),
]


def compute_velocity_24h(snapshots: list[dict[str, Any]]) -> int:
    """Compute 24-hour impression velocity from an ordered list of post snapshots."""
    if not snapshots:
        return 0
    if len(snapshots) == 1:
        return int(snapshots[0].get("views_delta", 0))

    latest = snapshots[-1]
    latest_dt = parse_utc_datetime(latest["captured_at"])
    cutoff_dt = latest_dt - timedelta(hours=24)

    in_window = [
        snap
        for snap in snapshots
        if parse_utc_datetime(snap["captured_at"]) > cutoff_dt
    ]
    if not in_window:
        return 0

    if len(in_window) == 1 and len(snapshots) >= 2:
        prev_dt = parse_utc_datetime(snapshots[-2]["captured_at"])
        hours_span = max(1.0, (latest_dt - prev_dt).total_seconds() / 3600.0)
        if hours_span > 24.0:
            return int(round(int(in_window[0].get("views_delta", 0)) * (24.0 / hours_span)))

    return sum(int(snap.get("views_delta", 0)) for snap in in_window)


class Database:
    """Thread-safe local SQLite storage engine with migrations and snapshot history."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection and commit/rollback cleanly."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Initialize WAL mode and run any pending schema migrations."""
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                """
            )
            applied = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            now_iso = utc_now_iso()
            for version, name, sql_script in MIGRATIONS:
                if version not in applied:
                    conn.executescript(sql_script)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, now_iso),
                    )

    def _ensure_account_exists(
        self, conn: sqlite3.Connection, handle: str, timestamp: str
    ) -> str:
        norm = normalize_handle(handle)
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts (
                handle, display_name, avatar_url, bio,
                followers_count, following_count, total_posts,
                created_at, last_synced_at
            ) VALUES (?, ?, '', '', 0, 0, 0, ?, ?)
            """,
            (norm, norm, timestamp, timestamp),
        )
        return norm

    def upsert_account(
        self,
        handle: str,
        display_name: str = "",
        avatar_url: str = "",
        bio: str = "",
        followers_count: int = 0,
        following_count: int = 0,
        total_posts: int = 0,
        created_at: str | None = None,
        last_synced_at: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update a tracked X handle."""
        norm = normalize_handle(handle)
        if not norm:
            raise ValueError("Handle cannot be empty.")
        created_iso = normalize_utc_iso(created_at) if created_at else utc_now_iso()
        synced_iso = normalize_utc_iso(last_synced_at) if last_synced_at else utc_now_iso()
        disp = (display_name or "").strip() or norm

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM accounts WHERE handle = ?", (norm,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO accounts (
                        handle, display_name, avatar_url, bio,
                        followers_count, following_count, total_posts,
                        created_at, last_synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        norm,
                        disp,
                        avatar_url or "",
                        bio or "",
                        safe_int(followers_count),
                        safe_int(following_count),
                        safe_int(total_posts),
                        created_iso,
                        synced_iso,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts SET
                        display_name = ?,
                        avatar_url = ?,
                        bio = ?,
                        followers_count = ?,
                        following_count = ?,
                        total_posts = ?,
                        last_synced_at = ?
                    WHERE handle = ?
                    """,
                    (
                        disp if display_name else existing["display_name"],
                        avatar_url if avatar_url else existing["avatar_url"],
                        bio if bio else existing["bio"],
                        safe_int(followers_count, existing["followers_count"]),
                        safe_int(following_count, existing["following_count"]),
                        safe_int(total_posts, existing["total_posts"]),
                        synced_iso,
                        norm,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM accounts WHERE handle = ?", (norm,)
            ).fetchone()
        return dict(row) if row else {}

    def record_profile_snapshot(
        self,
        handle: str,
        followers_count: int,
        following_count: int,
        total_posts: int,
        captured_at: str | None = None,
        initial_delta: int | None = None,
    ) -> dict[str, Any]:
        """Record a timestamped profile snapshot and compute net deltas vs prior snapshot."""
        captured_iso = normalize_utc_iso(captured_at)
        followers_val = safe_int(followers_count)
        following_val = safe_int(following_count)
        posts_val = safe_int(total_posts)

        with self.connect() as conn:
            norm = self._ensure_account_exists(conn, handle, captured_iso)
            prev = conn.execute(
                """
                SELECT followers_count, following_count, total_posts
                FROM profile_snapshots
                WHERE handle = ? AND captured_at <= ?
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
                (norm, captured_iso),
            ).fetchone()

            if prev is None:
                f_delta = initial_delta if initial_delta is not None else 0
                fg_delta = 0
                p_delta = 0
            else:
                f_delta = followers_val - int(prev["followers_count"])
                fg_delta = following_val - int(prev["following_count"])
                p_delta = posts_val - int(prev["total_posts"])

            cursor = conn.execute(
                """
                INSERT INTO profile_snapshots (
                    handle, captured_at, followers_count, following_count,
                    total_posts, followers_delta, following_delta, posts_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm,
                    captured_iso,
                    followers_val,
                    following_val,
                    posts_val,
                    f_delta,
                    fg_delta,
                    p_delta,
                ),
            )
            conn.execute(
                """
                UPDATE accounts
                SET followers_count = ?, following_count = ?, total_posts = ?, last_synced_at = ?
                WHERE handle = ?
                """,
                (followers_val, following_val, posts_val, captured_iso, norm),
            )
            row = conn.execute(
                "SELECT * FROM profile_snapshots WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row) if row else {}

    def upsert_post_with_snapshot(
        self,
        post: dict[str, Any],
        captured_at: str | None = None,
        snapshot_views_delta: int | None = None,
    ) -> dict[str, Any]:
        """Upsert canonical post metadata and append a time-series snapshot."""
        post_id = str(post.get("post_id", "")).strip()
        if not post_id:
            raise ValueError("post_id is required")

        captured_iso = normalize_utc_iso(captured_at)
        published_iso = normalize_utc_iso(post.get("published_at"))
        handle_norm = normalize_handle(str(post.get("handle", "")))
        if not handle_norm:
            raise ValueError("handle is required")

        views = safe_int(post.get("views"))
        likes = safe_int(post.get("likes"))
        replies = safe_int(post.get("replies"))
        reposts = safe_int(post.get("reposts"))
        bookmarks = safe_int(post.get("bookmarks"))
        quotes = safe_int(post.get("quotes"))
        media_type = str(post.get("media_type") or "none").strip().lower() or "none"
        is_reply = 1 if post.get("is_reply") else 0
        is_repost = 1 if post.get("is_repost") else 0
        is_thread = 1 if post.get("is_thread") else 0
        permalink = str(post.get("permalink") or "").strip() or f"https://x.com/{handle_norm}/status/{post_id}"
        text = str(post.get("text") or "")

        with self.connect() as conn:
            self._ensure_account_exists(conn, handle_norm, captured_iso)
            conn.execute(
                """
                INSERT INTO posts (
                    post_id, handle, published_at, text, media_type,
                    is_reply, is_repost, is_thread, permalink,
                    views, likes, replies, reposts, bookmarks, quotes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    text = CASE WHEN excluded.text != '' THEN excluded.text ELSE posts.text END,
                    media_type = CASE WHEN excluded.media_type != 'none' THEN excluded.media_type ELSE posts.media_type END,
                    is_reply = excluded.is_reply,
                    is_repost = excluded.is_repost,
                    is_thread = excluded.is_thread,
                    permalink = CASE WHEN excluded.permalink != '' THEN excluded.permalink ELSE posts.permalink END,
                    views = MAX(posts.views, excluded.views),
                    likes = MAX(posts.likes, excluded.likes),
                    replies = MAX(posts.replies, excluded.replies),
                    reposts = MAX(posts.reposts, excluded.reposts),
                    bookmarks = MAX(posts.bookmarks, excluded.bookmarks),
                    quotes = MAX(posts.quotes, excluded.quotes),
                    updated_at = excluded.updated_at
                """,
                (
                    post_id,
                    handle_norm,
                    published_iso,
                    text,
                    media_type,
                    is_reply,
                    is_repost,
                    is_thread,
                    permalink,
                    views,
                    likes,
                    replies,
                    reposts,
                    bookmarks,
                    quotes,
                    captured_iso,
                ),
            )
            self._insert_post_snapshot_conn(
                conn=conn,
                post_id=post_id,
                captured_iso=captured_iso,
                views=views,
                likes=likes,
                replies=replies,
                reposts=reposts,
                bookmarks=bookmarks,
                quotes=quotes,
                views_delta=snapshot_views_delta,
            )
            row = conn.execute(
                "SELECT * FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
        return self._row_to_post(row) if row else {}

    def _insert_post_snapshot_conn(
        self,
        conn: sqlite3.Connection,
        post_id: str,
        captured_iso: str,
        views: int,
        likes: int,
        replies: int,
        reposts: int,
        bookmarks: int,
        quotes: int,
        views_delta: int | None,
    ) -> dict[str, Any]:
        prev = conn.execute(
            """
            SELECT views
            FROM post_snapshots
            WHERE post_id = ? AND captured_at <= ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (post_id, captured_iso),
        ).fetchone()

        if views_delta is not None:
            delta = int(views_delta)
        elif prev is None:
            delta = 0
        else:
            delta = max(0, views - int(prev["views"]))

        cursor = conn.execute(
            """
            INSERT INTO post_snapshots (
                post_id, captured_at, views, likes, replies, reposts, bookmarks, quotes, views_delta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (post_id, captured_iso, views, likes, replies, reposts, bookmarks, quotes, delta),
        )
        row = conn.execute(
            "SELECT * FROM post_snapshots WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row) if row else {}

    def insert_post_snapshot(
        self,
        post_id: str,
        captured_at: str,
        views: int,
        likes: int,
        replies: int,
        reposts: int,
        bookmarks: int,
        quotes: int = 0,
        views_delta: int | None = None,
    ) -> dict[str, Any]:
        """Insert a time-series post snapshot and update latest post metrics if newer."""
        captured_iso = normalize_utc_iso(captured_at)
        v = safe_int(views)
        lk = safe_int(likes)
        rp = safe_int(replies)
        rt = safe_int(reposts)
        bm = safe_int(bookmarks)
        qt = safe_int(quotes)

        with self.connect() as conn:
            snap = self._insert_post_snapshot_conn(
                conn=conn,
                post_id=str(post_id),
                captured_iso=captured_iso,
                views=v,
                likes=lk,
                replies=rp,
                reposts=rt,
                bookmarks=bm,
                quotes=qt,
                views_delta=views_delta,
            )
            conn.execute(
                """
                UPDATE posts SET
                    views = MAX(views, ?),
                    likes = MAX(likes, ?),
                    replies = MAX(replies, ?),
                    reposts = MAX(reposts, ?),
                    bookmarks = MAX(bookmarks, ?),
                    quotes = MAX(quotes, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE post_id = ?
                """,
                (v, lk, rp, rt, bm, qt, captured_iso, str(post_id)),
            )
        return snap

    def list_accounts(self) -> list[dict[str, Any]]:
        """Return all tracked accounts ordered by handle."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY handle ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_account(self, handle: str) -> dict[str, Any] | None:
        """Return a single tracked account or None."""
        norm = normalize_handle(handle)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE handle = ?", (norm,)
            ).fetchone()
        return dict(row) if row else None

    def delete_account(self, handle: str) -> bool:
        """Delete a tracked account and cascade delete its posts and snapshots."""
        norm = normalize_handle(handle)
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM accounts WHERE handle = ?", (norm,))
        return cursor.rowcount > 0

    def _row_to_post(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["is_reply"] = bool(item.get("is_reply"))
        item["is_repost"] = bool(item.get("is_repost"))
        item["is_thread"] = bool(item.get("is_thread"))
        return item

    def get_posts(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
        search: str | None = None,
        post_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query posts for a handle with optional UTC date bounds, search, and post type."""
        norm = normalize_handle(handle)
        clauses = ["handle = ?"]
        params: list[Any] = [norm]

        if start_date:
            clauses.append("published_at >= ?")
            params.append(normalize_utc_iso(start_date))
        if end_date:
            clauses.append("published_at <= ?")
            params.append(normalize_utc_iso(end_date))
        if search and search.strip():
            clauses.append("LOWER(text) LIKE ?")
            params.append(f"%{search.strip().lower()}%")

        ptype = (post_type or "").strip().lower()
        if ptype == "original":
            clauses.append("is_reply = 0 AND is_repost = 0")
        elif ptype == "thread":
            clauses.append("is_thread = 1")
        elif ptype == "reply":
            clauses.append("is_reply = 1")
        elif ptype == "repost":
            clauses.append("is_repost = 1")
        elif ptype == "media":
            clauses.append("media_type != 'none' AND media_type != ''")
        elif ptype in {"text", "text-only"}:
            clauses.append("(media_type = 'none' OR media_type = '')")

        where_sql = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM posts WHERE {where_sql} ORDER BY published_at DESC",
                params,
            ).fetchall()
        return [self._row_to_post(r) for r in rows]

    def get_post_detail(self, post_id: str) -> dict[str, Any] | None:
        """Return post metadata, ordered snapshot history, and 24h velocity."""
        with self.connect() as conn:
            post_row = conn.execute(
                "SELECT * FROM posts WHERE post_id = ?", (str(post_id),)
            ).fetchone()
            if post_row is None:
                return None
            snap_rows = conn.execute(
                """
                SELECT * FROM post_snapshots
                WHERE post_id = ?
                ORDER BY captured_at ASC, id ASC
                """,
                (str(post_id),),
            ).fetchall()

        post = self._row_to_post(post_row)
        snapshots = [dict(s) for s in snap_rows]
        post["snapshots"] = snapshots
        post["velocity_24h"] = compute_velocity_24h(snapshots)
        return post

    def get_profile_snapshots(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ordered profile snapshots for a handle within optional UTC date bounds."""
        norm = normalize_handle(handle)
        clauses = ["handle = ?"]
        params: list[Any] = [norm]
        if start_date:
            clauses.append("captured_at >= ?")
            params.append(normalize_utc_iso(start_date))
        if end_date:
            clauses.append("captured_at <= ?")
            params.append(normalize_utc_iso(end_date))

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM profile_snapshots
                WHERE {' AND '.join(clauses)}
                ORDER BY captured_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_post_snapshots_in_window(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return post snapshots joined with post metadata within optional UTC date bounds."""
        norm = normalize_handle(handle)
        clauses = ["p.handle = ?"]
        params: list[Any] = [norm]
        if start_date:
            clauses.append("ps.captured_at >= ?")
            params.append(normalize_utc_iso(start_date))
        if end_date:
            clauses.append("ps.captured_at <= ?")
            params.append(normalize_utc_iso(end_date))

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    ps.*,
                    p.handle,
                    p.published_at,
                    p.text,
                    p.media_type,
                    p.is_reply,
                    p.is_repost,
                    p.is_thread,
                    p.permalink
                FROM post_snapshots ps
                JOIN posts p ON p.post_id = ps.post_id
                WHERE {' AND '.join(clauses)}
                ORDER BY ps.captured_at ASC, ps.id ASC
                """,
                params,
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["is_reply"] = bool(item.get("is_reply"))
            item["is_repost"] = bool(item.get("is_repost"))
            item["is_thread"] = bool(item.get("is_thread"))
            result.append(item)
        return result

    def get_all_post_snapshots_for_handle(self, handle: str) -> dict[str, list[dict[str, Any]]]:
        """Return snapshots grouped by post_id for a handle (ordered ASC)."""
        norm = normalize_handle(handle)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ps.*
                FROM post_snapshots ps
                JOIN posts p ON p.post_id = ps.post_id
                WHERE p.handle = ?
                ORDER BY ps.post_id ASC, ps.captured_at ASC, ps.id ASC
                """,
                (norm,),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            pid = str(r["post_id"])
            grouped.setdefault(pid, []).append(dict(r))
        return grouped
