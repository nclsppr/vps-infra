ALTER TABLE app_session
    ADD COLUMN organization_id UUID;

UPDATE app_session session
   SET organization_id = membership.organization_id
  FROM membership
 WHERE membership.id = session.active_membership_id;

ALTER TABLE membership
    ADD CONSTRAINT membership_tenant_session_identity_unique
        UNIQUE (organization_id, id, user_account_id);

ALTER TABLE app_session
    ALTER COLUMN organization_id SET NOT NULL,
    ADD CONSTRAINT app_session_active_membership_tenant_fk
        FOREIGN KEY (organization_id, active_membership_id, user_account_id)
        REFERENCES membership(organization_id, id, user_account_id);

ALTER TABLE outbox_event
    ADD CONSTRAINT outbox_event_tenant_identity_unique
        UNIQUE (organization_id, id);

CREATE TABLE outbox_dispatch (
    event_id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, event_id)
        REFERENCES outbox_event(organization_id, id)
        ON DELETE CASCADE
);

CREATE INDEX idx_outbox_dispatch_available
    ON outbox_dispatch(available_at, created_at);

INSERT INTO outbox_dispatch (event_id, organization_id, available_at, created_at)
SELECT id, organization_id, next_attempt_at, created_at
  FROM outbox_event
 WHERE organization_id IS NOT NULL
   AND delivered_at IS NULL
ON CONFLICT (event_id) DO NOTHING;

COMMENT ON TABLE outbox_dispatch IS
    'Cross-tenant scheduler index containing technical identifiers only; payload access remains protected by outbox_event RLS.';

CREATE FUNCTION app_current_organization_id()
RETURNS UUID
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.organization_id', true), '')::UUID
$$;

CREATE FUNCTION app_current_verified_email()
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.verified_email', true), '')
$$;

CREATE FUNCTION app_current_verified_domain()
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.verified_domain', true), '')
$$;

CREATE FUNCTION app_current_identity_user_id()
RETURNS UUID
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.identity_user_id', true), '')::UUID
$$;

CREATE FUNCTION app_current_requested_email()
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.requested_email', true), '')
$$;

CREATE FUNCTION app_current_magic_link_hash()
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.magic_link_hash', true), '')
$$;

CREATE FUNCTION app_current_session_hash()
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(current_setting('app.session_hash', true), '')
$$;

ALTER TABLE user_account ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_account FORCE ROW LEVEL SECURITY;
CREATE POLICY user_account_identity_isolation ON user_account
    USING (
        id = app_current_identity_user_id()
        OR EXISTS (
            SELECT 1
              FROM membership
             WHERE membership.user_account_id = user_account.id
               AND membership.organization_id = app_current_organization_id()
        )
    )
    WITH CHECK (id = app_current_identity_user_id());

ALTER TABLE user_email ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_email FORCE ROW LEVEL SECURITY;
CREATE POLICY user_email_identity_isolation ON user_email
    USING (
        normalized_email = app_current_verified_email()
        OR user_account_id = app_current_identity_user_id()
        OR EXISTS (
            SELECT 1
              FROM membership
             WHERE membership.user_account_id = user_email.user_account_id
               AND membership.organization_id = app_current_organization_id()
        )
    )
    WITH CHECK (
        normalized_email = app_current_verified_email()
        AND user_account_id = app_current_identity_user_id()
    );

ALTER TABLE magic_link_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE magic_link_request FORCE ROW LEVEL SECURITY;
CREATE POLICY magic_link_request_read ON magic_link_request
    FOR SELECT
    USING (
        normalized_email = app_current_requested_email()
        OR token_hash = app_current_magic_link_hash()
    );
CREATE POLICY magic_link_request_create ON magic_link_request
    FOR INSERT
    WITH CHECK (normalized_email = app_current_requested_email());
CREATE POLICY magic_link_request_invalidate ON magic_link_request
    FOR UPDATE
    USING (normalized_email = app_current_requested_email())
    WITH CHECK (normalized_email = app_current_requested_email());
CREATE POLICY magic_link_request_consume ON magic_link_request
    FOR UPDATE
    USING (token_hash = app_current_magic_link_hash())
    WITH CHECK (token_hash = app_current_magic_link_hash());

ALTER TABLE app_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_session FORCE ROW LEVEL SECURITY;
CREATE POLICY app_session_read ON app_session
    FOR SELECT
    USING (token_hash = app_current_session_hash());
CREATE POLICY app_session_create ON app_session
    FOR INSERT
    WITH CHECK (
        organization_id = app_current_organization_id()
        AND user_account_id = app_current_identity_user_id()
    );
CREATE POLICY app_session_revoke ON app_session
    FOR UPDATE
    USING (token_hash = app_current_session_hash())
    WITH CHECK (token_hash = app_current_session_hash());

ALTER TABLE organization ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization FORCE ROW LEVEL SECURITY;
CREATE POLICY organization_tenant_isolation ON organization
    USING (id = app_current_organization_id())
    WITH CHECK (id = app_current_organization_id());

ALTER TABLE organization_domain ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_domain FORCE ROW LEVEL SECURITY;
CREATE POLICY organization_domain_tenant_isolation ON organization_domain
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());
CREATE POLICY organization_domain_verified_bootstrap ON organization_domain
    FOR SELECT
    USING (
        normalized_domain = app_current_verified_domain()
        AND status IN ('CLAIMED', 'VERIFIED')
    );

ALTER TABLE membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE membership FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_tenant_isolation ON membership
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE invitation ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitation FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_tenant_isolation ON invitation
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());
CREATE POLICY invitation_verified_bootstrap ON invitation
    FOR SELECT
    USING (
        normalized_email = app_current_verified_email()
        AND status = 'PENDING'
        AND expires_at > now()
    );

ALTER TABLE admin_claim ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_claim FORCE ROW LEVEL SECURITY;
CREATE POLICY admin_claim_tenant_isolation ON admin_claim
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE parking_site ENABLE ROW LEVEL SECURITY;
ALTER TABLE parking_site FORCE ROW LEVEL SECURITY;
CREATE POLICY parking_site_tenant_isolation ON parking_site
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE parking_spot ENABLE ROW LEVEL SECURITY;
ALTER TABLE parking_spot FORCE ROW LEVEL SECURITY;
CREATE POLICY parking_spot_tenant_isolation ON parking_spot
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE spot_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE spot_assignment FORCE ROW LEVEL SECURITY;
CREATE POLICY spot_assignment_tenant_isolation ON spot_assignment
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE availability_offer ENABLE ROW LEVEL SECURITY;
ALTER TABLE availability_offer FORCE ROW LEVEL SECURITY;
CREATE POLICY availability_offer_tenant_isolation ON availability_offer
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE reservation ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservation FORCE ROW LEVEL SECURITY;
CREATE POLICY reservation_tenant_isolation ON reservation
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE idempotency_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_record FORCE ROW LEVEL SECURITY;
CREATE POLICY idempotency_record_tenant_isolation ON idempotency_record
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE outbox_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_event FORCE ROW LEVEL SECURITY;
CREATE POLICY outbox_event_tenant_isolation ON outbox_event
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());

ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_event FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_event_tenant_isolation ON audit_event
    USING (organization_id = app_current_organization_id())
    WITH CHECK (organization_id = app_current_organization_id());
