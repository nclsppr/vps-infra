# Host Automation

This directory configures an Ubuntu 26.04 LTS host. It contains no secret,
production address, or production inventory. Host convergence does not start
the platform or an application.

## Prepare the controller

Run these commands from the repository root:

```bash
make setup
cp ansible/inventories/production/hosts.example.yml \
  ansible/inventories/production/hosts.yml
cp ansible/inventories/production/group_vars/bootstrap-public.yml.example \
  /private/path/bootstrap-public.yml
```

The root `pyproject.toml` and `uv.lock` files define the Python and
`ansible-core` versions. The Ansible directory does not have a second Python
environment.

The managed VPS requires the Ubuntu `python3-jsonschema` package. The base role
installs it. The deployment controller uses it to validate the Draft 2020-12
release schema. Production validation stops if this independent schema check
is unavailable.

Host convergence also installs the standalone Codex CLI package without Node
or npm. The `codex_cli` role pins the release archive and every executable,
publishes it atomically, and runs it only through the isolated `codex` account.
The enforced launcher applies aggregate resource limits and keeps all state on
a dedicated 6 GiB filesystem. Optional desktop access uses a separate
unprivileged SSH gateway and a persistent App Server bound only to a Unix
socket. A root-owned forced command maps only the current desktop protocol to
fixed launcher actions, so the gateway cannot obtain an arbitrary shell. It
installs no public Codex port, application checkout, or credential.
See [`docs/operations/codex-cli.md`](../docs/operations/codex-cli.md).

Confirm the SSH host-key fingerprint through the OVHcloud console before you
add it to `known_hosts`. Keep host-key checking enabled. Do not accept a
fingerprint through the same network path as the SSH connection.

## Bootstrap and converge

Bootstrap creates only the administrator account and its sudo rule. It does
not change OpenSSH or the firewall:

```bash
make bootstrap \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

Keep the bootstrap session open. Prove a new SSH connection with `vpsadmin`.
Then set `ansible_user: vpsadmin` in the inventory.

Run host convergence from the repository root:

```bash
make converge \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

After a successful convergence, run the bounded predictive check:

```bash
make converge-check \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

`converge-check` always supplies both `--check` and `--diff`. The wrapper
rejects every other command-line argument. This restriction prevents an
operator-controlled Ansible option from changing the captured playbook scope
or execution policy.

Run this mode only after one successful normal convergence. It is a predictive
drift check, not a bootstrap rehearsal. Ansible skips command-based Docker
network creation and some final effective-state assertions in check mode. An
expired APT cache can also report a predicted change without refreshing the
cache. The repository contract allowlists every command that must execute
outside check mode and requires each command to be read-only.

`scripts/converge` fetches `main` with an explicit destination refspec. It
captures the full `origin/main` commit once. It exports that commit to a new
temporary directory. It installs the locked tools and dependencies in that
directory. It then runs `site.yml` from the exported tree and passes the same
commit as `vps_infra_revision`.

An encrypted administrator key must already be unlocked in the caller's SSH
agent. The script resolves and validates a caller-owned `SSH_AUTH_SOCK`, then
passes that socket only to `ansible-playbook`. Git, mise, dependency setup, and
Ansible Galaxy continue to run without access to the agent.

The script accepts external inventory and public-key variable files, then
canonicalizes their paths before it captures `origin/main`. The inventory input
is one static YAML file. Each local file path inside it must be absolute.
It copies only the private inventory file into the exported production
inventory directory before Ansible starts. The tracked `group_vars` therefore
come from the captured `origin/main` commit, not from the caller's checkout or
the directory that contains the external inventory. The public-key variable
file stays an explicit extra-vars input. Git and Ansible commands start with a
small environment allowlist. Therefore, a divergent branch, a tracked local
modification, an untracked role, adjacent external `group_vars`, or an external
Ansible plugin path cannot change the executed playbook tree. A fetch, archive,
dependency, collection, or inventory-copy failure stops before Ansible contacts
the host.

`site.yml` rejects the `root` and `deploy` connection users. Run a second
convergence and a predictive check after the first successful convergence.
Use the same captured revision method for each command. Do not run `site.yml`
directly from an arbitrary working tree.

## Administrator key rotation

Normal convergence adds each declared administrator key. It does not remove an
existing key. It reports and stops on an undeclared key. This behavior prevents
a persistent OpenSSH control connection from hiding an unusable replacement
key.

Use this two-phase procedure:

1. Keep the proven key in `vps_admin_authorized_keys`.
2. Add the replacement key to the same variable.
3. Run convergence.
4. Start a new SSH process with the replacement private key. Set
   `ControlMaster=no` and `ControlPath=none`. Keep host-key checking enabled.
5. Use a separate explicit operation to retire the old key.
6. Remove the retired key from `vps_admin_authorized_keys`.
7. Run convergence again. Prove one more new SSH connection.

Do not replace all keys in one unverified operation. The normal convergence
role intentionally does not implement key retirement.

The initial SSH port is 22. A port migration must open the old and new ports,
prove a connection to the new port, and then close the old port.

## Firewall state

After UFW takes control, each convergence checks the exact numbered rule set.
An unknown manual rule, such as a public `5432/tcp` rule, stops the playbook.
The playbook reports the rule. It does not remove it. The operator must review
and remove the rule explicitly.

The Docker `DOCKER-USER` policy accepts only the original published ports
`80/tcp`, `443/tcp`, and `443/udp` on the public interface. It uses conntrack
original-destination ports because Docker evaluates this chain after DNAT. An
allow rule also requires the conntrack DNAT state and original packet direction.
It drops direct non-DNAT forwarding and all other new public Docker forwarding.

## Deployment controller

After a reviewed convergence of this role, the `deploy` account is locked and
is not a member of the `docker` group. It has a valid shell because OpenSSH
requires one. The installed `ForceCommand` then sends every key to a parser that
accepts only `deploy <full-git-sha>` or the exact `deploy-static-live` or
`deploy-application-live` tuple for one allowlisted application.

The controller files are installed under `/usr/local/libexec/vps`. The marker
`/etc/vps/production-enabled` and the executable `apply-release` are absent.
The generic controller can validate and plan. Static activation has its own
reviewed gate. The 2026-08-18 rollout converged repository revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b` and proved that the root-owned
`deploy-application` controller and its argument-free gate are installed.
Surplasse and Parkventory are both `enabled: false` in the protected application
contract, so the controller refuses them before any runtime validation or
network operation. No application deployment workflow invokes the gate.

