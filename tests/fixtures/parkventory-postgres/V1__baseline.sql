CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE organization (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'COMMUNITY' CHECK (mode IN ('COMMUNITY', 'GOVERNED', 'SUSPENDED')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, name)
);

CREATE TABLE user_account (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    oidc_subject TEXT UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('PENDING', 'ACTIVE', 'SUSPENDED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_email (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_account_id UUID NOT NULL REFERENCES user_account(id),
    normalized_email TEXT NOT NULL UNIQUE,
    email_type TEXT NOT NULL DEFAULT 'PROFESSIONAL' CHECK (email_type IN ('PROFESSIONAL', 'RECOVERY')),
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE organization_domain (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    normalized_domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (status IN ('CLAIMED', 'VERIFIED', 'REVOKED')),
    proof_method TEXT,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (normalized_domain, status)
);

CREATE TABLE membership (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    user_account_id UUID NOT NULL REFERENCES user_account(id),
    role TEXT NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('MEMBER', 'ADMIN')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('INVITED', 'ACTIVE', 'SUSPENDED', 'LEFT')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, user_account_id),
    UNIQUE (organization_id, id)
);

CREATE TABLE invitation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    invited_by_membership_id UUID,
    normalized_email TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'ACCEPTED', 'EXPIRED', 'REVOKED')),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, invited_by_membership_id)
        REFERENCES membership(organization_id, id),
    UNIQUE (organization_id, id)
);

CREATE TABLE admin_claim (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    claimant_membership_id UUID NOT NULL,
    proof_method TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, claimant_membership_id)
        REFERENCES membership(organization_id, id)
);

CREATE TABLE parking_site (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
    address_label TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, name),
    UNIQUE (organization_id, id)
);

CREATE TABLE parking_spot (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    parking_site_id UUID NOT NULL,
    label TEXT NOT NULL,
    level_label TEXT,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, parking_site_id)
        REFERENCES parking_site(organization_id, id),
    UNIQUE (organization_id, parking_site_id, label),
    UNIQUE (organization_id, id)
);

CREATE TABLE spot_assignment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    parking_spot_id UUID NOT NULL,
    membership_id UUID NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ENDED', 'REVOKED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_at IS NULL OR starts_at < ends_at),
    FOREIGN KEY (organization_id, parking_spot_id)
        REFERENCES parking_spot(organization_id, id),
    FOREIGN KEY (organization_id, membership_id)
        REFERENCES membership(organization_id, id),
    UNIQUE (organization_id, id)
);

ALTER TABLE spot_assignment
    ADD CONSTRAINT spot_assignment_no_active_overlap
    EXCLUDE USING gist (
        organization_id WITH =,
        parking_spot_id WITH =,
        tstzrange(starts_at, COALESCE(ends_at, 'infinity'::timestamptz), '[)') WITH &&
    ) WHERE (status = 'ACTIVE');

CREATE TABLE availability_offer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    parking_spot_id UUID NOT NULL,
    spot_assignment_id UUID NOT NULL,
    offered_by_membership_id UUID NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'PUBLISHED' CHECK (status IN ('DRAFT', 'PUBLISHED', 'WITHDRAWN', 'EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),
    FOREIGN KEY (organization_id, parking_spot_id)
        REFERENCES parking_spot(organization_id, id),
    FOREIGN KEY (organization_id, spot_assignment_id)
        REFERENCES spot_assignment(organization_id, id),
    FOREIGN KEY (organization_id, offered_by_membership_id)
        REFERENCES membership(organization_id, id),
    UNIQUE (organization_id, id)
);

CREATE TABLE reservation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    availability_offer_id UUID NOT NULL,
    parking_spot_id UUID NOT NULL,
    reserved_by_membership_id UUID NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),
    FOREIGN KEY (organization_id, availability_offer_id)
        REFERENCES availability_offer(organization_id, id),
    FOREIGN KEY (organization_id, parking_spot_id)
        REFERENCES parking_spot(organization_id, id),
    FOREIGN KEY (organization_id, reserved_by_membership_id)
        REFERENCES membership(organization_id, id),
    UNIQUE (organization_id, reserved_by_membership_id, idempotency_key),
    UNIQUE (organization_id, id)
);

ALTER TABLE reservation
    ADD CONSTRAINT reservation_no_active_overlap
    EXCLUDE USING gist (
        organization_id WITH =,
        parking_spot_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status IN ('HELD', 'CONFIRMED'));

CREATE TABLE idempotency_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id),
    actor_membership_id UUID NOT NULL,
    command_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, actor_membership_id)
        REFERENCES membership(organization_id, id),
    UNIQUE (organization_id, actor_membership_id, command_type, idempotency_key)
);

CREATE TABLE outbox_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES organization(id),
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id UUID,
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES organization(id),
    actor_membership_id UUID,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id UUID,
    result TEXT NOT NULL CHECK (result IN ('SUCCESS', 'DENIED', 'FAILURE')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_membership_user ON membership(user_account_id);
CREATE INDEX idx_spot_site ON parking_spot(organization_id, parking_site_id);
CREATE INDEX idx_offer_window ON availability_offer(organization_id, starts_at, ends_at)
    WHERE status = 'PUBLISHED';
CREATE INDEX idx_reservation_member ON reservation(organization_id, reserved_by_membership_id, starts_at);
CREATE INDEX idx_outbox_pending ON outbox_event(next_attempt_at)
    WHERE delivered_at IS NULL;
