# Legacy locked Surplasse Atlas adapter

This directory is a legacy, locked preparation adapter. It remains useful for
local policy tests and the bounded database-preparation command. It is not the
canonical application-release producer and it is not a production activation
path. The canonical controller reads only its exact `payment` projection as an
independent versioned Atlas policy input. Surplasse publishes an immutable
`application-release` descriptor and common integration bundle for the shared
admission contract. The canonical protected entry is enabled for the tester
profile by ADR-0013. The legacy adapter remains locked.

Atlas converged the shared transactional application controller from
`vps-infra` revision `da04a09bfa9788ae8127b63f9f3a6692bef2551b` on 2026-08-18.
The root controller and gate are installed, and
`vps-application-recover.service` is loaded and inactive after a successful run
(`Result=success`, `ExecMainStatus=0`). This legacy adapter does not invoke that
gate. No application deployment workflow invokes it, and repository admission
does not prove a live Atlas application release.

The adapter does not define Caddy, PostgreSQL, Prometheus, Grafana, or an
exporter. The shared platform owns those services.

The Compose candidate has five long-running services:

- `backend`
- `onboarding`
- `commande`
- `dashboard`
- `docs`

It also defines one transient `migrator` behind the `migration` profile. The
job uses the exact Backend image. It is not a sixth application image. No
service publishes a host port or mounts a volume.

## Locked state

`adapter.json` has `activation_policy: locked`. The preparation controller can
stage this directory and provision its private database boundary, but it cannot
activate the application. Do not copy it manually to Atlas or run `compose up`
from it.

The five candidate images in `.env.example` were published from Surplasse
revision `d915388d11bf0dbe9111d049cf8f6c72add4d245`. All five exact digests are
published for Linux AMD64. The Backend digest contains executable migration and
healthcheck runners, and its V1 through V14 migration hashes match the reviewed
source revision. The adapter records `published_in_backend_image: true`.

The adapter also stays locked for these integration reasons:

- the platform Caddy service does not join `app_surplasse` at the fixed trusted
  proxy address `172.30.10.254`;
- the preparation controller can create the `surplasse` database, its
  `NOLOGIN` owner, the `surplasse_migrator` role, and the
  `surplasse_runtime` role, but creation does not prove the live database and
  role state;
- the platform Prometheus service does not join `app_surplasse` and has no
  active Surplasse scrape job;
- the Caddy route and the Prometheus target and rules still have the
  `.disabled` suffix;
- the Caddy service does not receive scoped OVH DNS credentials;
- the application secret files, restore proof, DNS cutover, and public smoke
  proof do not exist in the release contract;
- no managed transactional email provider is selected or provisioned;
- SPF, DKIM, DMARC, STARTTLS from Atlas, final delivery, bounce handling, and
  operator alerting do not have reviewed evidence;
- immutable producer images, integration bundle, and `application-release`
  publication do not prove the remaining host, branch-protection, Stripe
  Connect, secret, route, database, migration, or live-activation gates.

The existing disabled candidates are contract inputs. This adapter does not
duplicate them:

```text
platform/caddy/routes/surplasse.caddy.disabled
platform/observability/prometheus/targets/surplasse.yml.disabled
platform/observability/prometheus/rules/surplasse.yml.disabled
```

## Tester payment profile

The versioned `adapter.json` file fixes the payment profile to schema `1`,
audience `testers`, and mode `test`. The runtime deployment profile remains
`production`, because Atlas uses production URLs and production operational
controls. The Backend receives `STRIPE_LIVE_MODE=false`. Test orders can create
Stripe sandbox objects, but they cannot debit a real payment method.

ADR-0013 enables the separate canonical admission entry for this profile. That
change does not install a key, create a webhook endpoint, change DNS, activate a
release, or prove a connected account capability. A public URL is discoverable
even when only invited testers know it. Do not treat a low visitor count as
access control.

