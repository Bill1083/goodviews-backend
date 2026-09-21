-- Taste dashboard + Wrapped: slim, stats-friendly movie metadata. Apply
-- manually via the Supabase SQL editor — this repo has no migration
-- tooling, schema lives in hosted Supabase (see
-- app/services/supabase_client.py for the access pattern).
--
-- MUST be applied BEFORE deploying the backend that ships with it: from that
-- deploy on, every movie fetch (app/services/movie_cache.py) writes these
-- columns. The backend retries without them if PostgREST reports them
-- missing, so a forgotten migration degrades to "no taste stats" rather than
-- breaking movie click-throughs — but nothing gets populated until this runs.
-- Same ordering constraint as sql/004.
--
-- Why derived directors/top_cast columns instead of reading credits: the raw
-- TMDB credits jsonb is 30-100 KB per popular film. A stats computation over
-- a few hundred reviewed films would drag tens of MB through PostgREST on
-- every cache miss; the slim shapes here are under 1 KB per film. They are
-- written from the same TMDB payload as credits, in the same UPDATE, so they
-- can never drift from it.

ALTER TABLE movies
    ADD COLUMN IF NOT EXISTS directors            jsonb,
    ADD COLUMN IF NOT EXISTS top_cast             jsonb,
    ADD COLUMN IF NOT EXISTS original_language    text,
    ADD COLUMN IF NOT EXISTS production_countries text[],
    ADD COLUMN IF NOT EXISTS budget               bigint,
    ADD COLUMN IF NOT EXISTS revenue              bigint,
    ADD COLUMN IF NOT EXISTS popularity           real,
    ADD COLUMN IF NOT EXISTS vote_count           integer,
    ADD COLUMN IF NOT EXISTS collection_id        integer,
    ADD COLUMN IF NOT EXISTS collection_name      text,
    ADD COLUMN IF NOT EXISTS tagline              text;

-- Backfill directors/top_cast from the credits already on disk — no TMDB
-- calls needed. Rows whose credits were never fetched (fallback stubs written
-- while TMDB was down) stay NULL and are picked up by
-- `flask backfill-movie-extras`, which also fills the columns below.
UPDATE movies m
SET directors = COALESCE((
        SELECT jsonb_agg(jsonb_build_object('id', c->'id', 'name', c->'name', 'profile_path', c->'profile_path'))
        FROM jsonb_array_elements(m.credits->'crew') AS c
        WHERE c->>'job' = 'Director'
    ), '[]'::jsonb)
WHERE m.credits IS NOT NULL
  AND jsonb_typeof(m.credits->'crew') = 'array'
  AND m.directors IS NULL;

UPDATE movies m
SET top_cast = COALESCE((
        SELECT jsonb_agg(jsonb_build_object('id', s.c->'id', 'name', s.c->'name', 'profile_path', s.c->'profile_path')
                         ORDER BY NULLIF(s.c->>'order', '')::int NULLS LAST)
        FROM (
            SELECT c
            FROM jsonb_array_elements(m.credits->'cast') AS c
            ORDER BY NULLIF(c->>'order', '')::int NULLS LAST
            LIMIT 10
        ) AS s
    ), '[]'::jsonb)
WHERE m.credits IS NOT NULL
  AND jsonb_typeof(m.credits->'cast') = 'array'
  AND m.top_cast IS NULL;

-- The stats loader pages through one user's reviews newest-first (and the
-- existing per-user review lookups benefit too).
CREATE INDEX IF NOT EXISTS idx_reviews_user_created_at ON reviews (user_id, created_at DESC);

COMMENT ON COLUMN movies.directors IS 'Stats-friendly [{id, name, profile_path}] of credits.crew entries with job = Director. [] = fetched but none credited; NULL = credits never fetched (see sql/008).';
COMMENT ON COLUMN movies.top_cast IS 'Stats-friendly first 10 credits.cast entries by billing order, [{id, name, profile_path}]. NULL = credits never fetched.';
COMMENT ON COLUMN movies.original_language IS 'TMDB original_language (ISO 639-1). NULL means the row was last written before sql/008 — used as the marker by `flask backfill-movie-extras`.';
COMMENT ON COLUMN movies.production_countries IS 'TMDB production_countries as ISO 3166-1 codes.';
COMMENT ON COLUMN movies.budget IS 'TMDB budget in USD; NULL where TMDB reports 0 (unknown).';
COMMENT ON COLUMN movies.revenue IS 'TMDB revenue in USD; NULL where TMDB reports 0 (unknown).';
COMMENT ON COLUMN movies.popularity IS 'TMDB popularity score at last refresh — low values flag "hidden gems" in the taste stats.';
COMMENT ON COLUMN movies.vote_count IS 'TMDB vote count at last refresh — guards "hot take" comparisons against thinly-rated films.';
COMMENT ON COLUMN movies.collection_id IS 'TMDB belongs_to_collection.id (franchise), when any.';
COMMENT ON COLUMN movies.collection_name IS 'TMDB belongs_to_collection.name, when any.';
COMMENT ON COLUMN movies.tagline IS 'TMDB tagline, when any.';
