# Inactive public edge candidate

`public-edge.override.yaml` is an inactive extension of the exact
`vps-public-static-edge` Compose project. The root-only
`deploy-surplasse-public-edge` controller can stage and apply the reviewed
composition. No Ansible state or release contract activates it automatically.
Canonical Surplasse admission is enabled, but this edge extension still needs a
separate deliberate activation. The legacy adapter remains locked.

The candidate adds only these resources to the current public edge:

- the external `app_surplasse` network at `172.30.10.254`;
- three file-backed OVH credentials from
  `/etc/vps/secrets/dns/surplasse`;
- the Atlas-owned `/etc/caddy/surplasse-tls.caddy` snippet.

The base project continues to mount
`/srv/vps/runtime/public-static-edge/routes` at `/etc/caddy/routes`. The
platform controller publishes the verified application route atomically at
`/srv/vps/runtime/public-static-edge/routes/surplasse.caddy`. The active file
must equal the route from the admitted `vps-integration` artifact byte for
byte and must equal the approved route in the reviewed `vps-infra` revision.
That route imports the Atlas-owned TLS snippet. It does not select a DNS
provider or contain an OVH credential name. This triple equality prevents a
producer route from claiming an additional public identity without a platform
review.

The reviewed producer source is Surplasse commit
`5bb20806760a2c2f9d4ebaabd96133eb0b0583e2`. Its route SHA-256 is
`645931a03bd1c80ab5b3982d6a285e673906d68fb010adbd1756d43d8a0cd306`.
The source commit alone is not an admitted application release. The controller
still requires the exact route inside a materialized release. Do not transform
an older route on Atlas to make it fit.

Run the local candidate checks with:

```text
make check-surplasse-public-edge-candidate
```

This check proves the static edge policy after removal of the exact candidate
extension. It also proves the derived Compose contract and validates Caddy
with placeholder values in the exact immutable image. It does not prove a
published producer artifact, OVH IAM scope, a live ACME transaction, or DNS
cutover.

The wildcard DNS record would resolve every direct Surplasse subdomain. The
reviewed route reserves `app`, `admin`, `local`, `mail`, `autoconfig`,
`autodiscover`, `mta-sts`, `smtp`, `imap`, `pop`, `pop3`, `webmail`, `status`,
`reports`, and `grafana`; each returns 503 instead of falling through to
Commande. This route policy does not create the wildcard record or authorize a
DNS change.

## Transaction controller

The controller takes `/run/lock/vps-static.lock`. It accepts only an attested
route below one materialized Surplasse application release. It requires the
route to equal the reviewed route byte for byte. It also requires the exact
Atlas TLS snippet, overlay, base edge revision, immutable Caddy image digest,
and pre-existing DNS credential bundle.

Stage without changing the running edge:

```bash
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge \
  --stage /srv/applications/surplasse/releases/sha256-<digest>/integration/caddy/surplasse.caddy
```

The command returns one immutable candidate path. Activate only that returned
path:

```bash
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge \
  --activate /srv/vps/releases/public-static-edge-surplasse/<fingerprint>
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge --verify-live
```

The candidate keeps the three static route files byte for byte. It uses the
same `vps-public-static-edge` project, container, named volumes, and public
ports. It adds only the Surplasse route, the Atlas TLS snippet, the three scoped
Docker secrets, and the managed `app_surplasse` attachment at
`172.30.10.254`.

The release-link replacement is atomic. Docker Compose must then recreate the
sole Caddy container to add the secrets and network attachment. This runtime
recreation has a short interruption window. A zero-downtime switch is not
possible while one container owns ports 80 and 443.

The controller writes a protected, fsync-backed transaction before the link
switch. A crash in the prepared or switched phase restores the previous edge.
A crash after reconciliation commits forward only if the candidate is healthy
and owns the exact network identity. The enabled
`vps-public-edge-surplasse-recover.service` performs this recovery before Caddy
starts at boot.

Docker file-backed secrets are bind mounts. An atomic credential-file rotation
does not change the inode already mounted in Caddy. Run `--activate` again with
the current candidate after each approved rotation. The controller uses
`docker compose up --force-recreate` and then repeats the health and network
proof. `--verify-live` alone does not prove that a newly replaced credential
inode is mounted.

Base edge convergence refuses to replace an active Surplasse extension. A
future base-edge revision needs a reviewed rebase through this controller.
This refusal preserves the active static and Surplasse composition. Do not run
the override directly with Docker Compose on Atlas.
