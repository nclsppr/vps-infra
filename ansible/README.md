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

The script uses absolute external inventory and public-key variable files. It
starts Git and Ansible commands with a small environment allowlist. Therefore,
a divergent branch, a tracked local modification, an untracked role, or an
external Ansible plugin path cannot change the executed playbook tree. A fetch,
archive, dependency, or collection failure stops before Ansible contacts the
host.

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

The `deploy` account is locked and is not a member of the `docker` group. It
has a valid shell because OpenSSH requires one. `ForceCommand` sends every key
to a parser that accepts only `deploy <full-git-sha>`.

The controller files are installed under `/usr/local/libexec/vps`. The marker
`/etc/vps/production-enabled` and the executable `apply-release` are absent.
The current controller can validate and plan. It cannot activate production.

The deploy role initializes the root-owned mirror at `/srv/vps/repository` from
the single allowed public origin:

```text
https://github.com/nclsppr/vps-infra.git
```

It verifies that the requested commit is reachable from `origin/main`. It also
verifies a clean checkout before it installs root-owned controller files. The
file `/usr/local/share/vps-infra/controller-revision` records the installed
commit.

## Prepared Docker networks

Ansible creates six external Docker networks with fixed properties. The locked
base platform joins only `ops` and `db_monitoring`. PostgreSQL joins only
`db_monitoring`. Caddy and Prometheus join only `ops`.

The four application networks remain empty until a reviewed application
integration package attaches the required services. An existing network with
an unexpected driver, internal flag, CIDR, or management label stops
convergence. Ansible does not delete that network.
