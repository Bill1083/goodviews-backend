-- Personalized "For You" recommendations + first-run onboarding. Apply
-- manually via the Supabase SQL editor — this repo has no migration
-- tooling, schema lives in hosted Supabase (see
-- server/app/services/supabase_client.py for the access pattern).
--
-- has_onboarded defaults to false, which back-fills EVERY existing row —
-- this is deliberate: every current account (not just new signups) should
-- land on the onboarding wizard once, since none of them have been through
-- it and "For You" has nothing to work with otherwise.

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS has_onboarded boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS onboarding_genre_ids integer[] NOT NULL DEFAULT '{}';

-- Cached "For You" feed per user. `items` is a jsonb array of
-- {"movie_id": int, "reason": string} in rank order — a jsonb array
-- preserves order natively (unlike an integer[] re-fetched via PostgREST's
-- .in_(), which doesn't), and gives each recommended movie a durable home
-- for its "why this is here" reason without a second table.
CREATE TABLE IF NOT EXISTS user_recommendations (
    user_id uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    items jsonb NOT NULL DEFAULT '[]',
    computed_at timestamptz NOT NULL DEFAULT now()
);

-- Only the Flask backend (service-role key) ever touches this table, same
-- pattern as every other table here.
ALTER TABLE user_recommendations ENABLE ROW LEVEL SECURITY;

COMMENT ON COLUMN profiles.has_onboarded IS 'Whether this user has completed the first-run onboarding wizard (genres, quick movie ratings, favourite actors/directors). Defaults false so existing accounts are onboarded once too.';
COMMENT ON COLUMN profiles.onboarding_genre_ids IS 'TMDB genre ids the user picked as favourites during onboarding — a direct input to the "For You" genre-affinity score, distinct from genres inferred from their reviews.';
COMMENT ON TABLE user_recommendations IS 'Cached "For You" feed per user, recomputed on demand when computed_at is more than 24h old (see server/app/services/recommendations.py).';
COMMENT ON COLUMN user_recommendations.items IS 'Rank-ordered [{"movie_id": int, "reason": string}, ...] — reason is shown in the movie detail modal when opened from the For You page.';
