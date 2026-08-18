# Inactive public edge candidate

`public-edge.override.yaml` is an inactive extension of the exact
`vps-public-static-edge` Compose project. No controller applies this file.
Surplasse remains disabled and its adapter remains locked.

The candidate adds only these resources to the current public edge:

- the external `app_surplasse` network at `172.30.10.254`;
- three file-backed OVH credentials from
  `/etc/vps/secrets/dns/surplasse`;
- the Atlas-owned `/etc/caddy/surplasse-tls.caddy` snippet.

The base project continues to mount
`/srv/vps/runtime/public-static-edge/routes` at `/etc/caddy/routes`. A future
platform controller must publish the verified application route atomically at
`/srv/vps/runtime/public-static-edge/routes/surplasse.caddy`. The active file
must equal the route from the admitted `vps-integration` artifact byte for
byte and must equal the approved route in the reviewed `vps-infra` revision.
That route imports the Atlas-owned TLS snippet. It does not select a DNS
provider or contain an OVH credential name. This triple equality prevents a
producer route from claiming an additional public identity without a platform
review.

The producer release published before this import contract does not satisfy
that invariant. A new producer revision and a new attested integration digest
are required. Do not transform an older route on Atlas to make it fit.

Run the local candidate checks with:

```text
make check-surplasse-public-edge-candidate
```

This check proves the static edge policy after removal of the exact candidate
extension. It also proves the derived Compose contract and validates Caddy
with placeholder values in the exact immutable image. It does not prove a
published producer artifact, OVH IAM scope, a live ACME transaction, an atomic
edge switch, recovery gating, or DNS cutover.

The wildcard DNS record will resolve every direct Surplasse subdomain. The
current route sends each name that is not explicitly reserved to Commande. A
reviewed cutover must decide and enforce the complete reservation policy for
mail and service names such as `autoconfig`, `autodiscover`, `mta-sts`, `smtp`,
and `imap` before it creates that wildcard record.

The current public edge controller cannot stage and switch this extension.
That controller must gain a reviewed atomic and recoverable transition before
activation. Do not run the override directly with Docker Compose on Atlas.
