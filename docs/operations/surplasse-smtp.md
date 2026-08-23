# Prepare the Surplasse SMTP relay

This runbook defines the evidence required for the Surplasse email channel.
Scaleway Transactional Email (TEM) is the selected outbound relay. Atlas does
not run a mail transfer agent. This runbook does not authorize a DNS change,
credential creation, secret materialization, remote execution, or production
activation.

[ADR-0007](../decisions/0007-relais-email-transactionnel-surplasse.md)
defines the architecture. The
[locked Surplasse adapter](../../applications/surplasse/README.md) defines the
current application boundary.

## Historical DNS observation

A public DNS observation on 13 August 2026 recorded these answers:

```text
surplasse.com. MX 1   mx1.mail.ovh.net.
surplasse.com. MX 5   mx2.mail.ovh.net.
surplasse.com. MX 100 mx3.mail.ovh.net.
surplasse.com. TXT    v=spf1 include:mx.ovh.com -all
```

It recorded no public DMARC answer. Queries for common DKIM selectors did not
prove that no DKIM selector existed. This is a historical observation, not
current DNS evidence. Export the complete OVH zone before any conclusion or
change.

## Required authority and inputs

The operator must supply these items outside Git:

- acceptance of current service limits, costs, data residency, DPA, support,
  and SLA terms;
- access to Scaleway project `Pieper Atlas`;
- authorization to verify `surplasse.com` and to change its SPF, DKIM, and DMARC
  records;
- an operated DMARC report mailbox;
- the Project ID and the Secret Key for IAM application
  `surplasse-prod-smtp`, through the approved private channel;
- the exact Atlas target and separate authority for any remote execution.

The selected service must support authenticated SMTP submission on port `587`
with required STARTTLS. It must provide exact sender-authentication values and
delivery, bounce, and complaint events. Provider selection is complete.
Credential provisioning and materialization remain separate controlled
changes.

## Current repository boundary

The public provider contract uses `smtp.tem.scaleway.com` on port `587` with
STARTTLS. The SMTP username is the Scaleway Project ID. The SMTP password is the
API Secret Key. The Access Key is not an SMTP input. Git must not contain the
Project ID, Secret Key, Access Key, test recipient, or message content.

The required paths and their observed state are in
[`secrets/registry.json`](../../secrets/registry.json). The registry declares
the three IAM application names:

- `surplasse-prod-smtp`;
- `parkventory-prod-smtp`;
- `monflorian-prod-smtp`.

Each application must have one API key and one policy scoped to project
`Pieper Atlas`. The policy must grant only
`TransactionalEmailEmailSmtpCreate`. The three keys separate storage, rotation,
and revocation. They do not enforce a domain boundary. TEM does not appear in
Scaleway's documented products that support resource-level conditions. This
repository therefore infers that a project-scoped key can send from any
verified TEM domain in the project.

`materialize-smtp-secrets` owns the three exact SMTP credential sets and writes
a registry generation marker. Surplasse and Parkventory declare runtime paths.
Mon Florian is storage-only and has no SMTP runtime consumer. The separate
Parkventory provider-bundle helper owns its Auth0 input and public runtime
configuration. It must not rotate the SMTP files.

Repository admission, credential creation, and tester-order authorization do
not prove this email channel. Technical activation remains incomplete.

## Provider review

Review these properties before credential activation:

- SMTP submission on port `587` with required STARTTLS and normal certificate
  validation;
- the exact project-scoped IAM policy and dedicated application;
- exact domain-verification and DKIM values;
- the exact SPF requirement, including whether the provider needs no SPF
  mechanism;
- authenticated and documented delivery, bounce, complaint, and delay events;
- quotas, rate limits, retention, data residency, DPA, support, SLA, and total
  cost;
- credential rotation and immediate revocation;
- a tested service-status and operator-alert path.

A successful TLS handshake does not prove account eligibility, delivery, event
processing, or contractual acceptance.

## Review DNS before a change

1. Export all A, AAAA, CNAME, MX, TXT, CAA, wildcard, redirect, and TTL data for
   the complete zone. Keep the dated export and its identity outside Git.
2. Compare each provider value with the reviewed public contract.
3. Change the existing SPF record only when the provider supplies an exact
   mechanism. Never publish two TXT records that start with `v=spf1`.
4. Add only the exact domain-verification and DKIM values from the provider
   control plane. Do not infer a selector or target.
5. Start DMARC with `p=none` and an operated report mailbox. A later change to
   `quarantine` or `reject` requires review of reports and all legitimate
   senders.
6. Do not change an MX record.

DNS propagation does not satisfy the release gates by itself. Evidence must
bind the observed records to the reviewed provider contract and collection
time.

Scaleway TEM sends transactional email. It does not replace the inbound OVH
mail service. Keep the MX records on OVH. An address such as
`contact@surplasse.com` needs an OVH mailbox, alias, or redirect only when the
MVP must receive messages at that address. A `no-reply@surplasse.com` sender
does not require an inbound mailbox.

