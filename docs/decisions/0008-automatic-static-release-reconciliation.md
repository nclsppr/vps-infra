# ADR-0008: Reconcile approved static releases from GitHub Actions

## Status

Accepted on 17 August 2026. The implementation remains fail-closed until the
`static-production` environment has its dedicated Atlas identity and the
environment variable `VPS_STATIC_DEPLOY_ENABLED` is explicitly set to `true`.
This decision does not unlock the dynamic application controller.

## Context

Personal, Papers Empire, and the Parkventory demo publish separate site and
route OCI artifacts. Publication alone did not activate Atlas. The active
Personal and Parkventory releases consequently lagged their canonical
branches.

The producer workflows also do not share one ordering rule. In particular, a
Parkventory release can finish publishing before its independent `verify`
workflow finishes. A new registry tag is therefore evidence of available
bytes, not evidence that every check for the source revision is green.

GitHub cannot natively deliver a cross-repository `workflow_run` event to
`vps-infra`. Giving every application a production SSH key or a broad token
would multiply the production trust roots. A persistent Actions runner on
Atlas would let repository workflow code execute inside the production
boundary.

## Decision

`vps-infra` owns one scheduled and manually dispatchable reconciliation
workflow. Every ten minutes it performs these operations independently for the
three allowlisted applications:

1. resolve the exact HEAD of the canonical branch with Git smart HTTP;
2. read the check runs for that exact SHA;
3. require every observed check to be completed with `success`, `neutral`, or
   `skipped`, and require every application-specific expected check to conclude
   exactly `success`;
4. resolve only `site:sha-<HEAD>` and `routes:sha-<HEAD>`;
5. validate both OCI manifests and turn their bytes into immutable digest
   references;
6. send one exact `deploy-static-live` command through the central
   `static-production` SSH identity.

The workflow never selects an older green revision. An incomplete or red HEAD
leaves the current Atlas release unchanged. A missing site or route manifest is
treated as a publication race and retried by a later reconciliation. The
platform integration artifact and Caddy image are reviewed pins in
`releases/static-production.json`; they do not follow a registry `latest` tag.
Every static application also has an explicit Boolean promotion switch and a
reviewed mode. Personal and Papers Empire are `static-site`; Parkventory is a
`temporary-static-demo`. Disabled entries remain visible in reconciliation
evidence but never enter the deployment matrix.

Atlas accepts only allowlisted repositories and exact digest syntax through
the forced-command parser and a second root-owned, no-argument stdin gate.
`sudo-rs` authorizes only that fixed executable, without unsupported argument
regular expressions. The dynamic
`deploy` controller remains locked, `/etc/vps/production-enabled` remains
absent, and `apply-release` remains absent.

Both CI and Atlas read the application release manifest as a separate trust
input. Atlas also reads the digest-bound persisted active application state.
They reject any application that is simultaneously enabled or still active as
Compose while its static promotion is enabled. This is especially important
for Parkventory: its future React and Java Compose application must replace,
never overlap, the temporary static demo that owns `parkventory.com`.

Before activation, Atlas independently confirms that the source revision is
still the canonical branch HEAD. When an active managed revision exists, a
network-enabled `DynamicUser` worker with runtime, memory, file-size, tmpfs, and
network limits proves that the candidate descends from it. A normal revert is a
new descendant; a force-reset or replay is refused. Atlas then verifies the OCI contracts and GitHub
attestations, materializes the release, and exercises the complete route
inventory through temporary HTTPS Caddy. Immediately before the symlink switch
it confirms the branch HEAD a second time. After the public probe and before
committing active state, it confirms the HEAD once more; a superseded but valid
candidate is rolled back without quarantine.

Live activation writes a durable transaction, atomically switches `current`,
and probes the running public edge through `127.0.0.1:443` with normal
certificate-chain and hostname validation. The probe checks every route body,
the 404 body, cache and security headers, compression, and the redirects
actually served by the public edge. Only then is the complete source, site,
routes, integration, and Caddy tuple written as active state.

If the public probe fails, Atlas records a neutral rejection phase and durably
restores the prevalidated previous symlink before any potentially slow
classification request. It then rechecks the exact Caddy identity and source
HEAD. The exact tuple is quarantined only when those prerequisites are
unchanged; a prerequisite change made durable as `superseded` leaves the
candidate retryable. If activation is interrupted after rollback but before
classification completes, recovery quarantines the rejected tuple
conservatively so it cannot cause a repeated availability window. A quarantined tuple cannot
be retried until an operator removes its record after investigation. An interrupted transaction
is recovered before a new candidate is considered, by the root gate after a
failed attempt and by a boot oneshot before the public edge. Each activation
runs in a transient systemd unit whose stop hook invokes the same recovery, so
an SSH disconnect does not abandon an uncommitted switch. Recovery revalidates
the protected inventory and release filesystem before it serves or commits a
managed target, and removes only strictly named, labeled, and bounded probe or
staging residue. A second pair of site or
route digests for an already active source SHA is rejected as a reproducibility
violation.

## Considered alternatives

### Application workflow sends SSH directly

Rejected. It would place a production credential in three repositories and
would still need a common definition of “all green”.

### Atlas polls GitHub with a systemd timer

Rejected for the initial implementation. It avoids an inbound deployment
request, but loses the central Actions history, consumes the low anonymous API
budget from Atlas, and duplicates the existing Actions-to-bounded-SSH
operational model. There must never be both an Atlas timer and the GitHub
reconciler.

### Deploy when either mutable tag changes

Rejected. Site and routes are published separately, tags can be moved, and
publication can precede another required check. Only the coherent pair for the
current canonical SHA is eligible, and only immutable digests cross the SSH
boundary.

### Deploy the latest green historical SHA

Rejected. A red canonical HEAD must stop promotion instead of silently serving
an older newly selected candidate.

## Consequences

- Normal propagation is eventual: ten minutes plus any GitHub schedule delay
  and deployment duration.
- A transient GitHub API or GHCR failure cannot mutate Atlas; a later run
  retries discovery.
- All production SSH material remains in one environment of `vps-infra`.
- Scheduled no-op requests are safe because Atlas compares the complete active
  tuple and protected symlink before doing registry work. It reloads the
  protected inventory, verifies the release filesystem, Caddy image and health,
  then samples the public TLS contract without downloading GHCR again.
- Changes to required check names, repositories, integration, or Caddy require
  a reviewed infrastructure change.
- Replacing the Parkventory demo requires a reviewed cross-contract handoff:
  first disable its static promotion, drain static activation, then let the
  future dynamic applicator verify that no static route or active state still
  owns the domain before it activates React, Java, or migrations.
- Static automation does not authorize any dynamic application, database
  migration, secret change, DNS change, or generic platform activation.

## Activation and rollback

Activation requires a dedicated deploy key installed by Ansible, strict known
hosts, the `static-production` secrets `VPS_STATIC_HOST`,
`VPS_STATIC_SSH_PRIVATE_KEY`, and `VPS_STATIC_KNOWN_HOSTS`, the environment
variables `VPS_STATIC_DEPLOY_USER`, `VPS_STATIC_DEPLOY_PORT`, and
`VPS_STATIC_DEPLOY_ENABLED=true`, plus a main-only environment policy. A
required reviewer would make the scheduled path manual and is therefore not
part of this fully automatic environment.

To stop automatic requests, set that variable to `false`; do not enable an
Atlas polling timer. To roll back content, revert the producer change with a
new descendant commit. A quarantined tuple must be
investigated before its root-owned record is removed on Atlas.
