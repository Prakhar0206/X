"""Realistic 120-day demo dataset seeder for local testing and evaluation."""

from __future__ import annotations

import base64
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from x_tracker.database import Database
from x_tracker.models import UTC_ISO_FORMAT, parse_utc_datetime


def _svg_avatar(initials: str, bg: str, fg: str = "#F8FAFC") -> str:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">'
        f'<rect width="80" height="80" rx="18" fill="{bg}"/>'
        f'<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" '
        f'font-family="sans-serif" font-weight="700" font-size="28" fill="{fg}">{initials}</text>'
        f"</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


DEMO_PROFILES: list[dict[str, Any]] = [
    {
        "handle": "alexrivera_dev",
        "display_name": "Alex Rivera",
        "avatar_url": _svg_avatar("AR", "#0284C7"),
        "bio": "Systems engineer writing on SQLite internals, Python performance, and local-first software.",
        "start_followers": 16200,
        "daily_growth_base": 72,
        "following_start": 385,
        "base_posts": 1410,
        "id_offset": 1890000000000,
        "topics": [
            ("SQLite WAL benchmarks: 42k concurrent reads/sec with zero lock contention on local NVMe.", "photo", True, False),
            ("Why we moved our telemetry ingestion pipeline from managed SaaS to a single 18MB Python service.", "none", True, False),
            ("Profiled our FastAPI JSON serializer today. Switching hot loops shaved P99 latency from 38ms to 6ms.", "video", False, False),
            ("Local-first architecture checklist: deterministic timestamps, append-only snapshots, zero cloud bills.", "photo", True, False),
            ("Quick benchmark poll: what is your primary local analytics store in 2026?", "poll", False, False),
            ("Replying to @mchen_ops: yes, PRAGMA synchronous=NORMAL is safe under WAL for desktop telemetry.", "none", False, True),
            ("How exponential backoff with full jitter prevents thundering herds against rate-limited endpoints.", "photo", False, False),
            ("Shipped zero-dependency CSV/JSON archive parsing that normalizes RFC-2822 and Unix epochs.", "none", False, False),
        ],
    },
    {
        "handle": "quant_pulse",
        "display_name": "Quant Pulse",
        "avatar_url": _svg_avatar("QP", "#0F766E"),
        "bio": "Market microstructure, volatility surface notes, and reproducible Python backtests.",
        "start_followers": 31400,
        "daily_growth_base": 88,
        "following_start": 210,
        "base_posts": 2180,
        "id_offset": 1910000000000,
        "topics": [
            ("Treasury auction tail vs intraday VIX term-structure inversion across the last 24 sessions.", "photo", True, False),
            ("Orderbook imbalance decay half-life dropped 19% after the tick-size adjustment.", "photo", False, False),
            ("Why Sharpe ratio hides tail kurtosis in short-volatility strategies (code + notebooks).", "video", True, False),
            ("Which rolling window do you trust most for realized vol regime switching?", "poll", False, False),
            ("Macro liquidity note: TGA rebuild pace slowed this week while repo spreads stayed anchored.", "none", False, False),
            ("Replying to @rate_desk: agreed, 49-day (7W) windows align neatly with quarterly FOMC cycles.", "none", False, True),
        ],
    },
    {
        "handle": "designcraft_io",
        "display_name": "DesignCraft Studio",
        "avatar_url": _svg_avatar("DC", "#B45309"),
        "bio": "Interface typography, dark-mode data density, and interactive web telemetry systems.",
        "start_followers": 11900,
        "daily_growth_base": 56,
        "following_start": 540,
        "base_posts": 890,
        "id_offset": 1930000000000,
        "topics": [
            ("Why tabular numerals (font-variant-numeric: tabular-nums) make KPI tables instantly scannable.", "photo", True, False),
            ("Before/after redesign of a dense analytics drawer: cutting card chrome by 40% doubled clarity.", "video", False, False),
            ("Color discipline in dark UI: reserve high-chroma accents strictly for actionable signals.", "photo", False, False),
            ("Which chart pairing gives faster intuition for volume vs efficiency?", "poll", False, False),
            ("Replying to @ux_kieran: 1px #243044 borders outperform soft drop shadows on dark graphite surfaces.", "none", False, True),
            ("Typography scale breakdown for telemetry dashboards: 12px mono labels paired with 28px metrics.", "none", True, False),
        ],
    },
]


