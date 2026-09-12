-- "Remember this device" tokens for the MFA challenge. Apply manually via
-- the Supabase SQL editor — this repo has no migration tooling, schema lives
-- in hosted Supabase (see server/app/services/supabase_client.py for the
-- access pattern, server/app/services/trusted_devices.py for how this table
-- is used, and TRUSTED_DEVICE_TTL_DAYS in config.py for the 30-day default).

CREATE TABLE IF NOT EXISTS trusted_devices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trusted_devices_user_id ON trusted_devices (user_id);
CREATE INDEX IF NOT EXISTS idx_trusted_devices_token_hash ON trusted_devices (token_hash);

-- Only the Flask backend (service-role key) ever touches this table, same as
-- every other table here — RLS with no policies means the anon/authenticated
-- keys can't read or write it directly even if a client somehow tried.
ALTER TABLE trusted_devices ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE trusted_devices IS '"Remember this device" tokens that let a session skip its MFA challenge for TRUSTED_DEVICE_TTL_DAYS after a successful verify.';
COMMENT ON COLUMN trusted_devices.token_hash IS 'sha256(raw_token) — only the hash is stored; the raw token is shown to the client once, at creation time.';
COMMENT ON COLUMN trusted_devices.last_used_at IS 'Bumped on every successful verify; reserved for a future "trusted devices" list in Settings.';
