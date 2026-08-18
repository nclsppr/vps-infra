# Static reconciliation rollout evidence - 2026-08-18

## Scope and evidence boundary

This record captures the controlled activation, repeated no-op verification,
deployment-key recovery, and final reconciliation of the automatic static
release path on Atlas. The GitHub runs occurred on 2026-08-17 and 2026-08-18
UTC. The final host and public observations were made on 2026-08-18 in
Europe/Paris.

The static activation evidence applies only to Personal, Papers Empire, and the
temporary static Parkventory demo. The final convergence also proves that the
disabled Compose application controller is installed on Atlas. It does not
authorize or activate Surplasse or the Parkventory React/Java application. Both
application entries remained `enabled: false`, and no application deployment
workflow invoked the controller.

## GitHub environment

The `static-production` environment had these non-secret values during the
verification:

| Variable | Value |
|---|---|
| `VPS_STATIC_DEPLOY_ENABLED` | `true` |
| `VPS_STATIC_DEPLOY_USER` | `deploy` |
| `VPS_STATIC_DEPLOY_PORT` | `22` |

The environment contained `VPS_STATIC_HOST`, `VPS_STATIC_KNOWN_HOSTS`, and
`VPS_STATIC_SSH_PRIVATE_KEY`. Their values are not part of this public record.
The deployment public key was rotated during the final verification. Its
non-secret ED25519 fingerprint is
`SHA256:/6br9opzdHN2NQt1CYLEEVpIXCO6dWjDCUqNVREjdF8`.

The environment used a custom deployment branch policy admitting exactly
`main`; it did not use the generic protected-branch selector. GitHub
administrators could bypass the environment. That bypass is a revocable
administrative capability, not part of the routine deployment path.

## Reconciliation runs

