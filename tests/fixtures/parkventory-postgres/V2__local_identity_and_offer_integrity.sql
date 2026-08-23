CREATE TABLE magic_link_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    normalized_email TEXT NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    purpose TEXT NOT NULL DEFAULT 'SIGN_IN' CHECK (purpose IN ('SIGN_IN', 'INVITATION')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (created_at < expires_at)
);

CREATE INDEX idx_magic_link_active
    ON magic_link_request(token_hash, expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE app_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_account_id UUID NOT NULL REFERENCES user_account(id),
    active_membership_id UUID NOT NULL REFERENCES membership(id),
    token_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (created_at < expires_at)
);

CREATE INDEX idx_app_session_active
    ON app_session(token_hash, expires_at)
    WHERE revoked_at IS NULL;

ALTER TABLE availability_offer
    ADD CONSTRAINT availability_offer_no_published_overlap
    EXCLUDE USING gist (
        organization_id WITH =,
        parking_spot_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status = 'PUBLISHED');
