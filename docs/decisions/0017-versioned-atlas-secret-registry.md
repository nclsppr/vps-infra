# ADR-0017: Track Atlas secret metadata in Git

## Status

Accepted on 23 August 2026. This decision adds a public metadata registry. It
does not create, copy, rotate, revoke, decrypt, or load a secret. It does not
change DNS, start an application, or prove a provider credential.

## Context

The repository already defines several strict secret file contracts. The
platform helper generates four local values. The Surplasse helper manages its
database and operator files. A separate helper manages the permanent OVH DNS
identity. Ansible can copy the Mon Florian OpenAI key from a private source.
Parkventory has a materializer for its two PostgreSQL passwords, but not for
SMTP inputs.

These contracts are distributed across documentation, Ansible, Python helpers,
Compose files, and tests. Git did not contain one complete inventory. Git also
did not record which generation a read-only Atlas audit had observed.

The Surplasse application and DNS helpers publish local root-only manifests.
Those manifests contain content-derived SHA-256 values. They prove that one
local file set is complete. They are not versioned deployment evidence and must
not enter this public repository.

## Decision

Add `secrets/registry.json` as the public inventory for every product and
platform secret and protected input under `/etc/vps/secrets`. Validate it with
`schemas/secret-registry.schema.json` and repository checks. Service-generated
state and interactive operator authentication use separate recovery contracts.
This exception must not be used for a product deployment secret.

The registry records only non-secret metadata:

- a stable identifier and scope;
- the provider and public credential-set name when applicable;
- the required provider permission set;
- the Atlas path, owner, group, and mode;
- the bounded materializer and consumers;
- the source and recovery method;
- declared, host, and provider states;
- the last observed generation and the next target generation;
- the audit time and installed controller revision;
- the recovery-system state for the complete registry.

The registry must never contain a secret value, private key, project ID, API
identifier, private source path, decrypted payload, or content-derived digest.
The public credential-set name is metadata. It is not a provider credential.

Register a value before materialization. Before a rotation, increment its target
generation. After the operation, run a read-only metadata audit and update the
observed generation. A materialized state proves only the file path and
metadata observed during that audit. A runtime-loaded state requires separate
proof that the current consumer loaded the declared generation and passed its
probes.

Git history records each reviewed registry update. Atlas keeps no Git write
credential. An operator performs the audit from a trusted workstation and
submits the metadata change through the normal reviewed Git path.

Keep local content digests in the existing root-only manifests on Atlas. Each
registry generation is an opaque counter. It is not derived from a secret. The
target can exceed the observed generation by one while an authorized operation
is pending.

## Baseline observation

The read-only audit on 23 August 2026 found exactly six materialized files:

- four platform secrets;
- the Surplasse PostgreSQL migrator password;
- the Surplasse PostgreSQL runtime password.

No other registered file was present. No entry had runtime-loaded evidence.
All Scaleway Transactional Email entries were absent.

The three files for the temporary Surplasse OVH cutover identity were absent.
Their shared provider generation is recorded as revoked and must not be
restored.

## Transactional email metadata

The target provider is Scaleway Transactional Email in the existing `Pieper
Atlas` project. Use three IAM applications:

- `surplasse-prod-smtp`;
- `parkventory-prod-smtp`;
- `monflorian-prod-smtp`.

Limit each application to the project and permission
`TransactionalEmailEmailSmtpCreate`. Give each application its own API key.
For SMTP, the shared project ID is the username and each Secret Key is a
different password. The Access Key is not an SMTP input.

Three keys in one project separate storage, rotation, and revocation. The IAM
permission remains project-scoped, so this design does not enforce strict
per-domain isolation. Separate Scaleway projects are not required for the MVP.

The registry declares these credential sets as planned or required, but absent.
It contains no project ID or key. Surplasse has an existing SMTP file contract.
Parkventory has no SMTP materializer. Mon Florian has neither an admitted SMTP
runtime contract nor a materializer. Do not claim TEM readiness or create an
orphan secret file.

Scaleway TEM sends transactional mail only. OVH MX records and published DKIM
records are separate. Receiving mail at an address such as
`contact@surplasse.com` still requires an OVH mailbox or forwarding rule. The
MVP does not require that inbound path when applications send only from
`no-reply` addresses.

## Recovery boundary

The registry reproduces the file contract and lifecycle record. It does not
recover the value.

The repository has no SOPS payload, no `.sops.yaml` policy, and no proved age
recovery identity. The registry therefore declares `value_recovery_state` as
`not-configured`. Keep provider values in an approved external store. The six
materialized generated values also need external recovery before a fresh-host
restore can be claimed. Database backups can retain role credentials that do
not match newly generated files.

A later decision may add encrypted SOPS files after it proves the public age
recipient, external private-key recovery, rights separation, and a complete
restore exercise. Until then, SOPS is a blocked recovery design, not an
implemented control.

## Consequences

- Git can show the last reviewed metadata observation for every known Atlas
  secret and protected input.
- Git cannot prove that the observation is still current after its audit time.
- A local manifest remains the file-set commit marker. It is not a Git record.
- A planned entry does not authorize provider provisioning or host mutation.
- A materialized entry does not authorize application activation.
- Secret recovery remains dependent on an external store or an explicit
  regeneration procedure.

## Alternatives

### Commit local manifest digests

Rejected. Public secret fingerprints add disclosure and correlation risk. They
do not recover a value or prove runtime use.

### Use only local manifests

Rejected. A lost VPS would also lose the only generation record.

### Add SOPS files now

Rejected. The repository has no proved age identity or recovery exercise.
