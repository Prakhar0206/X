"""CLI entrypoint for the local-first X (Twitter) Analytics & Impression Tracker."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import uvicorn

from x_tracker.database import DEFAULT_DB_PATH, Database
from x_tracker.fetcher import MultiSourceFetcher
from x_tracker.models import normalize_handle
from x_tracker.scheduler import BackgroundSyncScheduler, parse_interval_seconds
from x_tracker.seeder import seed_demo_data
from x_tracker.server import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local-first X (Twitter) Analytics & Impression Tracker"
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database file (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--seed-demo",
        action="store_true",
        help="Populate 120 days of realistic multi-handle sample data into SQLite.",
    )
    parser.add_argument(
        "--sync",
        nargs="*",
        default=None,
        metavar="HANDLE",
        help="Run immediate multi-source sync for given handles (or all tracked handles if omitted).",
    )
    parser.add_argument(
        "--import-file",
        default=None,
        help="Import an official X Analytics CSV or archive JSON/tweets.js file.",
    )
    parser.add_argument(
        "--handle",
        default=None,
        help="Default @handle to attach when using --import-file.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Enable background snapshot recording scheduler.",
    )
    parser.add_argument(
        "--interval",
        default="6h",
        help="Watch scheduler interval (e.g. 6h, 1h, 30m). Default: 6h.",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Execute CLI actions (--seed-demo, --sync, --import-file, --watch) without starting the web server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web server bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db = Database(args.db)
    db.init_db()
    fetcher = MultiSourceFetcher(db=db)

    if args.seed_demo:
        summary = seed_demo_data(db, days=120, force=True)
        print(
            f"[seed-demo] Seeded {summary['accounts_seeded']} handles, "
            f"{summary['profile_snapshots_seeded']} profile snapshots, "
            f"{summary['posts_seeded']} posts, "
            f"{summary['post_snapshots_seeded']} post snapshots into {args.db}."
        )

    if args.import_file:
        path = Path(args.import_file)
        if not path.exists():
            print(f"[error] Import file not found: {path}", file=sys.stderr)
            return 1
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() in {".json", ".js"}:
            res = fetcher.import_json(text, default_handle=args.handle)
        else:
            res = fetcher.import_csv(text, default_handle=args.handle)
        print(
            f"[import] Imported {res.posts_imported} posts and {res.snapshots_created} snapshots "
            f"for @{res.handle} ({res.format})."
        )

    if args.sync is not None:
        targets = [normalize_handle(h) for h in args.sync if normalize_handle(h)] or [
            a["handle"] for a in db.list_accounts()
        ]
        if not targets:
            print("[sync] No handles tracked yet. Pass --sync @handle or --seed-demo first.")
        for handle in targets:
            res = fetcher.sync_handle(handle)
            print(
                f"[sync] @{res.handle} | source={res.source_used} | "
                f"posts={res.posts_synced} | snapshots={res.snapshots_recorded} | "
                f"success={res.success}"
            )
            for warn in res.warnings:
                print(f"  warning: {warn}")

    scheduler: BackgroundSyncScheduler | None = None
    if args.watch:
        interval_sec = parse_interval_seconds(args.interval)
        scheduler = BackgroundSyncScheduler(db=db, fetcher=fetcher, interval_seconds=interval_sec)
        if args.no_server:
            print(f"[watch] Running snapshot scheduler every {args.interval} ({interval_sec}s). Press Ctrl+C to stop.")
            scheduler.start_background(run_immediately=True)
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                scheduler.stop()
                print("\n[watch] Stopped cleanly.")
            return 0
        scheduler.start_background(run_immediately=False)
        print(f"[watch] Background snapshot scheduler active (interval={args.interval}).")

    if args.no_server:
        return 0

    app = create_app(db_path=args.db, scheduler=scheduler)
    print(f"[server] Starting X Telemetry dashboard at http://{args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if scheduler is not None:
            scheduler.stop()
    return 0


if __name__ != "__main__":
    app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
