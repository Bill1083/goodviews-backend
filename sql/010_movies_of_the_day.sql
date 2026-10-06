-- Movies of the Day: a short history of what each user has been shown, so the
-- daily picks never repeat a film within two weeks. Apply manually via the
-- Supabase SQL editor — this repo has no migration tooling, schema lives in
-- hosted Supabase.
--
-- The table keeps its user_weekly_picks name: it now holds the daily picks
-- (app/services/daily_picks.py), but renaming it would break the running
-- backend partway through a deploy.
--
-- Safe to apply before or after deploying. Until it is applied the daily
-- picks still work and still refresh every day; they just can only avoid
-- repeating the previous day's films rather than the last fourteen days'.

ALTER TABLE user_weekly_picks
    ADD COLUMN IF NOT EXISTS recent jsonb NOT NULL DEFAULT '[]';

COMMENT ON TABLE user_weekly_picks IS 'Movies of the Day per user (name kept from the weekly feature it replaced). items: today''s picks, each {movie_id, reason, day, source}.';
COMMENT ON COLUMN user_weekly_picks.recent IS 'Films shown as Movies of the Day in the last 14 days, [{movie_id, day}] — never picked again within that window.';
