-- Segmented TMDB cache columns for `movies`. Apply manually via the Supabase
-- SQL editor — this repo has no migration tooling, schema lives in hosted
-- Supabase (see server/app/services/supabase_client.py for the access pattern).
--
-- Segments and their freshness columns:
--   core      -> title, poster_path, release_date, vote_average, genre_ids,
--                overview, runtime, credits          (core_updated_at)
--   media     -> backdrop_path, videos (trailers)    (media_updated_at)
--   providers -> watch_providers                     (providers_updated_at)

ALTER TABLE movies
    ADD COLUMN IF NOT EXISTS overview text,
    ADD COLUMN IF NOT EXISTS runtime integer,
    ADD COLUMN IF NOT EXISTS credits jsonb,
    ADD COLUMN IF NOT EXISTS backdrop_path text,
    ADD COLUMN IF NOT EXISTS videos jsonb,
    ADD COLUMN IF NOT EXISTS watch_providers jsonb,
    ADD COLUMN IF NOT EXISTS core_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS media_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS providers_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_viewed_at timestamptz;

-- Supports the pruning job's "not viewed in N days" lookup.
CREATE INDEX IF NOT EXISTS idx_movies_last_viewed_at ON movies (last_viewed_at);

COMMENT ON COLUMN movies.last_viewed_at IS 'Last time this movie was read through the app (details view, review/watchlist add). Used by the prune-movies job to find cache bloat safe to delete.';
COMMENT ON COLUMN movies.credits IS 'Raw TMDB {cast:[...], crew:[...]} — core segment.';
COMMENT ON COLUMN movies.videos IS 'Raw TMDB videos.results array (trailers etc.) — media segment.';
COMMENT ON COLUMN movies.watch_providers IS 'Raw TMDB watch/providers.results object, keyed by ISO country — providers segment.';
COMMENT ON COLUMN movies.core_updated_at IS 'Last time overview/runtime/credits/genre_ids/vote_average were fetched fresh from TMDB.';
COMMENT ON COLUMN movies.media_updated_at IS 'Last time backdrop_path/videos were fetched fresh from TMDB.';
COMMENT ON COLUMN movies.providers_updated_at IS 'Last time watch_providers was fetched fresh from TMDB.';