def seed_demo_data(
    db: Database,
    days: int = 120,
    reference_now: datetime | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Populate realistic 120-day multi-handle demo data for immediate UI and analytics testing."""
    db.init_db()
    now = parse_utc_datetime(reference_now) if reference_now else datetime.now(timezone.utc)
    rng = random.Random(20261120)

    demo_handles = [p["handle"] for p in DEMO_PROFILES]
    existing = {a["handle"] for a in db.list_accounts()}
    if not force and all(h in existing for h in demo_handles):
        return {
            "accounts_seeded": len(demo_handles),
            "profile_snapshots_seeded": sum(len(db.get_profile_snapshots(h)) for h in demo_handles),
            "posts_seeded": sum(len(db.get_posts(h)) for h in demo_handles),
            "skipped_existing": 1,
        }

    if force:
        for h in demo_handles:
            db.delete_account(h)

    total_prof_snaps = 0
    total_posts = 0
    total_post_snaps = 0

    with db.connect() as conn:
        for profile in DEMO_PROFILES:
            handle = profile["handle"]
            if not force and handle in existing:
                continue

            created_dt = now - timedelta(days=days + 180)
            synced_iso = now.strftime(UTC_ISO_FORMAT)
            cur_followers = int(profile["start_followers"])
            cur_following = int(profile["following_start"])
            cur_posts_total = int(profile["base_posts"])

            conn.execute(
                """
                INSERT OR REPLACE INTO accounts (
                    handle, display_name, avatar_url, bio,
                    followers_count, following_count, total_posts,
                    created_at, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle,
                    profile["display_name"],
                    profile["avatar_url"],
                    profile["bio"],
                    cur_followers,
                    cur_following,
                    cur_posts_total,
                    created_dt.strftime(UTC_ISO_FORMAT),
                    synced_iso,
                ),
            )

            # 1. Generate daily profile snapshots across `days`
            prof_rows: list[tuple[Any, ...]] = []
            for day_offset in range(days, -1, -1):
                snap_dt = (now - timedelta(days=day_offset)).replace(
                    hour=8, minute=0, second=0, microsecond=0
                )
                if snap_dt > now:
                    snap_dt = now
                if day_offset == days:
                    f_delta = 0
                    fg_delta = 0
                    p_delta = 0
                else:
                    spike = rng.choice([0, 0, 0, 1, 2]) * rng.randint(45, 160)
                    f_delta = max(12, int(profile["daily_growth_base"] + rng.randint(-28, 52) + spike))
                    fg_delta = rng.choice([0, 0, 1, 1, 2])
                    p_delta = rng.choice([0, 1, 1, 2])
                    cur_followers += f_delta
                    cur_following += fg_delta
                    cur_posts_total += p_delta

                prof_rows.append(
                    (
                        handle,
                        snap_dt.strftime(UTC_ISO_FORMAT),
                        cur_followers,
                        cur_following,
                        cur_posts_total,
                        f_delta,
                        fg_delta,
                        p_delta,
                    )
                )

            conn.executemany(
                """
                INSERT INTO profile_snapshots (
                    handle, captured_at, followers_count, following_count,
                    total_posts, followers_delta, following_delta, posts_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prof_rows,
            )
            conn.execute(
                """
                UPDATE accounts SET followers_count = ?, following_count = ?, total_posts = ?
                WHERE handle = ?
                """,
                (cur_followers, cur_following, cur_posts_total, handle),
            )
            total_prof_snaps += len(prof_rows)

            # 2. Generate ~96 posts evenly distributed across all 120 days (with dense coverage in 7D/30D/7W/90D)
            day_buckets: list[int] = []
            for d in range(0, 7):
                day_buckets.append(d)
                if d in (1, 3):
                    day_buckets.append(d)
            for d in range(7, 30):
                day_buckets.append(d)
                if d % 7 == 0:
                    day_buckets.append(d)
            for d in range(30, 49):
                if d % 2 == 0 or d % 3 == 0:
                    day_buckets.append(d)
            for d in range(49, 90):
                if d % 2 == 0 or d % 5 == 0:
                    day_buckets.append(d)
            for d in range(90, days):
                if d % 2 == 0 or d % 3 == 0:
                    day_buckets.append(d)

            topics = profile["topics"]
            post_rows: list[tuple[Any, ...]] = []
            post_snap_rows: list[tuple[Any, ...]] = []

            for idx, d_ago in enumerate(sorted(day_buckets, reverse=True)):
                # Weighted posting hours favoring Tue-Thu morning/afternoon UTC
                peak_hours = [9, 13, 14, 15, 16, 17, 18, 20]
                hour = rng.choice(peak_hours) if rng.random() < 0.7 else rng.randint(6, 22)
                minute = rng.randint(2, 55)
                pub_dt = (now - timedelta(days=d_ago)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if pub_dt >= now:
                    pub_dt = now - timedelta(hours=rng.randint(2, 18))

                base_text, media_type, is_thread, is_reply = topics[idx % len(topics)]
                post_id = str(profile["id_offset"] + (idx * 137) + rng.randint(1, 50))
                is_evergreen = (not is_reply) and (idx % 5 == 0)

                # Performance profile: media & threads get stronger views & bookmarks
                mult = 1.0
                if media_type == "photo":
                    mult *= 1.85
                elif media_type == "video":
                    mult *= 2.25
                elif media_type == "poll":
                    mult *= 1.35
                if is_thread:
                    mult *= 1.7
                if is_reply:
                    mult *= 0.28
                if pub_dt.weekday() in (1, 2, 3) and 13 <= pub_dt.hour <= 17:
                    mult *= 1.42
                if is_evergreen:
                    mult *= 2.1

                final_views = int(rng.randint(2200, 14500) * mult)
                er_target = rng.uniform(0.022, 0.068) if not is_reply else rng.uniform(0.015, 0.04)
                total_eng = max(5, int(final_views * er_target))

                bm_share = rng.uniform(0.20, 0.36) if is_thread else rng.uniform(0.08, 0.22)
                rp_share = rng.uniform(0.07, 0.14)
                rt_share = rng.uniform(0.09, 0.18)
                lk_share = max(0.25, 1.0 - (bm_share + rp_share + rt_share))

                final_bookmarks = max(1, int(total_eng * bm_share))
                final_replies = max(1, int(total_eng * rp_share))
                final_reposts = max(1, int(total_eng * rt_share))
                final_likes = max(1, int(total_eng * lk_share))
                final_quotes = max(0, int(final_reposts * rng.uniform(0.15, 0.35)))

                # Build 5-7 realistic time-series snapshots per post
                age_hours = max(2.0, (now - pub_dt).total_seconds() / 3600.0)
                if age_hours <= 48:
                    fractions_and_hours = [
                        (0.18, min(1.0, age_hours * 0.12)),
                        (0.42, min(4.0, age_hours * 0.30)),
                        (0.68, min(10.0, age_hours * 0.55)),
                        (0.86, min(20.0, age_hours * 0.78)),
                        (1.00, age_hours - 0.15),
                    ]
                else:
                    fractions_and_hours = [
                        (0.16, 2.0),
                        (0.38, 6.0),
                        (0.62, 18.0),
                        (0.76, 36.0),
                        (0.88, min(age_hours - 2.0, 120.0)),
                    ]
                    if is_evergreen and d_ago >= 14:
                        # Evergreen post continues gaining meaningful views in recent 7d window!
                        tail_hour = max(125.0, age_hours - rng.uniform(6.0, 72.0))
                        fractions_and_hours.append((0.94, tail_hour))
                        fractions_and_hours.append((1.00, age_hours - 0.25))
                    else:
                        fractions_and_hours.append((1.00, age_hours - 0.25))

                prev_views = 0
                latest_snap_iso = pub_dt.strftime(UTC_ISO_FORMAT)
                for s_idx, (frac, hrs_after) in enumerate(fractions_and_hours):
                    sdt = pub_dt + timedelta(hours=hrs_after)
                    if sdt > now:
                        sdt = now - timedelta(minutes=max(1, len(fractions_and_hours) - s_idx))
                    s_iso = sdt.strftime(UTC_ISO_FORMAT)
                    latest_snap_iso = s_iso
                    sv = max(prev_views, int(round(final_views * frac)))
                    v_delta = 0 if s_idx == 0 else max(0, sv - prev_views)
                    prev_views = sv

                    post_snap_rows.append(
                        (
                            post_id,
                            s_iso,
                            sv,
                            int(round(final_likes * frac)),
                            int(round(final_replies * frac)),
                            int(round(final_reposts * frac)),
                            int(round(final_bookmarks * frac)),
                            int(round(final_quotes * frac)),
                            v_delta,
                        )
                    )

                post_rows.append(
                    (
                        post_id,
                        handle,
                        pub_dt.strftime(UTC_ISO_FORMAT),
                        f"{base_text} [#{idx + 1}]",
                        media_type,
                        1 if is_reply else 0,
                        0,
                        1 if is_thread else 0,
                        f"https://x.com/{handle}/status/{post_id}",
                        prev_views,
                        final_likes,
                        final_replies,
                        final_reposts,
                        final_bookmarks,
                        final_quotes,
                        latest_snap_iso,
                    )
                )

            conn.executemany(
                """
                INSERT OR REPLACE INTO posts (
                    post_id, handle, published_at, text, media_type,
                    is_reply, is_repost, is_thread, permalink,
                    views, likes, replies, reposts, bookmarks, quotes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                post_rows,
            )
            conn.executemany(
                """
                INSERT INTO post_snapshots (
                    post_id, captured_at, views, likes, replies, reposts, bookmarks, quotes, views_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                post_snap_rows,
            )
            total_posts += len(post_rows)
            total_post_snaps += len(post_snap_rows)

    return {
        "accounts_seeded": len(DEMO_PROFILES),
        "profile_snapshots_seeded": total_prof_snaps,
        "posts_seeded": total_posts,
        "post_snapshots_seeded": total_post_snaps,
    }
