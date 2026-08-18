# ADR-0011: Use Stripe test mode for the Surplasse tester production

## Status

Accepted on 18 August 2026. The product owner authorizes production URLs and
test orders before the public launch. This decision does not modify
`releases/application-production.json`, enable Surplasse, install a credential,
change DNS, start a container, or run a migration.

## Context

Surplasse must become available to the owner and invited testers on the Atlas
VPS. The deployment uses production domains and production operational
boundaries, but the MVP must not collect real card payments. The earlier locked
adapter required Stripe live mode and a live restricted key. That contract does
not match the authorized tester phase.

The public Internet remains discoverable. A small expected audience is not an
authentication control. The product must therefore state clearly that payments
are simulated and must not imply that a real debit occurred.

## Decision

Record this exact payment profile in the versioned Surplasse adapter:

```json
{
  "audience": "testers",
  "mode": "test",
  "schema": 1
}
```

Keep `DEPLOYMENT_PROFILE=production` for the Atlas runtime. Set
`STRIPE_LIVE_MODE=false` in the reviewed Compose definition. The infrastructure
contract rejects `STRIPE_LIVE_MODE=true` while this payment profile is active.

The secret materializer accepts only a dedicated restricted Stripe test key
that starts with `rk_test_`. [Stripe documents that
prefix](https://docs.stripe.com/keys) for server-side test restricted keys. The
materializer rejects `sk_test_`, every live prefix, and placeholder-like
values. It does not accept the payment mode from an operator argument or an
environment variable. Operator input cannot override the versioned repository
decision.

The materializer requires two distinct `whsec_` values. One value signs Stripe
account webhooks. The other value signs payment webhooks. A shared value is not
accepted. Operator manifest version `3` records the fixed payment mode and the
digest of every supplied file without recording a secret value.

Offline validation proves only framing and mode consistency. Before activation,
the operator must use the protected Atlas input path and prove all of these
facts without printing a key:

1. Stripe authenticates the key in test mode for the intended platform account.
2. The key has only the permissions required by the observed Backend requests.
3. Both test webhook endpoints verify signatures with their own installed
   signing secret.
4. The intended test connected account has the required card payment capability
   active.
5. The application release embeds the matching `pk_test_` public key and shows
   a permanent test-payment notice on every order and operator surface.

The application release and the Atlas release must use the same payment mode.
The activation change must prove that contract and reject a test and live
mixture. A future live profile needs a separate reviewed change. That change
must atomically switch the application public key, the Atlas restricted key,
both webhook endpoints and signing secrets, and `STRIPE_LIVE_MODE`.

## Consequences

- Testers can exercise the complete order and refund flow with Stripe test
  objects.
- No real card payment can be collected through this profile.
- A valid-looking key is not activation evidence. Stripe must authenticate it.
- This decision keeps the application release disabled until the separate
  activation contract and real operator inputs are ready.
- The existing same-VPS backup is accepted for this tester MVP. Loss or
  compromise of Atlas can also remove that backup.

After activation, production and test orders can open for invited testers
before the public launch. The operator must then repeat this reminder in every
production status: production and orders are open, but the following work is
mandatory before the public launch:

- switch the complete Stripe contract to live mode and prove the connected
  account capability;
- prove transactional email delivery, domain authentication, bounce handling,
  and alerting;
- add an off-VPS backup and repeat a restore rehearsal from that copy;
- close the recovery, migration compatibility, resource budget, observability,
  and public smoke gates in ADR-0010;
- remove the tester restriction only after the product, legal, and operational
  launch checks are complete.

## Alternatives

### Accept a live key but avoid real test cards

Rejected. A configuration or tester error could create a real charge.

### Accept either test or live keys at materialization time

Rejected. An operator choice could diverge from the versioned application
public key and Compose mode.

### Use an unrestricted Stripe test key

Rejected. A restricted key limits the impact of a credential exposure and is
the documented server-side option for this use case.
