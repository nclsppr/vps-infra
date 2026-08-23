# ADR-0017: Prepare a transactional Mon Florian public edge

## Status

Accepted on 23 August 2026. This decision adds inactive controller paths and
tests. It does not enable Mon Florian, create a private value, change DNS,
switch Caddy, start the backend, or deploy a release.

## Context

ADR-0016 admitted the dormant application profile but left its Caddy route
outside the running edge. Adding that route directly to the static edge would
break two existing guarantees: Surplasse already owns a transactional edge
extension, and its recovery state must remain readable after a controller
upgrade.

The Mon Florian backend can call paid APIs. A status-only check is not enough:
an unauthenticated POST must stop at Caddy before the backend starts. The
private-access snippet also needs rotation and rollback. Reading one mutable
host path from every candidate would make an old candidate depend on the new
password hash.

The application production policy remains `enabled: false`. The normal
application materializer therefore cannot create the route needed to stage the
edge. Temporarily enabling the application just to obtain that file would
weaken the activation boundary.

## Decision

Keep schema 1 of the Surplasse edge controller byte compatible. Add schema 2
only for a composite candidate. It retains the active static base and the
complete Surplasse overlay, then adds:

- the attested Mon Florian route;
- the external `app_monflorian` network at Caddy address `172.30.40.254`;
- one read-only bind for the private-access snippet;
- the route revision, overlay digest, and private-input digest in the candidate
  fingerprint.

The private operator input stays at
`/etc/vps/secrets/monflorian/monflorian-private-access.caddy`, owned by
`root:root` with mode `0400`. Staging validates one canonical `basic_auth`
account with a bcrypt cost of 14, then copies those bytes into the candidate's
root-only `private` directory. The rendered Compose file binds that versioned
copy. A changed input creates a different fingerprint; rollback continues to
use the previous candidate's copy. State and logs contain a digest, never the
bcrypt value.

Add `deploy-application --materialize-edge-route`, restricted to Mon Florian.
It verifies the immutable application and integration attestations, then writes
only `monflorian.caddy` and its canonical application state under
`/srv/applications/monflorian/edge-releases`. It does not render the application
runtime, inspect an OpenAI key, pull a runtime image, create `current`, or start
a service. Normal and live materialization still reject a disabled policy.

Add the `preserve` public-edge Ansible state. It installs the schema 1/2
controller and recovery unit without entering the stop or base-switch blocks.
The first schema 2 state must never be written before that recovery controller
is installed.

Activation keeps the existing crash journal. A schema 2 candidate may roll
back to schema 1 in every transaction phase. Removing an active composition is
accepted only through `--remove-monflorian` and a schema 2 transaction that
records that authorization. Normal staging cannot silently drop Mon Florian.
The recovery tests also retain the schema 2 to schema 2 transition needed for a
future credential rotation. That tested capability is not permission to rotate
the production credential in this first tranche.

Before the application backend starts, the controller sends unauthenticated
POST requests to `/api/itineraries` and `/api/illustrations` through the local
TLS edge. Each request must return `401` with one Basic authentication
challenge. The release identity route remains public so Atlas can prove the
source revision without credentials.

## Required follow-up

This tranche does not delete old composite candidates. Each one retains a
root-only copy of the private-access snippet. Credential rotation is therefore
not an authorized operation until the controller has a bounded purge command.
No credential change is planned before that command exists.

The purge must run under the shared deployment lock and preserve, at minimum,
the runtime target, the active state, both releases named by an open
transaction, and the operator's recorded rollback candidate. It must accept
only protected candidate directories below the fixed extension root and must
not read, print, or hash the private snippet during deletion. Recovery tests
must cover an interruption at each deletion boundary before this restriction
is lifted.

## Consequences

- Surplasse and every static route keep their existing state and byte checks.
- A dormant, attested Mon Florian route can be staged without enabling the
  application.
- A future password rotation and its rollback will not share mutable secret
  bytes, once the bounded purge follow-up authorizes that operation.
- Caddy validation proves the authentication handler precedes the sole Mon
  Florian reverse proxy.
- DNS and application activation remain separate, explicitly authorized steps.

The [operation checklist](../operations/monflorian-public-edge.md) records the
commands and stop conditions. Repository convergence alone is not activation
evidence.
