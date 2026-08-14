# ADR-0007: Use a managed transactional email relay for Surplasse

## Status

Accepted on 14 August 2026 for the target architecture. No provider is
selected. This decision does not authorize a purchase, a DNS change, a secret
change, or production activation.

## Context

Surplasse sends authentication magic links by email. An SMTP relay can accept a
message without delivering it to the recipient. SMTP acceptance also does not
prove SPF, DKIM, or DMARC alignment. It does not prove bounce and complaint
handling.

The Backend is an SMTP client. Its Atlas adapter requires a host, a port, a
username, and a password. The common platform has no mail transfer agent. Only
SSH, HTTP, and HTTPS use public Atlas ports. The target contract preserves the
reviewed OVH MX records as the inbound mail service. Application delivery is a
separate path.

A public Postfix or Exim service on Atlas would add a persistent queue, another
public service, backup requirements, and monitoring requirements. It would not
provide sender reputation, reverse DNS, DKIM signing, or event processing by
itself. It would also make authentication email depend on the single VPS.

## Considered options

### Public mail transfer agent on Atlas

Rejected. This option exceeds the application requirement. It couples the
authentication channel to Atlas and its IP address.

### Local Postfix relay

Rejected. Quarkus can connect directly to an authenticated relay. A local queue
would add state and another failure point without proving external delivery.

### Managed transactional email relay

Accepted. The provider operates SMTP submission, sender reputation, and
delivery events. Atlas does not expose another inbound port.

## Decision

1. The Backend connects directly to a managed transactional email relay on port
   `587`. It requires STARTTLS, normal certificate-chain validation, and
   hostname validation.
2. The application sender is exactly `no-reply@surplasse.com`.
3. Only the SMTP username and password are secret. The provider identity, relay
   host, port, and expected DNS records form a reviewed public contract. This
   repository does not contain that contract yet. A later reviewed change must
   add it before provider activation.
4. The OVH MX records remain unchanged. Add a provider SPF mechanism only when
   the selected provider specifies its exact value. Keep one SPF record. Use
   only domain-verification and DKIM values from the provider control plane.
   Publish DMARC with an operated report mailbox.
5. Do not add a mail transfer agent, inbound port `25`, SMTP queue volume, or
   provider secret to the common Atlas platform.
6. Keep five independent evidence gates:
   `transactional-email-provider`, `email-domain-authentication`,
   `smtp-atlas-connectivity`, `smtp-effective-runtime-configuration`, and
   `email-delivery-observability`.
7. Generic GitHub Actions evidence cannot satisfy an external evidence gate.
   Surplasse stays disabled until reviewed evidence formats bind each result to
   the provider contract, Backend digest, collection time, and Atlas identity.

## Consequences

### Positive

- Atlas has no new inbound surface and no stateful mail service.
- A dedicated submission identity limits the secret scope.
- DNS, transport security, effective runtime configuration, and final delivery
  use separate evidence.
- A provider change requires review of the public contract and a bounded
  credential rotation.

### Negative

- Surplasse authentication depends on a third-party service.
- Operators must review service limits, costs, data residency, support, and
  contractual terms.
- Backend SMTP acceptance does not prove final delivery. Provider events and an
  external alert must cover bounces, complaints, and delays.

## Rollback

Before an authorized DNS change, export the complete zone and record each TTL.
If validation fails, keep Surplasse disabled and revoke the dedicated SMTP
identity. Restore the previous SPF, DKIM, and DMARC records exactly. Do not
change the OVH MX records. A provider change requires a new review. Do not use a
silent SMTP fallback.

## Verification

The [Surplasse SMTP relay runbook](../operations/surplasse-smtp.md) defines the
required evidence. Activation requires all these results:

- a reviewed public provider contract that contains no secret;
- unchanged OVH MX records and one exact SPF record;
- the exact DKIM and DMARC records;
- an Atlas connection to the public relay FQDN on port `587`, with STARTTLS,
  certificate-chain validation, and hostname validation;
- inspection of the exact Backend image by digest and a sanitized view of the
  started process, with no embedded SMTP or TLS override;
- received magic links in controlled Gmail, Outlook, and OVH test mailboxes,
  with aligned `Authentication-Results` headers;
- an observed hard bounce, a simulated relay failure, and a received operator
  alert.
