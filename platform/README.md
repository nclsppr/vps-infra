# Shared Platform

This directory contains the shared platform baseline. Do not start it on a
production host until the controller has validated every required secret,
external network, and immutable image reference.

## Public static edge

`public-static-edge/` is an independently deployable Caddy-only unit. It serves
the already materialized Personal and Papers Empire releases. It does not start
PostgreSQL, Prometheus, Grafana, either exporter, Surplasse, or Parkventory.

This unit is an ordered deployment step, not a replacement for the shared
platform. PostgreSQL and the internal observability services remain required
for the two application stacks. Their image admission and data controls can be
completed without withholding the static sites from the Internet.

The unit uses the promoted Caddy image by its exact digest. It receives no OVH
credential. Its preparation routes are HTTP-only. After the A cutover and the
explicit removal of every previous AAAA record, a separate activation phase
enables HTTPS and requires strict public certificate probes. Only Caddy
publishes public host ports. The complete shared platform continues to bind
Grafana to `127.0.0.1:3000` and publishes no PostgreSQL, Prometheus, or exporter
port. The public static edge joins only the dedicated `edge` network. It has no
attachment to the internal observability network `ops`.

## Image versions

The platform uses the following service versions:

| Service | Version |
|---|---|
| Caddy | `2.11.4-alpine` |
| PostgreSQL | `17.10-bookworm` |
| Prometheus | `3.13.1-busybox` |
| Grafana | `13.1.1` |
| Node Exporter | `1.12.1` |
| PostgreSQL Exporter | `0.20.1` |

Each Compose image reference contains a readable tag and an immutable digest.
The upstream Caddy image does not contain an OVH DNS provider. CI builds one
Caddy image with `caddy-dns/ovh` v1.1.0 at commit
`17fd665136b593153167bf9dfee9a3c0bd2c7ac0`.

`platform/caddy/build.env` defines the immutable upstream images. The generated
entry point and complete Go graph are in `platform/caddy/build/`. The graph
locks Caddy v2.11.4, the OVH module v1.1.0, `golang.org/x/text` v0.39.0, and
`google.golang.org/grpc` v1.82.1. It also locks `github.com/google/cel-go`
v0.29.2. Caddy v2.11.4 needs the two-line compatibility change from upstream
commit `b2693fb63a30e6d7be0972c3645e9a2c0a500e93` to compile with that version.
The build applies only that committed patch after `go mod verify`. It then
checks the patched source file checksum before compilation. This avoids the 32
unrelated Caddy changes between v2.11.4 and the upstream compatibility commit.

The build uses `go build -mod=readonly` and the committed `go.sum`. It does not
let `xcaddy` resolve a new graph during a production build.

The runtime layer installs the reviewed Alpine 3.23 fixes for `c-ares`, `curl`,
and `libcurl`. The Dockerfile downloads each architecture-specific APK from an
exact URL with a BuildKit SHA-256 check. `apk` then installs only these local
files with network access disabled. A missing file, changed payload, missing
dependency, or checksum mismatch fails the build.

