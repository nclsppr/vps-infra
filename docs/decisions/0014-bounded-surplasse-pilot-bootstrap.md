# ADR-0014: Bound the first Surplasse tester pilot bootstrap

## Status

Accepted on 18 August 2026. This decision adds an operator boundary. It does
not install a pilot manifest, run a container, contact Stripe or PostgreSQL,
change DNS, start Surplasse, or prove an Atlas execution.

## Context

The admitted Surplasse producer release contains a one-shot
`pilot-bootstrap` service. It creates or verifies the first tester graph while
order intake remains paused. The input manifest contains personal data and
must stay outside Git, command arguments, environment values, and logs.

The admitted producer source is
`520a6d7f480f408746bedbcfa217983074540a48`. The resolver classified its
application release as `ready` with these immutable subjects:

- `ghcr.io/nclsppr/surplasse/application-release@sha256:c4abdb777cbbe5a0e2cc8d589503773341340f5c36bb3741e95af8d1e4ef8322`;
- `ghcr.io/nclsppr/surplasse/vps-integration@sha256:0de8c0711c4aca5804a805441a83a524ab1df37438db81650763cccaa7b7a392`.

This evidence proves publication and admission. It does not prove that the
release or pilot runs on Atlas.

## Decision

The application bundle policy admits the exact signed schema, contract,
transient service, and image relationship. `pilot-bootstrap` must use the
Backend digest, the `pilot-bootstrap` profile, the fixed entrypoint, only the
runtime PostgreSQL password and restricted Stripe test key, and only
`db_surplasse` plus egress through `app_surplasse`. It has no port, alias,
health check, or automatic restart. The signed source Compose must explicitly
set `create_host_path: false`. The normalized Linux Compose output may omit
that portable source-only option, but all remaining bind fields stay exact.

`materialize-surplasse-pilot-manifest` accepts only `--check` or one absolute
root-owned source passed after protected Ansible staging. It validates the
complete semantic policy, canonicalizes JSON, and atomically installs the
manifest as a regular single-linked `root:10001` file with mode `0440` and a
16 KiB limit. It uses the shared deployment lock, file descriptors with
`O_NOFOLLOW`, metadata and identity readback, `fsync`, and an atomic rename.
It recovers only its narrowly named safe staging residues. It never prints a
manifest value.

`surplasse-pilot-bootstrap` accepts exactly `status` or `apply`. It accepts no
source SHA, release digest, component reference, manifest path, or arbitrary
Compose argument. It derives the active release only from protected active
state, proves the `current` link and complete materialized policy, and uses a
deterministic one-off container name bound to the active application release.
Child stdout and stderr are bounded and discarded.

Before any container inspection, cleanup, or invocation, `status` replaces the
previous journal with a durable non-applicable checking phase. `checking-empty`
preserves only a new or previously confirmed empty lineage.
`checking-ambiguous` preserves every applying, applied, verified, or ambiguous
lineage. An interrupted status therefore closes the apply gate without losing
the evidence needed to classify a later empty result.

This recovery model depends on the admitted producer `status` operation being
strictly read-only. The immutable application release and consumer policy pin
that operation before Atlas can invoke it. A producer change that can write
during status requires a new reviewed contract.

An empty `status` from the empty lineage creates a 15-minute durable
confirmation bound to the full active-state digest, Backend reference,
application release reference, and installed manifest digest. `apply` writes
`applying` before container mutation and then `applied-unverified` after a zero
result and exact container cleanup. A separate `status` is required to write
`verified`. Any interrupted or uncertain apply remains non-replayable. A later
empty result from the ambiguous lineage becomes `ambiguous-empty`; it never
authorizes an implicit rebootstrap.

Ansible installs both helpers as `root:root 0500`. Manifest transfer and every
pilot command use `no_log: true` and `argv`. The local wrapper copies the
manifest bytes before any fetch through a bounded `O_NOFOLLOW` file-descriptor
copier. The copier proves the source before and after the copy and creates one
exclusive `0600` destination in the isolated home. The wrapper passes only that
protected path to Ansible and removes both local and remote temporary trees
through the existing cleanup boundaries.

## Consequences

- Parkventory, the static edge, DNS, and the platform activation policy are
  unchanged.
- A successful apply is not a verification result. The operator must run a
  separate status command.
- There is no delete, reset, purge, or rollback operation. Recovery keeps
  order intake paused and requires a separately reviewed action.
- The controller does not claim multi-system atomicity. The producer owns one
  serializable PostgreSQL transaction; Stripe verification occurs before its
  database writes. Atlas only controls one container invocation and its own
  durable journal.
- Stripe remains test-only under ADR-0011. A live bootstrap needs another
  reviewed policy and separate credentials.

## Alternatives

### Accept the application release digest at apply time

Rejected. An operator-supplied target can diverge from protected active state
between status and apply.

### Retry apply after an uncertain result

Rejected. The database transaction may have committed before the controller
lost the result. Only a separate exact readback can resolve that ambiguity.

### Put the manifest in Ansible variables

Rejected. Personal data could enter process arguments, callback output, fact
state, or logs.
