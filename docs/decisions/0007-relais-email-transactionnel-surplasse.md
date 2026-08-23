# ADR-0007: Use a managed transactional email relay for Surplasse

## Status

Accepted on 14 August 2026 for the target architecture. Updated on 23 August
2026 to select Scaleway Transactional Email (TEM). This decision records the
provider contract. It does not authorize a DNS change, credential creation,
secret materialization, or production activation.

ADR-0013 later enables repository admission for the Surplasse tester release.
It does not supersede the SMTP evidence gates or authorize runtime activation.

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
3. Use Scaleway TEM at `smtp.tem.scaleway.com` on port `587`.
4. Use the Scaleway Project ID as the SMTP username. Use the API Secret Key as
   the SMTP password. The API Access Key is not an SMTP input. Treat the
   username as a protected input even though it is not a password. Do not put
   either SMTP input or the Project ID in Git.
5. Use project `Pieper Atlas`. Create these three IAM applications and one API
   key for each application:

   - `surplasse-prod-smtp`;
   - `parkventory-prod-smtp`;
   - `monflorian-prod-smtp`.

   Attach each application to a policy scoped to project `Pieper Atlas`. Grant
   only `TransactionalEmailEmailSmtpCreate`.
6. The three applications use the same Project ID as their SMTP username. They
   use three different Secret Keys as their SMTP passwords. This separation
   supports independent storage, rotation, and revocation.
7. Scaleway IAM documents project scope for this permission. It does not
   document a TEM domain resource condition. Therefore, this repository does
   not claim strict isolation between domains. It infers that each key can send
   from any verified TEM domain in the project. Separate Scaleway projects are
   not required for the MVP.
8. The provider identity, relay host, port, application names, and permission
   set are public contract data. The exact credentials are not public contract
   data. Track their required paths and deployment state only in
   [`secrets/registry.json`](../../secrets/registry.json).
9. The OVH MX records remain unchanged. Add a provider SPF mechanism only when
   the selected provider specifies its exact value. Keep one SPF record. Use
   only domain-verification and DKIM values from the provider control plane.
   Publish DMARC with an operated report mailbox.
10. Do not add a mail transfer agent, inbound port `25`, SMTP queue volume, or
   provider secret to the common Atlas platform.
11. Keep five independent evidence gates:
   `transactional-email-provider`, `email-domain-authentication`,
   `smtp-atlas-connectivity`, `smtp-effective-runtime-configuration`, and
   `email-delivery-observability`.
12. Generic GitHub Actions evidence cannot satisfy an external evidence gate.
   Surplasse runtime activation stays blocked until reviewed evidence formats
   bind each result to the provider contract, Backend digest, collection time,
   and Atlas identity.

## Consequences

### Positive

- Atlas has no new inbound surface and no stateful mail service.
- A dedicated submission identity limits the secret scope.
- Separate keys isolate operational rotation and revocation between products.
- DNS, transport security, effective runtime configuration, and final delivery
  use separate evidence.
- A provider change requires review of the public contract and a bounded
  credential rotation.

### Negative

- Surplasse authentication depends on a third-party service.
- Operators must review service limits, costs, data residency, support, and
  contractual terms.
- Project-scoped IAM does not enforce a domain boundary between the three keys.
- Backend SMTP acceptance does not prove final delivery. Provider events and an
  external alert must cover bounces, complaints, and delays.

## Rollback

Before an authorized DNS change, export the complete zone and record each TTL.
If validation fails, keep the Surplasse runtime inactive and revoke the
dedicated SMTP identity. Restore the previous SPF, DKIM, and DMARC records
exactly. Do not change the OVH MX records. A provider change requires a new
review. An API key revocation does not change the TEM domain verification or
published DKIM records and does not require a DNS rollback. Do not use a silent
SMTP fallback.

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
