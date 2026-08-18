# ADR-0011: Prepare Parkventory on the shared PostgreSQL 17.10 cluster

## Status

Accepted on 18 August 2026. The repository implementation is preparatory. No
Parkventory database preparation or application activation is proved on Atlas
by this decision.

## Context

Atlas already operates one private PostgreSQL cluster. Parkventory needs a
dedicated database and different credentials for schema migration and normal
runtime access. A second PostgreSQL 18 cluster would add backup, monitoring,
upgrade, and recovery work without a pilot requirement.

The shared PostgreSQL port is not published on the host. The application
database network is an internal Docker bridge with the fixed subnet
`172.30.21.0/24`. The host root boundary controls both endpoints. Transport TLS
inside this single-host boundary is therefore not required for the pilot. This
exception does not apply to a published port, a second host, or an untrusted
container. Any such change requires TLS and a new review.

Local backup and restore rehearsal do not protect against host loss. A usable
Parkventory activation also requires an encrypted off-site backup proof. That
proof is outside this change.

## Decision

Use PostgreSQL 17.10 on the existing `vps-platform` cluster. Create these
identities only:

- `parkventory_owner`: `NOLOGIN` owner of the `parkventory` database and its
  `public` schema;
- `parkventory_migrator`: bounded login role with explicit membership in the
  owner role and database-local `SET ROLE`;
- `parkventory_runtime`: bounded login role with no owner membership, no DDL,
  no superuser capability, and no row-security bypass.

Revoke database and schema privileges from `PUBLIC`. Give the runtime role only
database connect, schema usage, and the reviewed default DML privileges for
future tables, sequences, functions, and types. Keep migrator and runtime
passwords in different files under `/etc/vps/secrets/parkventory`. The parent
directory is `root:root 0700`. Each password file is `root:10001 0440`.

The preparation command temporarily attaches the healthy shared PostgreSQL
container to `db_parkventory`. It restores the previous network membership in
an `always` block. It does not start Parkventory, change Caddy, change DNS, or
release the active static site.

The provisioner emits canonical, root-only readiness evidence only after it
proves PostgreSQL 17.10, role attributes, database and schema ownership,
database grants, schema grants, and exact default privileges. The protected
application contract keeps that evidence digest unset. It also keeps encrypted
off-site backup evidence explicitly absent. Setting `enabled: true` alone is
therefore invalid.

The evidence is only a point-in-time observation. Its contract records that a
fresh live check and the effective PostgreSQL attachment to `db_parkventory`
are required. Preparation restores a previously absent attachment, so the
resulting file no longer represents current readiness after that restoration;
the verifier fails closed in that state. A later cutover must add the network
to the immutable internal-platform integration contract, reconcile it
persistently, and pass the live verifier before recording the evidence digest.

## Consequences

- Parkventory uses the same monitored and backed-up PostgreSQL service as the
  other Atlas workloads.
- A runtime compromise does not give schema-owner or migration rights.
- Password values do not enter Git, Ansible output, or readiness evidence.
- `make plan-parkventory-postgres` is non-mutating.
- `make verify-parkventory-postgres` requires existing exact evidence and a
  current PostgreSQL attachment to `db_parkventory`.
- `make prepare-parkventory-postgres` is the only mutation path in this slice.
- Dynamic activation remains blocked by static ownership, encrypted off-site
  backup, identity, RLS, route, migration, recovery, and public-probe gates.
