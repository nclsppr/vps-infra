# ADR-0015: Authorize the Surplasse tester DNS cutover policy

## Status

Accepted on 23 August 2026. This decision changes repository authorization
only. It does not install an OVHcloud identity, call an API, create or apply a
plan, change DNS, request a certificate, start Surplasse, or prove an Atlas
deployment.

This decision supersedes only the locked DNS policy state in ADR-0012 and
ADR-0013. It retains the bounded controller, the dedicated identity, the
two-phase TTL procedure, the complete zone snapshot, the explicit plan digest,
the durable journal, the public resolver checks, and the explicit rollback.

## Context

The product owner authorizes `surplasse.com` for the owner and invited testers.
The canonical application release is admitted for Stripe test orders. The DNS
policy still has `enabled: false` and `activation_policy: locked`, so the
controller refuses every plan before it can inspect its dedicated identity.

The controller already validates only two exact policy pairs:

- `enabled: false` with `activation_policy: locked`;
- `enabled: true` with `activation_policy: ready`.

The public-edge Ansible role copies the policy and controller. It creates empty
root-owned state and credential directories. It does not execute the
controller. No workflow invokes `doctor`, `plan`, `apply`, `verify`, `recover`,
or `plan-rollback`.

## Decision

Set `policies/surplasse-dns-cutover-v1.json` to `enabled: true` and
`activation_policy: ready`. Keep every fixed policy value unchanged, including
the zone, Atlas IPv4 address, baseline target and TTL, cutover TTL, old-TTL wait,
plan lifetime, endpoint, and dedicated credential root.

Keep the legacy Surplasse adapter locked. Keep Stripe in test mode. Do not
change the generic production activation policy.

Installing the ready policy is inert. `doctor` reports the authorization state
without opening the credential directory, constructing an API client, creating
state, or calling OVHcloud. The first `plan` remains an explicit root command.
It requires the exact short-lived credential inventory and uses the reviewed
read routes before it creates a digest-bound local plan. It does not change a
DNS record.

Only `apply` can change DNS. It accepts only an unexpired plan identifier and
SHA-256. The first forward plan can only lower the apex and `www` TTL values.
The second plan remains unavailable until the complete old-TTL wait has passed.
The target plan can only create the wildcard A record and change the apex and
`www` A targets to the fixed Atlas IPv4 address. It cannot create an AAAA record
or change an unrelated record.

Before the first `plan`, require direct Atlas application and TLS probes, the
exact baseline DNS state, a complete zone export, matching authoritative and
recursive authority evidence, an assigned rollback owner and window, and the
dedicated short-lived OVHcloud identity. Install that identity through a
private operator channel only. Do not put a credential in Git, a command
argument, an environment value, a log, or a chat.

## Consequences

- The repository no longer blocks the bounded tester DNS cutover.
- A merge, release, or Ansible convergence does not change DNS.
- An absent or invalid credential inventory remains a fail-closed operational
  gate for `plan`.
- The operator must approve each displayed plan identifier and SHA-256 before
  `apply`.
- DNS activation remains separate from application deployment, public-edge
  activation, certificate issuance, and commercial public launch readiness.
- The temporary cutover identity must be revoked after a verified cutover or
  rollback. The permanent Caddy DNS-01 identity remains separate.

## Alternatives

### Keep the policy locked until the credential exists

Rejected. Repository authorization and private credential delivery are
separate boundaries. The ready policy is inert until an operator supplies the
dedicated identity and runs an explicit command.

### Add a plan-only policy state

Rejected for this revision. A plan-only state would change the reviewed policy
schema and controller state machine. The existing digest confirmation,
two-phase procedure, and explicit root command already separate planning from
mutation.

### Reuse the permanent Caddy credential

Rejected. Certificate renewal and DNS migration have different scopes and
lifetimes. Reuse would expand the long-lived certificate identity.
