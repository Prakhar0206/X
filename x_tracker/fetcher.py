"""Multi-source fallback fetching engine, pagination, and CSV/JSON importers."""

from __future__ import annotations

import csv
import io
import json
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

import httpx
from bs4 import BeautifulSoup

from x_tracker.database import Database
from x_tracker.models import (
    ImportResult,
    SyncResult,
    normalize_handle,
    normalize_utc_iso,
    safe_int,
    utc_now_iso,
)

USER_AGENTS: tuple[str, ...] = (
    "curl/8.7.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "XAnalyticsTracker/1.0 (+https://github.com/local-first/x-analytics-tracker)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
)


def _first_non_none(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        val = mapping.get(key)
        if val is not None:
            return val
    return None

DEFAULT_NITTER_INSTANCES: tuple[str, ...] = (
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.privacyredirect.com",
)

STATUS_URL_RE = re.compile(r"https?://(?:x\.com|twitter\.com|[^/]+)/([A-Za-z0-9_]{1,30})/status/(\d+)")
YTD_PREFIX_RE = re.compile(r"^\s*window\.YTD\.[A-Za-z0-9_]+\.part\d+\s*=\s*", re.IGNORECASE)


def _extract_handle_from_url(url: str) -> str:
    match = STATUS_URL_RE.search(url or "")
    if match:
        return normalize_handle(match.group(1))
    return ""


def _extract_id_from_url(url: str) -> str:
    match = STATUS_URL_RE.search(url or "")
    if match:
        return match.group(2)
    return ""


def _detect_media_type(media_obj: Any, entities_obj: Any = None) -> str:
    """Infer normalized media_type ('photo', 'video', 'poll', 'none') from upstream fields."""
    if isinstance(media_obj, dict):
        if media_obj.get("videos") or media_obj.get("video"):
            return "video"
        if media_obj.get("photos") or media_obj.get("photo"):
            return "photo"
        if media_obj.get("poll"):
            return "poll"
        all_items = media_obj.get("all")
        if isinstance(all_items, list) and all_items:
            first_type = str(all_items[0].get("type", "")).lower()
            if "video" in first_type or "gif" in first_type:
                return "video"
            if "photo" in first_type or "image" in first_type:
                return "photo"

    if isinstance(media_obj, list) and media_obj:
        first = media_obj[0]
        if isinstance(first, dict):
            mtype = str(first.get("type", "")).lower()
            if "video" in mtype or "gif" in mtype:
                return "video"
            if "photo" in mtype or "image" in mtype:
                return "photo"
            if "poll" in mtype:
                return "poll"

    if isinstance(entities_obj, dict):
        ent_media = entities_obj.get("media")
        if isinstance(ent_media, list) and ent_media:
            mtype = str(ent_media[0].get("type", "")).lower()
            if "video" in mtype or "gif" in mtype:
                return "video"
            return "photo"

    return "none"


class MultiSourceFetcher:
    """Resilient multi-source X data fetcher with exponential backoff, fallback, and archive import."""

    def __init__(
        self,
        db: Database,
        timeout: float = 10.0,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        nitter_instances: tuple[str, ...] = DEFAULT_NITTER_INSTANCES,
        transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.base_backoff = base_backoff
        self.nitter_instances = nitter_instances
        self.transport = transport
        self.sleep_fn = sleep_fn
        self._ua_index = 0
        self._thread_local = threading.local()

    def _next_user_agent(self) -> str:
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        return ua

    def request_with_retry(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        retry_on_404: bool = False,
    ) -> httpx.Response:
        """Perform an HTTP GET with User-Agent rotation, exponential backoff, and jitter."""
        last_exc: Exception | None = None
        last_response: httpx.Response | None = None
        shared_client: httpx.Client | None = getattr(self._thread_local, "client", None)

        for attempt in range(self.max_retries):
            req_headers = {
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
                "User-Agent": self._next_user_agent(),
            }
            if headers:
                req_headers.update(headers)

            try:
                if shared_client is not None:
                    response = shared_client.get(url, headers=req_headers, params=params)
                else:
                    with httpx.Client(
                        timeout=self.timeout,
                        transport=self.transport,
                        follow_redirects=True,
                    ) as client:
                        response = client.get(url, headers=req_headers, params=params)
                last_response = response

                should_retry = (
                    response.status_code == 429
                    or 500 <= response.status_code < 600
                    or (retry_on_404 and response.status_code == 404)
                )
                if should_retry:
                    if attempt < self.max_retries - 1:
                        delay = (self.base_backoff * (2 ** attempt)) + random.uniform(
                            0.01, max(0.02, self.base_backoff * 0.4)
                        )
                        self.sleep_fn(delay)
                        continue
                return response
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    delay = (self.base_backoff * (2 ** attempt)) + random.uniform(
                        0.01, max(0.02, self.base_backoff * 0.4)
                    )
                    self.sleep_fn(delay)
                    continue

        if last_response is not None:
            return last_response
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Failed request to {url}")

    def parse_fxtwitter_profile(self, data: Any, handle: str) -> dict[str, Any] | None:
        """Extract normalized profile metadata from an FxTwitter user payload."""
        if not isinstance(data, dict):
            return None
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        if not isinstance(user, dict) or not (
            user.get("screen_name") or user.get("name") or user.get("followers") is not None
        ):
            return None

        norm = normalize_handle(str(user.get("screen_name") or handle))
        return {
            "handle": norm,
            "display_name": str(user.get("name") or norm),
            "avatar_url": str(user.get("avatar_url") or ""),
            "bio": str(user.get("description") or user.get("bio") or ""),
            "followers_count": safe_int(_first_non_none(user, "followers", "followers_count")),
            "following_count": safe_int(_first_non_none(user, "following", "friends_count")),
            "total_posts": safe_int(_first_non_none(user, "tweets", "statuses", "statuses_count")),
            "created_at": normalize_utc_iso(user.get("joined") or user.get("created_at")),
        }

    def parse_fxtwitter_tweet(
        self, data: Any, handle: str, own_only: bool = False
    ) -> dict[str, Any] | None:
        """Extract normalized post metrics from an FxTwitter tweet payload without failing on partial fields."""
        if not isinstance(data, dict):
            return None
        tweet = data.get("tweet") if isinstance(data.get("tweet"), dict) else data
        if not isinstance(tweet, dict):
            return None

        post_id = str(tweet.get("id") or tweet.get("rest_id") or tweet.get("id_str") or "").strip()
        if not post_id:
            return None

        norm_target = normalize_handle(handle)
        author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
        author_screen = normalize_handle(str(author.get("screen_name") or ""))
        norm_handle = author_screen or norm_target

        if own_only:
            if tweet.get("reposted_by"):
                return None
            if author_screen and norm_target and author_screen != norm_target:
                return None

        raw_text_obj = tweet.get("raw_text") if isinstance(tweet.get("raw_text"), dict) else {}
        text = tweet.get("text")
        if text is None:
            text = raw_text_obj.get("text")
        text_str = str(text) if text is not None else ""

        ts = _first_non_none(tweet, "created_timestamp", "created_at")

        media_type = _detect_media_type(tweet.get("media"), tweet.get("entities"))
        if tweet.get("poll") and media_type == "none":
            media_type = "poll"

        permalink = str(tweet.get("url") or "").strip() or f"https://x.com/{norm_handle}/status/{post_id}"

        replying_to = tweet.get("replying_to")
        replying_handle = ""
        if isinstance(replying_to, dict):
            replying_handle = normalize_handle(str(replying_to.get("screen_name") or ""))
        elif isinstance(replying_to, str):
            replying_handle = normalize_handle(replying_to)
        in_reply_to_id = tweet.get("in_reply_to_status_id")
        is_self_reply = bool(replying_handle and replying_handle == norm_handle)
        is_reply = bool((replying_handle and not is_self_reply) or (in_reply_to_id and not is_self_reply))
        is_repost = bool(tweet.get("retweeted_status") or tweet.get("reposted_by") or text_str.startswith("RT @"))
        is_thread = bool(
            tweet.get("is_thread")
            or is_self_reply
            or "🧵" in text_str
            or re.search(r"\b1/\d+\b", text_str)
        )

        return {
            "post_id": post_id,
            "handle": norm_handle,
            "published_at": normalize_utc_iso(ts),
            "text": text_str,
            "media_type": media_type,
            "is_reply": is_reply,
            "is_repost": is_repost,
            "is_thread": is_thread,
            "permalink": permalink,
            "views": safe_int(_first_non_none(tweet, "views", "view_count")),
            "likes": safe_int(_first_non_none(tweet, "likes", "favorite_count")),
            "replies": safe_int(_first_non_none(tweet, "replies", "reply_count")),
            "reposts": safe_int(_first_non_none(tweet, "reposts", "retweets", "retweet_count")),
            "bookmarks": safe_int(_first_non_none(tweet, "bookmarks", "bookmark_count")),
            "quotes": safe_int(_first_non_none(tweet, "quotes", "quote_count")),
        }

    def parse_vxtwitter_tweet(self, data: Any, handle: str) -> dict[str, Any] | None:
        """Extract normalized post metrics from a VxTwitter payload."""
        if not isinstance(data, dict):
            return None
        post_id = str(data.get("tweetID") or data.get("id") or "").strip()
        if not post_id:
            return None

        norm_handle = normalize_handle(str(data.get("user_screen_name") or handle))
        text_str = str(data.get("text") or "")
        ts = data.get("date_epoch") if data.get("date_epoch") is not None else data.get("date")
        media_type = _detect_media_type(data.get("media_extended") or data.get("mediaURLs"))
        permalink = str(data.get("tweetURL") or "").strip() or f"https://x.com/{norm_handle}/status/{post_id}"

        return {
            "post_id": post_id,
            "handle": norm_handle,
            "published_at": normalize_utc_iso(ts),
            "text": text_str,
            "media_type": media_type,
            "is_reply": bool(data.get("replyingTo")),
            "is_repost": text_str.startswith("RT @"),
            "is_thread": bool("🧵" in text_str or re.search(r"\b1/\d+\b", text_str)),
            "permalink": permalink,
            "views": safe_int(data.get("views", data.get("viewCount"))),
            "likes": safe_int(data.get("likes")),
            "replies": safe_int(data.get("replies")),
            "reposts": safe_int(data.get("retweets", data.get("reposts"))),
            "bookmarks": safe_int(data.get("bookmarks")),
            "quotes": safe_int(data.get("quotes")),
        }

    def parse_syndication_timeline(
        self, html_or_json: str, handle: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Parse profile metadata and timeline posts from Twitter Syndication HTML or JSON."""
        norm_handle = normalize_handle(handle)
        payload: dict[str, Any] | None = None
        raw = (html_or_json or "").strip()
        if not raw:
            return None, []

        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None, []
        else:
            soup = BeautifulSoup(raw, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    payload = json.loads(next_script.string)
                except json.JSONDecodeError:
                    payload = None

        if not isinstance(payload, dict):
            return None, []

        timeline = (
            payload.get("props", {})
            .get("pageProps", {})
            .get("timeline")
            or payload.get("timeline")
            or {}
        )
        entries = timeline.get("entries") if isinstance(timeline, dict) else []
        if not isinstance(entries, list):
            entries = []

        profile: dict[str, Any] | None = None
        posts: list[dict[str, Any]] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tweet = entry.get("content", {}).get("tweet")
            if not isinstance(tweet, dict):
                continue

            user = tweet.get("user") if isinstance(tweet.get("user"), dict) else None
            if user and profile is None:
                profile = {
                    "handle": normalize_handle(str(user.get("screen_name") or norm_handle)),
                    "display_name": str(user.get("name") or norm_handle),
                    "avatar_url": str(user.get("profile_image_url_https") or ""),
                    "bio": str(user.get("description") or ""),
                    "followers_count": safe_int(user.get("followers_count")),
                    "following_count": safe_int(user.get("friends_count")),
                    "total_posts": safe_int(user.get("statuses_count")),
                    "created_at": normalize_utc_iso(user.get("created_at")),
                }

            post_id = str(tweet.get("id_str") or tweet.get("id") or "").strip()
            if not post_id:
                continue
            text_str = str(tweet.get("full_text") or tweet.get("text") or "")
            ext_entities = tweet.get("extended_entities") or tweet.get("entities")
            media_type = _detect_media_type(None, ext_entities)

            posts.append(
                {
                    "post_id": post_id,
                    "handle": norm_handle,
                    "published_at": normalize_utc_iso(tweet.get("created_at")),
                    "text": text_str,
                    "media_type": media_type,
                    "is_reply": bool(tweet.get("in_reply_to_status_id_str")),
                    "is_repost": bool(tweet.get("retweeted_status") or text_str.startswith("RT @")),
                    "is_thread": bool("🧵" in text_str or re.search(r"\b1/\d+\b", text_str)),
                    "permalink": f"https://x.com/{norm_handle}/status/{post_id}",
                    "views": safe_int(tweet.get("views_count", tweet.get("views"))),
                    "likes": safe_int(tweet.get("favorite_count", tweet.get("likes"))),
                    "replies": safe_int(tweet.get("reply_count", tweet.get("replies"))),
                    "reposts": safe_int(tweet.get("retweet_count", tweet.get("reposts"))),
                    "bookmarks": safe_int(tweet.get("bookmark_count", tweet.get("bookmarks"))),
                    "quotes": safe_int(tweet.get("quote_count", tweet.get("quotes"))),
                }
            )

        return profile, posts

    def parse_nitter_rss_or_json(
        self, payload: str, handle: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Parse Nitter RSS XML or JSON feed into normalized posts and optional next cursor."""
        norm_handle = normalize_handle(handle)
        raw = (payload or "").strip()
        if not raw:
            return [], None

        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return [], None
            items = data.get("tweets", []) if isinstance(data, dict) else data
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            posts: list[dict[str, Any]] = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or item.get("post_id") or "").strip()
                if not pid:
                    continue
                text_str = str(item.get("text") or "")
                posts.append(
                    {
                        "post_id": pid,
                        "handle": norm_handle,
                        "published_at": normalize_utc_iso(item.get("published_at") or item.get("date")),
                        "text": text_str,
                        "media_type": str(item.get("media_type") or "none").lower(),
                        "is_reply": bool(item.get("is_reply")),
                        "is_repost": bool(item.get("is_repost") or text_str.startswith("RT @")),
                        "is_thread": bool(item.get("is_thread")),
                        "permalink": str(item.get("permalink") or f"https://x.com/{norm_handle}/status/{pid}"),
                        "views": safe_int(item.get("views")),
                        "likes": safe_int(item.get("likes")),
                        "replies": safe_int(item.get("replies")),
                        "reposts": safe_int(item.get("reposts", item.get("retweets"))),
                        "bookmarks": safe_int(item.get("bookmarks")),
                        "quotes": safe_int(item.get("quotes")),
                    }
                )
            return posts, str(next_cursor) if next_cursor else None

        # XML RSS parsing
        posts = []
        next_cursor = None
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return [], None

        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            pid = _extract_id_from_url(link)
            if not pid:
                continue
            title_or_desc = item.findtext("description") or item.findtext("title") or ""
            clean_text = BeautifulSoup(title_or_desc, "html.parser").get_text(" ", strip=True)
            pub_date = item.findtext("pubDate")
            posts.append(
                {
                    "post_id": pid,
                    "handle": norm_handle,
                    "published_at": normalize_utc_iso(pub_date),
                    "text": clean_text,
                    "media_type": "photo" if "<img" in title_or_desc.lower() else "none",
                    "is_reply": clean_text.startswith("R to @"),
                    "is_repost": clean_text.startswith("RT by @") or clean_text.startswith("RT @"),
                    "is_thread": bool("🧵" in clean_text or re.search(r"\b1/\d+\b", clean_text)),
                    "permalink": f"https://x.com/{norm_handle}/status/{pid}",
                    "views": 0,
                    "likes": 0,
                    "replies": 0,
                    "reposts": 0,
                    "bookmarks": 0,
                    "quotes": 0,
                }
            )
        if posts:
            next_cursor = posts[-1]["post_id"]
        return posts, None if len(posts) < 20 else next_cursor

    def _default_fx_page_fetcher(
        self, handle: str, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        params = {"cursor": cursor} if cursor else None
        url = f"https://api.fxtwitter.com/{handle}/tweets"
        try:
            resp = self.request_with_retry(url, params=params)
        except httpx.RequestError:
            return [], None
        if resp.status_code != 200:
            return [], None
        try:
            data = resp.json()
        except ValueError:
            return [], None

        raw_tweets = data.get("tweets") if isinstance(data, dict) else None
        if not isinstance(raw_tweets, list):
            return [], None

        parsed: list[dict[str, Any]] = []
        for item in raw_tweets:
            post = self.parse_fxtwitter_tweet(item, handle)
            if post:
                parsed.append(post)
        next_cur = data.get("next_cursor") or data.get("cursor")
        return parsed, str(next_cur) if next_cur else None

    def fetch_timeline_paginated(
        self,
        handle: str,
        max_pages: int = 10,
        max_posts: int = 500,
        page_fetcher: Callable[[str, str | None], tuple[list[dict[str, Any]], str | None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Backfill timeline posts across paginated cursor pages with loop protection."""
        norm = normalize_handle(handle)
        fetcher_fn = page_fetcher or self._default_fx_page_fetcher
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        collected: list[dict[str, Any]] = []

        for _ in range(max_pages):
            page_posts, next_cursor = fetcher_fn(norm, cursor)
            if not page_posts:
                break

            new_added = 0
            for post in page_posts:
                pid = str(post.get("post_id", "")).strip()
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    collected.append(post)
                    new_added += 1
                    if len(collected) >= max_posts:
                        return collected

            if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors or new_added == 0:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return collected

    def _fetch_fxtwitter_v2_timeline(
        self,
        handle: str,
        max_pages: int = 20,
        max_posts: int = 1000,
        with_replies: bool = False,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Paginate FxTwitter API v2 (/2/profile/{handle}/statuses) filtering to own-authored posts."""
        norm = normalize_handle(handle)
        url = f"https://api.fxtwitter.com/2/profile/{norm}/statuses"
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        collected: list[dict[str, Any]] = []
        extracted_profile: dict[str, Any] | None = None
        label = "replies" if with_replies else "posts"

        for page_num in range(1, max_pages + 1):
            params: dict[str, Any] = {"count": 40}
            if with_replies:
                params["withReplies"] = "true"
            if cursor:
                params["cursor"] = cursor

            data: dict[str, Any] | None = None
            try:
                resp = self.request_with_retry(url, params=params, retry_on_404=True)
            except httpx.RequestError:
                resp = None
            if resp is not None and resp.status_code == 200:
                try:
                    parsed_json = resp.json()
                except ValueError:
                    parsed_json = None
                if isinstance(parsed_json, dict) and parsed_json.get("results"):
                    data = parsed_json

            if not data:
                break

            results = data.get("results")
            if not isinstance(results, list) or not results:
                break

            new_added = 0
            for item in results:
                if not isinstance(item, dict):
                    continue
                post = self.parse_fxtwitter_tweet(item, norm, own_only=True)
                if post is None:
                    continue
                if extracted_profile is None and isinstance(item.get("author"), dict):
                    extracted_profile = self.parse_fxtwitter_profile(item["author"], norm)
                pid = post["post_id"]
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    collected.append(post)
                    new_added += 1
                    if len(collected) >= max_posts:
                        if progress_callback:
                            progress_callback(page_num, f"Fetched {len(collected)} {label} via FxTwitter v2...")
                        return extracted_profile, collected

            if progress_callback:
                progress_callback(page_num, f"Fetched {len(collected)} {label} (page {page_num})...")

            cursor_obj = data.get("cursor")
            next_cursor = (
                str(cursor_obj.get("bottom") or "").strip()
                if isinstance(cursor_obj, dict)
                else ""
            )
            if (
                not next_cursor
                or next_cursor == cursor
                or next_cursor in seen_cursors
                or new_added == 0
            ):
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if self.max_retries > 1:
                self.sleep_fn(min(0.12, max(0.01, self.base_backoff * 0.25)))

        return extracted_profile, collected

    def parse_fxtwitter_rss(self, xml_text: str, handle: str) -> list[dict[str, Any]]:
        """Parse FxTwitter's RSS XML feed (fxtwitter.com/{handle}/feed.xml) filtered to own posts."""
        norm = normalize_handle(handle)
        posts, _ = self.parse_nitter_rss_or_json(xml_text, norm)
        own_posts = []
        for post in posts:
            permalink_handle = _extract_handle_from_url(post.get("permalink", ""))
            if not permalink_handle or permalink_handle == norm:
                own_posts.append(post)
        return own_posts

    def sync_handle(
        self,
        handle: str,
        max_pages: int = 20,
        post_ids: list[str] | None = None,
        include_replies: bool = True,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> SyncResult:
        """Synchronize profile and post snapshots using multi-source fallback."""
        norm = normalize_handle(handle)
        if not norm:
            raise ValueError("Handle cannot be empty.")

        fallback_chain: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        profile: dict[str, Any] | None = None
        posts_by_id: dict[str, dict[str, Any]] = {}
        source_used = "none"

        with httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=True,
        ) as session_client:
            self._thread_local.client = session_client
            try:
                return self._sync_handle_with_session(
                    norm=norm,
                    max_pages=max_pages,
                    post_ids=post_ids,
                    include_replies=include_replies,
                    progress_callback=progress_callback,
                    fallback_chain=fallback_chain,
                    warnings=warnings,
                    errors=errors,
                    profile=profile,
                    posts_by_id=posts_by_id,
                    source_used=source_used,
                )
            finally:
                self._thread_local.client = None

    def _sync_handle_with_session(
        self,
        norm: str,
        max_pages: int,
        post_ids: list[str] | None,
        include_replies: bool,
        progress_callback: Callable[[int, str], None] | None,
        fallback_chain: list[str],
        warnings: list[str],
        errors: list[str],
        profile: dict[str, Any] | None,
        posts_by_id: dict[str, dict[str, Any]],
        source_used: str,
    ) -> SyncResult:

        # 1. Primary: FxTwitter profile + API v2 statuses pagination (posts + optional replies)
        fallback_chain.append("fxtwitter")
        if progress_callback:
            progress_callback(10, f"Fetching @{norm} profile & posts from FxTwitter v2...")
        try:
            prof_resp = self.request_with_retry(
                f"https://api.fxtwitter.com/{norm}", retry_on_404=True
            )
            if prof_resp.status_code == 200:
                profile = self.parse_fxtwitter_profile(prof_resp.json(), norm)
                if profile:
                    source_used = "fxtwitter"
            else:
                warnings.append(f"FxTwitter profile HTTP {prof_resp.status_code}")
        except Exception as exc:
            warnings.append(f"FxTwitter profile error: {exc}")

        try:
            def _on_v2_progress(page: int, msg: str) -> None:
                if progress_callback:
                    pct = min(75, 15 + page * 5)
                    progress_callback(pct, msg)

            v2_profile, v2_posts = self._fetch_fxtwitter_v2_timeline(
                norm,
                max_pages=max_pages,
                with_replies=False,
                progress_callback=_on_v2_progress,
            )
            if profile is None and v2_profile is not None:
                profile = v2_profile
            for p in v2_posts:
                posts_by_id[p["post_id"]] = p

            if include_replies:
                rep_profile, rep_posts = self._fetch_fxtwitter_v2_timeline(
                    norm,
                    max_pages=max_pages,
                    with_replies=True,
                    progress_callback=_on_v2_progress,
                )
                if profile is None and rep_profile is not None:
                    profile = rep_profile
                for p in rep_posts:
                    posts_by_id.setdefault(p["post_id"], p)

            if posts_by_id:
                source_used = "fxtwitter_v2"
                if "fxtwitter_v2" not in fallback_chain:
                    fallback_chain.append("fxtwitter_v2")
        except Exception as exc:
            warnings.append(f"FxTwitter v2 timeline error: {exc}")

        # 1b. Secondary FxTwitter fallback: RSS feed.xml if v2 returned no posts
        if not posts_by_id:
            try:
                rss_resp = self.request_with_retry(
                    f"https://fxtwitter.com/{norm}/feed.xml",
                    params={"count": 100},
                )
                if rss_resp.status_code == 200 and "<item>" in rss_resp.text:
                    rss_posts = self.parse_fxtwitter_rss(rss_resp.text, norm)
                    for p in rss_posts:
                        posts_by_id.setdefault(p["post_id"], p)
                    if rss_posts and source_used == "none":
                        source_used = "fxtwitter_rss"
            except Exception as exc:
                warnings.append(f"FxTwitter RSS error: {exc}")

        # 2. Fallback: Twitter Syndication timeline
        if profile is None or not posts_by_id:
            fallback_chain.append("syndication")
            syn_endpoints = (
                (f"https://cdn.syndication.twimg.com/srv/timeline-profile/screen-name/{norm}", None),
                ("https://cdn.syndication.twimg.com/timeline/profile", {"screen_name": norm}),
            )
            for syn_url, syn_params in syn_endpoints:
                try:
                    syn_resp = self.request_with_retry(syn_url, params=syn_params)
                    if syn_resp.status_code == 200 and syn_resp.text.strip():
                        syn_profile, syn_posts = self.parse_syndication_timeline(syn_resp.text, norm)
                        if profile is None and syn_profile is not None:
                            profile = syn_profile
                        for p in syn_posts:
                            posts_by_id.setdefault(p["post_id"], p)
                        if (syn_profile or syn_posts) and source_used == "none":
                            source_used = "syndication"
                        if syn_posts:
                            break
                    else:
                        warnings.append(f"Syndication HTTP {syn_resp.status_code}")
                except Exception as exc:
                    warnings.append(f"Syndication error: {exc}")

        # 3. Fallback: Nitter RSS/JSON instances
        if not posts_by_id:
            fallback_chain.append("nitter")
            for base_url in self.nitter_instances:
                try:
                    nit_resp = self.request_with_retry(f"{base_url.rstrip('/')}/{norm}/rss")
                    if nit_resp.status_code == 200:
                        nit_posts, _ = self.parse_nitter_rss_or_json(nit_resp.text, norm)
                        for p in nit_posts:
                            posts_by_id.setdefault(p["post_id"], p)
                        if nit_posts:
                            if source_used == "none":
                                source_used = "nitter"
                            break
                except Exception as exc:
                    warnings.append(f"Nitter ({base_url}) error: {exc}")

        # 4. Individual post metric refresh via FxTwitter -> VxTwitter fallback
        existing_ids = [p["post_id"] for p in self.db.get_posts(norm)[:30]]
        target_ids: list[str] = []
        for pid in list(post_ids or []) + existing_ids:
            pid_str = str(pid).strip()
            if pid_str and pid_str not in target_ids:
                target_ids.append(pid_str)

        used_vx = False
        for pid in target_ids:
            if pid in posts_by_id and pid not in (post_ids or []):
                continue
            fetched_post: dict[str, Any] | None = None

            try:
                fx_post_resp = self.request_with_retry(
                    f"https://api.fxtwitter.com/{norm}/status/{pid}",
                    retry_on_404=True,
                )
                if fx_post_resp.status_code == 200:
                    fetched_post = self.parse_fxtwitter_tweet(fx_post_resp.json(), norm)
            except Exception as exc:
                warnings.append(f"FxTwitter post {pid} error: {exc}")

            if fetched_post is None:
                used_vx = True
                try:
                    vx_post_resp = self.request_with_retry(f"https://api.vxtwitter.com/{norm}/status/{pid}")
                    if vx_post_resp.status_code == 200:
                        fetched_post = self.parse_vxtwitter_tweet(vx_post_resp.json(), norm)
                except Exception as exc:
                    warnings.append(f"VxTwitter post {pid} error: {exc}")

            if fetched_post is not None:
                posts_by_id[pid] = fetched_post
                if source_used == "none":
                    source_used = "vxtwitter" if used_vx else "fxtwitter"

        if used_vx and "vxtwitter" not in fallback_chain:
            fallback_chain.append("vxtwitter")

        # 5. Persist to SQLite
        if progress_callback:
            progress_callback(88, f"Saving {len(posts_by_id)} post snapshots to SQLite...")
        synced_at = utc_now_iso()
        profile_updated = False
        if profile is not None:
            self.db.upsert_account(
                handle=norm,
                display_name=profile["display_name"],
                avatar_url=profile["avatar_url"],
                bio=profile["bio"],
                followers_count=profile["followers_count"],
                following_count=profile["following_count"],
                total_posts=profile["total_posts"],
                created_at=profile["created_at"],
                last_synced_at=synced_at,
            )
            self.db.record_profile_snapshot(
                handle=norm,
                followers_count=profile["followers_count"],
                following_count=profile["following_count"],
                total_posts=profile["total_posts"],
                captured_at=synced_at,
            )
            profile_updated = True
        else:
            existing_acc = self.db.get_account(norm)
            if existing_acc is not None:
                self.db.upsert_account(handle=norm, last_synced_at=synced_at)

        snapshots_recorded = 0
        for post in posts_by_id.values():
            self.db.upsert_post_with_snapshot(post, captured_at=synced_at)
            snapshots_recorded += 1

        if not profile_updated and snapshots_recorded == 0:
            errors.append(
                f"No public timeline or profile endpoints returned data for @{norm}. "
                "You can still import an official X Analytics CSV or archive JSON."
            )

        return SyncResult(
            handle=norm,
            source_used=source_used,
            fallback_chain=fallback_chain,
            profile_updated=profile_updated,
            posts_synced=len(posts_by_id),
            snapshots_recorded=snapshots_recorded,
            warnings=warnings,
            errors=errors,
            synced_at=synced_at,
            success=(profile_updated or snapshots_recorded > 0),
        )

    def import_csv(self, csv_text: str, default_handle: str | None = None) -> ImportResult:
        """Import posts from Official X Analytics CSV or native X Tracker CSV."""
        cleaned = (csv_text or "").lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(cleaned))
        imported = 0
        skipped = 0
        warnings: list[str] = []
        resolved_handle = normalize_handle(default_handle or "")
        captured_at = utc_now_iso()

        for row in reader:
            lower_row = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
            permalink = lower_row.get("permalink") or lower_row.get("tweet permalink") or ""
            post_id = (
                lower_row.get("post_id")
                or lower_row.get("tweet id")
                or lower_row.get("id")
                or _extract_id_from_url(permalink)
            ).strip()

            if not post_id:
                skipped += 1
                continue

            row_handle = (
                normalize_handle(lower_row.get("handle", ""))
                or _extract_handle_from_url(permalink)
                or resolved_handle
                or "imported_account"
            )
            if not resolved_handle:
                resolved_handle = row_handle

            text_val = lower_row.get("text") or lower_row.get("tweet text") or ""
            published_raw = lower_row.get("published_at") or lower_row.get("time") or lower_row.get("created_at")
            media_type = (lower_row.get("media_type") or "none").lower()
            is_reply = lower_row.get("is_reply", "").lower() in {"1", "true", "yes"} or text_val.startswith("@")
            is_repost = lower_row.get("is_repost", "").lower() in {"1", "true", "yes"} or text_val.startswith("RT @")
            is_thread = lower_row.get("is_thread", "").lower() in {"1", "true", "yes"} or "🧵" in text_val

            post_dict = {
                "post_id": post_id,
                "handle": row_handle,
                "published_at": normalize_utc_iso(published_raw),
                "text": text_val,
                "media_type": media_type or "none",
                "is_reply": is_reply,
                "is_repost": is_repost,
                "is_thread": is_thread,
                "permalink": permalink or f"https://x.com/{row_handle}/status/{post_id}",
                "views": safe_int(lower_row.get("views") or lower_row.get("impressions")),
                "likes": safe_int(lower_row.get("likes") or lower_row.get("favorites")),
                "replies": safe_int(lower_row.get("replies")),
                "reposts": safe_int(lower_row.get("reposts") or lower_row.get("retweets")),
                "bookmarks": safe_int(lower_row.get("bookmarks")),
                "quotes": safe_int(lower_row.get("quotes")),
            }
            self.db.upsert_post_with_snapshot(post_dict, captured_at=captured_at)
            imported += 1

        if not resolved_handle:
            resolved_handle = "imported_account"

        return ImportResult(
            format="csv",
            handle=resolved_handle,
            posts_imported=imported,
            snapshots_created=imported,
            skipped_rows=skipped,
            warnings=warnings,
            imported_at=captured_at,
        )

    def import_json(self, json_text: str, default_handle: str | None = None) -> ImportResult:
        """Import posts from Official X Archive tweets.js, JSON arrays, or native export bundles."""
        raw = YTD_PREFIX_RE.sub("", (json_text or "").lstrip("\ufeff").strip()).rstrip(";")
        data = json.loads(raw)
        resolved_handle = normalize_handle(default_handle or "")
        captured_at = utc_now_iso()
        imported = 0
        skipped = 0

        if isinstance(data, dict) and "account" in data and isinstance(data["account"], dict):
            acc = data["account"]
            resolved_handle = normalize_handle(str(acc.get("handle") or resolved_handle or "imported_account"))
            self.db.upsert_account(
                handle=resolved_handle,
                display_name=str(acc.get("display_name") or resolved_handle),
                avatar_url=str(acc.get("avatar_url") or ""),
                bio=str(acc.get("bio") or ""),
                followers_count=safe_int(acc.get("followers_count")),
                following_count=safe_int(acc.get("following_count")),
                total_posts=safe_int(acc.get("total_posts")),
                created_at=acc.get("created_at"),
                last_synced_at=captured_at,
            )

        items = data.get("posts", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []

        for entry in items:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            tweet = entry.get("tweet") if isinstance(entry.get("tweet"), dict) else entry
            post_id = str(tweet.get("post_id") or tweet.get("id_str") or tweet.get("id") or "").strip()
            if not post_id:
                skipped += 1
                continue

            permalink = str(tweet.get("permalink") or "").strip()
            row_handle = (
                normalize_handle(str(tweet.get("handle") or ""))
                or _extract_handle_from_url(permalink)
                or resolved_handle
                or "imported_account"
            )
            if not resolved_handle:
                resolved_handle = row_handle

            text_val = str(tweet.get("text") or tweet.get("full_text") or "")
            media_type = str(tweet.get("media_type") or "").lower()
            if not media_type:
                media_type = _detect_media_type(
                    tweet.get("media"), tweet.get("extended_entities") or tweet.get("entities")
                )

            post_dict = {
                "post_id": post_id,
                "handle": row_handle,
                "published_at": normalize_utc_iso(tweet.get("published_at") or tweet.get("created_at")),
                "text": text_val,
                "media_type": media_type or "none",
                "is_reply": bool(tweet.get("is_reply") or tweet.get("in_reply_to_status_id_str")),
                "is_repost": bool(tweet.get("is_repost") or text_val.startswith("RT @")),
                "is_thread": bool(tweet.get("is_thread") or "🧵" in text_val),
                "permalink": permalink or f"https://x.com/{row_handle}/status/{post_id}",
                "views": safe_int(tweet.get("views", tweet.get("impressions"))),
                "likes": safe_int(tweet.get("likes", tweet.get("favorite_count"))),
                "replies": safe_int(tweet.get("replies", tweet.get("reply_count"))),
                "reposts": safe_int(tweet.get("reposts", tweet.get("retweet_count"))),
                "bookmarks": safe_int(tweet.get("bookmarks", tweet.get("bookmark_count"))),
                "quotes": safe_int(tweet.get("quotes", tweet.get("quote_count"))),
            }
            self.db.upsert_post_with_snapshot(post_dict, captured_at=captured_at)
            imported += 1

        if not resolved_handle:
            resolved_handle = "imported_account"

        return ImportResult(
            format="json",
            handle=resolved_handle,
            posts_imported=imported,
            snapshots_created=imported,
            skipped_rows=skipped,
            imported_at=captured_at,
        )

    def export_csv(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Export tracked posts for a handle to a UTF-8 CSV string."""
        posts = self.db.get_posts(handle, start_date=start_date, end_date=end_date)
        output = io.StringIO()
        fieldnames = [
            "post_id",
            "handle",
            "published_at",
            "text",
            "media_type",
            "is_reply",
            "is_repost",
            "is_thread",
            "permalink",
            "views",
            "likes",
            "replies",
            "reposts",
            "bookmarks",
            "quotes",
            "updated_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for post in posts:
            writer.writerow({key: post.get(key, "") for key in fieldnames})
        return output.getvalue()

    def export_json(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Export account metadata, posts, and profile snapshots as a JSON bundle."""
        norm = normalize_handle(handle)
        account = self.db.get_account(norm)
        posts = self.db.get_posts(norm, start_date=start_date, end_date=end_date)
        snapshots = self.db.get_profile_snapshots(norm, start_date=start_date, end_date=end_date)
        bundle = {
            "exported_at": utc_now_iso(),
            "account": account,
            "posts": posts,
            "profile_snapshots": snapshots,
        }
        return json.dumps(bundle, indent=2, ensure_ascii=False)