The materializer records payment mode `test` in operator manifest version `3`.
It accepts only a dedicated restricted test key with the `rk_test_` prefix. It
rejects live keys, unrestricted keys, and placeholder-like values. Offline
format validation cannot prove that Stripe issued the key, that it belongs to
the correct account, or that it has the reviewed least-privilege permissions.
Activation evidence must prove those properties through Stripe without logging
the key.

The immutable integration `contract.json` repeats the exact tester payment
profile. During materialization, the canonical `deploy-application` controller
requires that contract to equal the versioned adapter, requires the rendered
Backend environment to contain `STRIPE_LIVE_MODE=false`, and explicitly parses
the protected operator manifest as version `3` with `payment_mode=test` and the
exact nine input digests. It repeats the complete binding from the materialized
release before `prepare_transaction`. A divergence therefore stops before an
image pull, public-edge preflight, migration, container start, or transaction
journal write. `validate-surplasse-adapter` is still useful locally, but it is
not the activation proof.

Before a later public launch with real payments, replace this complete profile
atomically. The application release must publish the matching live public key,
Atlas must receive a dedicated `rk_live_` key and two new live webhook signing
secrets, and the Backend must receive `STRIPE_LIVE_MODE=true`. Do not combine a
test key, a live key, test webhooks, live webhooks, or a public key from another
mode. This tester tranche does not implement that live credential rotation or
the controlled service recreation needed after file-secret replacement.

## Database boundary

The Backend always receives `QUARKUS_FLYWAY_MIGRATE_AT_START=false`. It uses
`surplasse_runtime` and its own password file. The migration job uses
`surplasse_migrator` and a different password file. The job joins only
`db_surplasse`. It has `restart: "no"` and no healthcheck.

`migrations.json` binds the candidate source revision to the SHA-256 value of
each versioned migration from V1 through V14. It does not include the repeatable
development seed.

## Operator input contract

The preparation controller installs a root-only helper at
`/usr/local/libexec/vps/materialize-surplasse-secrets`. It creates the two
database passwords, but it never creates an operator value. The application
operator must stage exactly the following nine files in a separate
`root:root 0700` directory. Each source file must be root-owned, regular,
single-linked, and inaccessible to group and other users:

```text
surplasse-jwt-jwks
surplasse-jwt-private-key
surplasse-jwt-key-id
surplasse-smtp-host
surplasse-smtp-password
surplasse-smtp-username
surplasse-stripe-account-webhook-secret
surplasse-stripe-payment-webhook-secret
surplasse-stripe-secret-key
```

Every single-line input must end with one newline. The Stripe key must be a
dedicated restricted test key with the `rk_test_` prefix. The helper rejects an
unrestricted `sk_test_` key and every live key. Start with no permissions in a
Stripe sandbox. Exercise the complete pilot flow and use the Stripe request log
to grant only Accounts v2 read, connected-account Payment Intents write, and
connected-account Refunds write. Add another permission only when a reviewed
request receives an explicit permission error. Restrict the test key to the
Atlas IPv4 address when Stripe supports the policy. Both webhook values must
have the Stripe signing-secret prefix, and the two values must be distinct.
The secret materializer accepts a bounded DNS name as operator input. Prefix
validation does not prove the key, its permissions, its account, or its network
policy. It is not Stripe or SMTP readiness evidence.

The rendered adapter requires a lowercase DNS name and the constant port `587`,
`SMTP_START_TLS=REQUIRED`, `SMTP_TLS=false`,
`QUARKUS_MAILER_AUTH_METHODS=PLAIN LOGIN`, the fixed sender, and the exact
secret paths. A later provider-selection change must bind the SMTP host to a
reviewed public provider contract. The adapter rejects every additional Backend
environment key declared in Compose. This rule prevents a declared Quarkus,
global TLS, or Java option from silently changing the reviewed contract. It
does not detect a value embedded in the image or exported by its entrypoint.
The `smtp-effective-runtime-configuration` gate stays closed until the exact
image and a sanitized view of the started process prove the effective values.

