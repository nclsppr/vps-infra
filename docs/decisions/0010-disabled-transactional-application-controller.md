# ADR-0010: Provide a disabled transactional Compose application controller

## Status

Accepted on 17 August 2026. Atlas proved installation on 18 August 2026 by
converging `vps-infra` revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b`. The root-owned
`deploy-application` controller and argument-free gate are installed, and
`vps-application-recover.service` is loaded and inactive after a successful run
(`Result=success`, `ExecMainStatus=0`). Both Surplasse and Parkventory remain
`enabled: false` in the protected production contract, and no application
deployment workflow invokes the gate. Installation did not activate an
application, create a database, provision a secret, change an edge route, or run
a migration on Atlas.

ADR-0013 later enables only the canonical Surplasse admission entry. It keeps
the controller, migration, recovery, secret, edge, SMTP, Stripe test, and public
probe boundaries from this decision. Parkventory and the legacy Surplasse
adapter remain disabled.

## Context

ADR-0009 defines one immutable `application-release` signal for every complete
Surplasse or Parkventory release. Its resolver admits a digest after it proves
the exact source HEAD and GitHub check state. Admission alone is not safe
activation: the descriptor points to component image indexes and an integration
archive whose signatures, media types, paths, modes, checksums, Compose policy,
migration inventory, and probes still need verification on Atlas.

The static controller already provides the relevant host-side security
architecture: one root orchestrator, isolated `DynamicUser` workers, immutable
release directories, durable state, a shared deployment lock, bounded forced
SSH input, and systemd recovery. The Compose applications need the same
properties plus dedicated database migration and runtime probe phases.

Parkventory also has a deployed static variant. A Compose candidate must never
take the same public identity while static state still owns it.

## Decision

Provide `deploy-application` and `deploy-application-live-gate` through the
deploy Ansible role beside the static controller. The proved convergence has
installed them. They reuse the static controller's registry download, GitHub
trusted-root, attestation, isolated-worker, protected-state, and residue checks.
Both controllers acquire the exact lock `/run/lock/vps-static.lock`.

The root application controller re-reads the protected application and static
production contracts before it validates the runtime or performs network I/O.
An application with `enabled: false` stops at that boundary. Both entries are
disabled in this decision.

For an enabled future entry, the controller performs the following admission
and materialization sequence:

1. require the supplied source revision to be the exact canonical `main` HEAD;
2. fetch the supplied `application-release@sha256` manifest and descriptor,
   require every runtime reference to be digest-only, and bind every component
   and integration source revision to the same exact release source SHA;
3. verify the release attestation from the allowlisted producer workflow;
4. fetch every component index, require exactly one `linux/amd64` runtime
   manifest plus its attestation manifest, verify the component attestation,
   bind the runtime config labels to the same source revision, and bound both
   the count and compressed sum of image layers;
5. fetch and attest the digest-only `vps-integration` manifest;
6. require the common artifact type and two ordered archive/inventory layers;
7. validate the canonical inventory, exact application file allowlist, safe
   tar members, ownership, modes, timestamps, sizes, and content hashes;
8. bind the exact canonical migration and probe inventory bytes to the hashes
   in `application-release` and keep runtime auto-migration disabled;
9. render Compose with only the exact root-owned runtime configuration keys and
   image digest variables, apply the repository Compose policy, and inspect
   the exact least-privilege secret allocation without reading or persisting
   secret bytes; the non-secret configuration values are snapshotted into the
   immutable release so recovery does not depend on later host-config drift;
10. materialize the verified bytes and evidence in a digest-named root-owned
    release, then write and fsync a complete filesystem inventory.

The resolver from ADR-0009 therefore remains an admission resolver. The
root-owned applicator independently verifies all release, component, and
integration attestations and all referenced content before any activation.
Neither layer treats the mutable discovery tag as an activation reference.

Live activation has an additional fail-closed preflight before database
migration. The public edge route currently installed for the application must
equal the attested route byte for byte, and the exact healthy public-edge Caddy
container must already be attached to the application's external network.
This controller does not rewrite the immutable platform edge release. Route and
network preparation remains a separate reviewed platform cutover.
Post-start public probes resolve every declared public hostname directly to
Atlas loopback, so a healthy old DNS target cannot prove the local candidate.

Parkventory also refuses activation while any protected static active state,
static transaction journal, or static `current` link exists. The static
controller symmetrically refuses a Compose active state, transaction journal,
or `current` link while it holds the same deployment lock. Recovery never
forward-commits a probed Compose candidate over a static owner.

After materialization and preflight, activation uses a durable per-application
transaction with these phases:

- `prepared`: candidate and exact previous state are journaled;
- `migration-running`: the dedicated one-shot `migrator` is running;
- `migrated`: the migration command completed;
- `started`: the exact runtime services are up and healthy in Compose;
- `probe-rejected`: activation failed after a potentially mutating phase;
- `probed`: all internal and public HTTP probes passed and the source is still
  the exact canonical HEAD.

Only a `probed` candidate can become the atomic `current` link and active state.
A candidate source must descend from the active source before activation.
The delivered recovery code can restore the previous Compose runtime and
quarantine the complete immutable candidate fingerprint after migration starts.
This path is not authorized for production while migration compatibility is
only an assertion. Before either entry can be enabled, an attested invariant
must prove that the previous runtime is compatible with the changed schema, or
the controller must stop after migration for explicit forward recovery. A
prepared-only failure is retryable and is not quarantined.

Candidate services are created with a generated, immutable `restart: no`
override. Their normal `unless-stopped` policy is applied only after the
`probed` journal and active tuple are durable. Boot recovery re-runs Compose and
waits for health before completing an interrupted forward commit. Docker
therefore cannot auto-restart an uncommitted candidate ahead of recovery.
Controller subprocess output is drained with streaming byte limits and the
activation and recovery units carry explicit memory, task, and file-size
bounds.

The one-shot migrator has a deterministic name derived from the complete
release digest. Before rollback, recovery locates only that exact name,
requires its Compose project, service, one-off label, image digest, and full
container identifier to match the journaled candidate, force-removes it, and
observes stable absence. Recovery refuses an identity mismatch instead of
stopping an unrelated container. This closes the Docker-daemon boundary where
a container could otherwise outlive a killed transient systemd client and keep
mutating the database while the previous runtime is restored.

The transaction is operationally reversible, but SQL migrations are not
magically reversible. Every migration deployed through this controller must be
backward compatible with the currently active runtime and with the previous
runtime used for recovery. A migration that requires destructive rollback is a
separate planned database operation and must not use this automatic path.

File-backed secrets live under root-owned `0700` application directories. Each
individual file is `root:10001`, single-link, and `0440`: the private parent
prevents host users from traversing to it, while the individual bind mount is
readable by the fixed `10001:10001` process inside its container. Compose
file-backed secrets do not remap host ownership or mode. The controller also requires the
exact per-service allocation, so a frontend cannot receive a database, SMTP,
JWT, or Stripe credential and the runtime backend cannot receive the dedicated
migrator credential.

The forced SSH record is exactly:

```text
deploy-application-live <surplasse|parkventory> <source-sha40> \
  <application-release@sha256>
