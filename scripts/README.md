# Contrôleur de release

These scripts form the local VPS control plane. They do not build an image. The
static controller is converged and its bounded transactional activation path is
operational. The generic controller remains locked. The Compose application
controller and recovery wiring are converged and proved healthy while idle on
Atlas. The canonical contract admits only the Surplasse tester release.
Parkventory and the legacy Compose entries remain disabled, no application
workflow invokes the gate, and no live Surplasse activation is proved.

## Installation contract

At the converged repository revision, Ansible installs these root-owned paths
without group or other write permission:

- `/usr/local/libexec/vps/deploy`
- `/usr/local/libexec/vps/deploy-application`
- `/usr/local/libexec/vps/deploy-application-live-gate`
- `/usr/local/libexec/vps/deploy-static`
- `/usr/local/libexec/vps/deploy-static-live-gate`
- `/usr/local/libexec/vps/forced-command`
- `/usr/local/libexec/vps/materialize-surplasse-dns-secrets`
- `/usr/local/libexec/vps/materialize-surplasse-pilot-manifest`
- `/usr/local/libexec/vps/parse-forced-command`
- `/usr/local/libexec/vps/plan-digests`
- `/usr/local/libexec/vps/reconcile`
- `/usr/local/libexec/vps/surplasse-dns-cutover`
- `/usr/local/libexec/vps/surplasse-pilot-bootstrap`
- `/usr/local/libexec/vps/validate-compose`
- `/usr/local/libexec/vps/validate-release`
- `/usr/local/libexec/vps/verify-github-evidence`
- `/usr/local/libexec/vps/verify-state`
- `/usr/local/libexec/vps/lib/release_policy.py`
- `/usr/local/libexec/vps/lib/application_bundle.py`
- `/usr/local/libexec/vps/lib/application_release.py`
- `/usr/local/libexec/vps/lib/platform_integration.py`
- `/usr/local/libexec/vps/lib/platform_proof.py`
- `/usr/local/share/vps-infra/schemas/production-release.schema.json`

Ansible also installs the checksum-verified GitHub CLI executable at
`/usr/local/bin/gh`. ORAS remains a locked local and CI publishing tool; the
materializer does not require it on Atlas.

`check`, `check-public-safe` et `doctor` sont des outils d'audit à installer si
le rôle d'exploitation doit les exécuter sur le VPS. `apply-release` est
volontairement absent : aucun chemin de mutation n'est livré dans cette tranche.
Le Python système du VPS doit fournir `jsonschema` Draft 2020-12 ; le contrôleur
production refuse de continuer sans ce validateur.

Le miroir autorisé est `/srv/vps/repository`, avec l'origine exacte
`https://github.com/nclsppr/vps-infra.git`. Le contrôleur ne récupère que
`refs/heads/main` et n'accepte depuis SSH que `deploy <sha40>`.

## Surplasse DNS credential materializer

`materialize-surplasse-dns-secrets` owns only the three OVH DNS-01 credential
files under `/etc/vps/secrets/dns/surplasse`. It accepts two modes:

```text
materialize-surplasse-dns-secrets --install-from /absolute/root-only/directory
materialize-surplasse-dns-secrets --check
```

The source directory must be `root:root 0700`. It must contain exactly
`ovh-application-key`, `ovh-application-secret`, and `ovh-consumer-key` as
`root:root 0400` or `root:root 0600` regular, single-linked files. Each value
must be one newline-terminated bounded ASCII token. The application secret and
consumer key must differ.

Installation takes `/run/lock/vps-static.lock` before the private credential
bundle lock. It stages and synchronizes all three `root:root 0400` files. It
renames `surplasse-dns-credential-manifest.json` last as the transaction commit
marker. The manifest contains only the contract version and SHA-256 values. A
missing or stale manifest makes `--check` fail.

An atomic host rename does not update a file that is already bind-mounted into
a running container. A separate reviewed edge transaction must validate this
manifest, recreate Caddy, and pass its probes before it reports a rotation as
complete.

