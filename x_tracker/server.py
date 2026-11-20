"""FastAPI REST API and static frontend server for the X Analytics Tracker."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from x_tracker.analytics import (
    AnalyticsEngine,
    compute_bookmark_to_like_ratio,
    compute_engagement_rate,
    compute_high_signal_score,
    compute_virality_ratio,
)
from x_tracker.database import DEFAULT_DB_PATH, Database
from x_tracker.fetcher import STATUS_URL_RE, MultiSourceFetcher
from x_tracker.models import normalize_handle, utc_now_iso
from x_tracker.scheduler import BackgroundSyncScheduler
from x_tracker.seeder import DEMO_PROFILES, seed_demo_data

STATIC_DIR = Path(__file__).parent / "static"
PROFILE_URL_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/@?([A-Za-z0-9_]{1,30})/?$", re.IGNORECASE)


def _parse_handle_input(raw_input: str) -> tuple[str, list[str] | None]:
    """Extract normalized handle and optional status ID from @handle, profile URL, or post URL."""
    text = (raw_input or "").strip()
    status_match = STATUS_URL_RE.search(text)
    if status_match:
        return normalize_handle(status_match.group(1)), [status_match.group(2)]
    profile_match = PROFILE_URL_RE.match(text)
    if profile_match:
        return normalize_handle(profile_match.group(1)), None
    return normalize_handle(text), None


class AddAccountPayload(BaseModel):
    handle: str
    sync: bool = True
    background: bool = False


class SyncPayload(BaseModel):
    handle: str | None = None
    max_pages: int = 20
    post_ids: list[str] | None = None
    background: bool = False


def create_app(
    db_path: str | Path | None = None,
    transport: httpx.BaseTransport | None = None,
    scheduler: BackgroundSyncScheduler | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application with local SQLite persistence."""
    resolved_db = Path(db_path) if db_path is not None else Path(DEFAULT_DB_PATH)
    db = Database(resolved_db)
    db.init_db()

    fetcher = MultiSourceFetcher(
        db=db,
        transport=transport,
        sleep_fn=(lambda _: None) if transport is not None else __import__("time").sleep,
    )
    analytics = AnalyticsEngine(db=db)
    active_scheduler = scheduler

    sync_lock = threading.Lock()
    sync_state: dict[str, Any] = {
        "is_running": False,
        "progress_pct": 0,
        "current_handle": None,
        "message": "Idle",
        "last_synced_at": None,
        "last_result": None,
    }

    def _run_sync_targets(
        targets: list[str], max_pages: int, post_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            total = len(targets)
            for idx, handle in enumerate(targets):
                with sync_lock:
                    sync_state["current_handle"] = handle
                    sync_state["progress_pct"] = max(12, int((idx / total) * 90) + 10)
                    sync_state["message"] = f"Fetching @{handle} ({idx + 1}/{total})..."

                def _on_sync_progress(pct: int, msg: str, base_idx: int = idx) -> None:
                    with sync_lock:
                        scaled = int((base_idx / total) * 90 + (pct / total) * 0.9)
                        sync_state["progress_pct"] = max(10, min(95, scaled))
                        sync_state["message"] = msg

                res = fetcher.sync_handle(
                    handle,
                    max_pages=max_pages,
                    post_ids=post_ids,
                    progress_callback=_on_sync_progress,
                )
                results.append(res.model_dump())
        finally:
            with sync_lock:
                sync_state["is_running"] = False
                sync_state["progress_pct"] = 100
                sync_state["current_handle"] = None
                sync_state["last_synced_at"] = utc_now_iso()
                sync_state["last_result"] = results[0] if len(results) == 1 else results
                sync_state["message"] = (
                    f"Synced {len(results)} handle(s) at {sync_state['last_synced_at']}"
                )
        return results

    def _start_sync(
        targets: list[str],
        max_pages: int,
        post_ids: list[str] | None,
        background: bool,
    ) -> tuple[bool, list[dict[str, Any]] | None]:
        with sync_lock:
            if background and sync_state["is_running"]:
                return False, None
            sync_state["is_running"] = True
            sync_state["progress_pct"] = 10
            sync_state["current_handle"] = targets[0]
            sync_state["message"] = f"Syncing @{targets[0]}..."

        if background:
            thread = threading.Thread(
                target=_run_sync_targets,
                args=(targets, max_pages, post_ids),
                name="x-tracker-sync",
                daemon=True,
            )
            thread.start()
            return True, None

        return True, _run_sync_targets(targets, max_pages, post_ids)

    app = FastAPI(title="X Telemetry — Local-First X Analytics", version="1.0.0")
    app.state.db = db
    app.state.fetcher = fetcher
    app.state.analytics = analytics

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_dashboard() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Dashboard frontend not found.")
        return FileResponse(str(index_path), media_type="text/html; charset=utf-8")

    @app.get("/api/accounts")
    def list_accounts() -> dict[str, Any]:
        accounts = db.list_accounts()
        sched_status = active_scheduler.get_status() if active_scheduler else None
        return {"accounts": accounts, "scheduler": sched_status}

    @app.post("/api/accounts")
    def add_account(payload: AddAccountPayload) -> dict[str, Any]:
        norm, extracted_post_ids = _parse_handle_input(payload.handle)
        if not norm:
            raise HTTPException(status_code=400, detail="A valid handle or X URL is required.")

        existing = db.get_account(norm)
        if existing is None:
            db.upsert_account(handle=norm, display_name=norm)

        sync_result = None
        started = False
        if payload.sync:
            started, results = _start_sync(
                targets=[norm],
                max_pages=15,
                post_ids=extracted_post_ids,
                background=payload.background,
            )
            if results:
                sync_result = results[0]

        account = db.get_account(norm)
        return {
            "account": account,
            "started": started,
            "sync_result": sync_result,
            "status": dict(sync_state),
        }

    @app.delete("/api/accounts/{handle}")
    def remove_account(handle: str) -> dict[str, Any]:
        deleted = db.delete_account(handle)
        if not deleted:
            raise HTTPException(status_code=404, detail="Handle not found.")
        return {"deleted": True, "handle": normalize_handle(handle)}

    @app.delete("/api/demo-data")
    def purge_demo_data() -> dict[str, Any]:
        removed: list[str] = []
        for profile in DEMO_PROFILES:
            h = profile["handle"]
            if db.delete_account(h):
                removed.append(h)
        return {"deleted": removed, "count": len(removed)}

    @app.get("/api/analytics")
    def get_analytics(
        handle: str = Query(..., min_length=1),
        timeframe: str = Query("30d"),
        mode: str = Query("cohort"),
        granularity: str = Query("daily"),
        start_date: str | None = Query(None),
        end_date: str | None = Query(None),
        search: str | None = Query(None),
        post_type: str | None = Query(None),
        sort_by: str = Query("views"),
        sort_dir: str = Query("desc"),
    ) -> dict[str, Any]:
        return analytics.compute_dashboard_analytics(
            handle=handle,
            timeframe=timeframe,
            mode=mode,
            granularity=granularity,
            start_date=start_date,
            end_date=end_date,
            search=search,
            post_type=post_type,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    @app.get("/api/posts/{post_id}")
    def get_post_detail(post_id: str) -> dict[str, Any]:
        detail = db.get_post_detail(post_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        views = int(detail.get("views", 0))
        likes = int(detail.get("likes", 0))
        replies = int(detail.get("replies", 0))
        reposts = int(detail.get("reposts", 0))
        bookmarks = int(detail.get("bookmarks", 0))
        quotes = int(detail.get("quotes", 0))
        detail["engagement_rate"] = compute_engagement_rate(
            views, likes, replies, reposts, bookmarks
        )
        detail["high_signal_score"] = compute_high_signal_score(
            views, likes, replies, reposts, bookmarks, quotes
        )
        detail["bookmark_to_like_ratio"] = compute_bookmark_to_like_ratio(bookmarks, likes)
        detail["virality_ratio"] = compute_virality_ratio(reposts, views)
        return detail

    @app.post("/api/sync")
    def trigger_sync(payload: SyncPayload) -> dict[str, Any]:
        targets = (
            [normalize_handle(payload.handle)]
            if payload.handle
            else [a["handle"] for a in db.list_accounts()]
        )
        if not targets:
            raise HTTPException(status_code=400, detail="No handles available to sync.")

        started, results = _start_sync(
            targets=targets,
            max_pages=payload.max_pages,
            post_ids=payload.post_ids,
            background=payload.background,
        )
        if payload.background:
            return {
                "started": started,
                "already_running": not started,
                "status": dict(sync_state),
            }

        assert results is not None
        return {
            "started": started,
            "result": results[0] if len(results) == 1 else None,
            "results": results,
            "status": dict(sync_state),
        }

    @app.get("/api/sync/status")
    def get_sync_status() -> dict[str, Any]:
        with sync_lock:
            return dict(sync_state)

    @app.post("/api/seed-demo")
    def trigger_seed_demo(force: bool = True) -> dict[str, Any]:
        return seed_demo_data(db, days=120, force=force)

    @app.post("/api/import")
    async def import_data(request: Request) -> dict[str, Any]:
        content_type = (request.headers.get("content-type") or "").lower()
        fmt = ""
        text_content = ""
        default_handle: str | None = None

        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            default_handle = str(form.get("handle") or "") or None
            fmt = str(form.get("format") or "").lower()
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="Missing uploaded file.")
            raw_bytes = await upload.read()
            text_content = raw_bytes.decode("utf-8-sig", errors="replace")
            filename = str(getattr(upload, "filename", "") or "").lower()
            if not fmt:
                fmt = "json" if filename.endswith((".json", ".js")) else "csv"
        else:
            try:
                body = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid request body: {exc}") from exc
            fmt = str(body.get("format") or "").lower()
            text_content = str(body.get("content") or "")
            default_handle = str(body.get("handle") or "") or None
            if not fmt:
                stripped = text_content.lstrip()
                fmt = "json" if stripped.startswith(("{", "[", "window.")) else "csv"

        if not text_content.strip():
            raise HTTPException(status_code=400, detail="Import content is empty.")

        try:
            if fmt == "json":
                result = fetcher.import_json(text_content, default_handle=default_handle)
            else:
                result = fetcher.import_csv(text_content, default_handle=default_handle)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Import failed: {exc}") from exc

        return result.model_dump()

    @app.get("/api/export")
    def export_data(
        handle: str = Query(..., min_length=1),
        format: str = Query("csv"),
        start_date: str | None = Query(None),
        end_date: str | None = Query(None),
    ) -> Response:
        norm = normalize_handle(handle)
        fmt = (format or "csv").strip().lower()
        if fmt == "json":
            payload = fetcher.export_json(norm, start_date=start_date, end_date=end_date)
            return Response(
                content=payload,
                media_type="application/json; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{norm}_x_analytics.json"'
                },
            )

        payload = fetcher.export_csv(norm, start_date=start_date, end_date=end_date)
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{norm}_x_analytics.csv"'
            },
        )

    return app
