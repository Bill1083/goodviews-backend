-- Review moderation: automatic strikes for hate/swearing in review text, and
-- an admin queue for accounts that reach the third strike.
--
-- Apply manually via the Supabase SQL editor — this repo has no migration
-- tooling. Apply BEFORE deploying the backend that ships with it. Until it is
-- applied the app degrades gracefully: moderation lookups fail open (nobody is
-- suspended or blocked) and the admin page simply has nothing to show.
--
-- Strike ladder (profiles.warning_count):
--   1 -> warning            (status stays 'active')
--   2 -> 7-day suspension   (status 'suspended', suspended_until set)
--   3 -> blocked            (status 'blocked' + a pending deletion_request)
-- An admin approves the deletion (account deleted) or rejects it (account
-- restored with 0, 1 or 2 warnings of the admin's choosing).
--
-- AFTER APPLYING, make yourself the admin (replace the username):
--   UPDATE profiles SET is_admin = true WHERE username = 'your-username';

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS warning_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS moderation_status text NOT NULL DEFAULT 'active'
        CHECK (moderation_status IN ('active', 'suspended', 'blocked')),
    ADD COLUMN IF NOT EXISTS suspended_until timestamptz,
    ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN profiles.warning_count IS 'Moderation strikes: 1 warned, 2 suspended, 3 blocked. Admins can set it back to 0-2 when restoring an account.';
COMMENT ON COLUMN profiles.moderation_status IS 'active | suspended (cannot post reviews until suspended_until) | blocked (no access; awaiting admin decision on deletion).';
COMMENT ON COLUMN profiles.is_admin IS 'Can use the /admin page and /api/admin endpoints. Set manually in SQL; never settable through the API.';

-- Audit trail. user_id / admin_id are deliberately NOT foreign keys so the
-- history survives the account being deleted.
CREATE TABLE IF NOT EXISTS moderation_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL,
    username      text,
    action        text NOT NULL CHECK (action IN (
                      'warning', 'suspension', 'block',
                      'suspension_lifted', 'warnings_set',
                      'restored', 'deleted')),
    category      text,            -- 'profanity' | 'hate' for automatic strikes
    flagged_text  text,            -- the removed comment, truncated, for the admin's context
    review_id     uuid,
    admin_id      uuid,
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS moderation_events_user_idx ON moderation_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS moderation_events_recent_idx ON moderation_events (created_at DESC);

CREATE TABLE IF NOT EXISTS deletion_requests (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL,
    username           text,
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    resolved_at        timestamptz,
    resolved_by        uuid,
    restored_warnings  integer CHECK (restored_warnings BETWEEN 0 AND 2)
);

CREATE INDEX IF NOT EXISTS deletion_requests_status_idx ON deletion_requests (status, created_at);
-- At most one open request per account.
CREATE UNIQUE INDEX IF NOT EXISTS deletion_requests_one_pending_idx ON deletion_requests (user_id) WHERE status = 'pending';

-- Only the backend (service role) touches these tables.
ALTER TABLE moderation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;