`--check` is read-only and takes only the private bundle lock. A public-edge
controller can call it while that controller owns the shared deployment lock.
The helper rejects an incomplete tree, a staging remainder, and every unexpected
entry. It does not remove or repair any pre-existing entry. It never calls the
OVHcloud API, checks IAM scope, changes DNS, starts Caddy, or activates a route.

## Surplasse pilot bootstrap

The root-only `materialize-surplasse-pilot-manifest` helper validates one
private JSON document and installs it atomically as
`/etc/vps/applications/surplasse-pilot-bootstrap.json`, `root:10001 0440`.
Before any fetch, `converge` calls the local
`stage-surplasse-pilot-manifest` fd copier. It accepts only a caller-owned,
single-linked `0400` or `0600` regular file of 1 through 16384 bytes and copies
it without reopening its path into the isolated `0700` home as `0600`.
The root-only `surplasse-pilot-bootstrap` controller accepts only `status` or
`apply`. It derives the active immutable Surplasse release from protected state
and never accepts a digest target. A fresh empty status is required before one
apply, and a separate status must resolve the durable `applied-unverified`
state. Child output is bounded and discarded. See
[`docs/operations/surplasse-pilot-bootstrap.md`](../docs/operations/surplasse-pilot-bootstrap.md).

## Public static edge convergence

`converge` has three exact operator modes for the isolated static edge:

```text
--prepare-public-static-edge
--activate-public-static-edge
--stop-public-static-edge
```

Preparation starts only the HTTP routes. Activation first proves exact A and
empty AAAA answers at every authoritative server and through the host resolver,
then enables HTTPS and runs strict certificate probes. Stop discovers and
stops every container carrying the fixed Compose project label even if its
systemd unit is absent. Each active release contains the exact validator copied
from the same archived Git revision; the host-installed controller cannot be
silently reused at another policy revision.

## Static OCI materializer

The operator-facing deployment form of `deploy-static` accepts seven bounded
arguments:

```text
deploy-static <personal|papersempire|parkventory> <source-sha40> \
  <site-ref@sha256> <routes-ref@sha256> \
  <integration-sha40> <integration-ref@sha256> \
  <caddy-image@sha256>
```

The automated SSH boundary accepts the same tuple only as the canonical
`deploy-static-live` record. `forced-command` validates it as the unprivileged
deploy account, then sends it over bounded stdin to the root-owned
`deploy-static-live-gate`. The gate takes no command-line arguments, performs an
independent validation, and starts the live flag in a transient systemd unit
whose stop hook runs transaction recovery. This extra boundary is
required because Atlas uses `sudo-rs`, whose sudoers implementation does not
support argument regular expressions.

`releases/static-production.json` independently controls whether each static
profile may enter that boundary. It records `static-site` for Personal and
Papers Empire and `temporary-static-demo` for Parkventory. The resolver omits a
disabled entry, and the root materializer re-reads both the protected static
contract, `releases/production.yaml`, and the protected active dynamic manifest.
It refuses an application that is disabled statically, enabled simultaneously
as a Compose application, or still present in active Compose state. The future
Parkventory React/Java applicator must enforce the symmetric guard before it
takes ownership of `parkventory.com`.

The seven-argument form without `--activate-live` only materializes and probes
the digest-named release. It never changes `current`. All live mutation goes
through the root gate and the transaction state machine.

The executable also has internal worker forms. They are generated by the root
orchestrator, receive fixed bounded contracts, and are not operator interfaces.

The site and route references must use the exact public GHCR repositories in
the application profile. The integration reference must use
`ghcr.io/nclsppr/vps-infra/platform-integration`. The Caddy reference must use
`ghcr.io/nclsppr/vps-infra/caddy` and must equal `CADDY_PLATFORM_IMAGE` in the
protected infrastructure mirror. This binds the probe to the promoted platform
image even when the platform is not active. The same Caddy reference must occur
in the exact integration package. Tags cannot replace a digest.

