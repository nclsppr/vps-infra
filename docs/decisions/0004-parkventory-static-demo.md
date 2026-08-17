# ADR-0004: Serve the Parkventory demo as an isolated static release

## Status

Accepted on 12 August 2026.

## Context

Parkventory has a public static demo. Its application backend is not ready for
production. The open gates include production identity, tenant isolation,
database roles, external email, metrics, backup, and restore proof.

The Parkventory repository can now build the demo for the root path and publish
a deterministic site artifact and route inventory. Atlas already has a bounded
static materializer and one isolated public Caddy edge. A separate Parkventory
runtime would duplicate the edge and could imply that the backend is available.

## Decision

Atlas serves the Parkventory demo as a third static release:

1. The source repository publishes `site` and `routes` OCI artifacts from an
   exact `main` revision.
2. The Atlas materializer binds the artifact digests to the Parkventory source,
   source ref, and `vps-release.yml` signer workflow.
3. The materializer validates every route through the reviewed static Caddy
   fragment before it changes `/srv/www/parkventory/current`.
4. The public static edge serves `parkventory.com` and redirects
   `www.parkventory.com` to the apex.
5. The release adds no application container, host port, secret, database, or
   API route.
6. The production release manifest keeps the Parkventory Compose application
   disabled. The static demo does not satisfy any backend readiness gate.
7. The static promotion contract marks Parkventory as an enabled
   `temporary-static-demo`. The static resolver and the root activation gate
   both reject a contradictory state where the Parkventory Compose application
   is enabled at the same time; the root gate also rejects a persisted active
   Compose manifest left from an incomplete handoff.

The first cutover uses IPv4 HTTP-01. The operator must publish exact Atlas A
records and remove old AAAA records before HTTPS activation. The edge receives
no OVH API credential.

## Consequences

### Positive

- Atlas can show the product without exposing an incomplete backend.
- Parkventory adds no runtime process or state.
- The release uses the same digest, provenance, local HTTPS probe, atomic
  switch, and rollback controls as the other static sites.
- GitHub Pages can continue to serve the `/parkventory/` build independently.

### Negative

- The operator must not describe the demo as the production application.
- The public edge activation now requires DNS readiness for three zones.
- The shared static controller and its tests must maintain one more bounded
  application profile.
- The future React frontend and Java backend cannot be activated by merely
  adding their image digests. Their reviewed Compose release must first disable
  the temporary static-demo mode and complete an exclusive handoff of the
  `parkventory.com` route.

## Replacement by the full application

Parkventory's production application is a different release class: a React
frontend image, a Java backend image, a dedicated migration job, and a platform
integration bundle. Before that application can be enabled, one reviewed
`vps-infra` revision must make the static promotion entry inactive and the
Compose release entry active without ever accepting both. Atlas must drain any
in-flight static activation, retire or archive the static state, and transfer
the public route while holding the future shared application-deployment lock.
The dynamic applicator must refuse to start while the static Parkventory state
or route still owns the domain.

## Rollback

Restore the previous Parkventory release symlink or remove only the Parkventory
route in a reviewed public edge revision. Do not change another static release,
the internal platform, a volume, or a database.

## Verification

- Validate the exact static profile and route candidate.
- Validate the public edge with exactly three route files.
- Probe the complete route inventory through temporary HTTPS Caddy.
- Probe the apex and redirect domains before and after DNS activation.
- Confirm that the route contains no `reverse_proxy` or API handler.