```

The unprivileged parser validates it, then passes one newline-terminated record
over stdin to a root-owned gate with no arguments. The gate independently
revalidates the record and creates a bounded transient systemd unit. Its
`ExecStopPost` invokes recovery. Ansible installs
`vps-application-recover.service` after static recovery and before the
public-edge systemd unit. Its proved idle state on 2026-08-18 was loaded,
inactive, `Result=success`, and `ExecMainStatus=0`. Docker can nevertheless
restart the existing
`unless-stopped` Caddy container as soon as the daemon starts. This tranche
therefore does not claim that boot recovery withholds public traffic; closing
that daemon-level bypass remains an activation blocker.

## Consequences

- A disabled application performs no source, registry, attestation, Docker, or
  probe network operation.
- Release state contains image and artifact references, inventories, and public
  evidence, but no secret value.
- Static and Compose changes cannot run concurrently, and Parkventory ownership
  cannot overlap.
- A crash after a completed probe can finish the atomic commit; an earlier crash
  restores the previous runtime and conservatively quarantines a mutating
  candidate.
- Proved host installation and healthy idle recovery do not make either
  application deployable while its reviewed contract entry remains disabled;
  there is no application deployment workflow or active runtime.
- The active state transition is atomic, but the current fixed Compose project
  and network aliases make runtime replacement rolling rather than blue/green:
  Caddy can observe candidate containers before the final public probes. The
  controller provides bounded rollback, not zero-exposure traffic isolation.

## Activation blockers retained

Before either application can be enabled, a separate reviewed change must
prepare the public edge route and network membership, provision the external
application and database networks, create database roles and restore evidence,
install all exact root-owned runtime configuration and secret files, activate
the required observability configuration, and prove the real public probes.
Parkventory additionally requires an explicit static-to-Compose ownership
handoff. Before activation, each application also needs either digest-scoped
blue/green aliases plus an atomic edge switch, or an explicitly reviewed
maintenance/cutover policy that accepts the rolling exposure described above.
The public edge must also be made recovery-gated at the Docker/firewall
boundary, rather than relying only on systemd unit ordering. Recovery timeouts
and output/memory bounds must be dimensioned against the complete worst-case
Compose path. Migration recovery must gain an attested backward-compatibility
invariant or stop for operator and forward recovery after migration instead of
automatically starting the previous runtime against a changed schema. This is a
fail-closed activation condition, not a task that can be deferred until after
enablement.
The enablement change must additionally add a fail-before-mutation disk budget
and safe release/image retention, aggregate CPU/memory/pid budgets for overlap,
latest-desired trigger reconciliation after lock contention, boot health
reconciliation for an active runtime, exact database/edge/container network
identity checks, and a route policy that cannot strand rollback behind a newer
candidate route.
Surplasse and Parkventory now publish the common USTAR integration format and
immutable `application-release` descriptors. Publication satisfies the producer
format prerequisite only. It does not close any host, database, secret,
network, route, migration-compatibility, resource-budget, recovery, or public
probe blocker above.

## Alternatives

### Let each producer SSH into Docker directly

Rejected. It gives producer workflows a broad host capability and bypasses the
shared lock, protected state, content verification, and recovery journal.

### Run migrations automatically inside the backend

Rejected. It mixes runtime availability with a privileged schema transition
and makes rollback classification ambiguous. The contract requires a dedicated
transient migrator and `runtime_auto_migrate: false`.

### Let the application controller rewrite the platform edge

Rejected for this tranche. The edge is an independently reviewed immutable
platform release. Its network and route cutover must remain explicit and must
be in place before an application migration can start.