The materializer verifies all three OCI manifest digests. It checks exact media
types, embedded configs, layer counts, source and revision annotations, and
creation timestamps. The site and route manifests contain one layer each. The
integration manifest contains the deterministic archive and canonical inventory
layers. The materializer then verifies each layer checksum and size, the route
inventory, every regular-file checksum, and the strict bijection between site
archive files and routes.

Before it downloads a payload layer, the materializer verifies GitHub artifact
attestations for the site manifest, route manifest, and integration manifest.
Each verification binds the subject to its exact repository, full source
revision, canonical source ref, and signer workflow. It rejects a self-hosted
runner as the signer. The Personal and Papers Empire source refs are
`refs/heads/main`. The integration source ref is
`refs/heads/main` in `nclsppr/vps-infra`.

A network-enabled systemd `DynamicUser` execution downloads the bounded
attestation index, manifest, and bundle objects. The root orchestrator copies
the exact bundle set into a root-owned file and removes the fetch state. One
other network-enabled execution uses the checksum-pinned GitHub CLI and its
embedded TUF bootstrap roots to obtain one current trusted-root snapshot for
the complete deployment. The root orchestrator validates and copies the two
JSONL records only when they match the versioned SHA-256. A trust-root rotation
therefore fails closed until a reviewed repository update changes the accepted
digest. The orchestrator then removes that fetch state. A separate sequential
offline execution of the same fixed transient unit gives the local bundle, copied
trusted root, and digest-bound local OCI manifest to GitHub CLI through
`--bundle` and `--custom-trusted-root`. The fixed unit name prevents concurrent
worker creation. GitHub CLI does not access OCI or receive a token. The verifier
succeeds only when the unit exits with code zero after its complete cgroup
exits.

An exact source ref in an attestation does not prove that the branch is
protected. Repository branch protection or an external ruleset is a separate
production gate. Keep this gate independent for `vps-infra`, Personal, Papers
Empire, and Parkventory.

`schemas/static-route-inventory-v1.schema.json` publishes the common JSON
shape. The dependency-free runtime validator adds application limits, canonical
encoding, route derivation, checksum totals, and the archive bijection that the
JSON Schema cannot express alone.

The consumer limits are profile-specific:

| Application | Compressed | File payload | Files | Tar members |
|---|---:|---:|---:|---:|
| `personal` | 50 MiB | 100 MiB | 2,000 | 4,001 |
| `papersempire` | 75 MiB | 150 MiB | 5,000 | 10,001 |
| `parkventory` | 50 MiB | 100 MiB | 2,000 | 4,001 |

Each OCI manifest is limited to 64 KiB and the route inventory layer is limited
to 2 MiB. The integration package applies its own archive and inventory limits.
Attestation indexes and bundles, registry token responses, OCI manifests,
inventories, and payload layers have explicit transfer limits. Registry token
responses, manifests, payload layers, and attestation discovery objects are
fetched by network-enabled `DynamicUser` executions. Manifest and payload
copies are checked against caller-known digests. Payload copies also use exact
sizes from reconstructed manifests. Attestation discovery uses the subject
digest tag, and the copied bundle set is size-bounded before the offline
signature verifier authenticates it. The bearer header stays in a private
worker file and does not appear in process arguments. The temporary Caddy image
pull remains a separate root-controlled Docker operation by immutable digest.
The trusted-root execution has runtime, memory, per-file size, inode, and tmpfs
limits. GitHub CLI does not expose an in-transfer size limit for its internal
TUF client. Bounded readers request at most the limit plus one byte so an
oversized local object cannot be accepted. The complete route probe has a
five-minute Personal and Parkventory budget and a ten-minute Papers Empire
budget. Lock acquisition stops after one minute.

The Personal profile accepts the one Git archive PAX comment that contains the
source SHA. The Papers Empire profile requires its GNU tar normalization. This
validation does not claim that a complete Papers Empire source rebuild is
byte-reproducible.

