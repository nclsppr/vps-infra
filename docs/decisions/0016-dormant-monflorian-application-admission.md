# ADR-0016: Admit a dormant Mon Florian application profile

## Status

Accepted on 23 August 2026. This decision prepares repository policy and host
layout only. Mon Florian remains `enabled: false` in both production contracts.
It does not deploy a release, write a secret, attach Caddy to an application
network, install a private-access hash, change DNS, or start a container.

This decision extends ADR-0009 and ADR-0010. It does not change the active
Surplasse tester policy or the disabled Parkventory policy.

## Context

Mon Florian publishes one backend image and one immutable `vps-integration`
bundle from `nclsppr/monflorian`. The bundle contains the Compose document,
Caddy route, exact image contract, migration inventory, and HTTP probes. The
application has no database and no schema migration runner.

The runtime needs one OpenAI API key. That value is private operator input. Git
may define its path, ownership, mode, and service allocation, but must never
contain the value. The public domain still resolves to OVH parking. The Caddy
route requires private access before launch, but no reviewed password hash is
available.

## Decision

Add `monflorian` to the exact application production, release, integration,
forced-command, reconciliation, recovery, Compose, and live-gate allowlists.
Keep the production entry disabled and blocked by the complete readiness set.
A disabled request stops before runtime validation or network access.

The admitted producer contract is exact:

- source repository `nclsppr/monflorian` on `main`;
- release and integration signer
  `nclsppr/monflorian/.github/workflows/vps-integration.yml`;
- component signer `nclsppr/monflorian/.github/workflows/images.yml`;
- one component, `backend`, from `ghcr.io/nclsppr/monflorian/backend`;
- one external network, `app_monflorian`, with no published host port;
- migration strategy `none`, `runtime_auto_migrate: false`, no database,
  migration list, runner, migrator service, or migration Compose profile;
- one file-backed secret named `monflorian_openai_api_key`, sourced from
  `/etc/vps/secrets/monflorian/monflorian-openai-api-key`, allocated only to
  the backend running as `10001:10001`;
- internal health status `200`, release identity status `200` containing the
  exact source SHA, apex status `401`, and `www` status `308`.

The host layout creates the empty application release directory, the private
secret parent, and the managed `app_monflorian` bridge on
`172.30.40.0/24`. It does not create the secret file without explicit private
input. When the operator supplies a private local source path, Ansible copies
the key without logging its value and requires the destination to be a regular,
non-symbolic file owned by `root:10001` with one link and mode `0440`. The
controller repeats those checks before an enabled activation can render the
runtime.

Store the reviewed route as
`platform/caddy/routes/monflorian.caddy.disabled`. Its disabled suffix keeps it
outside the active Caddy import. The route imports
`/etc/caddy/monflorian-private-access.caddy`, but this decision does not create
that file because no reviewed password hash exists. It also does not attach
the public edge to `app_monflorian`.

## Activation conditions

A later reviewed change may enable Mon Florian only after all of these
conditions are true:

1. the producer publishes one complete immutable release whose checks and
   attestations satisfy the exact admission contract;
2. an operator installs the OpenAI key outside Git at the required path with
   owner `root:10001` and mode `0440`;
3. an operator creates the private-access Caddy snippet through a private
   channel with a reviewed password hash;
4. the immutable public edge release contains the attested route and attaches
   Caddy to the exact managed `app_monflorian` network;
5. local internal, identity, unauthorized-apex, and redirect probes pass on
   Atlas before DNS changes;
6. the OVH parking records are replaced only through a reviewed DNS plan with
   recorded rollback state;
7. the controller, boot recovery, resource budget, retention, observability,
   and public smoke gates are proved against the actual release.

Repository convergence alone does not satisfy any of these conditions.

## Consequences

- Atlas can validate the Mon Florian producer contract without activating it.
- The no-migration profile cannot invent a database or reuse the dedicated
  migrator path required by Surplasse and Parkventory.
- Git contains secret metadata and an inactive route, but no OpenAI value or
  private-access hash.
- The domain remains on OVH parking until a separate authorized cutover.

## Alternatives

### Reuse the dedicated migration profile

Rejected. Mon Florian has no database. A fake database or no-op migrator would
add authority and recovery state with no product requirement.

### Commit a placeholder secret or password hash

Rejected. A placeholder weakens the activation boundary and can be mistaken
for deployable input. Private values must arrive through an operator channel.

### Activate the route before the application release

Rejected. It would direct traffic to an unproved backend and make DNS or Caddy
state appear complete before the release, secret, and access controls exist.