The initial scheduled runs used `vps-infra` revision
`0a9a0c1d1c7dd7934876c425cdca64340e10a564`. The final manual reconciliation
used installed and workflow revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b`:

| Run | Started (UTC) | Result and evidence |
|---|---|---|
| [`32076871842`](https://github.com/nclsppr/vps-infra/actions/runs/32076871842) | `2026-08-17T22:38:02Z` | Initial complete success: resolver plus all three deploy jobs. |
| [`32078379931`](https://github.com/nclsppr/vps-infra/actions/runs/32078379931) | `2026-08-17T22:57:55Z` | Complete healthy no-op verification. |
| [`32085096783`](https://github.com/nclsppr/vps-infra/actions/runs/32085096783) | `2026-08-18T00:35:27Z` | Failed closed before any controller call: all SSH requests were rejected after a convergence had rendered an empty deployment `authorized_keys`. No release, transaction, or quarantine changed. |
| [`32086151183`](https://github.com/nclsppr/vps-infra/actions/runs/32086151183) | `2026-08-18T00:52:32Z` | Final complete success after key recovery: resolver `ready=3`; Personal, Papers Empire, and Parkventory accepted by Atlas. |

In the final run, every `Request the exact static deployment` step completed
with `success` and every `Report disabled static deployment` step was skipped.
The jobs took 46 seconds for Personal, 41 seconds for Papers Empire, and 31
seconds for Parkventory. Their logs contained no runtime warning, canonical-HEAD
skip, or controller error. This job-set check is material because an overall
green result can cover only the profiles that the resolver classified as
`ready`.

The empty-key incident came from a deployment variable set to an empty list,
which the then-current role accepted before replacing the complete key file.
Recovery used a new dedicated key, updated the protected external inventory and
GitHub environment secret, disabled the workflow during convergence, proved
the forced-command authentication boundary with the non-mutating command
`deploy auth-proof` (expected parser status `64` and exact malformed-command
message), and only then re-enabled and dispatched reconciliation. The versioned
role now rejects an empty declaration before mutation; the runbook contains the
rotation and total-loss procedure.

## Active immutable tuples

The protected active JSON state and each `current` symlink matched these
values:

### Personal

| Field | Value |
|---|---|
| Source revision | `163b9c9643dd9c54e9b1bb5d558d34a670e28e52` |
| Site | `ghcr.io/nclsppr/personal/site@sha256:98a9e0b8846d74775bf0b6f22dd4519a0f63a6db4b99ed47643cebba55082110` |
| Routes | `ghcr.io/nclsppr/personal/routes@sha256:086d7dc704796808d648922b5156d16d01a8fd891cbd22844092d1cc98902a8f` |
| `current` | `releases/sha256-98a9e0b8846d74775bf0b6f22dd4519a0f63a6db4b99ed47643cebba55082110` |

### Papers Empire

| Field | Value |
|---|---|
| Source revision | `b95f9bdde468aac9d03bd0548c7aa42969e52df7` |
| Site | `ghcr.io/nclsppr/papersempire/site@sha256:1de518ac73ef67549ac1c6352ca63c21ad3f1c00255528c0f2b9e6a0ac375b2e` |
| Routes | `ghcr.io/nclsppr/papersempire/routes@sha256:647742f54a63092764a4788c6b73d680d3efea47b8798c49fea908e7b7f18055` |
| `current` | `releases/sha256-1de518ac73ef67549ac1c6352ca63c21ad3f1c00255528c0f2b9e6a0ac375b2e` |

### Parkventory static demo

| Field | Value |
|---|---|
| Source revision | `db9571cc59d0fcc31c6554af259eda4c29988a6a` |
| Site | `ghcr.io/nclsppr/parkventory-static-site@sha256:43eb75214c888ff28fbc295bfdf64af0b01302aee3ab637d1ed3b939b5903844` |
| Routes | `ghcr.io/nclsppr/parkventory-static-routes@sha256:e893fd9999203c489fb7ac06582ff4cb464a36f567a66d173d3323dfc421431c` |
| `current` | `releases/sha256-43eb75214c888ff28fbc295bfdf64af0b01302aee3ab637d1ed3b939b5903844` |

All three tuples used this reviewed platform integration and Caddy identity:

| Field | Value |
|---|---|
| Integration revision | `bd919bc6754e3fb1a53a80e45c1925fac495e7f2` |
| Integration artifact | `ghcr.io/nclsppr/vps-infra/platform-integration@sha256:c32b085bd4ee586d11e759be09b52b6a044cd56411ea6a600e7e0c2021bbb7a3` |
| Caddy image | `ghcr.io/nclsppr/vps-infra/caddy:sha-755565642f71918134151921f8eae63935951ad6@sha256:1879be5d28d98d49522d74f4fc1bd8c176fd4e35112f757b6639151f3170f11f` |

## Atlas controller and state

The final host convergence used the isolated canonical checkout at exact
`vps-infra` revision `da04a09bfa9788ae8127b63f9f3a6692bef2551b`. Its
result was `ok=355 changed=1 unreachable=0 failed=0 skipped=61`; the sole
change restored the forced-command deployment key.

After the activations and repeated scheduled reconciliation:

- Caddy was active and healthy on the expected immutable image;
- the protected active state matched each complete tuple above;
- `/var/lib/vps-static/transactions` was empty;
- `/var/lib/vps-static/quarantine` was empty;
- the forced-command key file contained exactly one restricted key with the
  fingerprint recorded above;
- the final GitHub run and protected host state matched the same exact tuples.

The same convergence installed the root-owned Compose application controller,
its live gate, schema, and `vps-application-recover.service`. The recovery unit
was `loaded`, inactive after its successful oneshot, with `Result=success` and
`ExecMainStatus=0`. The application active, transaction, and quarantine
directories were empty. This proves installation and recovery readiness, not
application activation. Surplasse and Parkventory remained disabled. The
private `atlas-codex-app-server.service` was also loaded, active, and running;
it exposes its managed private socket, not a public listener.

## Public TLS probes

Strict IPv4 HTTPS probes were repeated from outside Atlas on 2026-08-18. Every
apex and `www` name resolved to A record `137.74.174.163`, and none returned an
AAAA record. Curl used the normal certificate chain and hostname verification,
followed at most one redirect, and reported TLS verification result `0` and
remote address `137.74.174.163` for every request. The same probes also passed
with `--resolve` pinned directly to Atlas:

| Requested name | Final URL | HTTP | Redirects | Remote IP | TLS verify |
|---|---|---:|---:|---|---:|
| `nicolaspieper.com` | `https://nicolaspieper.com/` | `200` | `0` | `137.74.174.163` | `0` |
| `www.nicolaspieper.com` | `https://nicolaspieper.com/` | `200` | `1` | `137.74.174.163` | `0` |
| `papersempire.com` | `https://papersempire.com/` | `200` | `0` | `137.74.174.163` | `0` |
| `www.papersempire.com` | `https://papersempire.com/` | `200` | `1` | `137.74.174.163` | `0` |
| `parkventory.com` | `https://parkventory.com/` | `200` | `0` | `137.74.174.163` | `0` |
| `www.parkventory.com` | `https://parkventory.com/` | `200` | `1` | `137.74.174.163` | `0` |

These probes prove the observed public state on that date. They are not a
substitute for checking the current run, protected Atlas state, and current
public endpoints during a later operation.

## Remaining boundaries

- Static release garbage collection has no reviewed automatic policy.
- A quarantined tuple still requires explicit investigation and a reviewed
  recovery decision.
- Disaster recovery still lacks a selected encrypted off-site PostgreSQL
  backup target.
- The recovery units are ordered before the systemd-managed public edge, but
  Docker can restart the existing `unless-stopped` Caddy container earlier at
  boot. Closing that daemon-level traffic bypass remains platform work.
- The rotated deployment private key is protected locally and in the GitHub
  environment, but a separately proved encrypted off-machine recovery copy is
  still required to avoid another total-loss rotation.
- Dynamic Surplasse and Parkventory activation still requires all blockers in
  [ADR-0010](../decisions/0010-disabled-transactional-application-controller.md).

Follow the
[static reconciliation runbook](../operations/static-release-reconciliation.md)
for routine operation, rollback, recovery, and key rotation.
