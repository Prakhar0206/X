# X (Twitter) Analytics & Impression Tracker — Implementation Plan

> **For agentic workers:** Execute this plan continuously task-by-task using TDD (`superpowers:test-driven-development`) and verify every deliverable with fresh command output (`superpowers:verification-before-completion`).

**Goal:** Build a production-grade, local-first X (Twitter) Analytics & Impression Tracker in Python with SQLite time-series snapshots, a multi-source fallback fetching engine, dual-mode time-window analytics (`7D`, `30D`, `7W`, `90D`, `Lifetime`, `Custom` across Cohort and Velocity modes), a 120-day demo seeder (`--seed-demo`), a background scheduler (`--watch --interval 6h`), and an interactive FastAPI + TailwindCSS + Chart.js web dashboard.

**Project Root:** `C:\Users\aprak\.gemini\antigravity\scratch\x-analytics-tracker`

**Tech Stack:** Python 3.14, SQLite3 (WAL mode, indexed time-series schema), FastAPI + Uvicorn, HTTPX, Pydantic v2, Python-Dateutil, Pytest, TailwindCSS + Chart.js + custom CSS telemetry design system.

---

## Global Constraints & Exact Formulas

1. **Workspace & Database**:
   - Default SQLite file: `x_analytics.db` in the project root (configurable via `db_path` argument or `X_TRACKER_DB` environment variable).
   - Every timestamp stored in SQLite (`created_at`, `last_synced_at`, `captured_at`, `published_at`, `updated_at`) MUST be normalized to UTC ISO-8601 format: `YYYY-MM-DDTHH:MM:SSZ`.
   - Handle strings stored in the database MUST be normalized to lowercase without a leading `@` (e.g. `"alexrivera_dev"`), while UI formatting displays `@alexrivera_dev`.
2. **KPI Formulas**:
   - **Engagement Rate (%)**: `((likes + replies + reposts + bookmarks) / views) * 100` if `views > 0` else `0.0` (rounded to 2 decimal places).
   - **High-Signal Score**: `(((4.0 * bookmarks) + (3.0 * replies) + (2.0 * reposts) + (2.5 * quotes) + (1.0 * likes)) / views) * 1000` if `views > 0` else `0.0` (rounded to 2 decimal places).
   - **Bookmark-to-Like Ratio (%)**: `(bookmarks / likes) * 100` if `likes > 0` else `0.0` (rounded to 2 decimal places).
   - **Virality Ratio (%)**: `(reposts / views) * 100` if `views > 0` else `0.0` (rounded to 3 decimal places).
   - **Period-over-Period `%` Change**: `((current - previous) / previous) * 100` if `previous > 0` else (`100.0` if `current > 0` else `0.0`).
3. **Time Windows**:
   - `7d`: 7 days (`168` hours)
   - `30d`: 30 days
   - `7w`: 7 weeks (`49` days)
   - `90d`: 90 days
   - `lifetime`: all history from earliest record to `now`
   - `custom`: explicit `start_date` and `end_date` ISO-8601 bounds

---

## Ordered Execution Tasks

- Task 1: Project Scaffolding, Dependencies & Task Artifact Setup
- Task 2: SQLite Database & Snapshot Architecture (TDD)
- Task 3: Multi-Source / Fallback Fetching Engine & Importers (TDD)
- Task 4: Time-Window Analytics Engine — 7D, 30D, 7W, 90D, Lifetime, Cohort & Velocity (TDD)
- Task 5: 120-Day Realistic Demo Seeder & Background Scheduler (TDD)
- Task 6: FastAPI REST Server, Interactive Web Dashboard UI & CLI Entrypoint (TDD)
- Task 7: End-to-End Live Server & Browser Verification
