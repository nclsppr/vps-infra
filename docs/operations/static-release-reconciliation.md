# Operate automatic static release reconciliation

## Scope

This runbook operates the automatic releases for these three static profiles:

| Profile | Canonical branch | Mode |
|---|---|---|
| `personal` | `main` | `static-site` |
| `papersempire` | `master` | `static-site` |
| `parkventory` | `main` | `temporary-static-demo` |

The workflow is `.github/workflows/deploy-static-releases.yml`. It runs every
ten minutes and also accepts a manual dispatch. The schedule and the dispatch
use the same resolver, immutable artifact contract, SSH identity, Atlas gate,
and transaction state machine. A dispatch does not override a red check, select
an older revision, or accept operator-supplied digests.

This runbook does not authorize a Compose application, a database migration, a
secret change, a DNS change, or a platform release. Surplasse and the
Parkventory React/Java application remain disabled.

## Trust boundary

GitHub Actions selects only the current canonical branch HEAD. It requires all
observed checks to be complete and non-failing, and it requires each configured
check to conclude with `success`. It resolves the matching site and route tags
to immutable digests. Atlas then checks the canonical HEAD again, verifies the
attestations and artifact bytes, proves source ancestry, probes the candidate,
switches `current` atomically, and probes the real edge with strict public TLS.

The `static-production` environment owns the only automated production SSH
identity. Producer repositories do not have a VPS key. Atlas accepts only one
canonical `deploy-static-live` record through a forced command and an
argument-free root gate.

## Enable or suspend reconciliation

The environment must contain these variables:

```text
VPS_STATIC_DEPLOY_ENABLED
VPS_STATIC_DEPLOY_PORT
VPS_STATIC_DEPLOY_USER
```

It must contain these secrets:

```text
VPS_STATIC_HOST
VPS_STATIC_KNOWN_HOSTS
VPS_STATIC_SSH_PRIVATE_KEY
```

List the names and non-secret values without reading a secret:

```bash
gh variable list \
  --repo nclsppr/vps-infra \
  --env static-production
gh secret list \
  --repo nclsppr/vps-infra \
  --env static-production
```

Enable the deploy step only after Atlas has the matching public key, the host
key has been verified through an independent channel, and a reviewed
convergence has installed the static controller:

```bash
gh variable set VPS_STATIC_DEPLOY_ENABLED \
  --repo nclsppr/vps-infra \
  --env static-production \
  --body true
```

Suspend new SSH requests before investigation, key rotation, or maintenance:

```bash
gh variable set VPS_STATIC_DEPLOY_ENABLED \
  --repo nclsppr/vps-infra \
  --env static-production \
  --body false
```

Changing the variable does not cancel a job that already passed its step
condition. Inspect the `production-vps` concurrency group and let the current
job finish, or cancel that exact run only when the incident procedure requires
it. Atlas transaction recovery does not depend on the SSH client remaining
connected.

## Dispatch and inspect a run

Dispatch the current `main` workflow without application or digest inputs:

```bash
gh workflow run deploy-static-releases.yml \
  --repo nclsppr/vps-infra \
  --ref main
```

Find the exact run and wait for completion:

```bash
gh run list \
  --repo nclsppr/vps-infra \
  --workflow deploy-static-releases.yml \
  --limit 10 \
  --json databaseId,event,headSha,status,conclusion,url

gh run watch <run-id> \
  --repo nclsppr/vps-infra \
  --exit-status \
  --interval 10
```

Inspect the job and step set exactly:

```bash
gh run view <run-id> \
  --repo nclsppr/vps-infra \
  --json event,headBranch,headSha,status,conclusion,url,jobs \
  --jq '{event,headBranch,headSha,status,conclusion,url,jobs:[.jobs[] | {name,conclusion,steps:[.steps[] | {name,conclusion}]}]}'
```

Open the run summary and read the resolver table. The table classifies every
profile:

| Status | Meaning | Operator action |
|---|---|---|
| `ready` | The exact HEAD, required checks, site manifest, and route manifest are eligible. | Require the matching deploy job and inspect its result. |
| `pending` | Evidence is incomplete or temporarily unavailable. This includes in-progress checks, a publication race, or a transient GitHub or GHCR failure. | Keep the current Atlas release. Let the next schedule retry. |
| `blocked` | The current HEAD has a red check or structurally invalid evidence. | Fix the producer or contract. Do not bypass the resolver. |
| `disabled` | The reviewed static promotion switch is false. | Expect no deploy job. Enable only through a reviewed contract change. |

### A green run can be partial

The workflow resolves each profile independently. The resolver can complete
successfully while one profile is `pending`, `blocked`, or `disabled`; ready
profiles can still deploy. The overall run can therefore be green without
covering all three sites. A deploy step can also return success after it skips
SSH because the canonical HEAD changed immediately before the request.

For a complete three-site reconciliation, require all of these facts:

1. the resolver summary reports `ready` for all three profiles;
2. the run contains `Deploy personal`, `Deploy papersempire`, and
   `Deploy parkventory`;
3. `Request the exact static deployment` completed successfully in each job;
4. `Report disabled static deployment` was skipped in each job;
5. the logs contain no canonical-HEAD skip, disabled warning, refusal, or
   activation failure;
6. Atlas state and public probes match the selected immutable tuples.

Download and search the exact run log:

```bash
gh run view <run-id> \
  --repo nclsppr/vps-infra \
  --log > /tmp/vps-static-run-<run-id>.log

rg -n \
  'canonical HEAD could not|canonical HEAD changed|VPS_STATIC_DEPLOY_ENABLED is not true|static deployment refused|activation failed' \
  /tmp/vps-static-run-<run-id>.log
```

