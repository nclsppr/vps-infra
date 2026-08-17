# Static reconciliation rollout evidence - 2026-08-18

## Scope and evidence boundary

This record captures the controlled activation and repeated no-op verification
of the automatic static release path on Atlas. The GitHub runs occurred on
2026-08-17 UTC and on 2026-08-18 in Europe/Paris.

This evidence applies only to Personal, Papers Empire, and the temporary static
Parkventory demo. It does not prove that the Compose application controller was
converged on Atlas. It does not authorize Surplasse or the Parkventory
React/Java application. Both application entries remained `enabled: false`.

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

## Reconciliation runs

Both scheduled runs used `vps-infra` revision
`0a9a0c1d1c7dd7934876c425cdca64340e10a564`. Each run completed successfully
and contained the resolver plus all three deploy jobs:

| Run | Started (UTC) | Required jobs |
|---|---|---|
| [`32076871842`](https://github.com/nclsppr/vps-infra/actions/runs/32076871842) | `2026-08-17T22:38:02Z` | resolver, Personal, Papers Empire, Parkventory |
| [`32078379931`](https://github.com/nclsppr/vps-infra/actions/runs/32078379931) | `2026-08-17T22:57:55Z` | resolver, Personal, Papers Empire, Parkventory |

In each deploy job, `Request the exact static deployment` completed with
`success` and `Report disabled static deployment` was skipped. The second run
proved the repeated exact candidates as healthy no-ops on Atlas. This job-set
check is material because an overall green result can cover only the profiles
that the resolver classified as `ready`.

## Active immutable tuples

The protected active JSON state and each `current` symlink matched these
values:

### Personal

| Field | Value |
|---|---|
| Source revision | `328b535b934560fcaf6324383440a3c2a60641c4` |
| Site | `ghcr.io/nclsppr/personal/site@sha256:61b478b86fd01cc73b1a080fd2a581256032bbb109ee2a47ef155a1dc09d747e` |
| Routes | `ghcr.io/nclsppr/personal/routes@sha256:7109f8e15853b15948eaef0c920e5e0f1265d6d74710278b456b4600163f58be` |
| `current` | `releases/sha256-61b478b86fd01cc73b1a080fd2a581256032bbb109ee2a47ef155a1dc09d747e` |

### Papers Empire

| Field | Value |
|---|---|
| Source revision | `17db1b57414c3c611ce73637d6864dce76cad55b` |
| Site | `ghcr.io/nclsppr/papersempire/site@sha256:005dc7f7ab74e573951ed544585ff4e303b142d4296fb0de26f005592f2d69b6` |
| Routes | `ghcr.io/nclsppr/papersempire/routes@sha256:9ae43cc21ec9f0de4e15e5cfe14bf9524d4c54bfd2a3a2edebdbe6b9f545294e` |
| `current` | `releases/sha256-005dc7f7ab74e573951ed544585ff4e303b142d4296fb0de26f005592f2d69b6` |

### Parkventory static demo

| Field | Value |
|---|---|
| Source revision | `583e0e2b63701097aa4894ecc4fb3de8ad325346` |
| Site | `ghcr.io/nclsppr/parkventory-static-site@sha256:eb4596ac08e76bf59dc0c1ed6982f8cad6a25e98bc09b507790a78107e41553c` |
| Routes | `ghcr.io/nclsppr/parkventory-static-routes@sha256:47673d6906494ed128616357efe305e7be372e06022f4a2a794dcdc164ecbe7a` |
| `current` | `releases/sha256-eb4596ac08e76bf59dc0c1ed6982f8cad6a25e98bc09b507790a78107e41553c` |

All three tuples used this reviewed platform integration and Caddy identity:

| Field | Value |
|---|---|
| Integration revision | `bd919bc6754e3fb1a53a80e45c1925fac495e7f2` |
| Integration artifact | `ghcr.io/nclsppr/vps-infra/platform-integration@sha256:c32b085bd4ee586d11e759be09b52b6a044cd56411ea6a600e7e0c2021bbb7a3` |
| Caddy image | `ghcr.io/nclsppr/vps-infra/caddy:sha-755565642f71918134151921f8eae63935951ad6@sha256:1879be5d28d98d49522d74f4fc1bd8c176fd4e35112f757b6639151f3170f11f` |

## Atlas controller and state

The static rollout converged exact `vps-infra` revision
`27a8064400198611214d18853a87a606f349a2ae`. The convergence result was
`ok=327 changed=4 unreachable=0 failed=0 skipped=71`.

After the activations and repeated scheduled reconciliation:

- Caddy was active and healthy on the expected immutable image;
- the protected active state matched each complete tuple above;
- `/var/lib/vps-static/transactions` was empty;
- `/var/lib/vps-static/quarantine` was empty;
- the Atlas journal reported each exact tuple as already active and healthy on
  the repeated run.

Revision `0a9a0c1d1c7dd7934876c425cdca64340e10a564` merged the disabled
transactional application controller after the live static controller revision.
No convergence of that application-controller revision was proved during this
rollout. Repository availability is not host installation evidence.

## Public TLS probes

Strict IPv4 HTTPS probes were repeated from outside Atlas on 2026-08-18. Curl
used the normal certificate chain and hostname verification, followed at most
one redirect, and reported TLS verification result `0` for every request:

| Requested name | Final URL | HTTP | Redirects | TLS verify |
|---|---|---:|---:|---:|
| `nicolaspieper.com` | `https://nicolaspieper.com/` | `200` | `0` | `0` |
| `www.nicolaspieper.com` | `https://nicolaspieper.com/` | `200` | `1` | `0` |
| `papersempire.com` | `https://papersempire.com/` | `200` | `0` | `0` |
| `www.papersempire.com` | `https://papersempire.com/` | `200` | `1` | `0` |
| `parkventory.com` | `https://parkventory.com/` | `200` | `0` | `0` |
| `www.parkventory.com` | `https://parkventory.com/` | `200` | `1` | `0` |

These probes prove the observed public state on that date. They are not a
substitute for checking the current run, protected Atlas state, and current
public endpoints during a later operation.

## Remaining boundaries

- Static release garbage collection has no reviewed automatic policy.
- A quarantined tuple still requires explicit investigation and a reviewed
  recovery decision.
- Disaster recovery still lacks a selected encrypted off-site PostgreSQL
  backup target.
- Dynamic Surplasse and Parkventory activation still requires all blockers in
  [ADR-0010](../decisions/0010-disabled-transactional-application-controller.md).

Follow the
[static reconciliation runbook](../operations/static-release-reconciliation.md)
for routine operation, rollback, recovery, and key rotation.
