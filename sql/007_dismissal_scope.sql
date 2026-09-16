-- "Not interested in these types of movies" — lets a dismissal also act as a
-- soft taste signal, not just a single-movie exclusion.
-- Apply manually via the Supabase SQL editor — see
-- 003_recommendations_and_onboarding.sql for the established pattern this follows.

ALTER TABLE dismissed_recommendations
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'movie'
    CHECK (scope IN ('movie', 'type'));

COMMENT ON COLUMN dismissed_recommendations.scope IS '''movie'' = exclude just this movie. ''type'' = also demote candidates whose genre mix/director closely match it, weighted by similarity and decaying with dismissed_at (see _type_dislike_penalty / _load_user_signals in app/services/recommendations.py).';
