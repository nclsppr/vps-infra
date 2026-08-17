# ADR-0009: Admit immutable Compose application releases

## Status

Accepted on 17 August 2026. Surplasse and Parkventory remain disabled. This
decision adds admission only. It does not install an application, run a
migration, change a secret, open the dynamic controller, or send a command to
Atlas.

## Context

Surplasse and Parkventory use a Java backend and separate frontend images.
Each application also needs a versioned integration package. The package
contains its Compose integration, migration inventory, and probe inventory.

A component image is not a complete application release. Components can finish
at different times. A mutable registry tag can also change while a controller
reads it. Atlas needs one bounded item that identifies the complete release for
one exact source revision.

Parkventory currently also has a static demonstration site. The static site and
the future Compose application must not own the same public route at the same
time.

## Decision

Each producer publishes one OCI `application-release` artifact for the exact
HEAD of its allowlisted `main` branch. The discovery tag is `sha-<HEAD>`. The
consumer records and passes only an immutable `@sha256` reference.

The artifact has this fixed structure:

- repository `ghcr.io/nclsppr/<application>/application-release`;
- artifact type `application/vnd.vps-infra.application-release.v1`;
- one `application-release.json` layer with media type
  `application/vnd.vps-infra.application-release.v1+json`;
- canonical JSON with contract `vps-infra.application-release.v1`;
- the exact source repository, branch, and revision;
- the exact component allowlist and one digest-only image reference for each
  component;
- one digest-only `vps-integration` reference for the same source revision;
- a dedicated migration policy with runtime automatic migration disabled;
- the digest of the migration inventory and probe inventory in the same
  `vps-integration` artifact.

The Atlas admission resolver performs these operations for each enabled
application:

1. resolve the exact canonical branch HEAD;
2. read all latest check runs for that exact SHA;
3. require every observed check run to be complete and green;
4. require each application-specific check to conclude with `success`;
5. read only the `application-release:sha-<HEAD>` OCI manifest;
6. validate the exact manifest, layer, source, component, integration,
   migration, and probe contracts;
7. calculate the manifest digest and form an immutable release reference;
8. read the discovery tag a second time and require identical manifest bytes;
9. resolve the canonical branch HEAD a second time and require the same SHA.

A missing check or release artifact is pending evidence. A red check, a wrong
SHA, a malformed release, a digest mismatch, or a moving release tag blocks
admission. The resolver does not select an older green revision.

An application with `enabled: false` returns `disabled` before it resolves a
Git reference or creates any network request. Both application entries are
disabled in this change.

The application and static production contracts are separate reviewed inputs.
Validation fails if both the Parkventory static demonstration and Parkventory
Compose admission are enabled. A later handoff must first disable the static
contract. Runtime code must also verify that no active static state owns the
Parkventory route before it activates Compose.

## Consequences

- Producers and Atlas share one versioned release descriptor.
- A single digest binds all application components and both operational
  inventories.
- Admission can run in CI without access to Atlas or application secrets.
- Disabled admission performs no remote discovery.
- This change does not validate or extract the internal `vps-integration`
  layers. The future materializer must validate their exact media types,
  inventories, paths, sizes, modes, and content digests.
- This change does not verify GitHub artifact attestations. The future
  materializer must verify the allowlisted producer workflow before it accepts
  bytes for deployment.

## Required follow-up

The application applicator needs a separate reviewed change. It must use the
same deployment lock as the static applicator. It must materialize releases in
a new directory, validate secrets without storing them in release state, run
dedicated migrations, execute internal and public probes, switch state
atomically, and restore the previous release after failure. It must persist a
transaction journal and quarantine a reproducibly bad release. A boot recovery
unit must recover an interrupted transaction before either applicator runs.

The forced SSH boundary also needs a separate reviewed command. This admission
change does not modify `scripts/deploy`, `scripts/forced-command`, sudo rules,
systemd units, or Ansible installation tasks.

## Alternatives

### Deploy after any component tag changes

Rejected. It can combine components from different source revisions and does
not bind migrations or probes.

### Use the `vps-integration` artifact as the release signal

Rejected. It does not identify the complete set of component image digests.

### Allow static and Compose Parkventory during a transition

Rejected. Both variants would claim the same public application identity. The
handoff must be explicit and exclusive.
