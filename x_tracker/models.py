"""Core domain models and normalization utilities for the X Analytics Tracker."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from dateutil import parser as dateutil_parser
from pydantic import BaseModel, Field

UTC_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def normalize_handle(handle: str) -> str:
    """Normalize an X handle to lowercase without leading '@' or whitespace."""
    cleaned = (handle or "").strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned.strip().lower()


def safe_int(value: Any, default: int = 0) -> int:
    """Safely coerce an arbitrary upstream field into a non-negative integer."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        return max(0, int(float(text)))
    except (ValueError, TypeError):
        return default


def utc_now_iso() -> str:
    """Return the current UTC time formatted as YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.now(timezone.utc).strftime(UTC_ISO_FORMAT)


def parse_utc_datetime(value: str | int | float | datetime | None) -> datetime:
    """Parse any timestamp representation into a timezone-aware UTC datetime."""
    if value is None:
        return datetime.now(timezone.utc)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    raw = str(value).strip()
    if not raw:
        return datetime.now(timezone.utc)

    # Numeric epoch strings
    if raw.lstrip("-").replace(".", "", 1).isdigit():
        try:
            ts = float(raw)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError):
            pass

    # RFC-2822 Twitter dates (e.g. "Wed Oct 10 20:19:24 +0000 2018")
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    # ISO-8601 / X Analytics CSV timestamps / general dates
    try:
        dt = dateutil_parser.parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return datetime.now(timezone.utc)


def normalize_utc_iso(value: str | int | float | datetime | None) -> str:
    """Normalize any timestamp input into canonical UTC ISO-8601 (YYYY-MM-DDTHH:MM:SSZ)."""
    return parse_utc_datetime(value).strftime(UTC_ISO_FORMAT)


class AccountRecord(BaseModel):
    """Tracked X account metadata."""

    handle: str
    display_name: str = ""
    avatar_url: str = ""
    bio: str = ""
    followers_count: int = 0
    following_count: int = 0
    total_posts: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    last_synced_at: str = Field(default_factory=utc_now_iso)


class ProfileSnapshotRecord(BaseModel):
    """Timestamped account snapshot capturing follower/post growth deltas."""

    id: int | None = None
    handle: str
    captured_at: str = Field(default_factory=utc_now_iso)
    followers_count: int = 0
    following_count: int = 0
    total_posts: int = 0
    followers_delta: int = 0
    following_delta: int = 0
    posts_delta: int = 0


class PostRecord(BaseModel):
    """Canonical X post record with latest observed metrics."""

    post_id: str
    handle: str
    published_at: str = Field(default_factory=utc_now_iso)
    text: str = ""
    media_type: str = "none"
    is_reply: bool = False
    is_repost: bool = False
    is_thread: bool = False
    permalink: str = ""
    views: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    bookmarks: int = 0
    quotes: int = 0
    updated_at: str = Field(default_factory=utc_now_iso)


class PostSnapshotRecord(BaseModel):
    """Time-series metric snapshot for a single post."""

    id: int | None = None
    post_id: str
    captured_at: str = Field(default_factory=utc_now_iso)
    views: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    bookmarks: int = 0
    quotes: int = 0
    views_delta: int = 0


class SyncResult(BaseModel):
    """Outcome summary of a handle synchronization run."""

    handle: str
    source_used: str = "none"
    fallback_chain: list[str] = Field(default_factory=list)
    profile_updated: bool = False
    posts_synced: int = 0
    snapshots_recorded: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    synced_at: str = Field(default_factory=utc_now_iso)
    success: bool = True


class ImportResult(BaseModel):
    """Outcome summary of a CSV or JSON archive import."""

    format: str
    handle: str
    posts_imported: int = 0
    snapshots_created: int = 0
    skipped_rows: int = 0
    warnings: list[str] = Field(default_factory=list)
    imported_at: str = Field(default_factory=utc_now_iso)
