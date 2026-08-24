# ADR-0018: Admit the Mon Florian public preview

## Status

Accepted on 23 August 2026.

## Context

Atlas already receives the authoritative `monflorian.com` and
`www.monflorian.com` DNS answers. The public edge has no active route for these
hosts, so it returns its default `404 Not Found` response.

The Mon Florian repository published one immutable application release from
source revision `4ac2c42339941e34c128f779399688032c8ef304`. Its Compose contract
sets public access and disables generation and illustration. Its public probe
contract requires the page, the disabled service configuration, the release
identity, and the canonical redirect.

## Decision

Enable the Mon Florian application production policy for the reviewed source
revision. Add `app_monflorian` as a permanent public edge attachment at
`172.30.40.254` on the managed `172.30.40.0/24` network. Add the exact attested
Mon Florian route to the pre-cutover and active public edge route sets.

Keep preparation independent from the application container. The preparation
route returns a small HTTP response. The active route proxies the application
only after the base edge transaction succeeds.

The Atlas bundle validator requires these exact product settings:

- `MONFLORIAN_ACCESS_MODE: public`;
- `MONFLORIAN_GENERATION_ENABLED: "false"`;
- `MONFLORIAN_ILLUSTRATION_ENABLED: "false"`.

The public probe contract requires:

- `200` from `https://monflorian.com/`;
- `200` and `"serviceReady":false` from `/api/config`;
- the exact source revision from `/.well-known/monflorian-release`;
- `308` from `https://www.monflorian.com/` to the apex host.

This decision does not enable a provider, create a secret, rotate a secret,
change mail records, or claim that generation works. The existing OpenAI key
file contract remains unchanged. No secret value enters Git.

## Consequences

- Atlas can serve the reviewed mini-site on the public domain.
- The public edge retains the existing personal, Papersempire, Parkventory,
  and optional Surplasse network contracts.
- A later generation activation requires a separate product decision, an
  immutable release, secret generation evidence, and new public probes.

## Rollback

Run the bounded application rollback for Mon Florian. Then restore the previous
public edge base transaction. The controllers reject a foreign active tuple or
an unfinished transaction.