The deploy role declares GitHub CLI 2.97.0 from its official release archive.
On convergence, it selects `amd64` or `arm64`, verifies the archive SHA-256,
verifies the extracted executable with a second SHA-256, and confirms the
installed version. The role then installs
`/usr/local/libexec/vps/deploy-static` and its platform integration verifier.
It also installs `/usr/local/libexec/vps/deploy-application`, the strict
application bundle policy, and the argument-free root application gate.
Current Personal, Papers Empire, and platform integration packages are public,
so this path does not install a registry credential.

The static SSH form does not grant arbitrary `deploy-static` arguments. The
non-root wrapper sends one bounded canonical record over stdin to a root-owned
gate with no command-line arguments. That gate independently revalidates the
application, repositories, SHAs, digests, token count, ASCII encoding, and
framing before it invokes `deploy-static --activate-live`. This construction is
compatible with Atlas `sudo-rs` and does not rely on unsupported sudoers
argument regexes.

The root gate starts each activation in a transient systemd unit. Its stop hook
recovers any unfinished transaction even if the SSH session disappears. The
enabled `vps-static-recover.service` is ordered before the systemd-managed
public edge at boot; it orders itself after and requires Docker so it can remove
strictly labeled orphan probe containers. Docker can nevertheless restart the
existing `unless-stopped` Caddy container as soon as the daemon starts. The
systemd ordering therefore does not yet withhold all public traffic during
recovery. Root-only active, inventory, transaction, and quarantine state lives
under `/var/lib/vps-static`.

The application SSH form follows the same stdin-only forced-command boundary.
Its exact record contains the application, one full source SHA, and one
digest-only `application-release` reference. The root controller independently
verifies release, component, and integration attestations and referenced
content before activation. It shares `/run/lock/vps-static.lock` with static
deployments. Root-only application release, active, inventory, transaction, and
quarantine state lives under `/srv/applications` and
`/var/lib/vps-application`; runtime configuration lives under the root-only
`/etc/vps/applications` directory. Secret bytes remain in
`/etc/vps/secrets/<application>` and are never copied into state.

`vps-application-recover.service` is installed and loaded beside the static
recovery unit. On 2026-08-18 it was inactive after a successful recovery run
(`Result=success`, `ExecMainStatus=0`). It is ordered before the systemd-managed
edge. That ordering has the same Docker restart bypass described above.
Application activation also requires an exact pre-staged public edge route and
application-network attachment before it may run the dedicated migrator. The
controller does not perform that platform cutover itself, and the current
contracts keep both applications disabled.

