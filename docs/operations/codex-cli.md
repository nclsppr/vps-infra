# Operate Codex CLI on Atlas

## Installed boundary

Host convergence installs Codex CLI 0.147.0 as an interactive, unprivileged
operator tool. It has no persistent service and opens no port. The supported
launcher admits at most one Codex session at a time through the fixed
`atlas-codex-session` unit. That unit runs below `atlas-codex.slice`, so CPU,
memory, and task limits are also enforced in aggregate at the slice boundary.

The installed paths are:

```text
/opt/codex/releases/<version>-<target>/
/opt/codex/current
/usr/local/bin/codex
/usr/local/bin/codex-code-mode-host
/etc/codex/requirements.toml
/var/lib/vps-infra/codex-storage.ext4
/srv/codex/home/.codex/config.toml
/srv/codex/workspaces/
```

All persistent Codex state, including its home, credentials, session data, and
workspaces, lives on the dedicated `/srv/codex` ext4 filesystem. The filesystem
uses a root-owned loop backing file with a fixed capacity of 6 GiB. Filling it
can make Codex fail, but cannot consume the remainder of the host filesystem.

The package comes from the exact versioned OpenAI release URL. Ansible checks
the archive digest, the complete extracted file inventory, the package
metadata, every executable digest, root ownership, and `codex --version`
before switching `current` atomically. The standalone package includes pinned
`bwrap`, `rg`, and `zsh` executables. Atlas installs the Ubuntu `bubblewrap`
package and makes `/usr/bin/bwrap` the runtime sandbox executable. This path
matches the distribution AppArmor profile. The role rejects a setuid binary,
unexpected ownership or mode, and any file capability before it starts Codex.
It does not disable Ubuntu user namespace restrictions or grant network
administration capabilities. The packaged `bwrap` remains part of the verified
release inventory but is not placed on the runtime `PATH`.

The `codex` account has no direct SSH login, sudo, Docker group, production
repository access, controller access, or secret access. The supported session
also makes `/etc/vps`, all of `/srv/vps`, administrator homes, Docker state,
and controller state inaccessible in its mount namespace, including files
that would otherwise be world-readable. Use the existing administrator SSH
identity, then deliberately cross into the bounded account:

```bash
ssh <administrator-alias>
sudo atlas-codex
```

The release executables are `root:codex 0750`, and the public command wrappers
refuse ordinary execution outside `atlas-codex.slice`. This makes the launcher
the enforced entry point for the unprivileged `codex` account. The SSH
administrator retains passwordless root sudo and is therefore part of the
host trust root: root can deliberately bypass any local launcher or sandbox.
The boundary limits a Codex session; it does not constrain a malicious or
compromised administrator.

Do not add `codex` to `AllowUsers`, install an SSH key in its home, or grant it
sudo or Docker access. A future production mutation must use a separate,
root-owned, argument-bounded helper reviewed through its own decision record.
Do not substitute `sudo -iu codex`: sudo retains the administrator cgroup, so
that shell would miss the CPU, memory, task, lifetime, temporary filesystem,
and service sandbox applied by `atlas-codex`.

## First authentication

Convergence never authenticates Codex, accepts an OpenAI credential, or copies
one from another machine. From the `codex` shell, start the official headless
device flow:

```bash
codex login --device-auth
```

Managed requirements permit ChatGPT authentication only. The role executes a
negative runtime probe proving that API-key login is rejected before publishing
the policy.

Codex prints a URL and a short one-time code. Open the URL on the operator
workstation, authenticate there, and enter the code. Do not paste an API key,
access token, `auth.json`, or browser cookie into a shell command, Ansible
variable, Git file, issue, pull request, or chat.

After the browser confirms the device, verify the session:

```bash
codex login status
stat -c '%U:%G %a %n' /srv/codex/home/.codex/auth.json
```

The credential file must remain `codex:codex 600` below `0700` home and state
directories. Treat all of `/srv/codex/home/.codex` as sensitive, not only
`auth.json`. `codex logout` removes the locally cached credential when access
should end.

The client itself needs outbound HTTPS and WebSocket access to OpenAI. Commands
launched by the model remain inside the managed workspace profile with command
network access disabled. `approval_policy = "never"` means a session cannot
request or receive an escape from that boundary. Web search and external
integrations are also disabled. Loosening a managed boundary requires a
reviewed infrastructure change, not an interactive approval or CLI flag.

## Prepare work

Place only disposable, explicitly approved material below
`/srv/codex/workspaces`. The secret filename globs in managed policy are a
defense-in-depth check, not a secret-management system and not a guarantee that
every sensitive filename will be recognized. The primary rule is that no
credential, private key, production secret, database dump, Docker credential,
protected infrastructure mirror, or live application state enters a Codex
workspace.

