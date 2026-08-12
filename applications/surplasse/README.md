# Locked Surplasse Atlas adapter

This directory defines the reviewed application boundary for Surplasse. It does
not define Caddy, PostgreSQL, Prometheus, Grafana, or an exporter. The shared
platform owns those services.

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
- the platform PostgreSQL service does not join `db_surplasse`;
- the preparation controller can create the `surplasse` database, its
  `NOLOGIN` owner, the `surplasse_migrator` role, and the
  `surplasse_runtime` role, but the platform attachment needed at runtime is
  still inactive;
- the platform Prometheus service does not join `app_surplasse` and has no
  active Surplasse scrape job;
- the Caddy route and the Prometheus target and rules still have the
  `.disabled` suffix;
- the Caddy service does not receive scoped OVH DNS credentials;
- the application secret files, restore proof, DNS cutover, and public smoke
  proof do not exist in the release contract.
- the source branch, image provenance, Stripe Connect production adapter, and
  Surplasse integration bundle do not have complete release evidence.

The existing disabled candidates are contract inputs. This adapter does not
duplicate them:

```text
platform/caddy/routes/surplasse.caddy.disabled
platform/observability/prometheus/targets/surplasse.yml.disabled
platform/observability/prometheus/rules/surplasse.yml.disabled
```

## Database boundary

The Backend always receives `QUARKUS_FLYWAY_MIGRATE_AT_START=false`. It uses
`surplasse_runtime` and its own password file. The migration job uses
`surplasse_migrator` and a different password file. The job joins only
`db_surplasse`. It has `restart: "no"` and no healthcheck.

`migrations.json` binds the candidate source revision to the SHA-256 value of
each versioned migration from V1 through V14. It does not include the repeatable
development seed.

## Secret file contract

The future applicator must create these files under
`/etc/vps/secrets/surplasse` as `root:10001` with mode `0440`. GID `10001` is
the dedicated group of the Backend and migrator containers. Docker Compose
file secrets preserve the host file ownership; they do not remap it:

```text
surplasse-jwt-jwks
surplasse-jwt-private-key
surplasse-postgres-migrator-password
surplasse-postgres-runtime-password
surplasse-smtp-password
surplasse-smtp-username
surplasse-stripe-account-webhook-secret
surplasse-stripe-payment-webhook-secret
surplasse-stripe-secret-key
```

The repository contains only file paths. It contains no value for these
secrets.

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

A later reviewed slice must remove every blocker atomically. The applicator
must use this order:

1. verify the exact Backend image and its migration command;
2. prove a restorable PostgreSQL backup;
3. provision the database and the three roles;
4. run the transient migration job and require a successful exit;
5. start the five long-running services without host ports;
6. activate the Caddy route and Prometheus configuration with the required
   platform network attachments;
7. complete strict internal and public probes;
8. change DNS only after the direct Atlas probe succeeds.

The verified Backend digest satisfies the migration-entrypoint image gate. The
adapter remains locked by the other release, integration, secret, restore, and
public-proof gates.

## Fail-closed Atlas preparation

The host controller can prepare the private database boundary while this
adapter remains locked:

```bash
make prepare-surplasse \
  ANSIBLE_INVENTORY=/private/path/hosts.yml \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

Preparation validates and stages the exact adapter first. It then creates only
the two missing random database passwords as `root:10001` with mode `0440`. It
temporarily attaches the healthy shared PostgreSQL container to
`db_surplasse`, provisions the `surplasse` database and these roles, and removes
the temporary attachment:

- `surplasse_owner`: `NOLOGIN` database owner;
- `surplasse_migrator`: login role that defaults to the owner role;
- `surplasse_runtime`: login role without schema creation rights.

PostgreSQL does not publish a host port. Preparation does not start or stop an
application container. It does not change the persistent platform network
membership, public Caddy, Prometheus, DNS, or operator-supplied application
secrets.

`make activate-surplasse` is an intentional refusal while `adapter.json` is
locked. The refusal occurs before a database, application, shared platform, or
public edge mutation. The next release slice must complete image provenance,
persistent platform attachments, operator secrets, migration-first
orchestration, rollback, and public proof, then change the adapter through
review before activation code can be enabled.

The files under `integration/` are inactive attachment candidates. They show
the bounded target memberships: PostgreSQL joins only `db_surplasse`,
Prometheus joins only `ops` and `app_surplasse`, and public Caddy keeps `edge`
plus the fixed `172.30.10.254` address on `app_surplasse`. The preparation
controller stages these candidates but never applies them.
