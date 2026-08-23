# ADR-0015: Prepare the inactive Parkventory public beta path

## Status

Accepted on 23 August 2026. This decision prepares an inactive candidate. It
does not authorize an Atlas convergence, a provider change, a route handoff, a
database migration, or public activation.

## Context

Atlas serves Parkventory as a static demonstration. The immutable application
producer exists, but its production OIDC, tenant isolation, PostgreSQL state,
backup, monitoring, and cutover evidence are incomplete. The static and Compose
contracts cannot own `parkventory.com` at the same time.

The earlier PostgreSQL candidate in PR 82 selected the shared PostgreSQL 17.10
cluster and three roles. The earlier backup candidate in PR 78 selected an
`age`-encrypted, upload-only, versioned S3 object boundary. Both branches
diverged from current `main`. This decision reuses the bounded PostgreSQL design
and the backup trust boundary. It does not copy the provider controller or
invent its external inputs.

## Decision

Keep `releases/static-production.json` enabled for Parkventory. Keep
`releases/application-production.json` disabled. The manual application
workflow has no schedule or release input. It can reach Atlas only when the
protected application contract admits Parkventory, the static contract no
longer admits it, the `application-production` environment explicitly sets
`VPS_APPLICATION_DEPLOY_ENABLED=true`, and the resolver returns one canonical
immutable candidate.

Use the existing PostgreSQL 17.10 cluster. Attach PostgreSQL to the managed
internal `db_parkventory` network with alias `postgresql`. Create only:

- `parkventory_owner`, with `NOLOGIN` and `NOBYPASSRLS`;
- `parkventory_migrator`, with `LOGIN`, `NOINHERIT`, `NOBYPASSRLS`, bounded
  connections, owner membership, and database-local `SET ROLE`;
- `parkventory_runtime`, with `LOGIN`, `NOINHERIT`, `NOBYPASSRLS`, no owner
  membership, no DDL, and bounded DML privileges.

Revoke database and schema privileges from `PUBLIC`. Keep the two generated
database passwords in separate `root:10001 0440` files. Write canonical
root-only readiness evidence only after a live proof of the image, version,
network, exact incoming and outgoing role memberships, database and schema
grantees, existing object ownership and ACLs, and default privileges. The
runtime object defaults are deny-only: no future table, sequence, routine, or
type gets access implicitly. Column ACLs are forbidden, and the owner-level
defaults remove PostgreSQL's implicit public function execution.

The pre-migration readiness check accepts an empty database before the first
migration. As soon as it finds any application relation, policy, public routine,
standalone type, or public extension, the same `--check` also requires the
complete application catalog and runtime ACL proof. `--require-rls` keeps this requirement explicit
for callers that expect a migrated schema. The controller runs
`--reconcile-application-schema` only after Flyway has committed every migration
in the admitted candidate. That post-migration boundary compares the complete
application-table inventory, both PostgreSQL RLS flags, the exact `pg_policy`
allowlist, and the seven `app_current_*` helper fingerprints before any runtime
container can start. It does not pin a Flyway version or migration count; an
index-only migration is accepted when this final catalog remains exact.

The reviewed current V1-V5 matrix contains eighteen application tables. V5 adds
only the bounded active-offer index, so the RLS catalog remains unchanged.
Seventeen tables must
have both `relrowsecurity` and `relforcerowsecurity` enabled. The sole declared
exception is the payload-free scheduler index `outbox_dispatch`, with both
flags disabled and no policy. The policy allowlist contains exactly twenty-four
tuples, including command, permissive/restrictive mode, roles, `USING`, and
`WITH CHECK`. V3 created every canonical policy as `PERMISSIVE TO PUBLIC`; this
is intentional application behavior, not a reason to accept another permissive
policy. Exact equality rejects an added `USING (true)` policy, a changed
expression, another role, or another policy name.

After the catalog proof, the controller reconciles database access in one
transaction. It revokes table, sequence, function, type, column, `PUBLIC`, and
runtime grants; reapplies deny-only owner defaults; grants CRUD on exactly the
eighteen declared application tables; and grants `EXECUTE` only on the seven
RLS context helpers. It grants nothing on sequences or types and no direct
execution of the V4 trigger helper. The final proof compares every effective
privilege, including grant options. Therefore `public.flyway_schema_history`
and any unknown future object remain inaccessible to the runtime without a
reviewed contract update. Reconciliation is idempotent and completes before any
runtime container can start. A PostgreSQL 17.10 integration test applies the
versioned V1-V5 fixture, then proves reconciliation and its no-op second pass.

