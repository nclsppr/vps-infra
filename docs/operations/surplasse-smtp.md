# Prepare the Surplasse SMTP relay

This runbook defines the evidence required for the Surplasse email channel.
Atlas does not run a mail transfer agent. This runbook does not authorize a
provider purchase, a DNS change, a secret change, remote execution, or
production activation.

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

- explicit provider selection;
- acceptance of current service limits, costs, data residency, DPA, support,
  and SLA terms;
- a provider account or project dedicated to Surplasse;
- authorization to verify `surplasse.com` and to change its SPF, DKIM, and DMARC
  records;
- an operated DMARC report mailbox;
- dedicated SMTP credentials through the approved private channel;
- the exact Atlas target and separate authority for any remote execution.

The selected service must support authenticated SMTP submission on port `587`
with required STARTTLS. It must provide exact sender-authentication values and
delivery, bounce, and complaint events. Provider selection and provisioning are
separate reviewed changes.

## Current repository boundary

The repository does not contain a public provider contract or an accepted
external evidence format. It cannot bind an SMTP host to a provider, validate
provider DNS values, or satisfy an external email gate. Surplasse therefore
stays disabled.

A later change can define the public provider contract only after selection.
The contract must contain no username, password, token, test recipient, or
message content. It must bind the release to the exact public relay FQDN, port,
sender, and provider-supplied DNS values.

## Provider review

Review these properties before provider selection:

- SMTP submission on port `587` with required STARTTLS and normal certificate
  validation;
- an account boundary and submission identity dedicated to Surplasse;
- exact domain-verification and DKIM values;
- the exact SPF requirement, including whether the provider needs no SPF
  mechanism;
- authenticated and documented delivery, bounce, complaint, and delay events;
- quotas, rate limits, retention, data residency, DPA, support, SLA, and total
  cost;
- credential rotation and immediate revocation;
- a tested service-status and operator-alert path.

Do not select a provider from an undocumented connectivity test. A successful
TLS handshake does not prove account eligibility, delivery, event processing,
or contractual acceptance.

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

## Materialize the credentials

Create a dedicated submission identity with the minimum permissions. Transfer
the username and password through the private channel defined by the
[adapter input contract](../../applications/surplasse/README.md#operator-input-contract).
Do not put either value in a contract, command, log, issue, or pull request.
Revoke an exposed value before use.

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
without logging it, recreate only the Backend with an explicit generation
binding, pass its probes, and then revoke the old identity. It must restore the
previous generation after failure. An atomic host-file rename does not update a
Docker file bind mount that is already open.

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