## Materialize the credentials

Create the three declared IAM applications, policies, and API keys. Grant only
`TransactionalEmailEmailSmtpCreate` in project `Pieper Atlas`. Store the values
in the protected GitHub `application-production` environment under these
names:

- `SURPLASSE_SMTP_USERNAME` and `SURPLASSE_SMTP_PASSWORD`;
- `PARKVENTORY_SMTP_USERNAME` and `PARKVENTORY_SMTP_PASSWORD`;
- `MONFLORIAN_SMTP_USERNAME` and `MONFLORIAN_SMTP_PASSWORD`.

The username values are the shared Project ID. Each password is the dedicated
Secret Key. Do not put either value in a contract, command argument, log,
issue, or pull request. Do not transfer or store the Access Key for SMTP.
Revoke an exposed value before use.

Update `secrets/registry.json` in two separate steps. First, review the planned
contract and target generation before materialization. Run the exact product
profile at generation `1`:

```text
materialize-smtp-secrets --product surplasse --registry-generation 1 --install-from /absolute/root-only/directory
materialize-smtp-secrets --product parkventory --registry-generation 1 --install-from /absolute/root-only/directory
materialize-smtp-secrets --product monflorian --registry-generation 1 --install-from /absolute/root-only/directory
```

Run each profile again with `--check` instead of `--install-from`. Advance the
registry to generation `1` only after the read-only check verifies the exact
marker and file metadata. Do not use `runtime-loaded` without separate consumer
proof. Never add a value-derived hash to Git.

The materializer input must use port `587`. Its FQDN must equal the reviewed
public provider contract. The rendered adapter requires
`QUARKUS_MAILER_AUTH_METHODS=PLAIN LOGIN`. It rejects other declared `SMTP_*`,
`QUARKUS_MAILER_*`, `QUARKUS_TLS_*`, and Java options that could disable
STARTTLS, certificate validation, or real delivery.

The Compose validation cannot detect a value embedded in the Backend image or
exported by its entrypoint. Before satisfying
`smtp-effective-runtime-configuration`, inspect the exact Backend image by
digest. Inspect its configuration and entrypoint. Start the candidate in an
isolated environment and collect a sanitized view of the effective process
environment. The evidence must confirm the relay FQDN, port, sender, and TLS
mode without a username or password.

## Prove Atlas connectivity

Evidence for `smtp-atlas-connectivity` must come from the authorized Atlas
identity and the exact public relay FQDN. It must record the collection time,
resolved public addresses, port `587`, STARTTLS negotiation, certificate chain,
certificate hostname, TLS parameters, and compatible SMTP AUTH mechanisms. It
must bind these values to the reviewed provider contract.

The connectivity check must not log credentials or send a message. Connectivity
evidence does not prove authentication, final delivery, or the health of every
provider address.

## Prove delivery and observability

Before satisfying `email-delivery-observability`:

1. Send real magic links to controlled Gmail, Outlook, and OVH test mailboxes.
2. Record delivery latency. Confirm SPF, DKIM, and DMARC alignment for
   `surplasse.com` in each received `Authentication-Results` header.
3. Cause a hard bounce with an address approved for provider testing. Confirm
   the provider event.
4. Simulate relay refusal or unavailability. Confirm the Backend behavior.
5. Trigger the delivery alert. Confirm that an operator receives it.
6. Keep only dated, non-sensitive evidence. Do not keep a magic link, message
   body, or personal recipient address.

SMTP acceptance by the Backend is not receipt evidence. Authenticate each
provider event input. Process it with bounded, idempotent logic before it can
affect application state.

## Rotate or revoke credentials

Rotation for an active release is not implemented. Do not recreate the Backend
manually. The future rotation path must create a new identity, validate it
without logging it, publish and verify the non-secret generation marker,
recreate only the Backend with explicit marker-bound runtime evidence, pass its
probes, and then revoke the old identity. It must restore the previous
generation after failure. An atomic host-file rename does not update a Docker
file bind mount that is already open.

Revoking an IAM API key does not change the TEM domain verification or the
published DKIM records. Do not remove or replace DNS records as part of an API
key rotation or revocation.

## Roll back

If validation fails:

1. Keep or return the adapter to its locked state.
2. Stop new magic-link attempts when they can create false confirmation for a
   user.
3. Revoke the failing SMTP identity.
4. Restore the previous SPF, DKIM, and DMARC records exactly from the zone
   export.
5. Confirm that the three OVH MX records did not change.
6. Repeat DNS, STARTTLS, delivery, bounce, failure, and alert evidence before
   any activation review.

Do not use a silent fallback provider. A provider change requires a new public
contract, credential rotation, DNS review, and all five independent evidence
gates.
