-- Public profiles: a per-user switch for whether visitors may see who you are
-- friends with. Apply manually via the Supabase SQL editor — this repo has no
-- migration tooling, schema lives in hosted Supabase (see
-- app/services/supabase_client.py for the access pattern).
--
-- Apply BEFORE deploying the backend that ships with it, like sql/004 and
-- sql/008. The profile reads degrade gracefully if it is missing (the friends
-- list is simply treated as visible), but nobody can change the setting until
-- the column exists.
--
-- Defaults to false — showing your friends is the current behaviour of every
-- other social surface in the app (the friends list on your own profile, the
-- shared-film averages), so the switch is opt-in to hiding rather than a
-- silent change for existing accounts. Who may see your profile at all stays
-- with profile_visibility, which this release starts enforcing.

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS hide_friends_list boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN profiles.hide_friends_list IS 'When true, visitors to this user''s profile do not see their friends list or friend count. Separate from profile_visibility, which decides who may open the profile at all.';
