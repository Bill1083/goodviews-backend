-- Streaming services: which providers (Netflix, Stan, Disney+, etc. — TMDB
-- provider ids) a user has told us they subscribe to. Drives the "Your
-- Streaming Services" Discover carousel, which only shows films available
-- via one of these on TMDB/JustWatch's watch-provider data for AU.
--
-- Apply manually via the Supabase SQL editor — this repo has no migration
-- tooling, schema lives in hosted Supabase. Safe to apply before or after
-- deploying: until it's applied, GET /api/profile falls back to serving the
-- profile without this field (see PRE_STREAMING_COLUMNS in
-- app/controllers/profile.py), and the carousel just doesn't appear (no
-- provider ids to read means the client never gets this far).

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS streaming_provider_ids integer[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN profiles.streaming_provider_ids IS 'TMDB watch-provider ids (watch_region=AU) this user says they subscribe to — e.g. Netflix, Stan, Disney+. Picked from GET /api/movies/streaming-providers, the curated subset app/services/streaming_picks.py exposes.';
