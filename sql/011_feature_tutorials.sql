-- Feature tutorials: which in-app walkthroughs a user has already dismissed
-- or completed. One generic column, reused by every future tutorial —
-- adding a new tutorial never needs a new migration, just a new key in
-- client/src/features/tutorials/registry.ts and a PATCH to /api/profile,
-- same shape as hide_recent_movies etc. already use.
--
-- Apply manually via the Supabase SQL editor — this repo has no migration
-- tooling, schema lives in hosted Supabase. Safe to apply before or after
-- deploying: until it's applied, GET /api/profile falls back to serving the
-- profile without this field (see PRE_TUTORIALS_COLUMNS in
-- app/controllers/profile.py) and no tutorials are shown — they just start
-- appearing once the column exists.
--
-- Note: account age (used to decide whether a tutorial counts as "new" for
-- a given user) comes from the already-authenticated Supabase auth user's
-- own created_at, not a new column here — no backfill/migration needed for
-- that part.

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS seen_tutorials text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN profiles.seen_tutorials IS 'Keys of feature tutorials/walkthroughs this user has dismissed or completed. See client/src/features/tutorials/registry.ts for the full list of keys.';
