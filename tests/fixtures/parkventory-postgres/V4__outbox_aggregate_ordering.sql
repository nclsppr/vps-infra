ALTER TABLE outbox_dispatch
    ADD COLUMN aggregate_type TEXT,
    ADD COLUMN aggregate_id UUID;

-- The migration role owns the tenant tables but is deliberately neither a
-- superuser nor BYPASSRLS. Temporarily release FORCE for this transactional
-- backfill so that the owner can see pending events from every tenant. Flyway
-- rolls the whole migration back if FORCE cannot be restored.
ALTER TABLE outbox_event NO FORCE ROW LEVEL SECURITY;

UPDATE outbox_dispatch dispatch
   SET aggregate_type = event.aggregate_type,
       aggregate_id = event.aggregate_id
  FROM outbox_event event
 WHERE event.id = dispatch.event_id
   AND event.organization_id = dispatch.organization_id;

ALTER TABLE outbox_event FORCE ROW LEVEL SECURITY;

CREATE FUNCTION app_fill_outbox_dispatch_aggregate()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    SELECT event.aggregate_type, event.aggregate_id
      INTO NEW.aggregate_type, NEW.aggregate_id
      FROM public.outbox_event event
     WHERE event.organization_id = NEW.organization_id
       AND event.id = NEW.event_id;
    RETURN NEW;
END
$$;

CREATE TRIGGER outbox_dispatch_fill_aggregate
BEFORE INSERT ON outbox_dispatch
FOR EACH ROW
EXECUTE FUNCTION app_fill_outbox_dispatch_aggregate();

ALTER TABLE outbox_dispatch
    ALTER COLUMN aggregate_type SET NOT NULL;

CREATE INDEX idx_outbox_dispatch_aggregate_order
    ON outbox_dispatch (
        organization_id,
        aggregate_type,
        aggregate_id,
        created_at,
        event_id
    );

COMMENT ON COLUMN outbox_dispatch.aggregate_type IS
    'Technical aggregate discriminator used only to preserve notification order.';

COMMENT ON COLUMN outbox_dispatch.aggregate_id IS
    'Technical aggregate identifier used only to preserve notification order.';

COMMENT ON FUNCTION app_fill_outbox_dispatch_aggregate() IS
    'Copies canonical aggregate metadata from the tenant outbox event and keeps pre-V4 writers compatible.';
