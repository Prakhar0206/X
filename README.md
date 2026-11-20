# X Telemetry — Local-First X (Twitter) Analytics & Impression Tracker

A local-first X (Twitter) analytics engine and interactive web dashboard powered by Python, SQLite (WAL mode with time-series snapshots), FastAPI, and Chart.js.

## Features

- **Multi-Source Fallback Fetching Engine**: Queries FxTwitter (`api.fxtwitter.com`) and falls back automatically to VxTwitter (`api.vxtwitter.com`), Twitter Syndication (`cdn.syndication.twimg.com`), and Nitter RSS/JSON instances with exponential backoff, jitter, User-Agent rotation, cursor pagination, and graceful degradation.
- **SQLite Snapshot Architecture (`x_analytics.db`)**: Tracks `accounts`, `profile_snapshots`, `posts`, and `post_snapshots` with UTC ISO-8601 timestamps and computed deltas (`followers_delta`, `views_delta`, `+Δ views/24h`).
- **Dual-Mode Time-Window Analytics (`7D`, `30D`, `7W`, `90D`, `Lifetime`, `Custom`)**:
  - **Cohort Mode**: Aggregates metrics for posts published within the selected window.
  - **Velocity Mode**: Measures new impressions and followers gained *during* the window across all tracked posts using snapshot deltas.
- **Rich KPIs & 5 Interactive Visualizations**: Engagement Rate (`ER%`), High-Signal Score (weighted toward bookmarks & replies), Bookmark-to-Like Ratio, Virality Ratio, Daily/Weekly/Monthly Impressions & Engagement combo chart, Follower Growth Curve, Posting Cadence vs. Impressions, 7×24 Best Time to Post Heatmap, and Media vs. Text-Only Breakdown.
- **Archive & CSV Import/Export**: Imports Official X Analytics CSVs, `tweets.js` / JSON archives, and native bundles; exports clean CSV and JSON.
- **Background Scheduler & 120-Day Demo Seeder**: Run `--seed-demo` for instant 120-day multi-handle data and `--watch --interval 6h` to capture snapshots silently in the background.

## Quick Start

```bash
pip install -r requirements.txt

# 1. Populate 120 days of realistic multi-handle demo data and start the dashboard
python main.py --seed-demo

# 2. Or start the server directly (with optional background snapshot watcher)
python main.py --watch --interval 6h --port 8000
```

Open **http://127.0.0.1:8000** in your browser.

## CLI Options

```bash
# Sync specific handles without starting the web server
python main.py --sync @alexrivera_dev --no-server

# Import an official X Analytics CSV export
python main.py --import-file path/to/tweet_activity.csv --handle @alexrivera_dev --no-server

# Run background watcher standalone every 6 hours
python main.py --watch --interval 6h --no-server
```

## Running Tests

```bash
pytest -v
```
