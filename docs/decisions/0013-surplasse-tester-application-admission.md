# ADR-0013: Admit Surplasse tester releases through the canonical controller

## Status

Accepted on 18 August 2026. This decision changes repository admission only.
It does not install a secret, contact Stripe or an SMTP provider, request a TLS
certificate, change DNS, run a migration, start Surplasse, or prove a live Atlas
deployment.

This decision partially supersedes only the disabled canonical Surplasse
admission statements in ADR-0009, ADR-0010, ADR-0011, and ADR-0012. It does not
supersede their runtime security boundaries or the locked DNS cutover policy.

## Context

The product owner authorizes production domains and Stripe test orders for the
owner and invited testers before the public launch. The immutable Surplasse
producer now publishes a tester payment contract. The Atlas controller binds
that contract to `STRIPE_LIVE_MODE=false`, a restricted `rk_test_` key, and two
distinct test webhook signing secrets.

The canonical controller still refused Surplasse before it could resolve an
immutable release because `releases/application-production.json` had
`enabled: false`. The resolver also rejected the one `307` response that GHCR
uses to serve a public descriptor blob from GitHub's package content host.

The shared PostgreSQL service joined only `db_monitoring`. A Surplasse migrator
or Backend on `db_surplasse` therefore could not resolve the durable
`postgresql` endpoint after the legacy preparation attachment was removed.

## Decision

Set only the canonical Surplasse entry in
`releases/application-production.json` to `enabled: true`. Keep Parkventory
disabled. Keep the following legacy and external control planes locked:

- `releases/production.yaml` and its root `activation_policy`;
- `applications/surplasse/adapter.json`;
- `policies/surplasse-dns-cutover-v1.json`;
- the inactive Prometheus integration candidate.

The enabled entry authorizes release resolution and the existing explicit
`deploy-application-live` path. It is not an automatic deployment request. No
workflow invokes the application gate from this change.

Allow one endpoint-specific GHCR blob redirect. The first request goes only to
the exact GHCR repository and carries the registry bearer. The resolver accepts
only status `307` and a visible ASCII URL of at most 8192 bytes with all of these
properties:

- scheme `https`;
- host exactly `pkg-containers.githubusercontent.com`, with no port or user
  information;
- no fragment and a non-empty signed query;
- path `/ghcrblobs<digits>/blobs/<expected-sha256-digest>`.

The second request sends only the media-type accept header. It never sends the
GHCR bearer. The global redirect handler rejects a second hop. The resolver
still verifies the exact descriptor size and SHA-256 digest before it parses
the descriptor. An error never includes the signed redirect URL.

Attach the shared PostgreSQL service persistently to both `db_monitoring` and
`db_surplasse`. Keep the `postgresql` alias on both networks. The internal
platform controller requires `db_surplasse` to be the managed internal bridge
on `172.30.11.0/24` and verifies the live network membership and alias. No
service publishes PostgreSQL on a host port.

Remove the duplicate PostgreSQL section from the inactive Surplasse platform
override. The base internal platform now owns that durable membership. The
override continues to describe only the deferred Prometheus attachment.

Do not remove or bypass any existing application activation check. Activation
still requires the exact source HEAD, immutable release and integration
artifacts, attestations, protected operator inputs, the tester Stripe binding,
SMTP configuration, the pre-staged TLS edge, the database migration, internal
health checks, strict public HTTPS probes, and durable transaction recovery.

## Consequences

- The admission resolver can classify the canonical Surplasse release as
  `ready` when its producer checks and artifacts are complete.
- A deliberate operator command is still necessary to materialize and activate
  that release on Atlas.
- PostgreSQL keeps a durable private path for the Surplasse migrator and
  Backend across internal platform reconciliation.
- The legacy adapter remains available for bounded database preparation and
  negative tests, but it cannot activate Surplasse.
- DNS cutover, TLS credentials, SMTP evidence, Stripe account capability, and
  real runtime probes remain separate operational evidence.
- A local backup and successful restore rehearsal are strongly recommended but
  do not block the first tester activation. This exception ends before the real
  public launch and before any later schema-changing migration.
- This change does not add or change an off-VPS backup. The tester MVP accepts
  that remaining recovery risk until the public-launch gate.

After tester activation, every production status must state that production
and test orders are open, but the following work is mandatory before the public
launch: complete the Stripe live contract and connected-account proof, prove
transactional email and domain authentication, add an off-VPS backup and repeat
a restore rehearsal from it, close external monitoring and operational safety
gaps, and complete product and legal launch checks.

## Alternatives

### Unlock the legacy adapter

Rejected. It would create a second activation path that does not use the
immutable application release and shared transactional controller.

### Forward all registry redirects

Rejected. A generic redirect can disclose an authorization header or send the
resolver to an unreviewed endpoint.

### Attach PostgreSQL during each application deployment

Rejected. The shared platform owns PostgreSQL network membership. An imperative
attachment can disappear when Compose reconciles the platform.