The script rejects absolute paths, traversal, ambiguous components, duplicate
members, links, devices, FIFOs, sparse files, unknown tar extensions,
concatenated gzip streams, and each limit overrun. Manifest, gzip, tar,
inventory, package, and attestation parsing runs in short-lived systemd
`DynamicUser` units. Extraction uses `openat`, `O_NOFOLLOW`, and `O_EXCL`.
Worker state uses a dedicated bounded tmpfs. The root orchestrator waits for
the complete worker cgroup to exit, consumes only bounded results, copies exact
allowlisted files into separate root-owned trees, and removes the worker state.

The exact integration package must contain the base Caddyfile and one
`<application>.caddy.disabled` candidate. It must not contain the corresponding
active route. The probe copies only the requested candidate into its temporary
tree and renames that copy to `<application>.caddy`. It does not change the
package or activate a committed platform route.

A temporary container from the exact Caddy digest first runs `caddy validate`
against the exact packaged Caddyfile and temporary route set. The runtime probe
then changes one temporary Caddyfile copy exactly once to add `local_certs`.
It serves HTTPS on a loopback-only random port. The probe requests every
inventory route with the canonical host and compares the HTTP body SHA-256. It
also checks the 404 body, the `no-cache` HTML policy, the
`public, max-age=3600` asset policy, gzip for a payload larger than 1 KiB,
security headers, and configured host redirects. This local certificate test is
not the later public TLS test.

Before the probe, the script makes the complete release root-owned with files at
`0644` and directories at `0755`. It fsyncs every file and then every directory
from the deepest directory to the release root. After a successful probe, it
fsyncs the release parent before and after the digest-named rename. The release
directory is `releases/sha256-<site-manifest-digest>`.

Activation opens the application root, release root, and target release with
directory file descriptors and `O_NOFOLLOW`. It accepts only the exact relative
target `releases/sha256-<site-manifest-digest>`. It fsyncs the application
directory before and after the atomic `current` replacement. If the post-rename
fsync fails, it restores the previous `current` target and fsyncs the rollback.
It reports a separate failure if it cannot make that rollback durable.

The generic `deploy` policy stays locked and `apply-release` stays absent. The
separate static live form is wired only through the exact forced-command gate.
It requires the candidate source SHA to equal the current canonical branch
HEAD before validation and immediately before activation. It records the
complete immutable tuple, not only the site digest. An exact matching state and
symlink uses the protected persisted route inventory to verify the release
filesystem, exact running Caddy image and health, and a bounded public TLS route
sample without refetching registry payloads. A second site or routes digest for
one active source SHA is rejected.

Before the symlink switch, the live form writes a durable transaction under
`/var/lib/vps-static/transactions`. After the switch it probes the running edge
on loopback port 443 without `--insecure`, so the normal public certificate
chain and hostname must validate. It replays the route checksums, 404, cache and
security headers, gzip, and the redirects actually served by the public edge.
Each live redirect has an explicit HSTS expectation. Personal redirects must
omit HSTS; this matches the public edge and preserves rollback for the `.fr`
aliases. Papers Empire and Parkventory redirects must retain HSTS. Canonical
site responses always require HSTS.
Only a complete success persists the route inventory and replaces
`/var/lib/vps-static/active/<application>.json`. Failure restores and fsyncs the
previous target before rechecking the exact Caddy identity and source HEAD. It
records the exact tuple in `quarantine/` only when both remain unchanged;
recovery also quarantines conservatively when classification was interrupted,
while a durable `superseded` phase remains retryable. Interruption is
recovered before another candidate, after a failed transient activation, and at
boot before the systemd-managed public edge unit. Docker may still restart the
existing `unless-stopped` Caddy container as soon as the daemon starts, before
that ordering is applied. Recovery revalidates managed release
bytes and removes bounded labeled probe containers, strict staging directories,
and temporary activation symlinks. Git ancestry is checked in a separate
bounded network `DynamicUser` worker rather than in the root process.
Release garbage collection remains an operator responsibility.

