# ADR-0005: Run Codex CLI through a dedicated unprivileged account

## Status

Accepted on 13 August 2026. ADR-0006 amends the persistent-service and remote
entry-point parts of this decision.

## Context

Codex CLI is useful for interactive diagnosis and bounded work on Atlas. It
also executes model-proposed commands. Installing it under the administrator
account would therefore expose unrestricted sudo, the Docker control socket,
production secrets, the infrastructure mirror, and every live release to one
interactive agent session.

The production host is not a build runner. Adding Node.js, npm, a persistent
agent service, or an application checkout would contradict the existing host
and build boundaries. The runtime also writes credentials, sessions, rollout
records, caches, and workspace files, so both access and persistent capacity
need explicit limits.

## Decision

Ansible installs the official standalone Codex CLI package with these controls:

1. The version and architecture-specific archive SHA-256 values are fixed in
   Git. Every executable inside the package has a second pinned SHA-256.
2. The archive is downloaded from the versioned OpenAI release path, extracted
   into a root-owned staging directory, completely inventoried, and executed
   as the runtime account before an atomic release switch. The standalone
   artifact includes a pinned `bwrap`, but Atlas selects `/usr/bin/bwrap` from
   the Ubuntu `bubblewrap` package. This executable matches Ubuntu's AppArmor
   profile for unprivileged user namespace sandbox setup. Git fixes the exact
   package version, architecture-specific package archive digest, and installed
   executable digest. The package remains held between reviewed updates. The
   role and launcher validate the executable digest, owner, mode, setuid state,
   and file capabilities before use. Once the Codex launcher exists, ordinary
   convergence refuses any package version change because package state is
   outside the Codex activation snapshot. A separate package migration must
   capture and prove a restorable prior package before changing this boundary.
3. The `codex` system account has a locked password, no SSH key, no direct SSH
   login, no sudo rule, no supplementary group, and no Docker socket access.
4. The account owns only `/srv/codex/home` and `/srv/codex/workspaces`. Both are
   on a dedicated ext4 loop filesystem backed by the root-owned
   `/var/lib/vps-infra/codex-storage.ext4` file with a fixed 6 GiB capacity. The
   supported systemd session makes `/etc/vps`, `/srv/vps`, administrator homes,
   Docker state, and production controller state inaccessible in its mount
   namespace, including files that would otherwise be world-readable.
   Initial allocation requires an additional 10 GiB host free-space reserve.
5. Root owns `/etc/codex/requirements.toml`. It permits only
   `approval_policy = "never"`, a read-only profile, and one workspace profile.
   A session cannot approve or request escalation. Command network access,
   danger-full access, web search, MCP servers, apps, plugins, browser control,
   Computer Use, and unmanaged hooks are disabled. Remote control is denied by
   default and can be permitted only for the reviewed persistent App Server
   defined by ADR-0006. It does not enable command network access inside a
   workspace session.
6. Secret filename globs inside workspaces provide defense in depth only. The
   primary policy is that operators must never place secrets or production
   state in a Codex workspace.
7. The root-owned `atlas-codex` launcher creates the single fixed
   `atlas-codex-session` systemd unit below `atlas-codex.slice`. The fixed name
   prevents concurrent supported sessions. Both unit and aggregate slice are
   limited to 200 percent CPU, 2 GiB soft memory, 3 GiB hard memory, and 256
   tasks. The session has a twelve-hour lifetime and a reduced systemd service
   sandbox. Private `/tmp` and `/var/tmp` tmpfs mounts are capped at 512 MiB
   each, use `nosuid,nodev,noexec`, and consume memory below `MemoryMax`. Swap
   is disabled for both the session unit and aggregate slice.
8. Codex remains an interactive operator tool. Ansible installs no Codex-owned
   daemon or updater, public listening port, scheduled job, deployment helper,
   application toolchain, or source checkout for it. ADR-0006 defines the
   Ansible-owned private App Server service.
9. Authentication is a separate manual operation using the official headless
   device flow. Managed policy allows ChatGPT login only and rejects API-key
   login. No OpenAI credential is accepted by Ansible or Git, and convergence
   never authenticates a user automatically.

The normal host playbook owns this state so ordinary convergence and predictive
checks detect drift. The existing administrator connection is the only entry
point. An operator enters the isolated identity with `sudo atlas-codex`.
Running `sudo -iu codex` is not the supported entry point because sudo keeps
the administrator's existing cgroup and would bypass the session resource and
temporary filesystem limits.
The release executables are group-restricted and guarded wrappers reject
ordinary direct execution outside `atlas-codex.slice`. The passwordless-sudo
SSH administrator remains part of the host trust root and can deliberately
bypass any local control as root. The boundary constrains the unprivileged
Codex session, not the trusted administrator.

Convergence persistently masks the fixed session unit and proves it is strictly
inactive before mutation. Activation snapshots both policy files, the slice,
launcher, wrappers, and `current` selector in root-only durable state. The mask
is removed after complete verification, an exact verified rollback, or a safe
failure before publication. A controller interruption after publication starts
leaves the mask and snapshot in place for recovery on the next convergence.
Before any host write, a fixed-name transient systemd lease serializes
convergence. Its program is supplied inline rather than through a mutable
shared helper, and follows the persistent SSH controller process by PID plus
process start time. Each controller has a kernel-generated UUID and a release
signal in a root-only systemd runtime directory. The controller never stops
the shared unit name. It signals only the lease with its UUID and verifies the
matching systemd description. Concurrent controllers therefore race only on
atomic unit creation, while a disconnected controller releases the lease
automatically. A 24-hour runtime limit bounds a stale lease.

## Consequences

### Positive

- A Codex session cannot acquire root or Docker control through Unix group
  membership.
- A compromised workspace cannot read the production repository or secret
  tree through the configured sandbox or ordinary filesystem permissions.
- `approval_policy = "never"` prevents interactive approval from weakening the
  command sandbox, including its disabled network boundary.
- Updates remain reviewable Git changes with exact release evidence and a
  deterministic rollback target.
- The host gains no Node.js, npm, or public network surface. The Ubuntu
  `bubblewrap` package integrates Codex sandbox setup with the host AppArmor
  policy without disabling user namespace restrictions or granting host
  capabilities.
- Persistent Codex data cannot grow past its dedicated 6 GiB filesystem, and
  session resources are also capped in aggregate.

### Negative

- The ChatGPT device credential is a secret stored on Atlas under
  `/srv/codex/home/.codex/auth.json` after manual authentication.
- `history.persistence = "none"` disables `history.jsonl`, but session and
  rollout records, archived session data, caches, and workspaces still persist.
  Any of that state can contain sensitive operator or source material.
- The bounded filesystem can fill and interrupt Codex. Operators must monitor
  `df` and `du`; retention and deletion require explicit review because there
  is no automatic cleanup.
- Files that are deliberately world-readable remain visible unless the managed
  policy denies their path.
- The account can still consume its bounded CPU, memory, task, persistent disk,
  and client outbound traffic allowances.
- Codex cannot directly inspect or modify production state. An operator must
  prepare a separate bounded workspace for every legitimate use.

## Rollback

Select a previously retained validated release by changing the version and
digests in a reviewed pull request, then converge the host. Removing Codex
entirely is a separate destructive operation because it may delete
authentication, session, and workspace data. Normal convergence never deletes
that state or the loop backing file.

## References

- [Codex CLI installation](https://developers.openai.com/codex/cli)
- [Headless device authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
- [Approvals and sandboxing](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration)
- [Permission profiles](https://learn.chatgpt.com/docs/permissions)