V1 installs `btree_gist` 1.7 in `public`, owned by `parkventory_owner`.
PostgreSQL keeps the extension's 188 C routines and six base `gbtreekey*` types
owned by `platform_admin`. The proof follows `pg_depend` membership and pins
both member inventories by count and identity fingerprint. Non-extension
routines and standalone application types must still belong to
`parkventory_owner`. Each extension routine or type
may expose an ACL only to its own object owner. `PUBLIC` and
`parkventory_runtime` receive no extension function or type privilege. The
seven reviewed context helpers remain the only runtime function exception.
PostgreSQL array types remain outside the direct ACL inventory because their
usage follows the element type. The integration test proves that revoking the
six extension base types also removes runtime usage of their generated arrays,
while GiST exclusion constraints still accept normal runtime writes.

The application
contract keeps the evidence digest empty in this change. A later admitted
digest is not sufficient by itself: the application controller rehashes the
root-only file and runs the live PostgreSQL verifier before candidate fetch or
migration, then reruns it after migration and before starting the Backend.

Admit the OIDC candidate only with these public runtime keys:

- `PARKVENTORY_OIDC_AUTH_SERVER_URL`;
- `PARKVENTORY_OIDC_CLIENT_ID`;
- `PARKVENTORY_OIDC_ISSUER`.

The controller requires one HTTPS Auth0 EU origin and a matching issuer. Admit
only the three file-backed OIDC credentials
`parkventory-oidc-client-secret`, `parkventory-oidc-state-secret`, and
`parkventory-oidc-token-encryption-secret`. Only the Backend receives them.

The immutable bundle must contain the exact readiness probes, the
`/q/metrics` target, and the five-minute backend-unavailable alert. The central
candidate adds the missing `parkventory-backend` scrape job and an exact
Compose override that joins only Prometheus to `ops` and `app_parkventory`.
The config, override, rule, and target all remain disabled. The alert also uses
`absent()` so a missing target cannot suppress the signal after activation.
Each Parkventory service uses the Docker local log driver with three 10 MiB
files. Structured application logs and working alert delivery stay activation
gates.

Retain the current daily local logical backup, seven-backup retention, and
monthly isolated restore rehearsal. These controls are the minimum local
recovery layer. They do not survive Atlas loss. Keep the encrypted off-site
readiness digest unbound. Public activation remains invalid until a reviewed
change binds a sanitized proof to an approved PR-78-format encrypted receipt
and an independent off-Atlas restore rehearsal. The application controller
validates the exact proof structure, both source-manifest identities, provider
gates, digest, and freshness. It does not receive provider credentials. The
private age identity and restore credential must stay off Atlas.

## Activation sequence

A later reviewed change must complete all of these steps:

1. merge and re-publish the canonical Parkventory RLS and OIDC release;
2. install public configuration and all file-backed credentials outside Git;
3. prepare and verify PostgreSQL, then bind its exact evidence digest;
4. prove a fresh local backup, isolated restore, encrypted off-site receipt,
   and independent restore, install the sanitized root-only readiness file,
   then bind its exact digest;
5. stage the exact disabled Prometheus candidate, activate its scrape job,
   network override, target, rule, and tested alert delivery;
6. prove structured logs, retention, and operator access;
7. disable and drain static reconciliation under the shared lock;
8. prepare the attested route and Caddy application-network attachment;
9. change the protected application contract and environment gate in reviewed
   commits and configuration;
10. dispatch the workflow and verify migrations, internal probes, strict public
    probes, active state, logs, alerts, and backup state.

## Rollback

Before migration, leave the static owner active and remove only the inactive
candidate changes. The transaction controller normally restores the previous
runtime and quarantines a rejected tuple. It does not use that rollback after a
Parkventory migration starts without a valid post-migration PostgreSQL proof.
In that state, it stops and verifies the absence of every Parkventory runtime
container, removes the migrator, quarantines the candidate, and leaves the
previous runtime stopped. An operator must repair or prove the database before
another activation. Never reverse an applied migration automatically. Use
expand-and-contract migrations so the prior image remains compatible, or apply
a reviewed forward repair. Keep backup data, encrypted objects, receipts, and
the previous database volume until recovery is independently proved.

## Consequences

- A merge of this decision cannot deploy Parkventory by itself.
- Runtime compromise does not provide an owner role or an RLS bypass role.
- A failed or interrupted post-migration PostgreSQL proof cannot restart the
  previous Parkventory runtime against an untrusted database.
- The controller checks off-site backup freshness immediately before the first
  candidate start and immediately before its durable commit. Forward recovery
  and partial-commit finalization repeat the commit-time check.
- OIDC secrets remain files and do not enter Git, workflow output, or runtime
  environment files.
- Monitoring and logs are bounded before activation, but alert delivery and
  structured application logs still require external proof.
- Provider selection, DNS, Auth0, SMTP, credentials, and activation remain
  explicit external operations.