No match is necessary but not sufficient. Complete the Atlas checks below.
Delete the local log after the investigation if it is no longer required.

## Inspect Atlas state

Use an administrator SSH identity. Do not use the automated deploy identity for
an interactive session. Replace the placeholders with local, ignored values:

```bash
ssh -i /absolute/path/to/admin-key <administrator>@<atlas-host>
```

Read the installed controller revision and recovery state:

```bash
sudo cat /usr/local/share/vps-infra/controller-revision
sudo systemctl is-enabled vps-static-recover.service
sudo systemctl status --no-pager vps-static-recover.service
sudo systemctl status --no-pager vps-public-static-edge.service
```

Inspect each complete active tuple and its matching symlink:

```bash
for application in personal papersempire parkventory; do
  sudo python3 -m json.tool \
    "/var/lib/vps-static/active/${application}.json"
  sudo readlink "/srv/www/${application}/current"
done
```

The `current` target must be exactly
`releases/sha256-<site-manifest-digest>`. The source revision, site reference,
route reference, integration revision, integration reference, and Caddy image
in the protected JSON state must match the GitHub candidate. Do not accept only
the symlink as release evidence.

Inspect transaction and quarantine filenames without changing them:

```bash
sudo find /var/lib/vps-static/transactions \
  -mindepth 1 -maxdepth 1 -type f -printf '%f\n'
sudo find /var/lib/vps-static/quarantine \
  -mindepth 1 -maxdepth 1 -type f -printf '%f\n'
```

Both commands normally print nothing. A transaction requires recovery. A
quarantine record requires an investigation of that exact immutable tuple. Do
not delete either record to make a retry pass.

Inspect recent activation and recovery messages:

```bash
sudo journalctl \
  -u 'vps-static-live-*' \
  -u vps-static-recover.service \
  --since '2 hours ago' \
  --no-pager
```

An exact no-op reports `static release already active and healthy with the
complete immutable tuple`. A new activation reports `static release activated
after strict live HTTPS probe`.

## Public probes

Run strict IPv4 HTTPS probes from outside Atlas:

```bash
for url in \
  https://nicolaspieper.com/ \
  https://www.nicolaspieper.com/ \
  https://papersempire.com/ \
  https://www.papersempire.com/ \
  https://parkventory.com/ \
  https://www.parkventory.com/
do
  curl \
    --ipv4 \
    --proto '=https' \
    --tlsv1.2 \
    --fail \
    --silent \
    --show-error \
    --location \
    --max-redirs 1 \
    --output /dev/null \
    --write-out '%{url_effective} %{http_code} %{num_redirects} %{ssl_verify_result}\n' \
    "$url"
done
```

Each apex must return `200` with zero redirects and TLS verification result `0`.
Each `www` name must follow exactly one redirect to its apex, return `200`, and
have TLS verification result `0`.

## Safe content rollback

Rollback is a new producer revision, not an Atlas filesystem operation:

1. suspend reconciliation if the current content presents an active incident;
2. revert the bad producer change on a review branch;
3. merge the revert so the canonical branch has a new descendant commit;
4. wait for every required check and both immutable artifacts for that new SHA;
5. enable reconciliation and dispatch it, or wait for the next schedule;
6. verify the complete run, Atlas state, and public probes.

Do not force-push the canonical branch, move an OCI tag, request an old SHA, or
manually repoint `/srv/www/<application>/current`. The ancestry gate rejects a
history rollback. A normal revert is a new descendant and remains auditable.

## Recovery and emergency boundary

Suspend new reconciliation before recovery. Capture the active JSON files,
transaction filenames, quarantine filenames, symlink targets, service status,
and journal. Then invoke only the installed recovery entry point:

```bash
sudo systemctl start vps-static-recover.service
sudo systemctl status --no-pager vps-static-recover.service
sudo journalctl -u vps-static-recover.service -n 200 --no-pager
```

Recovery validates the protected inventory and release tree before it restores
or commits a managed target. It removes only bounded controller residue. It
does not make an unknown target trusted.

If recovery fails, do not edit `current`, active state, the transaction, or
quarantine by hand. Stop the public edge only to contain an availability or
integrity incident, preserve the state for diagnosis, and prepare a reviewed
controller or state-repair change. A manual service stop is emergency
containment; it is not a release rollback or a new source of truth.

## Rotate the automated SSH key

Use an overlap rotation. Never print the private key or put it in Git.

1. suspend reconciliation and wait for the current `production-vps` job;
2. create a new Ed25519 key in a private temporary location and record its
   public fingerprint;
3. add the new public key to the ignored Ansible value
   `vps_deploy_authorized_keys` while retaining the proven old key;
4. run reviewed convergence and verify that both public keys have the forced
   `restrict` entry on Atlas;
5. replace only the `VPS_STATIC_SSH_PRIVATE_KEY` environment secret;
6. enable reconciliation, dispatch one run, and prove all three no-ops;
7. suspend reconciliation again, remove the old public key from the Ansible
   value, converge, and prove that the retired key is rejected;
8. enable reconciliation and destroy every local copy of the retired private
   key according to the secret-handling procedure.

Rotate `VPS_STATIC_KNOWN_HOSTS` separately only after the Atlas host key has
been verified through the OVH console or another independent trusted channel.
Do not use opportunistic `ssh-keyscan` output as production trust.

## Related records

- [ADR-0008](../decisions/0008-automatic-static-release-reconciliation.md)
- [Static rollout evidence on 2026-08-18](../evidence/2026-08-18-static-reconciliation-rollout.md)
- [Deployment architecture](../deployment.md)
- [Release controller contract](../../scripts/README.md)
