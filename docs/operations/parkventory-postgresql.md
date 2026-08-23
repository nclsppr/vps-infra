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

The contract fixes PostgreSQL 17.10 at the exact digest-bound platform image,
database `parkventory`, owner
`parkventory_owner`, migrator `parkventory_migrator`, runtime
`parkventory_runtime`, and private network `db_parkventory`.

## Non-mutating host plan

Use the normal private inventory and public-key variable file:

```bash
make plan-parkventory-postgres \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

The plan runs Ansible check mode against the disabled Compose contract, active
static owner, internal-platform service boundary, and private network. It shows
the controller and contract files that a later preparation would install. It
does not create a password, database, role, evidence file, network attachment,
or persistent host file.

## Preparation

Review the plan. Then run:

```bash
make prepare-parkventory-postgres \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

The operation creates only missing database passwords and the two local OIDC
state and token-encryption secrets. It never rotates an existing value. It
publishes `/etc/vps/secrets/parkventory/parkventory-secret-generation.json`
last. This root-only marker binds target generation 1 to the exact four-file
generated set. It contains file identifiers and no value or content-derived
digest.

The current platform contract attaches PostgreSQL durably to
`db_parkventory`. Against an older live platform revision only, this command
uses a temporary attachment and restores the previous membership. It applies
an idempotent SQL contract, proves the effective state, writes
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
PostgreSQL version, or evidence byte differs. It also rejects any unexpected
incoming or outgoing membership involving the owner, migrator, or runtime;
unexpected database, schema, table, sequence, routine, or type grantees; an
object not owned by `parkventory_owner`; or runtime privileges outside the
exact allowlist. It forbids per-column ACLs and proves that global owner default
privileges do not restore PostgreSQL's implicit `PUBLIC EXECUTE` on new
functions. The activation controller repeats this proof after migrations and
before starting the Backend. If that post-migration proof fails or cannot
complete, the controller stops and verifies the absence of every Parkventory
runtime container. It removes the migrator, quarantines the candidate, and does
not restart the previous runtime against the untrusted database.

## Persistent network transition

This repository now adds `db_parkventory` with alias `postgresql` to the
immutable internal platform contract. This is repository intent only. A later
reviewed convergence must prove that Atlas uses the exact contract and that a
PostgreSQL container recreation preserves the membership. Do not use an ad-hoc
`docker network connect` as lasting state. The Parkventory release attaches
only its migrator and Backend to the database network.

After that persistent reconciliation, run
`make verify-parkventory-postgres` while the application remains disabled. A
successful live check may then be reviewed and its exact evidence digest bound
in the application contract. Do not set that digest, enable Parkventory, or
transfer the public route until the independent encrypted off-site backup gate
and every other activation gate are also verified. On a later deployment, the
application controller rehashes the bound file and reruns this live verifier
under the shared deployment lock before it can fetch or migrate the candidate.

## OIDC and application secrets

The root-only runtime configuration file must contain the exact key allowlist.
For OIDC it contains only the Auth0 EU URL, client identifier, and issuer. The
issuer must equal the HTTPS Auth0 EU origin with one trailing slash. Install
the following credentials outside Git as `root:10001 0440` files:

```text
/etc/vps/secrets/parkventory/parkventory-oidc-client-secret
/etc/vps/secrets/parkventory/parkventory-oidc-state-secret
/etc/vps/secrets/parkventory/parkventory-oidc-token-encryption-secret
```

`prepare-parkventory-postgres` generates the state and token-encryption files
on Atlas. A separate helper installs the Auth0 client secret and public runtime
configuration from one private source directory. The directory must be
`root:root 0700` and contain exactly:

```text
parkventory-oidc-client-secret
parkventory.env
```

Validate the source before installation:

```bash
sudo /usr/local/libexec/vps/materialize-parkventory-provider-secrets \
  --validate-source /absolute/root-only/source
sudo /usr/local/libexec/vps/materialize-parkventory-provider-secrets \
  --install-from /absolute/root-only/source
sudo /usr/local/libexec/vps/materialize-parkventory-provider-secrets --check
```

The install takes the shared deployment lock. It installs the Auth0 client
secret as `root:10001 0440` and `parkventory.env` as `root:root 0600`. It
publishes the root-only generation marker last. The marker names the exact
registered Auth0 input and target generation 1. It contains no value, digest,
or source path. A committed marker blocks rotation until a reviewed registry
change authorizes a new target generation.

The runtime configuration fixes both database users, the private JDBC URL,
Scaleway TEM at port 587, sender `no-reply@parkventory.com`, and public URL
`https://parkventory.com`. It requires one Auth0 EU HTTPS issuer, the same auth
server URL and issuer, and a bounded client identifier. This operation does
not change SPF, DKIM, DMARC, MX, or any other DNS record.

