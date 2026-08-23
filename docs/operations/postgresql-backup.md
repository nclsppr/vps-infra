# PostgreSQL backup and restore rehearsal

## Current result

The repository provides one bounded local backup stage for the shared
PostgreSQL 17 cluster. It does not provide disaster recovery yet.

The daily job creates:

- one `pg_dumpall --globals-only` file for roles and global grants;
- one custom-format `pg_dump` archive for each non-template database;
- one canonical manifest with the exact PostgreSQL image, server identity,
  role inventory, database inventory, file sizes, and SHA-256 values;
- one SHA-256 value for the manifest itself.

The job writes a private staging directory, syncs every completed file, and
renames the directory only after all dumps pass. A failed job removes only its
own partial directory. Retention starts only after a new backup succeeds. It
keeps seven verified complete backup directories. It does not delete an
unknown, linked, or invalid entry.

The backup directory is `/srv/vps/backups/postgresql`. Root owns it with mode
`0700`. Each backup file has mode `0600`. The global dump contains PostgreSQL
role password verifiers. Treat the complete directory as sensitive data.

The manifest records these limitations as machine-readable false values:

- `offsite: false`;
- `encrypted: false`;
- `cross_database_snapshot_atomic: false`.

Each database dump is internally consistent. Dumps of different databases do
not share one transaction snapshot. The controller refuses custom PostgreSQL
tablespaces because they need a separate reviewed restore contract.

## Schedule

`vps-postgres-backup.timer` runs each day after 03:17 UTC with a random delay
of at most 30 minutes. It creates and verifies one backup.

`vps-postgres-restore-rehearsal.timer` runs on the first Sunday of each month
after 04:17 UTC with a random delay of at most 30 minutes. It restores the
latest backup into one disposable PostgreSQL container and one disposable
Docker volume. The rehearsal:

1. verifies every manifest and file checksum;
2. uses the exact digest-bound image recorded in the backup;
3. disables the scratch container network and publishes no port;
4. initializes `platform_admin` as the scratch bootstrap role, removes exactly
   its one redundant `CREATE ROLE` statement, and replays every other globals
   dump byte with error-stop behavior;
5. restores every database with error-stop behavior;
6. compares the restored role and database inventories with the manifest;
7. verifies the `postgres_exporter` membership grant, grantor, and options;
8. proves a connection to every restored database;
9. removes the scratch container and volume on success or failure;
10. writes a protected readiness file only after successful cleanup.

The rehearsal does not mount or stop the production volume. It does not prove
application-specific business invariants. Surplasse and Parkventory must add
their own checks before an actual recovery can reopen traffic.

## Installation and immediate proof

Run these commands from a trusted workstation. Each command uses the exact
`origin/main` snapshot selected by `scripts/converge`:

```bash
make install-postgres-backup \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml

make backup-postgres-now \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml

make rehearse-postgres-restore \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

The immediate backup and rehearsal modes require the verified internal
platform to be active. They do not start Caddy or an application.

The rehearsal writes
`/var/lib/vps-readiness/postgresql/local-restore.json` with mode `0400`. The
file binds the backup identifier and manifest digest, lists every restored
database, and records that the copy is local and unencrypted. The application
controller verifies the selected backup again before Parkventory migration and
runtime start.

Inspect the timer and service outcomes without reading backup content:

```bash
sudo systemctl list-timers 'vps-postgres-*'
sudo systemctl status vps-postgres-backup.service
sudo systemctl status vps-postgres-restore-rehearsal.service
sudo journalctl -u vps-postgres-backup.service --since today
sudo journalctl -u vps-postgres-restore-rehearsal.service --since '2 months ago'
```

Disable both schedules without deleting a backup:

```bash
make stop-postgres-backup-schedule \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

Do not delete the backup directory or a named PostgreSQL volume to stop a
timer.

## Production restore boundary

The repository intentionally does not provide a command that overwrites the
production PostgreSQL volume. A real recovery is a separate approved
operation. It must:

1. keep all application backends stopped;
2. provision a new empty PostgreSQL 17 volume with the recorded exact image;
3. verify the selected off-site backup before any SQL executes;
4. restore globals and databases into the new volume;
5. verify owners, extensions, Flyway history, and product invariants;
6. switch the platform to the recovered volume in one reviewed change;
7. keep the previous volume until the recovered applications pass.

Never rehearse by restoring into the live cluster. A rollback of an
application image is not a database restore.

## Missing disaster-recovery decision

No active architecture decision selects an off-site provider. The archived
`docs/archive/VPS-SETUP-v0.md` mentions an S3-compatible provider as an old
option. That archive is not an executable decision and supplies no credential.

The local stage does not survive loss, compromise, or encryption of Atlas. It
is useful only for local operator error and for continuous restore testing.
Parkventory's first public launch explicitly accepts this risk while it looks
for a first real user signal. This exception does not count as disaster
recovery proof. After that signal, and before intentionally retaining
irreplaceable business data, the operator must select and record:

- the recovery point objective and recovery time objective;
- an off-site object store and region with versioning or object lock;
- a retention policy for daily, weekly, and monthly copies;
- a client-side encryption format and a recovery key held outside Atlas;
- a write-only or narrowly scoped upload identity and a separate restore
  identity;
- storage monitoring, failed-upload alerting, and one off-host restore test.

Provider snapshots can be another recovery layer. They do not replace an
encrypted, independently controlled off-site backup.
