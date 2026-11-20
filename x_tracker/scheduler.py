"""Background snapshot scheduler for periodic automated handle syncing."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from x_tracker.database import Database
from x_tracker.fetcher import MultiSourceFetcher
from x_tracker.models import SyncResult, normalize_handle, utc_now_iso

INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)


def parse_interval_seconds(interval_str: str) -> int:
    """Parse human interval strings like '6h', '30m', '45s', '1d' into seconds."""
    match = INTERVAL_RE.match(interval_str or "")
    if not match:
        raise ValueError(f"Invalid interval '{interval_str}'. Expected formats like '6h', '30m', '1d'.")
    value = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = value * multipliers[unit]
    if seconds <= 0:
        raise ValueError("Interval must be positive.")
    return seconds


class BackgroundSyncScheduler:
    """Daemon thread scheduler that periodically syncs all tracked handles."""

    def __init__(
        self,
        db: Database,
        fetcher: MultiSourceFetcher,
        interval_seconds: int = 21600,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.interval_seconds = max(1, int(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_run_at: str | None = None
        self._next_run_at: str | None = None
        self._runs_completed = 0
        self._last_results: list[dict[str, Any]] = []

    def run_once(self, handles: list[str] | None = None) -> list[SyncResult]:
        """Execute a single sync pass across requested or all tracked handles."""
        if handles:
            target_handles = [normalize_handle(h) for h in handles if normalize_handle(h)]
        else:
            target_handles = [a["handle"] for a in self.db.list_accounts()]

        results: list[SyncResult] = []
        for h in target_handles:
            try:
                res = self.fetcher.sync_handle(h)
            except Exception as exc:
                res = SyncResult(
                    handle=h,
                    success=False,
                    errors=[str(exc)],
                    synced_at=utc_now_iso(),
                )
            results.append(res)

        with self._lock:
            now = datetime.now(timezone.utc)
            self._last_run_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            self._next_run_at = (now + timedelta(seconds=self.interval_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            self._runs_completed += 1
            self._last_results = [r.model_dump() for r in results]

        return results

    def _loop(self, handles: list[str] | None = None) -> None:
        while not self._stop_event.is_set():
            self.run_once(handles=handles)
            if self._stop_event.wait(self.interval_seconds):
                break

    def start_background(self, handles: list[str] | None = None, run_immediately: bool = False) -> None:
        """Start the background daemon thread."""
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._running = True
            self._next_run_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=0 if run_immediately else self.interval_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        def target() -> None:
            if not run_immediately:
                if self._stop_event.wait(self.interval_seconds):
                    return
            self._loop(handles=handles)

        self._thread = threading.Thread(target=target, name="x-tracker-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to exit and wait briefly."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._lock:
            self._running = False

    def get_status(self) -> dict[str, Any]:
        """Return live scheduler status for API and UI display."""
        with self._lock:
            return {
                "running": self._running,
                "interval_seconds": self.interval_seconds,
                "last_run_at": self._last_run_at,
                "next_run_at": self._next_run_at,
                "runs_completed": self._runs_completed,
                "last_results_summary": self._last_results,
            }
