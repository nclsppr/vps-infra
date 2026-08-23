-- Keep the member management view bounded and efficient without changing the
-- tenant isolation contract. The application serializes and caps future offers
-- per membership. Keep ends_at in the search key because expired PUBLISHED
-- offers remain historical rows and must not make the active count degrade.
CREATE INDEX idx_offer_owner_active
    ON availability_offer (organization_id, offered_by_membership_id, ends_at, starts_at)
    WHERE status = 'PUBLISHED';