`CADDY_PLATFORM_IMAGE` in `platform/.env.example` is the promotion point for an
already published and attested output. It references the custom manifest
`sha256:1879be5d28d98d49522d74f4fc1bd8c176fd4e35112f757b6639151f3170f11f`,
built by the `main` image workflow at infrastructure revision
`755565642f71918134151921f8eae63935951ad6`. Workflow run
[`31575365861`](https://github.com/nclsppr/vps-infra/actions/runs/31575365861)
verified both native child images, passed the strict scan for each exact child
digest, and created and verified GitHub provenance for the exact
multi-architecture manifest. A promotion does not rebuild the image.
Production uses the custom image by digest.

## Caddy publication gate

The Caddy image workflow fails closed at two points:

1. Each pull request builds and loads native `linux/amd64` and `linux/arm64`
   images. It verifies the OVH module, both route sets, locked package versions,
   and the Caddy dependency graph. Trivy 0.73.0 then rejects every HIGH or
   CRITICAL vulnerability, including findings without a published fix. The
   workflow has no vulnerability ignore file or ignore flag.
2. A `main` build publishes a multi-architecture image without GitHub
   provenance. The workflow resolves both child manifest digests, verifies the
   OCI source and revision labels, verifies both exact images, and scans both
   child digests directly from GHCR. Trivy creates an ephemeral, GHCR-scoped
   registry login with the job's `GITHUB_TOKEN` and `--password-stdin`, then
   mounts that configuration read-only for each scan. The Trivy containers run
   with the runner UID and GID so their temporary authentication and shared
   database cache remain removable. They do not require a Docker socket. Public
   packages also remain anonymously readable. Only a successful scan can create
   and verify GitHub build provenance or show a digest for a separate promotion
   pull request.

The pushed package can exist when a post-push gate fails. Such a package has no
verified GitHub provenance and must not be promoted.

## Platform integration artifact

`.github/workflows/platform-integration.yml` publishes the secret-free runtime
configuration as one OCI artifact. The artifact is not a deployment request.
It does not change `releases/production.yaml`, enable a service, or contact a
host.

The builder reads one exact Git commit. It includes only these runtime roots:

- `platform/.env.example` and `platform/compose.yaml`;
- the base Caddyfile and the four reviewed route candidates;
- the Prometheus, Grafana, and exporter configuration;
- the PostgreSQL configuration and initialization script.

It excludes the Caddy Dockerfile, Go graph, build inputs, entry point, and
documentation. The exact 20-file allowlist rejects a missing path, an extra
path in a runtime root, a symbolic link, a submodule, an executable file, a
special file, invalid UTF-8, and an oversized payload.

The artifact has these media types:

| Object | Media type |
|---|---|
| OCI artifact | `application/vnd.vps-infra.platform-integration.v1` |
| Deterministic archive | `application/vnd.vps-infra.platform-integration.v1+tar+gzip` |
| Canonical inventory | `application/vnd.vps-infra.platform-integration.inventory.v1+json` |

The archive uses sorted paths, regular files with mode `0644`, numeric owner
`0:0`, the source commit timestamp, USTAR headers, and a deterministic gzip
header. The canonical JSON inventory binds every path to its mode, size, and
SHA-256 digest. It also binds the source URL, full source revision, creation
time, and media types.

The workflow pushes `sha-<source-revision>` to
`ghcr.io/nclsppr/vps-infra/platform-integration`. It resolves the manifest
digest, verifies the exact manifest shape and annotations, fetches both layer
blobs by digest, compares their bytes with the local package, and verifies the
downloaded package again. Only these successful checks can create provenance.
The final verification requires this repository, the full source revision,
`refs/heads/main`, the exact `platform-integration.yml` signer workflow, and a
GitHub-hosted runner.

The OCI artifact and its registry provenance are the durable records. The
workflow also uploads a canonical raw JSON audit record for 90 days. It checks
the raw artifact digest returned by GitHub before it publishes the result.

## Application state

The production release manifest disables all four applications. Therefore,
the base platform has no active application route, scrape target, or alert
rule. Each candidate file has a `.disabled` suffix:

```text
platform/caddy/routes/
  papersempire.caddy.disabled
  parkventory.caddy.disabled
  personal.caddy.disabled
  surplasse.caddy.disabled

platform/observability/prometheus/
  targets/surplasse.yml.disabled
  rules/surplasse.yml.disabled
```

`scripts/validate-application-state` requires the file state to match
`releases/production.yaml`. It rejects an unknown file and an active file for a
disabled application. This locked baseline also rejects every enabled
application. A reviewed integration package must update the validator and
activate each required file as part of the same versioned release change. An
environment variable cannot activate a file.

The base Caddy service does not receive an OVH credential or an application
network. The Caddy entry point requires the three OVH credential files only
when `surplasse.caddy` is active. This requirement makes an incomplete
Surplasse activation fail before Caddy starts.

## Network boundaries

Ansible creates seven external Docker networks. The isolated public static
edge joins only `edge`. The locked complete platform definition continues to
use `ops` and `db_monitoring`:

| Network | Subnet | Reviewed members |
|---|---|---|
| `edge` | `172.30.32.0/24` | Isolated public static edge Caddy |
| `ops` | `172.30.30.0/24` | Locked complete platform Caddy, Prometheus, Grafana, and exporters |
| `db_monitoring` | `172.30.31.0/24` | PostgreSQL and PostgreSQL Exporter |
| `app_surplasse` | `172.30.10.0/24` | None |
| `db_surplasse` | `172.30.11.0/24` | None |
| `app_parkventory` | `172.30.20.0/24` | None |
| `db_parkventory` | `172.30.21.0/24` | None |

A reviewed application integration package attaches only the required
services to an application network. It must use a unique alias such as
`surplasse-backend`. It must not use a generic alias such as `backend`.

PostgreSQL does not join `ops`. Caddy, Grafana, and Prometheus have no direct
TCP path to PostgreSQL. PostgreSQL Exporter joins `db_monitoring` for SQL access
and `ops` for metrics access. Database roles and `pg_hba.conf` enforce the
database authorization boundary.

The isolated public static edge does not join `ops`. The exact Compose policy
rejects an `ops` attachment for this unit.

Caddy publishes `80/tcp`, `443/tcp`, and `443/udp`. Grafana binds to
`127.0.0.1:3000` for an SSH tunnel. No other platform service publishes a host
port. The public bindings use IPv4. Do not publish an `AAAA` record for this
host until the repository defines and verifies an equivalent IPv6 policy.

## Secrets

The repository contains no secret value. SOPS and Ansible will create these
base platform files under `/etc/vps/secrets/platform`:

| File | Container reader |
|---|---|
| `postgres-superuser-password` | PostgreSQL startup process |
| `postgres-exporter-password` | Numeric group `999` |
| `grafana-admin-password` | UID `472` |
| `grafana-secret-key` | UID `472` |

The parent directory has mode `0700` and owner `root`. A secret with one reader
has mode `0400`. The PostgreSQL Exporter password has mode `0440` and owner
`root:999`. The PostgreSQL initialization process runs as `999:999`. The
exporter runs as `65534:999`.

An active Surplasse integration also requires three scoped OVH DNS credential
files. The integration package must add those Compose secrets and the related
file variables. Do not add them to the disabled base platform.

## PostgreSQL

The volume name `vps-platform-postgresql-17-data` identifies the major version.
A PostgreSQL 18 change requires a migration plan. The bootstrap creates only
the `postgres_exporter` read role.

A separate idempotent controller must create an application database and its
roles. It must use a `NOLOGIN` owner and separate migration and runtime roles.
Parkventory has no database or role while its production readiness gates are
incomplete.

The current memory values require a VPS with at least 4 GiB of memory. Ansible
must render host-specific values after the operator confirms the host size.

## Local validation

Run the complete contract from the repository root:

```bash
make check
```

The contract performs these platform checks:

- It renders Compose and applies the structural production policy.
- It validates the active Prometheus configuration.
- It validates each inactive Prometheus rule candidate.
- It validates Caddy with the inactive route set.
- It validates all Caddy route candidates with placeholder OVH credentials.
- It verifies the Caddy version, OVH module, Go replacements, and fixed Alpine
  package versions in the locally built image.

The checks do not start the platform. They do not call the OVH API. The
`--structural-only` Compose mode is valid only for local analysis. A production
controller must use the exact references from the release manifest and the
verified integration package.

Prometheus alert rules have no configured Alertmanager or notification
channel. Production activation requires a tested external alert path.
