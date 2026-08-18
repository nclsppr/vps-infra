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
9. removes the scratch container and volume on success or failure.

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

## Encrypted off-site candidate

[ADR-0011](../decisions/0011-encrypted-offsite-postgresql-backup.md) selects a
provider-neutral candidate. It does not select a provider or authorize a live
installation. The local stage still does not survive loss, compromise, or
encryption of Atlas until every gate below passes and one upload exists.

The candidate performs these steps:

1. acquire `/run/lock/vps-postgres-offsite.lock`;
2. acquire the existing `/run/lock/vps-postgres-backup.lock`;
3. verify one complete local backup with the existing validator;
4. create one canonical tar stream and encrypt it directly with an age X25519
   public recipient;
5. release the local lock after the encrypted transaction is durable;
6. send one conditional S3 `PutObject` with the upload-only identity;
7. require the exact server-returned SHA-256 checksum and a non-null object
   version;
8. atomically commit a root-only local receipt;
9. remove only the completed encrypted transaction from the local spool.

It does not change or delete the local backup. It never sends a remote delete,
copy, list, or get request. It writes no plaintext copy. The internal local
manifest remains inside the encrypted object.

The systemd service can create only IPv4 and IPv6 sockets. It cannot create an
`AF_UNIX` socket, so it cannot open the Docker or systemd control sockets. Its
mount namespace also hides `/etc/vps/secrets`, `/run/docker.sock`,
`/var/run/docker.sock`, and `/run/systemd/private`. The systemd manager copies
the one upload credential into the dedicated service credential directory
before this sandbox is applied.

## Provider and recovery gates

Do not set any gate to `true` from a product page or an assumption. Retain dated
provider-control-plane or API evidence outside Atlas for all these facts:

- the bucket belongs to a failure domain that is independent from Atlas and
  the Atlas account;
- the selected endpoint supports conditional `PutObject`, SHA-256 request and
  response checksums, object metadata, version identifiers, and exact-version
  `GetObject` with the installed Ubuntu AWS CLI;
- bucket versioning is enabled;
- Object Lock was enabled when the bucket was created and has an adequate
  default retention rule;
- account recovery and billing cannot be disabled by the Atlas upload identity;
- the Atlas identity permits `PutObject` only in the exact backup prefix and
  denies read, list, overwrite, delete, retention, and policy changes;
- a separate restore identity can read exact versions but is absent from Atlas;
- the age private identity has at least one protected recovery copy outside
  Atlas;
- storage limits, upload failures, retention expiry, and cost have an external
  alert;
- the recovery point objective, recovery time objective, and daily, weekly,
  and monthly retention meet the product requirement.

Object Lock and versioning are bucket-side controls. The upload-only identity
cannot prove them. The five Boolean Ansible values are explicit operator gates,
not automatic evidence.

Provider snapshots can be another recovery layer. They do not replace this
encrypted and independently controlled object.

## Prepare identities outside Atlas

On a trusted recovery host, install `age` from the signed operating-system
package source. Create the age identity there. Do not run this command on
Atlas:

```bash
umask 077
age-keygen -o /absolute/protected/path/postgres-offsite.age
age-keygen -y /absolute/protected/path/postgres-offsite.age
```

The second command prints the public recipient. Keep the private file on the
recovery host and in an independent protected recovery copy. Supply only the
public `age1...` recipient to Ansible.

Create two provider identities:

- an Atlas upload identity with only `PutObject` for
  `<prefix>/*.tar.age`;
- an off-host restore identity with only the read operations needed to fetch a
  named version.

The upload identity file on Atlas is supplied outside Git at this exact path:

```text
/etc/vps/secrets/postgres-offsite/upload.credentials
```

Root owns the file with mode `0400` or `0600`. Its exact INI shape is:

```ini
[default]
aws_access_key_id = <upload-access-key>
aws_secret_access_key = <upload-secret-key>
```

The final line feed is mandatory. The controller rejects CRLF, comments, blank
lines, `[DEFAULT]`, inherited defaults, interpolation expressions, another
section, another key, or a different key order. This textual contract prevents
the AWS parser from obtaining an identity that the controller did not inspect.

Do not use temporary session credentials for the timer. Do not put a restore
credential or an age private identity in this directory. The systemd unit uses
`LoadCredential` and does not place the secret value in the unit or process
arguments.

## Configure and install the candidate

Add the public values to the private Ansible extra-variable file. This file is
not committed because it also identifies operational external state:

```yaml
vps_postgres_offsite_endpoint: https://<s3-endpoint>
vps_postgres_offsite_region: <s3-region>
vps_postgres_offsite_bucket: <bucket-name>
vps_postgres_offsite_prefix: atlas/postgresql
vps_postgres_offsite_addressing_style: path
vps_postgres_offsite_age_recipient: age1<public-recipient>
vps_postgres_offsite_bucket_object_lock_verified: true
vps_postgres_offsite_bucket_versioning_verified: true
vps_postgres_offsite_failure_domain_independence_reviewed: true
vps_postgres_offsite_recovery_key_off_host_verified: true
vps_postgres_offsite_restore_identity_separate_verified: true
```

Use `virtual` addressing only when the selected S3 service requires it. The
endpoint must be one HTTPS origin. The controller does not accept an HTTP
endpoint, embedded credential, URL path, query, or TLS bypass.