Use the
[static reconciliation runbook](../docs/operations/static-release-reconciliation.md)
to inspect resolver status, Atlas active state, transactions, quarantine,
recovery, public probes, and key rotation. A green workflow can cover only the
profiles classified as `ready`.

## Canonical Compose application controller

This section specifies the controller installed on Atlas by the proved
convergence of repository revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b`. The root-owned
`deploy-application` executable and argument-free gate are present. The
repository now enables only the canonical Surplasse tester admission entry.
Parkventory remains disabled, and no application workflow invokes either gate.

`deploy-application` consumes only the immutable
`ghcr.io/nclsppr/<application>/application-release@sha256:<digest>` selected by
the admission resolver. It supports the exact `surplasse` and `parkventory`
profiles. `releases/application-production.json` enables only Surplasse. A
disabled application still stops before runtime validation or network access.
An enabled entry authorizes admission, but it does not invoke live activation.

The resolver normally rejects every redirect. Its only exception is one GHCR
descriptor-blob `307` to the exact GitHub package content host and the path for
the expected digest. The second request receives no registry bearer and cannot
redirect again. The descriptor size and digest checks remain mandatory.

For an enabled entry, the controller independently verifies the release,
every component image index and config, the application integration manifest,
and all allowlisted GitHub attestations. It validates the common two-layer
integration bundle, canonical inventories, safe archive metadata, migration and
probe hashes, rendered Compose policy, and root-owned secret metadata. Secret
bytes are never copied into a release or state journal.

Materialized releases live under
`/srv/applications/<application>/releases/sha256-<release-manifest-digest>`.
Active, inventory, transaction, and quarantine records live under
`/var/lib/vps-application`. Application and static deployments serialize on
`/run/lock/vps-static.lock`.

The live SSH record is exact and digest-only:

```text
deploy-application-live <surplasse|parkventory> <source-sha40> \
  <application-release@sha256>
```

`forced-command` sends that canonical line over stdin to the argument-free
root gate. The gate creates a bounded transient systemd unit whose stop hook
runs `deploy-application --recover-live`. Ansible has installed
`vps-application-recover.service`; on 2026-08-18 it was loaded and inactive after
a successful run (`Result=success`, `ExecMainStatus=0`). It is ordered before
the systemd-managed public edge, but does not close Docker's earlier
`unless-stopped` restart path.

Before a dedicated migration can run, Parkventory must no longer have static
active state. For both applications, the installed public edge route must equal
the attested bundle route byte for byte and the healthy edge Caddy container
must already be attached to the exact application network. The controller does
not mutate the immutable edge release. For Surplasse, the preflight also
requires the managed `app_surplasse` bridge identity, its exact
`172.30.10.0/24` subnet, and the same network identifier plus
`172.30.10.254/24` on Caddy.

The inactive Surplasse Compose override is not a cutover command. The separate
`deploy-surplasse-public-edge` controller stages the route and Atlas snippet,
recreates Caddy, proves the new runtime identity, and recovers the previous
edge release. It does not install DNS credentials and does not change DNS.
Those external operations remain separate reviewed prerequisites.

Activation journals `prepared`, `migration-running`, `migrated`, `started`,
`probe-rejected`, and `probed`. Only `probed` can become active. Recovery restores
the previous Compose runtime and quarantines a candidate that reached a
potentially mutating phase. SQL migrations themselves are not reversible; they
must remain compatible with both the current and previous runtime images.

The controller does not yet verify an attested backward-compatibility invariant
for that post-migration restore. The first Surplasse tester activation has no
previous application runtime, and ADR-0013 accepts only that bounded initial
case. Later schema-changing updates must remain fail-closed until compatibility
evidence is enforced, or recovery stops for explicit forward repair after
migration. Parkventory remains disabled, and Surplasse cannot use the current
restore behavior as readiness evidence for such an update.

## États séparés

- `desired/` : manifeste validé et demandé ;
- `active/` : manifeste effectivement appliqué ;
- `plans/` : plans de réconciliation non mutables ;
- `quarantine/commits` et `quarantine/artifacts` : refus persistants ;
- `journal.jsonl` : résultat structuré des tentatives.

Chaque paire `state.json`/`manifest.json` est revalidée par hash, par contenu Git
octet pour octet et par rattachement à `origin/main` avant toute nouvelle
réconciliation. Les clés JSON dupliquées sont refusées.

Persisted manifests are also revalidated with the installed Draft 2020-12 JSON
Schema and the Python release policy. A controller update cannot silently use
only one of these two trust roots.

## Current locks

The manifest requires `activation_policy: locked`. The controller rejects each
unit with `enabled: true`, even when its evidence is structurally valid. A
separate audited revision of the schema, policy, and applicator is necessary to
change this rule.

A disabled platform can contain a candidate declaration. The declaration
consists of the `images`, `integration`, `postgres`, and `readiness_evidence`
fields. The manifest must contain all four fields or none of them. Candidate
images and artifacts must use immutable digests. The platform integration
source revision must be an ancestor of the requested release commit. The
controller validates candidate evidence and applies artifact quarantine before
it records desired state. Candidate metadata does not publish a port, create a
runtime reference in the reconciliation plan, or authorize a service start.

Infrastructure and application Compose validation require an exact image
contract. The shared platform uses its versioned contract directly:

```text
validate-compose --expected-images platform/expected-images.json \
  --repository-root /path/to/vps-infra \
  vps-platform /path/to/rendered-compose.json
