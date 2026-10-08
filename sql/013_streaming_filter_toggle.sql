-- The "only show what I can stream" switch (Settings). Separate from
-- streaming_provider_ids (sql/012) so turning it off doesn't lose the
-- user's selected services — they can flip filtering on/off without
-- re-picking providers each time.
--
-- Apply manually via the Supabase SQL editor — this repo has no migration
-- tooling, schema lives in hosted Supabase. Safe to apply before or after
-- deploying: until it's applied, GET /api/profile falls back to serving the
-- profile without this field (see OPTIONAL_SELF_PROFILE_COLUMNS in
-- app/controllers/profile.py), which reads as "off" everywhere it's
-- checked, so For You / Movies of the Day just stay unfiltered.

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS streaming_filter_enabled boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN profiles.streaming_filter_enabled IS 'When true, For You and Movies of the Day are filtered to only films available (flatrate) on this user''s streaming_provider_ids. Most Popular This Week is deliberately never filtered by this — see app/services/streaming_picks.py.';
