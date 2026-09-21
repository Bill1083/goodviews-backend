# GoodViews API — Backend

Flask-based REST API that proxies TMDB and manages reviews via Supabase.

## Quick Start

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # fill in your keys
python run.py
```

See `../SETUP_AND_DEPLOYMENT.md` for full infrastructure setup.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

The taste-stats maths (`app/services/stats.py`) and the `/api/stats` gate are covered without a
network or a Redis; everything else is exercised manually.

## Database migrations

Schema lives in hosted Supabase. Files in `sql/` are applied by hand in the Supabase SQL editor,
in order. Additive migrations must be applied **before** deploying the backend that writes the new
columns (`sql/004`, `sql/008`).

After applying `sql/008_movie_stats_columns.sql`, populate the new movie columns once:

```bash
flask --app run.py backfill-movie-extras            # or --limit 200 for an incremental run
```

## Scheduled commands

Run these from cron (they are not triggered by the app itself):

| Command | Cadence | Purpose |
|---|---|---|
| `flask --app run.py prune-movies` | weekly | delete unreferenced movie cache rows |
| `flask --app run.py refresh-stale-movies` | monthly | TMDB terms: refresh cached data older than 150 days |
| `flask --app run.py backfill-movie-extras` | once after `sql/008` | fill the stats columns for reviewed/watchlisted films |

## Taste stats + Wrapped

`GET /api/stats/me` — all-time taste dashboard. `GET /api/stats/wrapped` — which years have a
Wrapped and whether each is unlocked. `GET /api/stats/wrapped/<year>` — the year's story; answers
**423** until the unlock date (`WRAPPED_UNLOCK_MONTH_DAY`, default `12-01`, evaluated in UTC), so
nothing about the current year can be previewed. Pass `?tz=<IANA zone>` so months and year
boundaries are bucketed in the user's local time.

Payloads are cached in Redis under a per-user version that every write of the user's own data bumps
(`app/services/cache.py`). `WRAPPED_PREVIEW_UNLOCK=1` bypasses the date for local development only.
