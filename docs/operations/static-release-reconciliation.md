# Operate automatic static release reconciliation

## Scope

This runbook operates the automatic releases for these three static profiles:

| Profile | Canonical branch | Mode |
|---|---|---|
| `personal` | `main` | `static-site` |
| `papersempire` | `main` | `static-site` |
| `parkventory` | `main` | `temporary-static-demo` |

The workflow is `.github/workflows/deploy-static-releases.yml`. It is scheduled
every ten minutes and also accepts a manual dispatch. GitHub schedules are
best-effort and can run late; the ten-minute expression is not a delivery-time
guarantee. The schedule and the dispatch use the same resolver, immutable
artifact contract, SSH identity, Atlas gate, and transaction state machine. A
dispatch does not override a red check, select an older revision, or accept
operator-supplied digests.

This runbook does not authorize a Compose application, a database migration, a
secret change, a DNS change, or a platform release. Surplasse and the
Parkventory React/Java application remain disabled.

## Routine release from a producer

A normal content release starts in the producer repository:

| Profile | Producer procedure | Merge branch |
|---|---|---|
| `personal` | [Comment déployer sur Atlas](https://github.com/nclsppr/personal#comment-déployer-sur-atlas) | `main` |
| `papersempire` | [Deploy to Atlas](https://github.com/nclsppr/papersempire#deploy-to-atlas) | `main` |
| `parkventory` | [Parkventory runbook](https://github.com/nclsppr/parkventory/blob/main/RUNBOOK.md) | `main` |

Open a producer PR, wait for `Validate VPS release`, merge it, then require the
merged revision's producer workflow `VPS release` to succeed. That workflow
publishes immutable OCI artifacts and attestations; it has no VPS credential
and does not deploy by itself. The next best-effort central schedule resolves
the canonical producer HEAD and activates it only while every gate remains
valid. Use the dispatch below when an immediate reconciliation is needed.

Do not edit `vps-infra` or copy a digest into Git for a routine content release.
A platform change is required only when the reviewed deployment contract,
required checks, Caddy integration, profile enablement, controller, or security
policy changes.

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

Verify that the environment admits exactly the `main` branch. The subshell
stops on the first failed assertion; the last command also displays the
administrative bypass setting so it cannot be mistaken for an immutable
repository invariant:

```bash
(
  set -euo pipefail

  test "$(gh api \
    repos/nclsppr/vps-infra/environments/static-production \
    --jq '.deployment_branch_policy.custom_branch_policies')" = true || {
    echo "static-production does not use custom branch policies" >&2
    exit 1
  }
  test "$(gh api \
    repos/nclsppr/vps-infra/environments/static-production \
    --jq '.deployment_branch_policy.protected_branches')" = false || {
    echo "static-production unexpectedly uses the protected-branch selector" >&2
    exit 1
  }
  test "$(gh api \
    repos/nclsppr/vps-infra/environments/static-production/deployment-branch-policies \
    --jq '[.branch_policies[] | [.type, .name]] == [["branch", "main"]]')" = true || {
    echo "static-production does not admit exactly the main branch" >&2
    exit 1
  }
  gh api \
    repos/nclsppr/vps-infra/environments/static-production \
    --jq '{can_admins_bypass,protection_rules,deployment_branch_policy}'
)
```

The environment has no required reviewer because that would make scheduled
deployment interactive. GitHub administrators can currently bypass environment
protection; treat that as a revocable administrative boundary, not as an
alternate routine deployment path.

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

Every `site.yml` convergence must receive a non-empty
`vps_deploy_authorized_keys` list. Before any role runs, the playbook parses
every key cryptographically even in check mode. The deploy role then requires
the desired set to retain at least one installed key identity, except on a true
first installation. Its template independently refuses to render without that
guard. Never use an empty value as a way to suspend deployments. Keep
`vps_deploy_key_recovery_nonce` empty during normal operation, suspend with the
environment variable above, then rotate or retire keys through the explicit
procedure below.

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

run_id=32000000000 # replace with the numeric ID selected above
gh run watch "$run_id" \
  --repo nclsppr/vps-infra \
  --exit-status \
  --interval 10
```

Inspect the job and step set exactly:

```bash
gh run view "$run_id" \
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
(
  set -euo pipefail

  log_file=$(mktemp "${TMPDIR:-/tmp}/vps-static-run.XXXXXX")
  readonly log_file
  chmod 600 "$log_file"
  trap 'rm -f -- "$log_file"' EXIT

  gh run view "$run_id" \
    --repo nclsppr/vps-infra \
    --log > "$log_file"

  if rg -n \
    'canonical HEAD could not|canonical HEAD changed|VPS_STATIC_DEPLOY_ENABLED is not true|static deployment refused|activation failed' \
    "$log_file"; then
    echo "The run contains a deployment skip, refusal, or activation failure." >&2
    exit 1
  else
    search_status=$?
    if ((search_status != 1)); then
      echo "The run log could not be searched safely (rg status ${search_status})." >&2
      exit "$search_status"
    fi
  fi
)
```

No match is necessary but not sufficient. Complete the Atlas checks below.
The isolated subshell deletes its private local log without replacing a caller's
existing trap.

## Inspect Atlas state

Use an administrator SSH identity. Do not use the automated deploy identity for
an interactive session. Replace the examples with local, ignored values:

```bash
atlas_host=atlas.example.invalid # replace with the verified Atlas host or IP
atlas_port=22
administrator=ubuntu # replace with the reviewed administrator account
admin_key=/absolute/path/to/admin-key
known_hosts=/absolute/path/to/verified-known-hosts

ssh \
  -p "$atlas_port" \
  -i "$admin_key" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$known_hosts" \
  "${administrator}@${atlas_host}"
```

Read the installed controller revision and recovery state:

```bash
sudo cat /usr/local/share/vps-infra/controller-revision
sudo systemctl is-enabled vps-static-recover.service
sudo systemctl show vps-static-recover.service \
  --property=LoadState,ActiveState,Result,ExecMainCode,ExecMainStatus
sudo systemctl status --no-pager vps-public-static-edge.service
```

`vps-static-recover.service` is a oneshot without `RemainAfterExit`; after a
successful run its `ActiveState` may be `inactive`. Require `Result=success`,
and `ExecMainStatus=0` instead of treating inactive as a failure. Some systemd
versions display the no-longer-running `ExecMainCode` as numeric `0`. The
systemd edge unit is ordered after recovery, but Docker can restart
the existing `unless-stopped` Caddy container earlier when the daemon starts.
This daemon-level bypass is a documented remaining hardening boundary.

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

Run strict IPv4 HTTPS probes from outside Atlas. Obtain `atlas_ipv4` from the
independently verified Atlas inventory or provider console, not from the DNS
record being tested. This checks public DNS separately, rejects an unexpected
AAAA record, records the actual remote IP, and then repeats the same TLS/HTTP
contract with both names pinned directly to Atlas:

```bash
atlas_ipv4=REPLACE_WITH_CONSOLE_VERIFIED_ATLAS_IPV4
failures=0

check_static_host() {
  local host=$1
  local apex=$2
  local redirects=$3
  local a_records aaaa_records expected mode result
  local -a curl_args

  a_records=$(dig +short A "$host" | awk '/^[0-9]+\./' | sort -u)
  aaaa_records=$(dig +short AAAA "$host" | awk '/:/' | sort -u)
  if [ "$a_records" != "$atlas_ipv4" ] || [ -n "$aaaa_records" ]; then
    printf 'DNS mismatch for %s: A=%s AAAA=%s\n' \
      "$host" "$a_records" "$aaaa_records" >&2
    return 1
  fi

  expected="https://${apex}/|200|${redirects}|0|${atlas_ipv4}"
  for mode in dns direct-atlas; do
    curl_args=(
      --disable
      --ipv4
      --proto '=https'
      --tlsv1.2
      --fail
      --silent
      --show-error
      --location
      --max-redirs 1
      --connect-timeout 10
      --max-time 30
      --output /dev/null
      --write-out '%{url_effective}|%{http_code}|%{num_redirects}|%{ssl_verify_result}|%{remote_ip}'
    )
    if [ "$mode" = direct-atlas ]; then
      curl_args+=(--resolve "${apex}:443:${atlas_ipv4}")
      if [ "$host" != "$apex" ]; then
        curl_args+=(--resolve "${host}:443:${atlas_ipv4}")
      fi
    fi

    if ! result=$(curl "${curl_args[@]}" "https://${host}/"); then
      printf '%s probe failed for %s\n' "$mode" "$host" >&2
      return 1
    fi
    if [ "$result" != "$expected" ]; then
      printf '%s mismatch for %s: %s != %s\n' \
        "$mode" "$host" "$result" "$expected" >&2
      return 1
    fi
  done
}

check_static_host nicolaspieper.com nicolaspieper.com 0 || failures=$((failures + 1))
check_static_host www.nicolaspieper.com nicolaspieper.com 1 || failures=$((failures + 1))
check_static_host pieper.fr nicolaspieper.com 1 || failures=$((failures + 1))
check_static_host www.pieper.fr nicolaspieper.com 1 || failures=$((failures + 1))
check_static_host nicolas.pieper.fr nicolaspieper.com 1 || failures=$((failures + 1))
check_static_host www.nicolas.pieper.fr nicolaspieper.com 1 || failures=$((failures + 1))
check_static_host papersempire.com papersempire.com 0 || failures=$((failures + 1))
check_static_host www.papersempire.com papersempire.com 1 || failures=$((failures + 1))
check_static_host parkventory.com parkventory.com 0 || failures=$((failures + 1))
check_static_host www.parkventory.com parkventory.com 1 || failures=$((failures + 1))
test "$failures" -eq 0
```

Each apex must return `200` with zero redirects and TLS verification result `0`.
Each alias must follow exactly one HTTPS redirect to its canonical apex, return
`200`, and have TLS verification result `0`. For the Personal aliases, the
Ansible runtime probe also requires a direct `308` from HTTP and HTTPS for a path
with a query. Both the DNS-routed and direct-Atlas probes must report the
independently verified Atlas IPv4 as `remote_ip`.

Before the `.fr` DNS change, the pre-cutover edge intentionally has no HTTPS
site block for those pending aliases. Probe their path-preserving HTTP redirects
directly on Atlas instead:

```bash
for host in pieper.fr www.pieper.fr nicolas.pieper.fr www.nicolas.pieper.fr; do
  curl --disable --silent --show-error --output /dev/null \
    --dump-header - \
    --header "Host: ${host}" \
    "http://${atlas_ipv4}/__vps_redirect_probe__?source=operator-precutover"
done
```

Each response must be `308` with an exact `Location` of
`https://nicolaspieper.com/__vps_redirect_probe__?source=operator-precutover`.
Run the complete valid-TLS probe set above only after DNS activation and the
bounded certificate issuance step.

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
sudo systemctl show vps-static-recover.service \
  --property=LoadState,ActiveState,Result,ExecMainCode,ExecMainStatus
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
   value, keep `vps_deploy_key_recovery_nonce` empty, converge, and prove that
   the retired key is rejected;
8. enable reconciliation and destroy every local copy of the retired private
   key according to the secret-handling procedure.

If every deploy key has already been lost, overlap is impossible. Suspend the
environment first and preserve the failed run and Atlas SSH journal. Generate
one replacement Ed25519 key and a separate recovery nonce without printing
either private value:

```bash
recovery_nonce=$(openssl rand -hex 32)
test "${#recovery_nonce}" -eq 64
```

In the ignored Ansible variable file, declare exactly the one replacement
public key in `vps_deploy_authorized_keys` and place the 64 lowercase
hexadecimal characters in `vps_deploy_key_recovery_nonce`. Use the independently
verified administrator identity to run the reviewed convergence exactly once.
An optional Ansible `--check` run still performs the cryptographic key parsing
and overlap/recovery guard evaluation, but it neither consumes the nonce nor
replaces `authorized_keys`; only the subsequent non-check convergence consumes
it and runs the point-of-mutation template validator.
Atlas stores only a SHA-256-derived marker, bound to the controller revision and
the installed and desired key identities, before it replaces the file. The same
nonce cannot be reused. If convergence fails after nonce consumption, preserve
the state, investigate, and generate a different nonce for a reviewed retry.

Immediately restore `vps_deploy_key_recovery_nonce: ""` in the ignored file.
A nonce left in place makes the next convergence fail closed. Verify the new
fingerprint and the exact forced `restrict` prefix on Atlas. Then prove the new
private key reaches the forced-command parser without invoking `sudo`, a deploy
controller, or a live mutation. Reuse the independently verified `atlas_host`,
`atlas_port`, and `known_hosts` values from the Atlas inspection section:

```bash
deploy_user=deploy
deploy_identity_file=/absolute/path/to/new-deploy-identity

(
  set -u

  proof_status=0
  proof_output=$(
    ssh -T \
      -p "$atlas_port" \
      -i "$deploy_identity_file" \
      -o BatchMode=yes \
      -o PreferredAuthentications=publickey \
      -o PasswordAuthentication=no \
      -o KbdInteractiveAuthentication=no \
      -o IdentitiesOnly=yes \
      -o IdentityAgent=none \
      -o ControlMaster=no \
      -o ControlPath=none \
      -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile="$known_hosts" \
      "${deploy_user}@${atlas_host}" \
      'deploy auth-proof' 2>&1
  ) || proof_status=$?

  expected='forced-command parser: malformed deploy command'
  if [ "$proof_status" -eq 255 ]; then
    echo "SSH transport or public-key authentication failed." >&2
    exit 1
  fi
  if [ "$proof_status" -ne 64 ] || \
     ! printf '%s\n' "$proof_output" | grep -Fqx -- "$expected"; then
    echo "The key did not reach the expected non-mutating ForceCommand rejection." >&2
    exit 1
  fi
  echo "Public-key authentication and the forced-command boundary are proved."
)
```

Exit `64` with that exact parser line means authentication succeeded and the
deliberately malformed SHA was rejected before dispatch. Exit `255` means SSH
transport or authentication failed; do not treat it as a successful boundary
probe. Only after this proof, replace the GitHub environment private-key secret,
re-enable reconciliation, dispatch a run, and prove all three no-ops. Do not
append a key manually without recording the same desired value in the ignored
Ansible variables; the next convergence would otherwise recreate the outage.

Rotate `VPS_STATIC_KNOWN_HOSTS` separately only after the Atlas host key has
been verified through the OVH console or another independent trusted channel.
Do not use opportunistic `ssh-keyscan` output as production trust.

## Related records

- [ADR-0008](../decisions/0008-automatic-static-release-reconciliation.md)
- [Static rollout evidence on 2026-08-18](../evidence/2026-08-18-static-reconciliation-rollout.md)
- [Deployment architecture](../deployment.md)
- [Release controller contract](../../scripts/README.md)