Codex on Atlas is not a CI runner and must not build or publish application
releases. Start it from the selected workspace:

```bash
cd /srv/codex/workspaces/<approved-workspace>
codex
```

The managed policy permits workspace edits but forbids all approval escalation.
It also denies command network access, danger-full access, MCP servers, apps,
plugins, remote control, browser control, and unmanaged hooks.

Each supported session receives private `/tmp` and `/var/tmp` tmpfs mounts.
Each mount has a 512 MiB size ceiling and `nosuid,nodev,noexec`; allocated tmpfs
pages count against the session and aggregate 3 GiB `MemoryMax`. The fixed unit
name prevents a second supported session from starting concurrently.

Stop the active session before host convergence or a Codex upgrade. The role
acquires a fixed-name transient systemd convergence lease before its first
host write. The lease program is passed inline by the controller, so two
different infrastructure revisions cannot overwrite shared lock code. It is
bound to the persistent SSH controller process by both PID and process start
time. Each controller also has a kernel-generated UUID. The UUID selects a
release signal in a root-only systemd runtime directory and identifies the
lease in its systemd description. A controller can signal only its own lease.
It never stops the shared unit name. A competing convergence fails before
mutation. If its controller disappears, the lease exits when that SSH process
disappears. The lease also has a 24-hour maximum lifetime.

Behind that controller lease, the role
persistently masks the fixed session unit, proves it is strictly inactive,
then snapshots every active policy, launcher, wrapper, slice, and release
selector surface before publication. It removes the mask only after a fully
verified activation or an exact verified rollback once publication starts. A
failure before publication can safely release the mask because no active
surface has changed. An interrupted published transaction is recovered on the
next convergence; incomplete recovery remains masked.

## Sensitive state and capacity

`history.persistence = "none"` prevents `history.jsonl` persistence. It does
not remove session or rollout records, archived session state, credentials,
caches, or workspace files. Any local Codex state may contain prompts, model
responses, command output, paths, or source text and must be handled as
sensitive.

Monitor the bounded filesystem from the administrator account without printing
file contents:

```bash
sudo df -h /srv/codex
sudo du -xhd1 /srv/codex/home/.codex /srv/codex/workspaces
sudo systemctl status atlas-codex-session.service atlas-codex.slice --no-pager
```

First allocation also requires the 6 GiB image plus 10 GiB of free host disk
reserve. If this preflight fails, free host space through a reviewed cleanup;
do not lower the reserve ad hoc.

There is intentionally no automatic retention or deletion job. Before any
cleanup, confirm `atlas-codex-session.service` is inactive, identify exact leaf
files or workspace directories with size-only listings, decide what must be
retained, and copy approved outputs to their proper destination. Delete only
the explicitly reviewed absolute paths as `codex`. Never use a wildcard, never
delete `/srv/codex`, and never remove the loop backing file during ordinary
operation. Run `codex logout` separately when the cached OpenAI credential must
be removed. Normal convergence preserves credentials, session records,
workspace content, and the loop backing file; it still reconciles the managed
policy and runtime surfaces.

## Verification

Run host convergence from the trusted controller after the change is merged to
`main`:

```bash
make converge \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
make converge-check \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

The role proves the version, the loop-backed storage identity and mount
options, sandbox startup inside the bounded systemd unit, exact primary group,
and inability of both the outer session and managed command sandbox to read or
traverse protected host paths. Lease, mask, snapshot, staging, and probe
operations are temporary safety bookkeeping and do not report durable drift;
a successful second normal convergence therefore reports `changed=0`. For a
manual verification, enter through the supported launcher and run:

```bash
sudo atlas-codex
id
codex --version
codex login status
test ! -r /etc/vps/secrets
test ! -r /srv/vps/repository
test ! -r /var/run/docker.sock
sudo -n true
```

On Ubuntu, `command -v bwrap` inside that session must return
`/usr/bin/bwrap`. This keeps the distribution AppArmor boundary active.

The final command must fail. Never print `auth.json` or session files while
diagnosing a login.

## Upgrade and rollback

Never run `codex update`, `npm install -g`, or an installer piped from the
network on Atlas. Upgrade through one pull request that changes the exact
version, archive digests, executable digests, and expected package inventory.
The role validates the new release, including a sandbox smoke test using the
packaged `bwrap`, before the active symlink changes and keeps older release
directories for rollback.

See [ADR-0005](../decisions/0005-dedicated-codex-cli-account.md) for the trust
boundary and accepted tradeoffs.
