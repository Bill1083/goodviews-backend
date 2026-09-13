-- "I'm not interested" (For You dismissals) + "Movie Picks of the Week".
-- Apply manually via the Supabase SQL editor — see
-- 003_recommendations_and_onboarding.sql for the established pattern this follows.

CREATE TABLE IF NOT EXISTS dismissed_recommendations (
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    movie_id integer NOT NULL REFERENCES movies (id) ON DELETE CASCADE,
    dismissed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, movie_id)
);

ALTER TABLE dismissed_recommendations ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE dismissed_recommendations IS 'Movies a user marked "not interested" on from the For You feed — permanently excluded from future For You candidate generation (see mark_not_interested / _compute in app/services/recommendations.py).';

-- Cached "Movie Picks of the Week" per user, recomputed every 7 days. Mirrors
-- user_recommendations.items (jsonb preserves rank order) so each pick can
-- carry its own reason string and a single slot can be patched out (e.g. once
-- reviewed) without a full recompute — same pattern as mark_not_interested.
CREATE TABLE IF NOT EXISTS user_weekly_picks (
    user_id uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    items jsonb NOT NULL DEFAULT '[]',
    computed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE user_weekly_picks ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE user_weekly_picks IS 'Cached "Movie Picks of the Week" (exactly 3 items) per user, recomputed every 7 days (see get_weekly_picks_for_user in app/services/recommendations.py). items is rank-ordered [{"movie_id": int, "reason": string}, ...], same shape as user_recommendations.items.';