The caller supplies the application, the application source revision, the
exact site and route references, the platform integration revision and
reference, and the exact Caddy image. The materializer runs
registry fetches, validation, extraction, and GitHub attestation verification
in short-lived systemd `DynamicUser` units. A dedicated bounded tmpfs stores
each private runtime directory. One separate network execution uses the pinned
GitHub CLI and its embedded TUF bootstrap roots to obtain one current trusted
root for the deployment. The controller also requires the versioned SHA-256 for
that root. A trust-root rotation therefore fails closed until a reviewed
repository update changes the accepted digest. Attestation fetch and offline
GitHub verification use other sequential executions of one fixed transient
unit. The offline verifier receives the copied root through
`--custom-trusted-root`. This unit name
serializes worker creation in systemd. Each accepted file is copied into a
root-owned tree and made durable before activation. Ansible also creates
root-only active, inventory, transaction, and quarantine directories under
`/var/lib/vps-static`.

The deploy role initializes the root-owned mirror at `/srv/vps/repository` from
the single allowed public origin:

```text
https://github.com/nclsppr/vps-infra.git
```

It verifies that the requested commit is reachable from `origin/main`. It also
verifies a clean checkout before it installs root-owned controller files. The
file `/usr/local/share/vps-infra/controller-revision` records the installed
commit.

## Public static edge playbook

`playbooks/public-static-edge.yml` is the only current live service path. The
local `converge` wrapper accepts `--prepare-public-static-edge`,
`--activate-public-static-edge`, and `--stop-public-static-edge` as exact
single-argument modes. All three modes execute from an isolated archive of
`origin/main`.

The preparation mode installs one root-owned Compose project, binds it to the
exact promoted Caddy image, and serves the four hosts over HTTP without asking
for a certificate. The activation mode first requires every authoritative and
recursive A answer to contain only Atlas and every AAAA answer to be empty. It
then switches atomically to the HTTPS release and runs strict certificate
probes. Each phase uses an immutable revision directory and the matching
Compose validator from that same checkout. The stop mode stops every container
owned by this Compose project even if the unit is absent or inactive. It
preserves the ACME volumes and static releases. None of these modes creates
`apply-release`, writes the production marker, changes DNS, reads an OVH
credential, or starts PostgreSQL, Grafana, Prometheus, exporters, Surplasse, or
Parkventory.

The project joins only the managed `edge` bridge on `172.30.32.0/24`. The
playbook rejects a missing or incompatible bridge and verifies that the live
Caddy container has no attachment to `ops`.

The internal platform remains necessary for the application stacks. Its later
activation keeps Grafana on loopback and every database or metrics endpoint off
the public host interfaces.

Use `make start-internal-platform` to activate only PostgreSQL, Prometheus,
Grafana, Node Exporter, and PostgreSQL Exporter from one immutable release.
Use `make stop-internal-platform` to stop the same service set. The dedicated
systemd unit never names Caddy. Both operations preserve named volumes and
secrets. The start role refuses any unselected container in the shared Compose
project before it can reconcile with `--remove-orphans`.

## Local PostgreSQL backup stage

Install the daily local backup and monthly isolated restore rehearsal only
after the internal platform controller exists:

```bash
make install-postgres-backup \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
make backup-postgres-now \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
make rehearse-postgres-restore \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

The backup playbook never starts or stops Caddy or an application. The
immediate modes require the internal platform to be active. The stop mode
disables only the two timers and preserves every backup and Docker volume:

```bash
make stop-postgres-backup-schedule \
  ANSIBLE_EXTRA_VARS=/absolute/private/path/bootstrap-public.yml
```

This stage is not encrypted and is not off-site. See
[`docs/operations/postgresql-backup.md`](../docs/operations/postgresql-backup.md)
before accepting production data.

## Prepared Docker networks

Ansible creates seven external Docker networks with fixed properties. The
isolated public static edge joins only `edge`. The locked complete platform
definition joins only `ops` and `db_monitoring`. PostgreSQL joins only
`db_monitoring`. Its Caddy and Prometheus services join only `ops`.

The four application networks remain empty until a reviewed application
integration package attaches the required services. An existing network with
an unexpected driver, internal flag, CIDR, or management label stops
convergence. Ansible does not delete that network.
