# ADR-0006: Keep Codex App Server private for bounded remote access

## Status

Accepted on 14 August 2026. This decision amends the persistent-service and
remote-entry-point parts of ADR-0005. The `codex` runtime identity remains
non-login and unprivileged.

## Context

The Codex desktop app can manage a Codex App Server on a remote machine over
SSH. The desktop starts and probes `codex` through the remote login shell, then
uses `codex app-server proxy` to reach an App Server Unix socket. The remote
login therefore cannot be the `codex` runtime account: that identity owns the
OpenAI credential and workspaces and must remain outside `AllowUsers`.

Using `vpsadmin` is also unacceptable. That account has unrestricted
passwordless sudo and is part of the Atlas root trust boundary. A Codex remote
connection under that identity would expose the entire host rather than the
bounded workspace.

The App Server transport is not an Internet service. OpenAI documents the
stdio and Unix socket transports as local transports and warns against
exposing the experimental WebSocket transport on a public or shared network.
Codex 0.147.0 also has an experimental remote-control relay for Unix hosts.
That relay makes an outbound authenticated WebSocket connection to ChatGPT.
It does not require an inbound App Server port.

## Decision

Atlas derives an internal `vps_codex_app_server_enabled` value. It is true when
either the desktop SSH gateway or direct mobile control is enabled. The two
public settings remain independent.

1. When `vps_codex_remote_enabled` is true, Ansible creates `codex-remote` as a
   separate SSH gateway account. It has a
   locked password, one primary group, no Docker access, and no general sudo.
   Its public keys come from the external public-variable file. Each key has
   the OpenSSH `restrict` option. The SSH policy also disables terminals, user
   startup files, agent forwarding, TCP forwarding, X11 forwarding, and
   tunnels. Root owns the gateway home and SSH files. Within its home, the
   account can write only the App Server control directory required by the
   desktop, so it cannot persist a login profile, add a key, or shadow the
   root-owned `codex` wrapper through `~/.local/bin`.
2. The runtime `codex` account remains a system account with no SSH key and no
   direct SSH login. It alone owns the credential, state, and workspaces.
3. When the derived App Server value is true,
   `atlas-codex-app-server.service` runs the exact pinned Codex executable as
   `codex` below `atlas-codex.slice`. It listens only on the managed Unix socket
   below `/srv/codex/home/.codex/app-server-control`. It is enabled at boot,
   restarts after failure, allows the UDP bind required by the pinned musl DNS
   resolver, and denies other IPv4 and IPv6 socket binding. It retains the
   filesystem, device, capability, memory, CPU, and task boundaries from the
   interactive launcher. It opens no firewall port, and UFW denies undeclared
   inbound traffic.
4. OpenSSH applies a root-owned `ForceCommand` gate to every `codex-remote`
   connection. The gate never evaluates the caller's command string. It parses
   the current desktop login-shell envelope and maps only the path probe,
   version probe, idempotent Unix App Server start, and `app-server proxy` to
   fixed actions. Interactive shells, composite commands, file transfer, and
   arbitrary non-interactive commands are rejected.
5. Sudo permits `codex-remote` to invoke only the three corresponding runtime
   `atlas-codex` modes plus one proxy smoke mode that is usable only while the
   durable activation transaction is in its runtime-validation phase. The
   root-owned launcher validates the sudo caller and reconstructs every runtime
   command. It accepts no caller-controlled path, environment, option, or
   trailing argument.
6. The proxy runs as `codex` in a unique transient service below the same
   slice. SSH transports its standard input and output. No SSH forwarding and
   no public App Server listener are needed.
7. Host convergence publishes a maintenance marker, stops the App Server, and
   proves it inactive before policy or release mutation. The systemd unit, boot
   link, command gate, restricted SSH authorization, and sudo policy are part
   of the durable activation snapshot. Before commit, the transaction starts
   the still-inaccessible service, proves its effective systemd properties and
   Unix socket, and opens
   a bounded WebSocket upgrade through the command gate, sudo rule, launcher,
   and proxy when the desktop gateway is enabled. Mobile-only activation does
   not create or exercise the SSH gateway. Failure stops the service and
   restores the snapshot. Only a verified commit releases the session
   interlock and any configured SSH path.
