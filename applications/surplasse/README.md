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

`adapter.json` has `activation_policy: locked`. No release controller consumes
this directory. Do not copy it to Atlas and do not run `compose up` from it.

The five candidate images in `.env.example` were published from Surplasse
revision `fab494ad2940f9ee46bf9a186ec7fb2735185367`. The published Backend
image does not contain `/opt/surplasse/scripts/backend-migrate.sh`. Therefore,
the migration job cannot run from that digest. The adapter records this fact as
`published_in_backend_image: false` and keeps the matching blocker.

The adapter also stays locked for these integration reasons:

- the platform Caddy service does not join `app_surplasse` at the fixed trusted
  proxy address `172.30.10.254`;
- the platform PostgreSQL service does not join `db_surplasse`;
- no controller creates the `surplasse` database, its `NOLOGIN` owner, the
  `surplasse_migrator` role, or the `surplasse_runtime` role;
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

Do not remove the Backend migration-entrypoint blocker until a new immutable
Backend digest is published and verified.
