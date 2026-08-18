# Surplasse DNS cutover

## Current state

The controller is installed as a locked candidate. It performs no OVHcloud
request in this revision. Canonical application admission is enabled, but the
DNS policy remains independent and locked. This runbook defines the future
operator sequence. It is not an activation instruction for the current
revision.

Run the read-only policy check:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover doctor --json
```

Require the DNS policy values `enabled: false`, `activation_policy: locked`, and
`mutations_available: false` until a separate reviewed activation change is
ready.

## Identity boundary

Create one short-lived OVHcloud identity for only the `surplasse.com` zone.
Set an expiry that covers the planned window and rollback margin. Grant only
the complete export, record list and read, bounded A create/update/delete, and
zone refresh routes described in ADR-0012. Verify the effective IAM policy in
the OVHcloud console before installation.

Install these three newline-terminated values by a private operator channel:

```text
/etc/vps/secrets/dns/surplasse-cutover/ovh-application-key
/etc/vps/secrets/dns/surplasse-cutover/ovh-application-secret
/etc/vps/secrets/dns/surplasse-cutover/ovh-consumer-key
```

The directory is `root:root 0700`. Each file is regular, single-linked, and
`root:root 0400`. The directory contains no other file. Never pass a value in
an argument, environment variable, prompt, log, chat, issue, or repository.
These files are not the permanent Caddy DNS-01 credential bundle at
`/etc/vps/secrets/dns/surplasse`.

## Preconditions for a future ready revision

Before policy activation, prove all these conditions:

1. The apex and `www` A records are each exactly `213.186.33.5` with TTL 3600.
2. The wildcard A record is absent.
3. Apex, `www`, and wildcard AAAA and CNAME records are absent.
4. The complete export contains the expected MX and SPF records. Preserve all
   MX, TXT, DKIM, DMARC, CAA, NS, SOA, and other records.
   Require `1.1.1.1` and `8.8.8.8` to return the same delegated NS and SOA
   state before planning.
5. Direct IPv4 probes to `137.74.174.163` prove the staged Surplasse route and
   TLS configuration without using public DNS.
6. Do not publish an AAAA record. The Atlas IPv6 public edge is not verified.
7. Record the exact rollback owner, time window, and alert channel.

The policy activation must be a separately reviewed repository revision. Do
not edit the installed policy on Atlas.

## Phase 1: lower TTL

Create one plan:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover plan --json
```

Review the complete two-record diff, plan identifier, SHA-256, snapshot
SHA-256, expiry, and non-atomicity warning. Confirm the exact plan only:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover apply \
  --plan-id '<plan-id>' \
  --plan-sha256 '<sha256>' \
  --json
```

Do not add a target or TTL to apply. Require exact API readback and every
authoritative and recursive DNS result. Check the recorded RCODE for every
query. Positive A, NS, and SOA evidence must be `NOERROR`. Empty AAAA and CNAME
answers at an existing name must also be `NOERROR`; the absent transaction
probe must be `NXDOMAIN`. Treat `SERVFAIL`, `REFUSED`, `FORMERR`, a missing
RCODE, or a name or type mismatch as ambiguous. If the result is
`applied_unverified`, do not run apply again. Run the one separate check:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover verify \
  --plan-id '<plan-id>' \
  --json
```

If a process stops after a journal exists, use recovery with the same exact
plan identity and digest:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover recover \
  --plan-id '<plan-id>' \
  --plan-sha256 '<sha256>' \
  --json
```

Recovery compares API state. It does not blindly repeat a record write.

## Phase 2: cut over IPv4

Wait until the `not_before` time from the durable active state. It is at least
3600 seconds after the verified phase 1 result. A new plan before that time is
refused.
Create and review the second plan with the same `plan --json` command. It must
contain exactly these sequential operations:

1. create wildcard A `137.74.174.163` TTL 300;
2. change apex A to `137.74.174.163` TTL 300;
3. change `www` A to `137.74.174.163` TTL 300.

Apply only the identifier and SHA-256. Require readback from the API, every
authoritative server, `1.1.1.1`, and `8.8.8.8`. Require no AAAA or CNAME answer.
The OVHcloud requests are sequential. Do not describe this as an atomic zone
transaction.

## Explicit rollback

Create a rollback plan from the transaction identifier:

```text
sudo -n -- /usr/local/libexec/vps/surplasse-dns-cutover plan-rollback \
  --transaction-id '<transaction-id>' \
  --json
```

Review and confirm it with the normal `apply` interface. From the complete
cutover state it deletes the Atlas wildcard A, then restores the apex and
`www` A records to `213.186.33.5` with TTL 3600. From phase 1 it restores only
the two TTL values. It refuses a protected-record change, a state mismatch,
an expired plan, or replay. The preserved raw export and canonical snapshot
remain `root:root 0400` under `/var/lib/vps-surplasse-dns-cutover`.

After a verified terminal result, revoke the temporary OVHcloud identity and
remove its three files through a separately reviewed operator action. Do not
remove the Caddy DNS-01 identity.
