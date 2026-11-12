-- Organizer accounts. Emails are stored lowercased.
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only a hash of each emailed token is kept, so a copy of the database can't be used to sign in
CREATE TABLE invites (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    invited_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE password_resets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL
);

-- Events made before accounts existed have no owner, and only the site owner sees them
ALTER TABLE events ADD COLUMN owner_user_id BIGINT REFERENCES users(id);
CREATE INDEX idx_events_owner ON events(owner_user_id);
CREATE INDEX idx_events_purge_after ON events(purge_after) WHERE purge_after IS NOT NULL;
