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
  consumer and generation. A file on disk or a Compose declaration is not
  enough.
- `provider_state` records only reviewed lifecycle metadata. It does not test a
  provider credential.

`generation` is the last generation observed on the host. Generation `0` means
that no host generation has been materialized. `target_generation` is the next
generation required by Git. It must equal `generation` or exceed it by one. A
task must increment `target_generation` before a first materialization or a
rotation. After a successful read-only audit, set `generation` to the target.
Do not derive either number from the secret value.

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

The audit found no other registered file. No entry has runtime-loaded evidence.
All Scaleway Transactional Email credentials are absent. The registry records
their intended contracts only.

The registry also preserves the known generation of the temporary Surplasse
OVH cutover identity. That provider credential is revoked and its three host
files are absent. A revoked generation is historical evidence, not a recovery
input.

## Required update sequence

1. Add the secret or protected input with `generation: 0`,
   `target_generation: 1`, and `host_state: absent`. For a rotation, increment
   `target_generation` before the operation.
2. Create or recover the value outside Git. Do not send it through chat, an
   issue, a pull request, a command argument, or a shared log.
3. Use the exact bounded materializer named by the registry. Leave the entry
   planned if no materializer exists.
4. Run a read-only metadata audit on Atlas.
5. Set `generation` to `target_generation`. Update the host state, audit time,
   and controller revision. Review and commit this metadata change.
6. Recreate and probe each consumer when the value must enter a running
   service. Set `runtime-loaded` only after that proof.

For a rotation, commit the new target generation before use. Materialize and
verify it, recreate the affected consumer, then revoke the old provider
generation. Record the final provider and host states in Git. A failed rotation
must keep or restore the last proved runtime generation.

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

The Parkventory PostgreSQL helper creates the migrator and runtime passwords
once and publishes a root-only local manifest. The application remains
disabled, and the baseline audit found neither file. The three admitted OIDC
files and two SMTP files also remain absent. They have no operator materializer
yet. Do not confuse the PostgreSQL helper with a complete application secret
restore path.

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
