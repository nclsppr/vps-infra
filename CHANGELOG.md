# Changelog

## Unreleased

### Added

- Prepare an inactive, fail-closed Parkventory public-beta application path.
- Define the private PostgreSQL network, database, and separate owner,
  migrator, and runtime roles without `BYPASSRLS`.
- Admit the immutable candidate's exact OIDC public variables and root-owned
  secret files without storing secret bytes in Git.
- Add a manual Parkventory-only deployment workflow that remains inert while
  application admission is disabled and requires a separate protected
  environment activation switch for any future dispatch.
- Add disabled-by-default Prometheus discovery and alert candidates, bounded
  local logs, transactional probes, rollback documentation, and PostgreSQL
  backup/restore readiness checks. The inactive Prometheus candidate includes
  its exact scrape job and application-network attachment.

### Security

- Keep the public static Parkventory profile enabled and the Compose
  application profile disabled.
- Keep application activation fail-closed until reviewed PostgreSQL readiness
  evidence and a verified encrypted off-site backup/restore receipt exist.
- Reject unexpected direct or inherited Parkventory database access, including
  object ACL drift introduced by a migration, before starting the runtime.
- Stop and verify the absence of every Parkventory runtime container when a
  migration ends without a valid PostgreSQL proof. Never restart the previous
  runtime against that untrusted database state.
- Revalidate the encrypted off-site backup proof after migration before the
  first runtime start and again immediately before the durable runtime commit.

No Atlas convergence, GitHub environment change, provider provisioning, DNS
cutover, or public application activation is part of this change.