8. Atlas enables Codex `remote_control` when
   `vps_codex_remote_control_enabled` is true, independently of the desktop SSH
   gateway. The managed requirement then permits remote control, and the
   reviewed App Server unit passes the hidden `--remote-control` startup flag.
   The removed compatibility feature remains false because Codex 0.147.0
   treats it as a no-op.
   The App Server keeps its private Unix socket and adds only an outbound
   authenticated WebSocket connection to ChatGPT. It does not add a firewall
   rule or an inbound listener.
9. The built-in `codex app-server daemon bootstrap` flow is not used. The
   reviewed Ansible unit owns boot persistence and the pinned update path.
10. Manual mobile pairing runs through the root-owned launcher. Only the Atlas
    administrator can request the short-lived pairing code. The launcher runs
    the exact pinned `codex remote-control pair` command as the `codex` account
    in a bounded transient unit. The SSH gateway key cannot request a code.

Both remote paths are disabled by default in the public repository. Enabling
the desktop gateway requires at least one valid public key in the external
variable file. Mobile-only control requires no Codex gateway key. No private
key, OpenAI credential, hostname inventory, pairing code, or App Server token
enters Git.

## Consequences

### Positive

- Neither the desktop connection nor a stolen gateway key receives the Atlas
  administrator identity, Docker control, or the Codex credential directly.
- The App Server survives desktop disconnects, process failures, and host
  boots without a public listener.
- The current desktop SSH workflow works through its expected login shell and
  command names while the privilege transition remains argument-bounded.
- All App Server and proxy work shares the existing aggregate resource limits.
- The pinned musl client can resolve DNS without permitting a TCP listener.

### Negative

- `codex-remote` needs a valid account shell because OpenSSH executes the
  root-owned forced command through it. The forced command rejects ordinary
  shell access, including otherwise unprivileged commands.
- The gateway key authorizes use of the already authenticated App Server. A
  stolen key can spend the Codex account allowance and control approved
  workspaces through the App Server even though it cannot read `auth.json`.
  Key revocation must therefore be immediate.
- The three accepted desktop command shapes are a compatibility contract. A
  future desktop release may require a reviewed wrapper update before it can
  connect.
- Direct mobile control is an experimental Codex 0.147.0 capability. The
  public OpenAI remote-connection documentation describes the desktop relay
  as the supported flow. A mobile product change can therefore require a
  reviewed Codex upgrade or disable direct pairing.
- A paired mobile controller can use the authenticated App Server without an
  SSH key. Treat the pairing code and each paired controller as access to the
  bounded Codex account. Revoke a lost controller immediately.
- The runtime may bind a transient UDP client socket for DNS. UFW remains the
  independent inbound boundary for every undeclared port.

## Rollback

Set `vps_codex_remote_control_enabled` to false and converge a reviewed `main`
revision to stop the direct relay. If the private desktop gateway remains
enabled, the service restarts without `--remote-control` and the managed policy
denies future relay activation. In a mobile-only configuration, the service and
boot link are removed because neither remote path still needs the App Server.

Set `vps_codex_remote_enabled` to false to remove the desktop path. Convergence
removes the forced-command gate, sudo policy, gateway authorized keys, and the
account from `AllowUsers`. The App Server remains only if direct mobile control
is still enabled. Disabling both settings stops and removes its unit and boot
link. Every case preserves the `codex` credential, sessions, workspaces, and
bounded storage.

Remove the local `atlas-codex` SSH alias and retire its private key separately
after the disabled state is verified. Do not remove the administrator key or
the `/srv/codex` backing file as part of this rollback.

## References

- [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [OpenAI Codex remote connections](https://learn.chatgpt.com/docs/remote-connections)
- [Codex 0.147.0 remote-control implementation](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/cli/src/remote_control_cmd.rs)
- [ADR-0005](0005-dedicated-codex-cli-account.md)
