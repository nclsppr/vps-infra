# ADR-0011: Use age and an upload-only S3 identity for PostgreSQL backups

## Status

Accepted on 18 August 2026 for the candidate architecture. No provider, bucket,
credential, recovery key, schedule, or live installation is approved by this
decision.

## Context

Atlas keeps seven verified logical PostgreSQL backups on its local disk. These
backups protect against bounded operator errors. They do not survive loss,
compromise, or encryption of Atlas. The global dump also contains PostgreSQL
role password verifiers.

The off-site writer must not have a decryption key. It must not need read,
list, overwrite, or delete access to the object store. A recovery test must use
another identity from another host. The storage service must keep an accepted
object immutable after Atlas is compromised.

The local backup controller already owns
`/run/lock/vps-postgres-backup.lock`. It validates every local file and protects
the source from retention while an operation reads it.

## Considered options

### Restic repository

Rejected for this layer. Restic provides deduplication, retention, and mature
repository checks. Its repository password is a symmetric decryption secret.
Putting that password on Atlas would let a compromised host decrypt the remote
backup. A normal Restic writer also reads repository metadata and manages
repository state. These permissions exceed a `PutObject`-only identity.

### Provider-side encryption only

Rejected. A provider-managed encryption key does not keep plaintext separate
from the storage provider. It also couples recovery to one provider account.

### One age-encrypted object per local backup

Accepted. Atlas needs only an age X25519 public recipient. The private identity
stays off Atlas. Each local backup becomes one encrypted object with an
independent internal manifest. S3 `PutObject` is the only remote mutation.

## Decision

1. The existing local backup remains the canonical plaintext source. The
   off-site layer does not change its manifest, retention, timer, or restore
   rehearsal.
2. Atlas packages one already verified backup while holding the existing local
   backup lock. It streams the archive directly through `age`. It never writes
   another plaintext backup.
3. The age public recipient is a reviewed public input on Atlas. The matching
   private identity stays on an operator-controlled recovery host and in an
   independent protected recovery copy. It never enters Atlas, Git, an Ansible
   variable, or a command log.
4. Atlas loads one S3 credential through systemd. The provider policy permits
   only `PutObject` in the exact backup prefix. It denies list, get, overwrite,
   and delete operations. A separate restore identity stays off Atlas. The
   service cannot create Unix sockets, and its mount namespace hides the
   source secret tree plus the Docker and systemd control sockets. The copied
   systemd credential remains accessible.
5. The writer uses one unique key per local backup and `If-None-Match: *`. It
   requires the S3 response to return the exact SHA-256 object checksum and a
   non-null version identifier. It writes a local receipt only after these
   checks pass. The receipt records when it was written as `recorded_at`; it
   does not claim to know the remote upload time.
6. The object store must enable versioning and Object Lock before the upload
   identity exists. The bucket supplies a default retention rule. The Atlas
   writer never requests, changes, or removes retention.
7. Versioning, Object Lock, retention duration, account recovery, billing, and
   provider failure-domain independence are provider-side facts. The upload
   identity cannot prove them. Explicit gates stay false until the operator
   retains external evidence for each fact.
8. The controller acquires its off-site lock before the existing local backup
   lock. The local controller never acquires the off-site lock. This order
   cannot deadlock. A pending encrypted transaction is resumable. No remote
   deletion exists.
9. The operator copies each accepted receipt to an operator-controlled store,
   reviews it, and makes it read-only. Recovery requires this approved off-host
   receipt. It downloads the exact recorded object version, not the current
   version of the key. It compares object size, ciphertext SHA-256, S3
   ChecksumSHA256, metadata, source-manifest SHA-256, and age-recipient SHA-256
   with the receipt before it invokes the existing local validator or the
   disposable Docker restore rehearsal.
10. The host tools come from the signed Ubuntu package indexes. The candidate
    uses the Ubuntu `age`, `awscli`, `python3`, and `tar` packages. Endpoint,
    region, bucket, key prefix, and S3 addressing style are provider-neutral
    configuration values. Installation verifies that the packaged AWS CLI
    exposes conditional upload, SHA-256, metadata, checksum-mode, and
    exact-version inputs. Provider support for those semantics remains a
    pre-activation compatibility gate.

## Crash boundaries

The controller writes the encrypted bundle and transaction manifest in one
private staging directory. It syncs both files and atomically renames the
directory to a pending transaction. A retry resumes that transaction. It
atomically commits the local receipt after S3 accepts the complete object.

There is one unavoidable distributed failure window. Atlas can stop after S3
accepts the object and before the local receipt is durable. The upload-only
identity cannot read the object to resolve that ambiguity. A retry must fail
closed because the conditional put cannot overwrite the existing key. The
operator then uses the separate restore identity to verify and recover that
object. The off-host `reconcile` operation downloads the ambiguous object,
compares it with a protected copy of the pending transaction, and creates an
approved receipt whose `recorded_at` value is the reconciliation time, not an
invented upload time. The operator can install that exact receipt on Atlas to
complete the local transaction. Granting read access to Atlas to hide this
window is rejected.

## Consequences

### Positive

- Loss of Atlas does not expose the decryption key.
- Compromise of the upload identity does not authorize remote reads or
  deletion.
- One object is independently recoverable without a mutable repository index.
- Object Lock is compatible because the controller never rewrites or deletes a
  completed object.
- The local backup and restore rehearsal remain unchanged.

### Negative

- Full backups use more storage and network than a deduplicated Restic
  repository.
- Remote retention is a bucket policy, not a host garbage-collection command.
- Provider-side immutability and independence need evidence outside Atlas.
- The distributed receipt gap needs an operator recovery procedure.

## Verification

Before installation, complete the gates in the
[PostgreSQL backup runbook](../operations/postgresql-backup.md). Then prove one
upload with the restricted identity and one download, decryption, validation,
and disposable restore rehearsal from an independent host. Keep only sanitized
receipts in operational evidence.

## Rollback

Disable the off-site timer. Preserve local backups, pending encrypted
transactions, receipts, and every remote object. Revoke only the upload
identity. Do not disable Object Lock, delete a version, or copy the private age
identity to Atlas. A provider change requires a new bucket, identity, and
off-host recovery proof.
