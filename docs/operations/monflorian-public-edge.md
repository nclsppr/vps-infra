# Mon Florian public edge checklist

This checklist prepares or changes the Mon Florian edge composition. It does
not authorize application activation, an OpenAI key, a password, DNS, or public
traffic. Keep private values in operator-owned files outside Git and chat.

## Inputs to record

Record these exact values before running a command:

- the reviewed `vps-infra` commit;
- the full 40-character Mon Florian source revision;
- the immutable application-release digest;
- the materialized Surplasse route path used by the current schema 1 state;
- the candidate path printed by the edge controller;
- the previous candidate path for rollback.

The short product revision is not accepted as deployment evidence. Resolve and
record its full Git SHA and the matching immutable release digest.

## Prepare without switching the edge

Install the reviewed controller and recovery unit with the public-edge state
set to `preserve`. Confirm that the runtime link and active state did not
change. Do not use `prepare`, `precutover`, or `activate` for this controller
upgrade.

Materialize only the attested route from the disabled application release:

```sh
sudo /usr/local/libexec/vps/deploy-application \
  --materialize-edge-route \
  monflorian \
  <FULL_SOURCE_SHA> \
  ghcr.io/nclsppr/monflorian/application-release@sha256:<RELEASE_DIGEST>
```

The command must print a path below
`/srv/applications/monflorian/edge-releases/sha256-<RELEASE_DIGEST>/`. Stop if it
creates an application `current` link, asks for the OpenAI key, or starts a
container.

Provide the private-access snippet to Ansible through the private variable
`vps_monflorian_private_access_source`. The destination contract is fixed at
`/etc/vps/secrets/monflorian/monflorian-private-access.caddy`, `root:root`, mode
`0400`. Do not place the source file in this repository.

Stage the composite candidate under the shared deployment lock:

```sh
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge \
  --stage <ATTESTED_SURPLASSE_ROUTE> \
  --monflorian-route \
  /srv/applications/monflorian/edge-releases/sha256-<RELEASE_DIGEST>/monflorian.caddy
```

Staging must not change the runtime link. Inspect the printed candidate path and
run `--verify-live` against the still-active schema 1 state.

## Activation checkpoint

Stop here unless public-edge activation has separate, explicit approval. DNS
approval and application approval do not imply edge approval.

With that approval, activate only the recorded candidate:

```sh
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge \
  --activate /srv/vps/releases/public-static-edge-surplasse/<CANDIDATE_FINGERPRINT>
```

Then run `--verify-live`. Before starting the Mon Florian backend, its
application controller must prove both local requests below return `401` and a
Basic challenge:

- `POST https://monflorian.com/api/itineraries`
- `POST https://monflorian.com/api/illustrations`

The identity path
`GET https://monflorian.com/.well-known/monflorian-release` must return the
attested full source revision without authentication.

Do not change OVH records in this procedure.

## Private-access rotation is not authorized

Do not replace the private-access snippet or stage a schema 2 to schema 2
credential change in this tranche. The controller versions the input safely,
but it does not yet purge old root-only candidate copies. Its recovery tests
retain the transition so the later purge work cannot weaken rollback.

Rotation can be added to this checklist only after a bounded purge command
protects the runtime target, active state, open transaction, and recorded
rollback candidate. Do not remove candidate directories by hand to bypass this
restriction.
Track the required purge in
[issue #104](https://github.com/nclsppr/vps-infra/issues/104).

## Remove Mon Florian from the edge

Normal `--stage` and `--activate` commands cannot remove an active composition.
Use the recorded pre-composition schema 1 candidate and the explicit command:

```sh
sudo /usr/local/libexec/vps/deploy-surplasse-public-edge \
  --remove-monflorian \
  /srv/vps/releases/public-static-edge-surplasse/<LEGACY_CANDIDATE_FINGERPRINT>
```

The controller journals the removal, recreates Caddy, verifies schema 1, and
restores the schema 2 candidate automatically if any step fails. Run
`--verify-live` after completion. This command does not stop the Mon Florian
application or change DNS; handle those scopes separately.