```

Application integration uses a contract from its verified bundle:

```text
validate-compose --expected-images /path/to/expected-images.json \
  <surplasse|parkventory> /path/to/rendered-compose.json
```

The image contract is a strict JSON object. Each key is a Compose service name.
Each value is an immutable image reference with a SHA-256 digest. The file is
limited to 64 KiB. Duplicate keys, mutable references, incomplete service sets,
and image differences fail validation. The `--expected-images` option and
`--structural-only` are mutually exclusive.

This option binds the rendered Compose document to exact image references. It
does not authenticate the JSON file or prove image provenance. The versioned
platform contract is included in the integration artifact. A production caller
must verify that artifact and the release manifest through independent trust
roots. Environment variable allowlists and declared `_FILE` secret bindings
remain required before application activation.

For Surplasse, `validate-surplasse-adapter` requires the complete and exact
Backend environment. It fixes the sender, secret paths, port `587`, required
STARTTLS mode, and the `PLAIN LOGIN` authentication methods. It rejects every
other Quarkus or Java override declared in Compose. This validation does not
prove that the image or its entrypoint adds no configuration. Effective runtime
configuration remains a separate release gate.

`verify-github-evidence` confirms the repository, branch, commit, run attempt,
and exact `.github/workflows/vps-release.yml` workflow through the public GitHub
API. The `caddy-ovh-image` and `immutable-image-digests` gates must also name a
raw Actions artifact. The verifier reconstructs the canonical proof bytes from
the candidate and run metadata. It compares the expected digest, size, name,
run, branch, and commit with the public artifact metadata. An older run or
artifact cannot prove a changed candidate or a repeated run attempt.

`prove-platform-candidate` validates the secret-free candidate with both the
JSON Schema and Python policy. It resolves each exact OCI manifest, hashes the
registry bytes, verifies the supported OCI labels, checks the Caddy OVH module,
and verifies exact signer workflows while rejecting self-hosted signers. The
PostgreSQL image must use `ghcr.io/nclsppr/vps-infra/postgres`. Its source label,
full revision label, readable `sha-<revision>` tag, and GitHub attestation must
match `.github/workflows/postgres-image.yml` on `main`.

Trivy always writes a JSON report when it finds a vulnerability. The proof
engine rejects every CRITICAL finding. It rejects a HIGH finding unless one
unexpired statement in `policies/platform-vex-v1.json` matches the service,
complete image reference, platform, target binary, package name, package PURL,
installed version, and CVE. It also rejects an unused exception. The VEX file
has a strict versioned schema. There is no global ignore file or ignore flag.
The evaluator permits only the four reviewed identities and their maximum
expiry dates. The canonical platform proof v2 binds the exact VEX file digest
and its earliest expiry. Evidence verification checks that expiry again in UTC.
GitHub artifact retention cannot extend the validity of the exceptions.

The tool writes one canonical JSON proof file. The workflow uploads that file
as a raw Actions artifact. Use this local command to calculate the workflow
input without registry access:

```bash
./scripts/prove-platform-candidate --print-subject candidate.json
```

The workflow requests 90-day proof retention, subject to the repository
retention policy. The proof is valid for locked candidate review, not for
long-term active-state provenance. A locked candidate can attach it only to
`caddy-ovh-image` and `immutable-image-digests`; all eight production blockers
remain. The activation policy stays locked.

Without `/etc/vps/production-enabled`, `deploy` stays in dry-run mode. It exits
with code 78 after evidence verification, reconciliation, and desired-state
recording. While the installed policy is locked, the controller rejects the
production marker explicitly. It cannot invoke an applicator or create active
state. The locked controller contains no applicator execution path. A future
live path requires a separately audited policy revision.

## Platform integration publication tools

`build-platform-integration` reads the exact runtime allowlist from one full
Git commit. It writes `platform-integration.tar.gz` and
`platform-integration.inventory.json` into an empty output directory.

`verify-platform-integration` rejects a non-canonical archive, a non-canonical
inventory, an unexpected path, a special file, an invalid mode, a digest
mismatch, and every configured size limit violation.

`verify-platform-integration-manifest` checks the raw OCI manifest bytes against
the resolved digest and the two local layer payloads. The publication workflow
then fetches both blobs from GHCR and runs the package verifier again.

`write-platform-integration-evidence` writes the canonical raw audit record only
after the workflow has verified GitHub provenance. These tools do not publish,
promote, or activate a release when an operator runs them locally.

## Surplasse public edge transaction

`deploy-surplasse-public-edge` stages, switches, verifies, and recovers the
optional Surplasse extension of the existing Caddy project. It never changes
DNS and never materializes a credential. It calls
`materialize-surplasse-dns-secrets --check` while it owns the shared deployment
lock. The controller retains every base static route and named volume. It
records its active and transaction state under
`/var/lib/vps-public-edge-surplasse`.

Each activation forces Caddy recreation. This is required after the DNS helper
atomically replaces a credential because an existing bind mount keeps the old
inode. A read-only live verification does not claim that a rotated inode is
already mounted.

Run its focused adversarial tests with:

```bash
make check-surplasse-public-edge-controller
```

## Locked Surplasse DNS cutover transaction

`surplasse-dns-cutover` is a separate root-owned controller for the later
`surplasse.com` IPv4 migration. Its versioned policy is disabled and locked.
While locked, it does not open its dedicated OVHcloud credential directory or
construct an API client. It never consumes the permanent Caddy DNS-01
credential bundle.

The controller retains a complete raw API export, a canonical record snapshot,
digest-bound expiring plans, and a durable per-write journal. It requires a
TTL-only plan and a full old-TTL wait before the target plan. It verifies all
authoritative servers and both fixed public recursive resolvers. Verification
requires an explicit expected DNS RCODE and exact answer name and type, so an
empty `SERVFAIL` or `REFUSED` response is never accepted as negative evidence.
A separate rollback plan restores the original A records. See
[`docs/operations/surplasse-dns-cutover.md`](../docs/operations/surplasse-dns-cutover.md).

Run the local adversarial tests with:

```bash
make check-surplasse-dns-cutover-controller
```
