"""Time-window analytics engine supporting Cohort and Velocity modes."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from x_tracker.database import Database, compute_velocity_24h
from x_tracker.models import normalize_handle, parse_utc_datetime, utc_now_iso

DAYS_OF_WEEK: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WINDOW_LABELS: dict[str, str] = {
    "7d": "Last 7 Days",
    "30d": "Last 30 Days",
    "7w": "Last 7 Weeks (49D)",
    "90d": "Last 90 Days",
    "lifetime": "Lifetime",
    "custom": "Custom Range",
}


def compute_engagement_rate(
    views: int, likes: int, replies: int, reposts: int, bookmarks: int
) -> float:
    """Compute Engagement Rate (%) = ((likes + replies + reposts + bookmarks) / views) * 100."""
    if views <= 0:
        return 0.0
    total = likes + replies + reposts + bookmarks
    return round((total / views) * 100.0, 2)


def compute_high_signal_score(
    views: int, likes: int, replies: int, reposts: int, bookmarks: int, quotes: int = 0
) -> float:
    """Compute High-Signal Score prioritizing bookmarks (4x) and replies (3x) per 1K views."""
    if views <= 0:
        return 0.0
    weighted = (4.0 * bookmarks) + (3.0 * replies) + (2.0 * reposts) + (2.5 * quotes) + (1.0 * likes)
    return round((weighted / views) * 1000.0, 2)


def compute_bookmark_to_like_ratio(bookmarks: int, likes: int) -> float:
    """Compute Bookmark-to-Like Ratio (%) = (bookmarks / likes) * 100."""
    if likes <= 0:
        return 0.0
    return round((bookmarks / likes) * 100.0, 2)


def compute_virality_ratio(reposts: int, views: int) -> float:
    """Compute Virality Ratio (%) = (reposts / views) * 100."""
    if views <= 0:
        return 0.0
    return round((reposts / views) * 100.0, 3)


def compute_pct_change(current: float, previous: float) -> float:
    """Compute period-over-period percentage change safely."""
    if previous > 0:
        return round(((current - previous) / previous) * 100.0, 2)
    if current > 0:
        return 100.0
    return 0.0


def _bucket_key_and_label(dt: datetime, granularity: str) -> tuple[str, str]:
    gran = (granularity or "daily").lower()
    if gran == "weekly":
        iso_year, iso_week, _ = dt.isocalendar()
        week_start = dt - timedelta(days=dt.weekday())
        return f"{iso_year}-W{iso_week:02d}", week_start.strftime("%b %d")
    if gran == "monthly":
        return dt.strftime("%Y-%m"), dt.strftime("%b %Y")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%b %d")


def _generate_bucket_sequence(
    start_dt: datetime, end_dt: datetime, granularity: str
) -> list[tuple[str, str]]:
    """Build ordered (bucket_key, label) pairs covering [start_dt, end_dt]."""
    gran = (granularity or "daily").lower()
    buckets: list[tuple[str, str]] = []
    seen: set[str] = set()
    cursor = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
    end_day = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)

    step = timedelta(days=1)
    while cursor <= end_day:
        key, label = _bucket_key_and_label(cursor, gran)
        if key not in seen:
            seen.add(key)
            buckets.append((key, label))
        cursor += step

    if not buckets:
        buckets.append(_bucket_key_and_label(end_dt, gran))
    return buckets


class AnalyticsEngine:
    """Computes Cohort & Velocity analytics, KPIs, heatmaps, and chart datasets."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def resolve_window(
        self,
        timeframe: str = "30d",
        start_date: str | None = None,
        end_date: str | None = None,
        reference_now: datetime | None = None,
        earliest_dt: datetime | None = None,
    ) -> tuple[datetime, datetime, datetime | None, datetime | None, int]:
        """Resolve a timeframe identifier into (start, end, prev_start, prev_end, days)."""
        now = parse_utc_datetime(reference_now) if reference_now else datetime.now(timezone.utc)
        tf = (timeframe or "30d").strip().lower()

        fixed_days = {"7d": 7, "30d": 30, "7w": 49, "90d": 90}
        if tf in fixed_days:
            days = fixed_days[tf]
            end_dt = now
            start_dt = end_dt - timedelta(days=days)
            prev_end = start_dt
            prev_start = start_dt - timedelta(days=days)
            return start_dt, end_dt, prev_start, prev_end, days

        if tf == "custom" and start_date and end_date:
            s_raw = start_date.strip()
            e_raw = end_date.strip()
            start_dt = parse_utc_datetime(s_raw)
            if len(s_raw) <= 10:
                start_dt = datetime(start_dt.year, start_dt.month, start_dt.day, 0, 0, 0, tzinfo=timezone.utc)

            end_dt = parse_utc_datetime(e_raw)
            if len(e_raw) <= 10:
                end_dt = datetime(end_dt.year, end_dt.month, end_dt.day, 23, 59, 59, tzinfo=timezone.utc)

            if end_dt < start_dt:
                start_dt, end_dt = end_dt, start_dt

            days = max(1, (end_dt.date() - start_dt.date()).days + 1)
            span = (end_dt - start_dt) + timedelta(seconds=1)
            prev_end = start_dt
            prev_start = start_dt - span
            return start_dt, end_dt, prev_start, prev_end, days

        # Lifetime fallback
        end_dt = now
        start_dt = earliest_dt if earliest_dt and earliest_dt < end_dt else (end_dt - timedelta(days=120))
        days = max(1, (end_dt.date() - start_dt.date()).days + 1)
        return start_dt, end_dt, None, None, days

    def _aggregate_cohort_metrics(self, posts: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "views": sum(int(p.get("views", 0)) for p in posts),
            "likes": sum(int(p.get("likes", 0)) for p in posts),
            "replies": sum(int(p.get("replies", 0)) for p in posts),
            "reposts": sum(int(p.get("reposts", 0)) for p in posts),
            "bookmarks": sum(int(p.get("bookmarks", 0)) for p in posts),
            "quotes": sum(int(p.get("quotes", 0)) for p in posts),
            "posts_count": len(posts),
        }

    def _aggregate_velocity_metrics(
        self,
        all_posts: list[dict[str, Any]],
        snaps_by_post: dict[str, list[dict[str, Any]]],
        w_start: datetime,
        w_end: datetime,
    ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        """Compute snapshot growth deltas inside [w_start, w_end] across all tracked posts."""
        totals = {
            "views": 0,
            "likes": 0,
            "replies": 0,
            "reposts": 0,
            "bookmarks": 0,
            "quotes": 0,
            "posts_count": 0,
        }
        bucket_gains: dict[str, dict[str, int]] = defaultdict(
            lambda: {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "bookmarks": 0, "quotes": 0}
        )
        active_posts = 0

        for post in all_posts:
            pid = post["post_id"]
            snaps = snaps_by_post.get(pid, [])
            if not snaps:
                continue

            pub_dt = parse_utc_datetime(post["published_at"])
            snaps_before: list[dict[str, Any]] = []
            snaps_in: list[dict[str, Any]] = []
            for s in snaps:
                sdt = parse_utc_datetime(s["captured_at"])
                if sdt < w_start:
                    snaps_before.append(s)
                elif w_start <= sdt <= w_end:
                    snaps_in.append(s)

            if not snaps_in:
                continue

            if pub_dt >= w_start:
                baseline = {
                    "views": 0,
                    "likes": 0,
                    "replies": 0,
                    "reposts": 0,
                    "bookmarks": 0,
                    "quotes": 0,
                }
            elif snaps_before:
                b = snaps_before[-1]
                baseline = {
                    "views": int(b.get("views", 0)),
                    "likes": int(b.get("likes", 0)),
                    "replies": int(b.get("replies", 0)),
                    "reposts": int(b.get("reposts", 0)),
                    "bookmarks": int(b.get("bookmarks", 0)),
                    "quotes": int(b.get("quotes", 0)),
                }
            else:
                first = snaps_in[0]
                baseline = {
                    "views": max(0, int(first.get("views", 0)) - int(first.get("views_delta", 0))),
                    "likes": int(first.get("likes", 0)),
                    "replies": int(first.get("replies", 0)),
                    "reposts": int(first.get("reposts", 0)),
                    "bookmarks": int(first.get("bookmarks", 0)),
                    "quotes": int(first.get("quotes", 0)),
                }

            latest = snaps_in[-1]
            g_views = max(0, int(latest.get("views", 0)) - baseline["views"])
            g_likes = max(0, int(latest.get("likes", 0)) - baseline["likes"])
            g_replies = max(0, int(latest.get("replies", 0)) - baseline["replies"])
            g_reposts = max(0, int(latest.get("reposts", 0)) - baseline["reposts"])
            g_bookmarks = max(0, int(latest.get("bookmarks", 0)) - baseline["bookmarks"])
            g_quotes = max(0, int(latest.get("quotes", 0)) - baseline["quotes"])

            if g_views > 0 or g_likes > 0 or g_bookmarks > 0:
                active_posts += 1

            totals["views"] += g_views
            totals["likes"] += g_likes
            totals["replies"] += g_replies
            totals["reposts"] += g_reposts
            totals["bookmarks"] += g_bookmarks
            totals["quotes"] += g_quotes

            # Attribute step-by-step deltas to snapshot dates for time-series charts
            prev_state = dict(baseline)
            for snap in snaps_in:
                sdt = parse_utc_datetime(snap["captured_at"])
                day_iso = sdt.strftime("%Y-%m-%d")
                for metric in ("views", "likes", "replies", "reposts", "bookmarks", "quotes"):
                    cur_val = int(snap.get(metric, 0))
                    step_delta = max(0, cur_val - prev_state[metric])
                    bucket_gains[day_iso][metric] += step_delta
                    prev_state[metric] = max(prev_state[metric], cur_val)

        totals["posts_count"] = active_posts
        return totals, bucket_gains

    def _compute_follower_growth(
        self,
        all_profile_snaps: list[dict[str, Any]],
        w_start: datetime,
        w_end: datetime,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        before = [s for s in all_profile_snaps if parse_utc_datetime(s["captured_at"]) < w_start]
        in_win = [
            s
            for s in all_profile_snaps
            if w_start <= parse_utc_datetime(s["captured_at"]) <= w_end
        ]

        if not in_win:
            current_followers = int(before[-1]["followers_count"]) if before else 0
            return current_followers, 0, []

        current_followers = int(in_win[-1]["followers_count"])
        if before:
            gained = current_followers - int(before[-1]["followers_count"])
        else:
            gained = sum(int(s.get("followers_delta", 0)) for s in in_win)

        series = [
            {
                "date": parse_utc_datetime(s["captured_at"]).strftime("%Y-%m-%d"),
                "captured_at": s["captured_at"],
                "followers_count": int(s["followers_count"]),
                "followers_delta": int(s.get("followers_delta", 0)),
            }
            for s in in_win
        ]
        return current_followers, gained, series

    def _build_heatmap(self, posts: list[dict[str, Any]]) -> dict[str, Any]:
        slot_views: dict[tuple[int, int], list[int]] = defaultdict(list)
        for post in posts:
            dt = parse_utc_datetime(post["published_at"])
            slot_views[(dt.weekday(), dt.hour)].append(int(post.get("views", 0)))

        matrix: list[list[dict[str, Any]]] = []
        best_day = "Mon"
        best_hour = 14
        best_avg = 0.0
        max_avg = 0.0

        for day_idx, day_name in enumerate(DAYS_OF_WEEK):
            row: list[dict[str, Any]] = []
            for hour in range(24):
                vals = slot_views.get((day_idx, hour), [])
                count = len(vals)
                total = sum(vals)
                avg = round(total / count, 1) if count else 0.0
                if avg > max_avg:
                    max_avg = avg
                if avg > best_avg:
                    best_avg = avg
                    best_day = day_name
                    best_hour = hour
                row.append(
                    {
                        "day_index": day_idx,
                        "day": day_name,
                        "hour": hour,
                        "hour_label": f"{hour:02d}:00",
                        "post_count": count,
                        "total_views": total,
                        "avg_views": avg,
                    }
                )
            matrix.append(row)

        return {
            "matrix": matrix,
            "days": list(DAYS_OF_WEEK),
            "hours": list(range(24)),
            "best_day": best_day,
            "best_hour": best_hour,
            "best_hour_label": f"{best_hour:02d}:00 UTC",
            "best_slot_avg_views": best_avg,
            "max_avg_views": max_avg,
        }

    def _build_media_breakdown(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {
            "text": [],
            "photo": [],
            "video": [],
            "poll": [],
        }
        labels = {
            "text": "Text-Only",
            "photo": "Photo / Image",
            "video": "Video",
            "poll": "Poll",
        }
        for p in posts:
            mtype = str(p.get("media_type") or "none").lower()
            key = "text" if mtype in {"none", "", "text"} else (mtype if mtype in buckets else "photo")
            buckets[key].append(p)

        result: list[dict[str, Any]] = []
        for cat, items in buckets.items():
            count = len(items)
            views = sum(int(i.get("views", 0)) for i in items)
            likes = sum(int(i.get("likes", 0)) for i in items)
            replies = sum(int(i.get("replies", 0)) for i in items)
            reposts = sum(int(i.get("reposts", 0)) for i in items)
            bookmarks = sum(int(i.get("bookmarks", 0)) for i in items)
            quotes = sum(int(i.get("quotes", 0)) for i in items)
            result.append(
                {
                    "category": cat,
                    "label": labels[cat],
                    "post_count": count,
                    "total_views": views,
                    "avg_views": round(views / count, 1) if count else 0.0,
                    "engagement_rate": compute_engagement_rate(views, likes, replies, reposts, bookmarks),
                    "avg_bookmarks": round(bookmarks / count, 1) if count else 0.0,
                    "high_signal_score": compute_high_signal_score(
                        views, likes, replies, reposts, bookmarks, quotes
                    ),
                }
            )
        return result

    def _enrich_and_filter_posts(
        self,
        posts: list[dict[str, Any]],
        snaps_by_post: dict[str, list[dict[str, Any]]],
        search: str | None = None,
        post_type: str | None = None,
        sort_by: str = "views",
        sort_dir: str = "desc",
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        q = (search or "").strip().lower()
        ptype = (post_type or "all").strip().lower()

        for p in posts:
            if q and q not in p.get("text", "").lower():
                continue

            is_reply = bool(p.get("is_reply"))
            is_repost = bool(p.get("is_repost"))
            is_thread = bool(p.get("is_thread"))
            media_type = str(p.get("media_type") or "none").lower()
            has_media = media_type not in {"none", ""}

            if ptype == "original" and (is_reply or is_repost):
                continue
            if ptype == "thread" and not is_thread:
                continue
            if ptype == "reply" and not is_reply:
                continue
            if ptype == "repost" and not is_repost:
                continue
            if ptype == "media" and not has_media:
                continue
            if ptype in {"text", "text-only"} and has_media:
                continue

            views = int(p.get("views", 0))
            likes = int(p.get("likes", 0))
            replies = int(p.get("replies", 0))
            reposts = int(p.get("reposts", 0))
            bookmarks = int(p.get("bookmarks", 0))
            quotes = int(p.get("quotes", 0))

            if is_repost:
                category = "repost"
            elif is_reply:
                category = "reply"
            elif is_thread:
                category = "thread"
            else:
                category = "original"

            snaps = snaps_by_post.get(p["post_id"], [])
            velocity_24h = compute_velocity_24h(snaps)

            item = dict(p)
            item.update(
                {
                    "post_category": category,
                    "engagement_rate": compute_engagement_rate(
                        views, likes, replies, reposts, bookmarks
                    ),
                    "high_signal_score": compute_high_signal_score(
                        views, likes, replies, reposts, bookmarks, quotes
                    ),
                    "bookmark_to_like_ratio": compute_bookmark_to_like_ratio(bookmarks, likes),
                    "virality_ratio": compute_virality_ratio(reposts, views),
                    "velocity_24h": velocity_24h,
                    "snapshot_count": len(snaps),
                }
            )
            enriched.append(item)

        valid_sorts = {
            "views",
            "velocity_24h",
            "engagement_rate",
            "bookmarks",
            "replies",
            "likes",
            "reposts",
            "high_signal_score",
            "published_at",
        }
        sort_key = sort_by if sort_by in valid_sorts else "views"
        reverse = (sort_dir or "desc").lower() != "asc"
        enriched.sort(key=lambda row: row.get(sort_key, 0), reverse=reverse)
        return enriched

    def compute_dashboard_analytics(
        self,
        handle: str,
        timeframe: str = "30d",
        mode: str = "cohort",
        granularity: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        search: str | None = None,
        post_type: str | None = None,
        sort_by: str = "views",
        sort_dir: str = "desc",
        reference_now: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute full dashboard KPIs, time-series charts, heatmap, and post list."""
        norm = normalize_handle(handle)
        account = self.db.get_account(norm)
        all_posts = self.db.get_posts(norm)
        all_profile_snaps = self.db.get_profile_snapshots(norm)
        snaps_by_post = self.db.get_all_post_snapshots_for_handle(norm)

        earliest_candidates: list[datetime] = []
        if all_posts:
            earliest_candidates.append(min(parse_utc_datetime(p["published_at"]) for p in all_posts))
        if all_profile_snaps:
            earliest_candidates.append(parse_utc_datetime(all_profile_snaps[0]["captured_at"]))
        earliest_dt = min(earliest_candidates) if earliest_candidates else None

        w_start, w_end, prev_start, prev_end, days = self.resolve_window(
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            reference_now=reference_now,
            earliest_dt=earliest_dt,
        )

        cohort_posts = [
            p
            for p in all_posts
            if w_start <= parse_utc_datetime(p["published_at"]) <= w_end
        ]
        prev_cohort_posts = (
            [
                p
                for p in all_posts
                if prev_start <= parse_utc_datetime(p["published_at"]) < prev_end
            ]
            if prev_start and prev_end
            else []
        )

        cohort_cur = self._aggregate_cohort_metrics(cohort_posts)
        cohort_prev = self._aggregate_cohort_metrics(prev_cohort_posts)

        vel_cur, vel_day_buckets = self._aggregate_velocity_metrics(
            all_posts, snaps_by_post, w_start, w_end
        )
        if prev_start and prev_end:
            vel_prev, _ = self._aggregate_velocity_metrics(
                all_posts, snaps_by_post, prev_start, prev_end - timedelta(seconds=1)
            )
        else:
            vel_prev = {k: 0 for k in vel_cur}

        selected_mode = (mode or "cohort").strip().lower()
        if selected_mode not in {"cohort", "velocity"}:
            selected_mode = "cohort"

        cur_metrics = vel_cur if selected_mode == "velocity" else cohort_cur
        prev_metrics = vel_prev if selected_mode == "velocity" else cohort_prev

        cur_er = compute_engagement_rate(
            cur_metrics["views"],
            cur_metrics["likes"],
            cur_metrics["replies"],
            cur_metrics["reposts"],
            cur_metrics["bookmarks"],
        )
        prev_er = compute_engagement_rate(
            prev_metrics["views"],
            prev_metrics["likes"],
            prev_metrics["replies"],
            prev_metrics["reposts"],
            prev_metrics["bookmarks"],
        )
        cur_hs = compute_high_signal_score(
            cur_metrics["views"],
            cur_metrics["likes"],
            cur_metrics["replies"],
            cur_metrics["reposts"],
            cur_metrics["bookmarks"],
            cur_metrics["quotes"],
        )
        prev_hs = compute_high_signal_score(
            prev_metrics["views"],
            prev_metrics["likes"],
            prev_metrics["replies"],
            prev_metrics["reposts"],
            prev_metrics["bookmarks"],
            prev_metrics["quotes"],
        )

        followers_now, followers_gained, follower_series = self._compute_follower_growth(
            all_profile_snaps, w_start, w_end
        )
        if prev_start and prev_end:
            _, followers_prev_gained, _ = self._compute_follower_growth(
                all_profile_snaps, prev_start, prev_end - timedelta(seconds=1)
            )
        else:
            followers_prev_gained = 0

        if account and followers_now == 0:
            followers_now = int(account.get("followers_count", 0))

        # Build time-series over_time & cadence_vs_impressions
        gran = (granularity or "daily").strip().lower()
        if gran not in {"daily", "weekly", "monthly"}:
            gran = "daily"
        bucket_seq = _generate_bucket_sequence(w_start, w_end, gran)
        bucket_map: dict[str, dict[str, Any]] = {
            bkey: {
                "bucket": bkey,
                "label": blabel,
                "impressions": 0,
                "engagements": 0,
                "likes": 0,
                "replies": 0,
                "reposts": 0,
                "bookmarks": 0,
                "posts_count": 0,
            }
            for bkey, blabel in bucket_seq
        }

        for p in cohort_posts:
            pdt = parse_utc_datetime(p["published_at"])
            bkey, blabel = _bucket_key_and_label(pdt, gran)
            if bkey not in bucket_map:
                bucket_map[bkey] = {
                    "bucket": bkey,
                    "label": blabel,
                    "impressions": 0,
                    "engagements": 0,
                    "likes": 0,
                    "replies": 0,
                    "reposts": 0,
                    "bookmarks": 0,
                    "posts_count": 0,
                }
            bucket_map[bkey]["posts_count"] += 1
            if selected_mode == "cohort":
                v = int(p.get("views", 0))
                lk = int(p.get("likes", 0))
                rp = int(p.get("replies", 0))
                rt = int(p.get("reposts", 0))
                bm = int(p.get("bookmarks", 0))
                bucket_map[bkey]["impressions"] += v
                bucket_map[bkey]["likes"] += lk
                bucket_map[bkey]["replies"] += rp
                bucket_map[bkey]["reposts"] += rt
                bucket_map[bkey]["bookmarks"] += bm
                bucket_map[bkey]["engagements"] += lk + rp + rt + bm

        if selected_mode == "velocity":
            for day_str, vals in vel_day_buckets.items():
                ddt = parse_utc_datetime(day_str)
                bkey, blabel = _bucket_key_and_label(ddt, gran)
                if bkey not in bucket_map:
                    bucket_map[bkey] = {
                        "bucket": bkey,
                        "label": blabel,
                        "impressions": 0,
                        "engagements": 0,
                        "likes": 0,
                        "replies": 0,
                        "reposts": 0,
                        "bookmarks": 0,
                        "posts_count": 0,
                    }
                bucket_map[bkey]["impressions"] += vals["views"]
                bucket_map[bkey]["likes"] += vals["likes"]
                bucket_map[bkey]["replies"] += vals["replies"]
                bucket_map[bkey]["reposts"] += vals["reposts"]
                bucket_map[bkey]["bookmarks"] += vals["bookmarks"]
                bucket_map[bkey]["engagements"] += (
                    vals["likes"] + vals["replies"] + vals["reposts"] + vals["bookmarks"]
                )

        over_time: list[dict[str, Any]] = []
        cadence_vs_impressions: list[dict[str, Any]] = []
        for bkey, _ in bucket_seq:
            row = bucket_map[bkey]
            er = compute_engagement_rate(
                row["impressions"],
                row["likes"],
                row["replies"],
                row["reposts"],
                row["bookmarks"],
            )
            over_time.append({**row, "engagement_rate": er})
            cadence_vs_impressions.append(
                {
                    "bucket": row["bucket"],
                    "label": row["label"],
                    "posts_count": row["posts_count"],
                    "impressions": row["impressions"],
                    "avg_impressions_per_post": (
                        round(row["impressions"] / row["posts_count"], 1)
                        if row["posts_count"] > 0
                        else 0.0
                    ),
                }
            )

        posts_for_charts = cohort_posts if cohort_posts else all_posts
        heatmap = self._build_heatmap(posts_for_charts)
        media_breakdown = self._build_media_breakdown(posts_for_charts)

        total_eng = (
            cur_metrics["likes"]
            + cur_metrics["replies"]
            + cur_metrics["reposts"]
            + cur_metrics["bookmarks"]
        )
        eng_totals = {
            "likes": cur_metrics["likes"],
            "replies": cur_metrics["replies"],
            "reposts": cur_metrics["reposts"],
            "bookmarks": cur_metrics["bookmarks"],
            "quotes": cur_metrics["quotes"],
        }
        eng_denom = max(1, total_eng)
        shares_pct = {
            k: round((v / eng_denom) * 100.0, 1) if total_eng > 0 else 0.0
            for k, v in eng_totals.items()
        }

        posts_base = all_posts if (timeframe or "").lower() == "lifetime" else (cohort_posts or all_posts)
        # In cohort/window views, show posts published inside the selected window (unless lifetime)
        table_source_posts = all_posts if (timeframe or "").lower() == "lifetime" else cohort_posts
        if selected_mode == "velocity" and not table_source_posts:
            table_source_posts = all_posts
        enriched_posts = self._enrich_and_filter_posts(
            posts=table_source_posts,
            snaps_by_post=snaps_by_post,
            search=search,
            post_type=post_type,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

        tf_norm = (timeframe or "30d").lower()
        return {
            "account": account
            or {
                "handle": norm,
                "display_name": norm,
                "avatar_url": "",
                "bio": "",
                "followers_count": followers_now,
                "following_count": 0,
                "total_posts": len(all_posts),
                "last_synced_at": utc_now_iso(),
            },
            "window": {
                "timeframe": tf_norm,
                "label": WINDOW_LABELS.get(tf_norm, "Custom Range"),
                "mode": selected_mode,
                "granularity": gran,
                "start_date": w_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date": w_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "prev_start_date": prev_start.strftime("%Y-%m-%dT%H:%M:%SZ") if prev_start else None,
                "prev_end_date": prev_end.strftime("%Y-%m-%dT%H:%M:%SZ") if prev_end else None,
                "days": days,
            },
            "kpis": {
                "impressions": cur_metrics["views"],
                "impressions_prev": prev_metrics["views"],
                "impressions_pct_change": compute_pct_change(
                    cur_metrics["views"], prev_metrics["views"]
                ),
                "cohort_impressions": cohort_cur["views"],
                "velocity_impressions": vel_cur["views"],
                "followers": followers_now,
                "followers_gained": followers_gained,
                "followers_prev_gained": followers_prev_gained,
                "followers_pct_change": compute_pct_change(
                    followers_gained, followers_prev_gained
                ),
                "follower_daily_velocity": round(followers_gained / max(1, days), 2),
                "engagement_rate": cur_er,
                "engagement_rate_prev": prev_er,
                "engagement_rate_delta": round(cur_er - prev_er, 2),
                "high_signal_score": cur_hs,
                "high_signal_score_prev": prev_hs,
                "high_signal_score_delta": round(cur_hs - prev_hs, 2),
                "bookmarks": cur_metrics["bookmarks"],
                "bookmarks_prev": prev_metrics["bookmarks"],
                "bookmarks_pct_change": compute_pct_change(
                    cur_metrics["bookmarks"], prev_metrics["bookmarks"]
                ),
                "bookmark_to_like_ratio": compute_bookmark_to_like_ratio(
                    cur_metrics["bookmarks"], cur_metrics["likes"]
                ),
                "virality_ratio": compute_virality_ratio(
                    cur_metrics["reposts"], cur_metrics["views"]
                ),
                "total_posts": cohort_cur["posts_count"],
                "total_posts_prev": cohort_prev["posts_count"],
                "total_posts_pct_change": compute_pct_change(
                    cohort_cur["posts_count"], cohort_prev["posts_count"]
                ),
                "active_posts": vel_cur["posts_count"],
                "likes": cur_metrics["likes"],
                "replies": cur_metrics["replies"],
                "reposts": cur_metrics["reposts"],
                "quotes": cur_metrics["quotes"],
                "total_engagements": total_eng,
            },
            "charts": {
                "over_time": over_time,
                "follower_growth": follower_series,
                "cadence_vs_impressions": cadence_vs_impressions,
                "heatmap": heatmap,
                "engagement_breakdown": {
                    "totals": eng_totals,
                    "shares_pct": shares_pct,
                },
                "media_breakdown": media_breakdown,
            },
            "posts": enriched_posts,
        }