Atlas runs no Postfix, Exim, or other mail transfer agent. The Backend connects
directly to a managed transactional email relay. Follow the
[SMTP relay runbook](../../docs/operations/surplasse-smtp.md) before provider
selection, DNS changes, secret installation, or activation.

The helper parses the JWKS as strict UTF-8 JSON. It rejects duplicate JSON
keys, private RSA parameters, keys other than RS256 signing keys, an RSA key
shorter than 2048 bits, and an exponent other than 65537. It accepts exactly
one unencrypted RSA private-key PEM object and rejects trailing material. It
uses OpenSSL to validate that key. It then proves that the private key matches
the public key selected by `surplasse-jwt-key-id`.

After the complete source bundle passes, install it without putting a value on
the command line:

```bash
sudo /usr/local/libexec/vps/materialize-surplasse-secrets \
  --install-operator-from /run/surplasse-operator-inputs
sudo /usr/local/libexec/vps/materialize-surplasse-secrets --operator-only
```

The secret destination is `/etc/vps/secrets/surplasse`. The seven supplied
values mounted in the Backend are `root:10001 0440`.
`surplasse-jwt-key-id` and `surplasse-smtp-host` are controller-only files with
mode `root:root 0400`. Under the same bundle lock, the helper derives
`/etc/vps/applications/surplasse.env`. This file is regular, single-linked,
`root:root 0600`, and contains exactly these canonical records:

```text
SURPLASSE_AUTH_JWT_KEY_ID=<validated kid>
SURPLASSE_SMTP_HOST=<validated DNS name>
```

Each record has one LF terminator. The file has no additional key. Port `587`
is an adapter constant and is not an operator input.

The helper stages each replacement, calls `fsync`, and uses an atomic rename.
Each mutating mode takes `/run/lock/vps-static.lock` before the bounded bundle
lock. This order excludes a secret or runtime change while the application
controller owns the deployment lock. `--operator-only` takes only the bundle
lock, so the controller can call it while it owns the deployment lock. The
helper replaces the supplied application files, then the runtime file, and
publishes manifest version 3 as the final commit marker. The manifest binds the
contract version, payment mode `test`, and SHA-256 value of all nine supplied
application files. It contains no secret value. A crash before the final
manifest rename leaves an absent or stale marker. Validation then fails even
if both the supplied files
and runtime file contain the new generation. A missing, stale, malformed, or
mismatched manifest makes validation fail. The helper removes bounded orphan
staging files during the next mutating operation under both locks.
`--operator-only` is read-only and rejects an orphan staging file. The helper
rejects any other application entry. It does not print a value. Repeating the
command with the same valid bundle is safe.

The manifest proves the installed on-disk generation. An atomic host rename
does not update an existing Docker file bind mount. A rotation controller must
validate the manifest, recreate each affected service in a controlled order,
and pass its probes before it reports the rotation as complete.

The helper also owns these two generated files in the same destination:

```text
surplasse-postgres-migrator-password
surplasse-postgres-runtime-password
```

These two files and the seven supplied mounted values form the exact set of nine
application secrets. Each one is `root:10001 0440`, regular, and single-linked.
GID `10001` is the dedicated group of the Backend and migrator containers.
Docker Compose file secrets preserve the host file ownership; they do not remap
it. The repository contains only file names and validation rules. It contains
no value.

The three OVH DNS values form a different operator contract:

```text
ovh-application-key
ovh-application-secret
ovh-consumer-key
```

The application helper does not accept, require, move, or delete these files.
It rejects a legacy OVH file in `/etc/vps/secrets/surplasse` as an unexpected
application entry. The separate public-edge secret helper owns their protected
materialization. The edge transition controller accepts only its successful
read-only `--check` result. Do not remove an existing value as part of
application installation.
The helper also rejects the obsolete `surplasse-smtp-port` file without
deleting it. Port `587` remains in the reviewed adapter policy.

