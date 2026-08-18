# Parkventory PostgreSQL preparation

This runbook prepares only the Parkventory database boundary on Atlas. It does
not activate the Compose application. It does not change Caddy, DNS, or the
active static Parkventory release.

## Local validation

Run the repository contract before any host operation:

```bash
make check-parkventory-postgres
make check
```

The contract fixes PostgreSQL 17.10, database `parkventory`, owner
`parkventory_owner`, migrator `parkventory_migrator`, runtime
`parkventory_runtime`, and private network `db_parkventory`.

## Non-mutating host plan

Use the normal private inventory and public-key variable file:

```bash
make plan-parkventory-postgres \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

The plan validates the disabled Compose contract, active static owner, shared
PostgreSQL container, and private network. It does not create a password,
database, role, evidence file, or network attachment.

## Preparation

Review the plan. Then run:

```bash
make prepare-parkventory-postgres \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

The operation creates only missing database passwords. It never rotates an
existing password. It temporarily attaches PostgreSQL to `db_parkventory`,
applies an idempotent SQL contract, proves the effective state, writes
`/var/lib/vps-readiness/parkventory/postgres.json`, and restores the previous
network membership.

The evidence is canonical JSON with mode `0400` and owner `root:root`. It
contains the contract digest, immutable PostgreSQL image reference, role and
privilege proof, secret file metadata, and secret file digests. It contains no
password value. It is a point-in-time observation, not a durable assertion by
itself: its validity explicitly requires a fresh live check and PostgreSQL's
effective attachment to `db_parkventory`.

When preparation had to create a temporary attachment, restoring the prior
detached state immediately makes that observation non-ready. This is
intentional. `make verify-parkventory-postgres` then fails closed even if the
evidence file is still byte-for-byte present. The file records the successfully
provisioned database boundary; only the live verifier can establish current
connectivity readiness.

## Verification

After preparation, run the read-only verification path:

```bash
make verify-parkventory-postgres \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

The command fails if PostgreSQL is not currently attached to
`db_parkventory`, or if a role, grant, default privilege, secret file, image,
PostgreSQL version, or evidence byte differs.

## Persistent network transition at cutover

A later reviewed cutover must not use an ad-hoc `docker network connect` as the
lasting state. Its immutable integration contract must attach the shared
PostgreSQL service to `db_parkventory` with alias `postgresql`, and attach only
the Parkventory migrator and backend consumers that require the database. The
platform must be reconciled from that contract so a PostgreSQL container
recreation preserves the membership.

After that persistent reconciliation, run
`make verify-parkventory-postgres` while the application remains disabled. A
successful live check may then be reviewed and its exact evidence digest bound
in the application contract. Do not set that digest, enable Parkventory, or
transfer the public route until the independent encrypted off-site backup gate
and every other activation gate are also verified.

## Activation remains blocked

Do not populate the application readiness digest or set `enabled: true` from
this runbook. The protected contract has two independent empty gates:

- the reviewed digest of the PostgreSQL readiness evidence;
- verified encrypted off-site backup evidence.

The second gate has no verifier in this change. Local backups and local restore
rehearsals do not satisfy it. Parkventory also retains the other ADR-0010
activation blockers and the explicit static-to-Compose ownership handoff.
