# Archived `manage-ovh-dns` skill prototype

## Status

Inactive design record. Do not install or invoke this skill.

This document preserves a local-only prototype recovered from the duplicate
`vps` checkout on 14 August 2026. The prototype was never committed. Its
referenced root-owned controller, policy, plan store, snapshots, activation
marker, and `sudoers` rule are not implemented by this repository. No live
Atlas verification supports the capability.

This record prevents the design from disappearing with one workstation. It is
not an operator runbook and it does not authorize an OVHcloud API call or DNS
change.

The recovered sources are preserved byte for byte as inert `.txt` files:

- [`SKILL.md.txt`](manage-ovh-dns-skill-prototype/SKILL.md.txt);
- [`openai.yaml.txt`](manage-ovh-dns-skill-prototype/openai.yaml.txt).

The `.txt` suffix is intentional: these files are evidence, not an installed
Codex skill.

## Intended capability

The proposed skill was named `manage-ovh-dns`. It was intended to inspect,
plan, apply, and verify one bounded OVHcloud DNS record change through a fixed
controller on Atlas:

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl <command>
```

The controller, not the skill text, was intended to be the security boundary.
The first version allowed only the modification of one existing and unique `A`
record. It did not allow record creation, deletion, wildcard records, `AAAA`,
`CNAME`, `TXT`, `CAA`, `MX`, `NS`, or `SOA` changes.

## Intended security boundary

The prototype required all these rules:

- Never read, search, copy, display, validate, or back up an OVHcloud
  credential file.
- Never pass a credential through an argument, standard input, environment
  variable, prompt, log, or workspace file.
- Never call the OVHcloud API directly with `curl`, an SDK, a browser, or
  improvised code.
- Never modify the controller, its policy, a stored plan, a snapshot, its
  `sudoers` rule, or its activation marker during an operation.
- Never bypass a controller refusal.
- Stop when the controller is absent or unavailable. Do not improvise another
  path to OVHcloud.
- Never infer a zone, record, type, target, or TTL that is absent from the
  explicit request and the controller's sanitized output.

## Intended transaction

The following commands are preserved only as design evidence. They are not
available commands in the current repository.

### Capability check

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl doctor --json
```

The skill required a healthy response and an explicit active capability before
inspection or planning.

### Read-only inspection

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl inspect \
  --record '<fqdn>' \
  --type A \
  --json
```

An inspection request stopped after reporting the sanitized current state. It
did not create a plan.

### Plan

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl plan \
  --record '<fqdn>' \
  --type A \
  --target '<ipv4>' \
  --ttl '<ttl>' \
  --json
```

Before an apply request, the operator had to receive the exact FQDN and type,
the old and requested values and TTLs, the plan identifier, its SHA-256 digest,
its expiry, warnings, and the controller validation result.

### Apply after exact confirmation

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl apply \
  --plan-id '<plan-id>' \
  --plan-sha256 '<sha256>' \
  --json
```

The apply command accepted no target or TTL. It could consume only the
root-owned plan that the operator had explicitly confirmed. The controller had
to revalidate the current state before mutation.

The result kept these facts separate:

- the OVHcloud API result;
- the zone refresh result;
- the API read-back result;
- the authoritative DNS-server verification result.

### Ambiguous result

For an `applied_unverified` result, the design allowed one verification and no
second apply:

```text
sudo -n -- /usr/local/libexec/vps/ovh-dnsctl verify \
  --plan-id '<plan-id>' \
  --json
```

The skill did not allow automatic rollback.

## Intended stop conditions

The transaction stopped without another command when:

- the record, target, or TTL was ambiguous;
- zero or multiple records matched;
- the controller rejected the FQDN, type, target, or TTL;
- the plan was expired, consumed, or altered;
- the policy, zone, or record changed after planning;
- the API and authoritative DNS servers disagreed;
- the operation required creation, deletion, a mail record, delegation,
  wildcard behavior, or IPv6.

Only the sanitized refusal message could be retained. The prototype prohibited
credential discovery as an explanation path.

## Recovered agent metadata

The local prototype also contained this non-secret interface metadata:

```yaml
interface:
  display_name: Manage OVH DNS
  short_description: Plan and apply controlled OVH DNS changes
  default_prompt: Use $manage-ovh-dns to prepare and execute a controlled OVH DNS change.

policy:
  allow_implicit_invocation: false
```

## Conditions for a future active implementation

A future change must implement and test the controller before it adds an active
skill. It must prove the root-owned file modes, bounded policy, credential
isolation, plan integrity and expiry, replay refusal, compare-before-write
behavior, authoritative verification, sanitized logging, and fixed `sudoers`
surface. It must also document recovery and an independently reviewed
activation procedure.

Until those proofs exist, this archived document is the only versioned form of
the prototype.