Only the Backend receives these files. The database migrator does not receive
them. Do not put a value in a workflow variable, Ansible variable, command, or
Git file.

## Backup and restore gates

Install the existing local backup timers. Before any activation, create a new
backup and run the isolated restore rehearsal:

```bash
make backup-postgres-now \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
make rehearse-postgres-restore \
  ANSIBLE_EXTRA_VARS=/absolute/path/to/bootstrap-public.yml
```

Verify that the selected manifest contains database `parkventory` and all
three Parkventory roles. The local result is not disaster recovery. The
application contract intentionally rejects activation until a reviewed digest
binds one encrypted off-site receipt and an independent restore from an
operator-controlled host. Keep the decryption identity and restore credential
off Atlas.

The sanitized readiness file is
`/var/lib/vps-readiness/parkventory/offsite-backup.json`, owned by `root:root`
with mode `0400`. It records no endpoint, bucket, credential, or private age
identity. Its exact contract includes the Parkventory database, one PR-78
`vps-postgres-offsite-receipt-v2` receipt digest, the local source-manifest and
age-recipient digests, an off-Atlas restore evidence digest, and all five
provider/recovery gates. The upload receipt must be at most 36 hours old at
activation. The matching off-Atlas rehearsal must be at most 31 days old and
cannot predate that upload beyond five minutes of clock tolerance. A reviewed
change then binds the canonical file digest in
`releases/application-production.json`. The controller rehashes and validates
the file before it runs any migration.
The controller revalidates the off-site proof after migration, immediately
before the first candidate start. It validates the proof again after runtime
probes and immediately before the durable commit. Recovery can forward-commit a
probed Parkventory candidate or finalize its partially visible active tuple only
after the same freshness check succeeds.

## Monitoring, logs, and probes

The application bundle is admitted only when it contains the exact Backend and
Frontend readiness probes, three public TLS probes, `/q/metrics` target, and
the `ParkventoryBackendUnavailable` alert. The central target and rule files,
the complete `parkventory-backend` scrape configuration, and the internal
platform Compose override remain disabled in this change. The override joins
only Prometheus to `app_parkventory` while retaining `ops`, and mounts the exact
runtime config, target, and rule directories. The alert covers both `up != 1`
and a missing series. The four reviewed inputs are:

- `applications/parkventory/integration/internal-platform.override.yaml.disabled`;
- `applications/parkventory/integration/prometheus/prometheus.yml.disabled`;
- `platform/observability/prometheus/targets/parkventory.yml.disabled`;
- `platform/observability/prometheus/rules/parkventory.yml.disabled`.

Activate all four pieces together, then prove the target
is `up` and deliver a test alert. `make check-parkventory-monitoring-candidate`
and `make check-prometheus` validate the inactive composition and Prometheus
configuration without starting it.

Each service keeps at most three local 10 MiB log files. Before public traffic,
prove structured Backend records, bounded retention, and operator access. Use
`docker compose logs` through the bounded application release directory. Do
not log OIDC cookies, tokens, secret file content, SMTP credentials, or tenant
business data.

## Workflow and cutover

The `Deploy Parkventory application` workflow is manual and has no release
input or schedule. While this change is current, the protected application
contract disables Parkventory and the static contract enables it. The workflow
therefore produces no deployment matrix. A future cutover must also require
`VPS_APPLICATION_DEPLOY_ENABLED=true` in the protected
`application-production` environment.

Disable and drain the static reconciler under the shared lock before route
handoff. Prepare the exact attested Caddy route and application-network
attachment before a migration. After dispatch, verify internal health, the
release identity endpoint, the public root and `/app`, Prometheus, alert
delivery, logs, active state, backup timers, and strict public TLS.

## Rollback

Before cutover, leave the static release active. During application activation,
the transaction controller normally restores the previous runtime and
quarantines a candidate that fails health, source-head, route, or public probes.
A failed or interrupted Parkventory migration proof uses a stricter path. The
controller leaves every Parkventory runtime container absent and never restarts
the previous runtime against the untrusted database. Do not reverse an applied
migration automatically. Use a compatible previous image only after the
database is proved compatible, or apply a reviewed forward repair. Preserve the
previous database volume and all backup evidence until recovery is complete.

## Activation remains blocked

Do not populate the application readiness digest or set `enabled: true` from
this runbook. The protected contract has two independent empty gates:

- the reviewed digest of the PostgreSQL readiness evidence;
- verified encrypted off-site backup evidence.

The second gate has a bounded sanitized-evidence verifier, but this change does
not provision a provider, upload identity, age recipient, receipt, or off-host
restore. Local backups and local restore rehearsals do not satisfy it.
Parkventory also retains the other ADR-0015 activation blockers and the explicit
static-to-Compose ownership handoff.
