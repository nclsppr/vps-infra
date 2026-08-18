# ADR-0012: Install a locked two-phase Surplasse DNS cutover controller

## Status

Accepted on 18 August 2026. The controller and policy are installed as an
inactive capability. The policy has `enabled: false` and
`activation_policy: locked`. This decision does not call OVHcloud, read a
credential, change DNS, or activate Surplasse.

## Context

The current `surplasse.com` apex and `www` A records both use
`213.186.33.5` with TTL 3600. The wildcard A record is absent. The apex,
`www`, and wildcard names have no AAAA or CNAME record. The reviewed Atlas
IPv4 address is `137.74.174.163`. Atlas does not have a verified public IPv6
edge, so this cutover must never create an AAAA record.

A DNS change is not one atomic transaction. The OVHcloud API changes one
record per request. A process or network failure can occur between requests.
The procedure must preserve every mail, delegation, certification, and other
unrelated record while the three bounded A records change.

The permanent Caddy DNS-01 identity is not a suitable migration identity. It
has a different lifetime and purpose. Reuse would turn an ACME credential into
a general zone migration credential.

## Decision

Install `/usr/local/libexec/vps/surplasse-dns-cutover` and its root-owned state
under `/var/lib/vps-surplasse-dns-cutover`. Install the reviewed policy at
`/etc/vps/policies/surplasse-dns-cutover-v1.json`. The policy is locked in this
revision. A later reviewed revision must change both policy flags before any
API client can be constructed.

The controller uses only the Python standard library. It implements the
current OVHcloud zone interface directly:

- `GET /domain/zone/surplasse.com/export`;
- `GET` and `POST /domain/zone/surplasse.com/record`;
- `GET`, `PUT`, and `DELETE /domain/zone/surplasse.com/record/{id}`;
- `POST /domain/zone/surplasse.com/refresh`.

It refuses redirects. It does not use an unpinned SDK. A short-lived,
zone-scoped identity uses three separate root-owned files under
`/etc/vps/secrets/dns/surplasse-cutover`. The controller never accepts a
credential through an argument or environment variable. It never includes a
credential in output or a subprocess call. Caddy continues to use a different
credential directory.

Before each plan, the controller reads every record, captures the complete API
zone export, and reads every record again. It stops if the canonical inventory
changes during that capture. The raw export and canonical record snapshot are
stored as root-owned `0400` files. SHA-256 binds the snapshot, each pre-change
export, each plan, and each result. The API preservation digest includes every
returned record outside the A, AAAA, and CNAME set at the apex, `www`, and
wildcard names. This includes all returned MX, TXT, CAA, NS, SOA, DKIM, DMARC,
and other records. The snapshot also requires `1.1.1.1` and `8.8.8.8` to agree
on the delegated NS set and SOA state because OVHcloud can keep
provider-managed NS or SOA data outside the general record list. OVHcloud
controls the SOA serial. Preservation checks normalize only that serial and
compare every other SOA field exactly.

The forward change has two confirmed plans:

1. Change only the apex and `www` TTL values from 3600 to 300. Refresh and
   verify the API and DNS state.
2. Record a durable `not_before` value at least 3600 seconds after the first
   successful authoritative verification. Only after that time, create the
   wildcard A and change the apex and `www` targets to `137.74.174.163`. Keep
   TTL 300.

Each plan expires after 900 seconds. The apply interface accepts only the plan
identifier and SHA-256. It does not accept a name, type, target, or TTL. Apply
rechecks the complete record state before it writes. The journal is durable
before the first write and after each observed write. Recovery compares the
current record set with both sides of the next operation. It does not replay a
write that the API already committed. Apply refuses every consumed plan.

After sequential writes, the controller requests one zone refresh, performs a
complete API readback, captures another full export, and verifies every OVH
authoritative name server. It then verifies `1.1.1.1` and `8.8.8.8`. It checks
the apex, `www`, and a transaction-specific name below the wildcard for A,
AAAA, and CNAME answers. Every observation includes an explicit DNS RCODE and
answers must match the exact queried name and type. NS, SOA, and positive A
evidence require `NOERROR`. Negative evidence at a name that exists requires
an empty `NOERROR` response, while the absent transaction-specific name
requires an empty `NXDOMAIN` response. `SERVFAIL`, `REFUSED`, `FORMERR`, a
missing RCODE, or any other ambiguity can never prove success. An unresolved
propagation result is `applied_unverified`. It permits one separate `verify`
operation and never a second apply.

Rollback is another explicit, expiring, digest-bound plan. It is eligible only
from an exact applied TTL or cutover state. It deletes only the Atlas wildcard
A when present and restores the original apex and `www` A target and TTL from
the first canonical snapshot. It rechecks all protected records before every
write. A completed rollback is terminal and cannot be replayed. The raw zone
export remains evidence; the provider-managed SOA serial is not and cannot be
set back to its historical number.

## Consequences

- This repository contains no OVHcloud credential and performs no live DNS
  request during validation.
- No command can create an AAAA record or change MX, TXT, CAA, NS, SOA, or any
  other unrelated record.
- The two-phase TTL procedure bounds cache exposure before the target change.
- The journal supports explicit recovery without claiming API-level
  multi-record atomicity.
- DNS success is separate from application and commercial activation. The
  Surplasse application remains disabled and its adapter remains locked.

## Activation blockers retained

A separate review must prove the exact OVHcloud IAM routes and expiry, install
a new short-lived credential by a private channel, change the policy to the
explicit ready pair, and prove direct Atlas application and TLS health before
the first plan. The operator must approve each displayed plan digest. Remove
the cutover identity after a verified cutover or rollback. Do not copy it into
the permanent Caddy credential bundle.
