-- Marks reviews created via the onboarding "quick ratings" step, so they
-- can be excluded from friend-facing "recent activity" feeds (a burst of
-- 5-20 reviews created in a couple of minutes during onboarding isn't
-- genuine "just watched this" activity). Apply manually via the Supabase
-- SQL editor — this repo has no migration tooling.

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS is_onboarding boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN reviews.is_onboarding IS 'True when this review was created via the onboarding "quick ratings" step rather than the normal review flow — excluded from the friend recent-activity feed (see server/app/controllers/friends.py:friends_recent_activity).';
