# Surplasse tester pilot bootstrap

This runbook is intentionally separate from application activation. It never
accepts an OCI target. The root controller derives the one admitted active
Surplasse release from protected Atlas state.

## Local input

Prepare one manifest outside Git. It must match the signed
`pilot-bootstrap.schema.json` contract, contain no secret or table code, be a
regular single-linked file, and use local mode `0400` or `0600`. Keep all six
UUID v4 values stable and distinct. Use a lowercase email, a non-reserved slug,
and the intended Stripe test connected-account identifier. The email must use
visible ASCII only; use an ASCII punycode domain if an internationalized domain
is required. This avoids Unicode case-mapping drift between host Python and the
Temurin runtime.

Keep the source between 1 and 16384 bytes. The local wrapper stages it through
one stable file descriptor into its isolated `0700` home before any fetch. It
refuses a changed path, unsafe metadata, an oversized file, and every existing
staging destination.

Do not place the manifest in `ANSIBLE_EXTRA_VARS`. That file is reserved for
public-key variables.

## Bounded sequence

After normal convergence has installed the root-only helpers:

```bash
make materialize-surplasse-pilot \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml \
  SURPLASSE_PILOT_MANIFEST=/private/path/pilot.json

make status-surplasse-pilot \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml

make apply-surplasse-pilot \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml

make status-surplasse-pilot \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

The first status must observe the exact empty Flyway V14 state. Its durable
confirmation expires after 15 minutes. Apply never accepts the release or
Backend digest from the command line. It leaves `applied-unverified` until the
last status proves the exact tester graph with order intake paused.

Each status first writes a durable non-applicable checking journal before it
inspects or removes a previous pilot container. `checking-empty` preserves a
new or previously confirmed empty lineage. `checking-ambiguous` preserves all
applying, applied, verified, and ambiguous history. A failed or interrupted
status leaves one of these phases, and apply refuses both. Run status again.
An exact graph can still move either phase to `verified`. An empty graph can
restore `empty-confirmed` only from `checking-empty`; from
`checking-ambiguous`, it becomes `ambiguous-empty` and refuses replay.
The admitted producer status operation is read-only. Do not substitute another
image, command, or manual database probe for this recovery step.

The status command reports only `empty-confirmed` or `verified`. Ansible derives
that result from the controller return code. It does not display controller
stdout, stderr, or manifest data. Apply reports only generic completion and
still requires the separate status command.

If apply times out, disconnects, or returns any refusal, do not run apply
again. Run status. An exact graph moves the journal to `verified`. An empty
result after apply history remains ambiguous and refuses replay. Preserve the
journal and investigate; this controller has no reset or purge operation.

## Output and evidence

Ansible suppresses manifest transfer and controller results with `no_log`.
The controller also discards bounded child output. Do not add debug tasks,
shell tracing, or manual Docker commands around this path.

Publication evidence for producer source
`520a6d7f480f408746bedbcfa217983074540a48` is recorded in ADR-0014. It proves
OCI admission only. Record Atlas materialization, status, apply, readback, and
the authenticated application-level paused-state probe separately when they
are actually executed.
