# Repository Instructions

## Scope

This public repository is the source of truth for the VPS host, the shared
platform, and the selected release state. Never add a secret, a private key, a
production inventory, or business data to this repository.

## Technical Writing

- Write in clear technical English. Follow ASD-STE100 Simplified Technical
  English principles.
- Use precise and unambiguous terminology. Use short declarative sentences.
- Use active voice when it makes responsibility clear.
- Use one term consistently for each concept. Do not use unnecessary synonyms.
- Do not use vague language, marketing language, or unnecessary jargon.
- Use ISO/IEC/IEEE 24765 terminology when it applies.
- Apply these rules to documentation, operator messages, code comments, pull
  requests, and commit messages.

## Engineering Rules

- Reference each production image and artifact by an immutable digest.
- Do not run application builds on the production VPS.
- Require explicit validation for PostgreSQL, Caddy, migration, and secret
  changes. Do not use a silent fallback.
- Make each script safe by default. Validate or use a dry run before a mutation.
  Bound each target. Reject each missing variable.
- Never create a real inventory, a `.env` file, a key, a token, or a decrypted
  file in Git.
- Any task that plans or requires deployment, rotation, or revocation of a
  product or platform secret on Atlas must update `secrets/registry.json` before
  completion. This rule also applies when the task starts in a product
  repository. If the task cannot update `vps-infra`, report the blocker and do
  not claim completion. Commit the target generation before the operation.
  The materializer must write a non-secret generation marker atomically with
  the exact file set. After the operation, use a read-only audit to verify that
  marker before you update the observed generation. A metadata-only audit can
  mark a file `materialized`, but it must keep generation `0` and binding
  `unlinked`. No current materializer writes this marker. Do not advance a
  generation or mark `runtime-loaded` until the marker exists and the current
  consumer has generation-bound runtime proof. Never record a value, a
  content-derived digest, or a private source path.
- Run `make check` before each commit.

## External State

OVHcloud addresses, API identifiers, DNS zones, SSH keys, and production
secrets are supplied separately. Their absence is an expected gate. Do not
invent a value.

## Agent skills

### Issue tracker

Use GitHub Issues for tickets and specifications. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default Matt Pocock triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Use one root context and the existing decision records. See `docs/agents/domain.md`.
