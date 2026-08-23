# Atlas secret contract

This directory contains the public secret contract. It contains no secret
value, private key, decrypted file, private source path, or content-derived
digest.

[`registry.json`](registry.json) is the versioned inventory for product and
platform secrets and protected inputs under `/etc/vps/secrets`. Its schema is
[`secret-registry.schema.json`](../schemas/secret-registry.schema.json). Every
entry must exist in the registry before an operator materializes or rotates its
value on Atlas.

The registry does not include service-generated state such as Caddy ACME data
or an interactive Codex login. Their recovery procedures remain separate. A
product must not use that exception. Put each new product deployment secret
under `/etc/vps/secrets` and add it to the registry.

## Evidence states

The registry separates policy, host evidence, and provider evidence.

- `declared_state` records whether the contract is `planned`, `required`, or
  `temporary`.
- `host_state: absent` means that the last read-only audit did not find the
  file.
- `host_state: materialized` means that the last read-only audit found one file
  with the declared path, owner, group, mode, type, and link count. This state
  does not prove that a service loaded the value.
- `host_state: runtime-loaded` requires separate runtime proof for the current
  consumer and a marker-bound generation. A file on disk, a Compose
  declaration, or a successful probe by itself is not enough.
- `provider_state` records only reviewed lifecycle metadata. It does not test a
  provider credential.
- `generation_binding: unlinked` means that no audited marker connects the
  installed file set to the target generation. `materializer-marker` means
  that the last read-only audit verified such a marker.

`generation` is the last target generation bound to the installed file set by
an audited materializer marker. Generation `0` means that no such binding
exists. A file can therefore be `materialized` with generation `0`.
`target_generation` is the next generation required by Git. It must equal
`generation` or exceed it by one. A task must increment `target_generation`
before a first materialization or a rotation. Do not derive either number from
the secret value.

The bounded materializer must publish the non-secret generation marker last,
after it installs and synchronizes the exact file set. The marker must contain
only public contract identifiers, the exact registered secret identifiers, and
the target generation. A read-only audit must verify that marker before Git can
set `generation_binding` to `materializer-marker` and advance `generation`.
The two Parkventory materializers write separate markers for the four generated
files and the three provider files. Other root-only manifests bind files
through private content digests, but they do not contain a registry generation.
They do not satisfy this requirement.

`observed_at` and `controller_revision` bind the registry to one audit. A Git
commit preserves that reviewed observation. It is not permanent proof of the
current host state.

`value_recovery_state` describes the recovery system for the complete registry.
The baseline value is `not-configured`. Do not claim fresh-host secret recovery
until this field is `verified` and a restore exercise supports that state.

## Baseline audit

The read-only Atlas audit on 23 August 2026 found exactly six materialized
files:

- the four platform files under `/etc/vps/secrets/platform`;
- `surplasse-postgres-migrator-password`;
- `surplasse-postgres-runtime-password`.

The audit found no other registered file. The six files have generation `0`
and binding `unlinked` because the materializers did not write a generation
marker. No entry has runtime-loaded evidence. All Scaleway Transactional Email
credentials are absent. The registry records their intended contracts only.

The registry preserves the revoked provider state of the temporary Surplasse
OVH cutover identity. Its three host files are absent. It has no marker-bound
host generation and it is not a recovery input.

## Required update sequence

1. Add the secret or protected input with `generation: 0`,
   `target_generation: 1`, and `host_state: absent`. For a rotation, increment
   `target_generation` before the operation.
2. Create or recover the value outside Git. Do not send it through chat, an
   issue, a pull request, a command argument, or a shared log.
3. Use the exact bounded materializer named by the registry. The materializer
   must publish the non-secret generation marker atomically with the file set.
   Leave the entry planned if no materializer exists. Keep the binding
   `unlinked` when the materializer has no generation marker.
4. Run a read-only audit on Atlas. Verify file metadata separately from the
   generation marker.
5. Set `host_state` from the file audit. Set `generation` to
   `target_generation` and set the binding to `materializer-marker` only when
   the audit verified the exact marker. Update the audit time and controller
   revision. Review and commit this metadata change.
6. Recreate and probe each consumer when the value must enter a running
   service. Bind the runtime evidence to the verified marker generation. Set
   `runtime-loaded` only after that proof.

For a rotation, commit the new target generation before use. Materialize the
new file set and publish its marker, verify both, recreate the affected
consumer, and then revoke the old provider generation. Record the final
provider and host states in Git. A failed rotation must keep or restore the
last proved runtime generation.

The local Surplasse and OVH DNS manifests contain secret digests so that Atlas
can detect partial writes. They remain root-only on Atlas. Never copy those
digests into the public registry.

