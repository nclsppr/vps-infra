# Changelog

## Unreleased

### Added

- Prepare an inactive, fail-closed Parkventory public-launch application path.
- Define the private PostgreSQL network, database, and separate owner,
  migrator, and runtime roles without `BYPASSRLS`.
- Admit the immutable candidate's exact OIDC public variables and root-owned
  secret files without storing secret bytes in Git.
- Generate the two local OIDC secrets with the Parkventory database passwords.
  Publish a non-secret generation marker last for the exact four-file set.
- Add a locked Parkventory provider-bundle importer for the Auth0 client
  secret, SMTP pair, and fixed public runtime configuration. Publish its
  separate three-file generation marker last.
- Add a manual Parkventory-only deployment workflow that remains inert while
  application admission is disabled and requires a separate protected
  environment activation switch for any future dispatch.
- Add disabled-by-default Prometheus discovery and alert candidates, bounded
  local logs, transactional probes, rollback documentation, and PostgreSQL
  backup/restore readiness checks. The inactive Prometheus candidate includes
  its exact scrape job and application-network attachment.
- Add a durable Caddy attachment and crash-safe static-to-Compose handoff that
  restores the static state, route, and public health on failure.

### Security

- Keep the public static Parkventory profile enabled and the Compose
  application profile disabled.
- Keep application activation fail-closed until reviewed PostgreSQL readiness
  evidence and a fresh local backup/restore proof exist. Record encrypted
  off-site backup proof as deferred for the first public launch.
- Reject unexpected direct or inherited Parkventory database access, including
  object ACL drift introduced by a migration, before starting the runtime.
- Stop and verify the absence of every Parkventory runtime container when a
  migration ends without a valid PostgreSQL proof. Never restart the previous
  runtime against that untrusted database state.
- Revalidate the local backup and restore proof after migration before the
  first runtime start and again before the durable runtime commit.
- Keep the Auth0 client secret and both SMTP inputs outside the local
  Parkventory generator. Import them only from an exact root-only source.

No Atlas convergence, GitHub environment change, provider provisioning, DNS
cutover, or public application activation is part of this change.
