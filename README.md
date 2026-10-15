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
columns (`sql/004`, `sql/008`, `sql/009`).

After applying `sql/008_movie_stats_columns.sql`, populate the new movie columns once:

```bash
flask --app run.py backfill-movie-extras            # or --limit 200 for an incremental run
```

`sql/009_friends_list_visibility.sql` adds the per-user friends-list switch used by public
profiles. Reads fall back gracefully without it, but the settings toggle can't save until it's applied.

`sql/010_movies_of_the_day.sql` adds `user_weekly_picks.recent`, the two-week history that stops
Movies of the Day repeating a film. Safe to apply before or after deploying: without it the picks
still change every day, but can only avoid repeating the previous day's.

## Scheduled commands

Run these from cron (they are not triggered by the app itself):

| Command | Cadence | Purpose |
|---|---|---|
| `flask --app run.py prune-movies` | weekly | delete unreferenced movie cache rows |
| `flask --app run.py refresh-stale-movies` | monthly | TMDB terms: refresh cached data older than 150 days |
| `flask --app run.py backfill-movie-extras` | once after `sql/008` | fill the stats columns for reviewed/watchlisted films |
| `flask --app run.py repair-missing-backdrops` | once, after deploying the stub fix | re-fetch films whose backdrop the old recommendation stubs blanked |

## Taste stats + Wrapped

`GET /api/stats/me` — all-time taste dashboard. `GET /api/stats/wrapped` — which years have a
Wrapped and whether each is unlocked. `GET /api/stats/wrapped/<year>` — the year's story; answers
**423** until the unlock date (`WRAPPED_UNLOCK_MONTH_DAY`, default `12-01`, evaluated in UTC), so
nothing about the current year can be previewed. Pass `?tz=<IANA zone>` so months and year
boundaries are bucketed in the user's local time.

Payloads are cached in Redis under a per-user version that every write of the user's own data bumps
(`app/services/cache.py`). `WRAPPED_PREVIEW_UNLOCK=1` bypasses the date for local development only.

## Public film pages + sitemap

The website has a public page per film (`/film/603-the-matrix`). Two public endpoints back it:

- `GET /api/movies/<id>/community` — how members rated the film: totals only (count, average, and
  the 1–5 star spread), never who rated it. The average and spread appear only once at least
  3 members have rated the film (`COMMUNITY_MIN_RATINGS` in `app/services/community.py`).
- `GET /api/sitemap/films.xml` — every film with at least that many ratings, as page URLs built from
  `PUBLIC_SITE_URL` (env, default `https://goodviews.online`; change it with the domain). The
  website's `robots.txt` points search engines here.

Page URL slugs come from `app/utils/slug.py`, which mirrors the client's `src/utils/filmUrl.ts`;
the two must stay in step or the sitemap and the pages' canonical URLs disagree.

Every API response also carries `nosniff`, `Referrer-Policy`, `X-Frame-Options` and (over HTTPS)
HSTS headers. The API has no `robots.txt` on purpose: search engines must be able to fetch
`/api/movies/...` to render film pages.