## Recovery and SOPS gate

The registry reproduces paths, permissions, consumers, and lifecycle metadata.
It cannot reproduce a secret value.

The repository has no SOPS payload, no `.sops.yaml` policy, and no proved age
recovery identity. SOPS recovery is blocked. Keep each required value in an
approved external secret store or use the declared regeneration procedure.
A later reviewed change may add encrypted SOPS files only after it proves the
public age recipient, external private-key recovery, file separation, and a
complete restore exercise.

## General rules

- Use a separate identity and file when permissions or lifecycle differ.
- Revoke a value after any exposure, even if Git history is later rewritten.
- Keep Compose references under `/run/secrets`.
- Reject a missing variable, unexpected file, symbolic link, extra hard link,
  wrong owner, wrong group, or wrong mode.
- Do not treat materialization as runtime use, activation, provider scope, or
  delivery proof.

## Platform

The internal controller owns these files under
`/etc/vps/secrets/platform`:

| File | Expected owner and mode |
|---|---|
| `postgres-superuser-password` | `root:70 0440` |
| `postgres-exporter-password` | `root:70 0440` |
| `grafana-admin-password` | `root:472 0440` |
| `grafana-secret-key` | `root:472 0440` |

The parent is `root:root 0700`. The controller creates each value once. It
rejects an unsafe or invalid existing file and never prints a value. The
baseline values are not recoverable from Git or a verified external store.
Their registry entries require external recovery because a data restore can
depend on the existing database role credentials.

## Permanent Surplasse DNS identity

The permanent Caddy DNS identity lives under
`/etc/vps/secrets/dns/surplasse`:

| File | Expected owner and mode |
|---|---|
| `ovh-application-key` | `root:root 0400` |
| `ovh-application-secret` | `root:root 0400` |
| `ovh-consumer-key` | `root:root 0400` |

The `materialize-surplasse-dns-secrets` helper accepts one exact private source
directory. It stages the three files, synchronizes them, and publishes its
root-only manifest last. The local contract does not prove the OVH IAM scope.
Keep this long-lived identity separate from a temporary DNS cutover identity.

## Surplasse application inputs

The Surplasse preparation controller generates separate migrator and runtime
database passwords. Its helper also validates the complete nine-file operator
bundle. Seven supplied values and the two generated database passwords are
application-readable files under `/etc/vps/secrets/surplasse`. The JWT key ID
and SMTP host remain controller-only inputs. The helper publishes their public
runtime form in `/etc/vps/applications/surplasse.env`.

The complete contract, validation, lock order, and local manifest rules are in
[`applications/surplasse/README.md`](../applications/surplasse/README.md).
The helper does not support a partial SMTP-only install. Runtime rotation is not
implemented.

## Parkventory

The Parkventory helper creates the migrator and runtime passwords and the two
local OIDC secrets once. It publishes the database manifest first and the
non-secret generation marker last. The marker binds target generation 1 to the
exact four registered files. The helper does not read or write the Auth0 client
secret or the two SMTP inputs.

`materialize-parkventory-provider-secrets` validates one exact root-only source
directory. It installs those three provider files and the fixed public runtime
configuration under the shared deployment lock. It publishes a separate
generation-1 marker last for the exact three registered provider identifiers.
It does not print or record a value, content digest, or source path. The
provider state stays `planned` until a separate provider audit verifies the
Auth0 and Scaleway TEM credentials.

## Mon Florian

The application contract admits one OpenAI API key. Normal convergence copies
it only when the operator supplies an explicit private source. Its SMTP pair is
planned in the registry but has no admitted runtime contract or materializer.
The baseline audit found no Mon Florian secret file.

## Scaleway Transactional Email

The registry reserves three separate credential sets in the same Scaleway
project:

- `scaleway-tem:surplasse-prod-smtp`;
- `scaleway-tem:parkventory-prod-smtp`;
- `scaleway-tem:monflorian-prod-smtp`.

Each IAM application is limited to the project and permission
`TransactionalEmailEmailSmtpCreate`. This separation supports independent
storage, rotation, and revocation. It does not isolate one domain from another
inside the shared project.

For SMTP, the project ID is the username and the Secret Key is the password.
The Access Key is not an SMTP input and must not be deployed for this purpose.
The registry contains none of these values.

Surplasse has an existing SMTP file contract. Parkventory has declared SMTP
paths but no SMTP materializer. Mon Florian has planned registry entries but no
admitted SMTP runtime contract or materializer. Do not create orphan files for
either application. Scaleway TEM is outbound only. It does not provide the OVH
mailbox or forwarding needed to receive mail at a domain address.