Before it materializes a release, `deploy-application` runs the helper in
`--operator-only` mode. Before activation and runtime operations, it repeats
that validation and requires the current two-line runtime file to equal the
immutable release snapshot. An operator cannot bypass the commit marker by
calling only the deployment controller.

The application helper performs no OVH token validation. The public-edge
controller proves the protected bundle contract and that Caddy applies the OVH
provider only to the `surplasse.com` apex and wildcard subjects. A token shape
or a successful certificate request cannot prove the complete IAM scope.
Review that scope at OVHcloud. Never reuse an OVH credential after it appeared
in a chat, issue, log, or commit.

## Local validation

Run the candidate check through the complete repository contract:

```bash
make check
```

For a focused non-mutating check:

```bash
make check-surplasse-adapter
```

The focused target renders both the five long-running services and the
transient migration profile. It applies the shared Compose policy, then checks
the Surplasse-specific roles, secrets, health probes, aliases, migration
command, disabled integration candidates, and locked blockers.

## Activation sequence

For the first tester activation authorized by ADR-0013, a local PostgreSQL
backup and successful restore rehearsal are strongly recommended. Their absence
is an explicitly accepted, non-blocking risk for that first activation only.
They become mandatory before the real public launch and before any later
schema-changing migration. The applicator must use this order:

1. verify the exact Backend image and its migration command;
2. run the local backup and restore rehearsal when available, or record the
   accepted first-activation risk when they are absent;
3. provision the database and the three roles;
4. install and verify every exact root-owned secret and runtime configuration;
5. stage and activate the exact Caddy route, TLS snippet, and platform network
   attachments while public DNS still points away from Atlas;
6. require the deployment controller to revalidate that public-edge identity
   and healthy Caddy on the exact application network before schema migration;
7. run the transient migration job and require a successful exit;
8. start the five long-running services without host ports, complete strict
   internal and direct-Atlas public probes, and commit the runtime tuple;
9. change DNS only after the direct Atlas probe succeeds, then verify recursive
   DNS, certificates, and public probes.

The verified Backend digest satisfies the migration-entrypoint image gate. The
legacy adapter remains locked by the other release, integration, secret,
route, and public-proof gates.

## Fail-closed Atlas preparation

The host controller can prepare the private database boundary while this
adapter remains locked:

```bash
make prepare-surplasse \
  ANSIBLE_INVENTORY=/private/path/hosts.yml \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

Preparation validates and stages the exact adapter first. It then creates only
the two missing random database passwords as `root:10001` with mode `0440`. The
internal platform now keeps the healthy shared PostgreSQL container on
`db_surplasse`. Preparation preserves that membership, provisions the
`surplasse` database and these roles, and uses a temporary attachment only when
it operates against an older platform revision:

- `surplasse_owner`: `NOLOGIN` database owner;
- `surplasse_migrator`: login role that defaults to the owner role;
- `surplasse_runtime`: login role without schema creation rights.

PostgreSQL does not publish a host port. Preparation does not start or stop an
application container. It does not change the persistent platform network
membership, public Caddy, Prometheus, DNS, or operator-supplied application
secrets.

`make activate-surplasse` is an intentional legacy-adapter refusal while
`adapter.json` is
locked. The refusal occurs before a database, application, shared platform, or
public edge mutation. Canonical production activation must use the immutable
application-release controller after its existing operator-input,
route-before-migration, recovery, and public-proof checks pass. It must not
unlock this legacy adapter as an alternate path.

The files under `integration/` are inactive attachment candidates. PostgreSQL
is absent because the base internal platform now owns its durable
`db_surplasse` membership. The remaining candidates show that Prometheus joins
only `ops` and `app_surplasse`, and public Caddy keeps `edge` plus the fixed
`172.30.10.254` address on `app_surplasse`. The preparation controller stages
these candidates but never applies them.