After review, install the exact Ubuntu runtime packages `age`, `awscli`,
`python3`, and `tar`, plus the controller and daily timer. The role verifies
their package state and executable paths. It also inspects the packaged AWS CLI
input models for conditional upload, SHA-256, metadata, checksum-mode, and
exact-version support before it installs the timer:

```bash
make install-postgres-offsite-backup \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

The timer runs after 05:17 UTC with a random delay of at most 30 minutes. It is
separate from the 03:17 UTC local backup timer and the monthly local restore
rehearsal. Create one immediate proof only after installation:

```bash
make upload-postgres-offsite-now \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

Inspect only sanitized state and receipts:

```bash
sudo systemctl status vps-postgres-offsite-backup.service
sudo systemctl list-timers vps-postgres-offsite-backup.timer
sudo find /srv/vps/backups/postgresql-offsite/receipts \
  -maxdepth 1 -type f -printf '%f\n'
```

A receipt proves that one S3 response returned the requested checksum and a
version identifier. It does not prove future retention or recoverability. Copy
each new receipt to an operator-controlled evidence store, review its backup
identifier, object key, version, checksums, recipient digest, and gates, then
make the retained copy read-only. Recovery refuses to use an unapproved latest
object or to infer a receipt from Atlas state. Receipt contract
`vps-postgres-offsite-receipt-v2` uses `recorded_at` for the time when the
controller wrote the receipt. It never claims to be the remote object upload
time.

Disable uploads without deleting local or remote data:

```bash
make stop-postgres-offsite-backup-schedule \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

## Off-host recovery proof

Run recovery as an unprivileged user on a trusted Linux host. Install `age`,
`awscli`, Docker, and this repository there. Copy the public `config.json` from
the retained operator configuration. Create an empty operator-owned directory
with mode `0700`. Keep the restore credential and age identity at mode `0400`
or `0600`.

First verify decryption and every local manifest checksum without starting a
container:

```bash
scripts/postgres-offsite-backup recover \
  --config /absolute/protected/path/config.json \
  --approved-receipt /absolute/protected/path/approved/<exact-backup-id>.json \
  --credentials-file /absolute/protected/path/restore.credentials \
  --identity-file /absolute/protected/path/postgres-offsite.age \
  --destination /absolute/protected/path/recovery \
  --backup-id <exact-backup-id> \
  --postgres-backup "$PWD/scripts/postgres-backup" \
  --docker /usr/bin/docker
```

Then make the exact PostgreSQL image from the recovered manifest available by
digest and run the isolated disposable restore rehearsal:

```bash
scripts/postgres-offsite-backup recover \
  --config /absolute/protected/path/config.json \
  --approved-receipt /absolute/protected/path/approved/<exact-backup-id>.json \
  --credentials-file /absolute/protected/path/restore.credentials \
  --identity-file /absolute/protected/path/postgres-offsite.age \
  --destination /absolute/protected/path/another-empty-recovery-root \
  --backup-id <exact-backup-id> \
  --postgres-backup "$PWD/scripts/postgres-backup" \
  --docker /usr/bin/docker \
  --rehearse
```

The recovery controller requests the exact version in the approved receipt. It
compares ciphertext size, ciphertext SHA-256, S3 ChecksumSHA256, S3 metadata,
source-manifest SHA-256, and age-recipient SHA-256 with that receipt. It then
decrypts into a private temporary directory, rejects links and path traversal,
extracts only the exact backup file names, and invokes the existing validator.
The rehearsal uses a network-disabled disposable PostgreSQL container and
volume. It never connects to the production cluster.

Retain the date, backup identifier, object version, result, and recovery-host
identity as sanitized evidence. Do not retain credentials, the age private
identity, plaintext SQL, or business data in a ticket or shared log.

## Distributed receipt failure

S3 can accept an object immediately before Atlas stops and before the local
receipt is durable. A retry uses `If-None-Match: *`, so it cannot overwrite that
key. The upload-only identity cannot read the object to decide whether the
first request succeeded.

In this case, do not widen the Atlas identity and do not delete the object. Copy
the pending `transaction.json` to the trusted recovery host. Keep that copy at
mode `0400`. Use the restore identity to reconcile the remote object:

```bash
scripts/postgres-offsite-backup reconcile \
  --config /absolute/protected/path/config.json \
  --credentials-file /absolute/protected/path/restore.credentials \
  --transaction /absolute/protected/path/pending-transaction.json \
  --work-root /absolute/protected/path/empty-reconcile-work \
  --output-receipt /absolute/protected/path/approved/<exact-backup-id>.json \
  --aws /usr/bin/aws
```

The command downloads the current object for the unique key and compares its
size, ciphertext digest, S3 checksum, and metadata with the pending
transaction. It records the returned exact version in a new read-only approved
receipt. The receipt uses the reconciliation time as `recorded_at`; it does not
invent an upload timestamp. The command does not decrypt the object and does
not modify S3.

After a separate review, securely install the exact approved receipt as
`/srv/vps/backups/postgresql-offsite/receipts/<exact-backup-id>.json` on Atlas.
Root must own it with mode `0600`. The next upload invocation validates that
receipt, removes the matching encrypted pending transaction, and does not send
a second `PutObject`.

A partial transaction that predates the atomic pending rename contains only
encrypted staging data. If its encrypted bundle and transaction manifest are
both complete and match, the controller promotes the directory to the pending
state. Otherwise, it removes only a strictly named, root-owned partial
directory with the exact bounded file allowlist while it owns the off-site
lock. An unknown entry remains a hard failure. Neither case is a reason to
disable Object Lock.
