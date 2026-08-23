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
runtime receives only the reviewed DML, sequence, routine, and type access.
Column ACLs are forbidden, and the owner-level global defaults explicitly
remove PostgreSQL's implicit public function execution before schema-local
runtime defaults are applied.
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
candidate changes. During application activation, the existing transaction
controller restores the previous runtime and quarantines a rejected tuple.
Never reverse an applied migration automatically. Use expand-and-contract
migrations so the prior image remains compatible, or apply a reviewed forward
repair. Keep backup data, encrypted objects, receipts, and the previous database
volume until recovery is independently proved.

## Consequences

- A merge of this decision cannot deploy Parkventory by itself.
- Runtime compromise does not provide an owner role or an RLS bypass role.
- OIDC secrets remain files and do not enter Git, workflow output, or runtime
  environment files.
- Monitoring and logs are bounded before activation, but alert delivery and
  structured application logs still require external proof.
- Provider selection, DNS, Auth0, SMTP, credentials, and activation remain
  explicit external operations.
