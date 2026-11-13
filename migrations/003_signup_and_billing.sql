-- Accounts made before open sign-up were invited, so they're already verified
ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMPTZ;
UPDATE users SET email_verified_at = created_at;

CREATE TABLE email_verifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL
);

-- A free event is a one-seller yard sale deleted at midnight. A paid event can be
-- set up before paid_at is set, but can't take sales until it is.
ALTER TABLE events ADD COLUMN plan TEXT NOT NULL DEFAULT 'paid' CHECK (plan IN ('free', 'paid'));
ALTER TABLE events ADD COLUMN paid_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN stripe_session_id TEXT;
-- Events from before billing were the site owner's own
UPDATE events SET paid_at = created_at;
