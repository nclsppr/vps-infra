# Contrôleur de release

Ces scripts forment le plan de contrôle local du VPS. Ils valident et planifient
un état Git immuable ; ils ne construisent aucune image et ne démarrent aucun
conteneur.

## Installation attendue

Ansible installe, root-owned et non modifiables par le groupe ou les autres :

- `/usr/local/libexec/vps/deploy`
- `/usr/local/libexec/vps/forced-command`
- `/usr/local/libexec/vps/parse-forced-command`
- `/usr/local/libexec/vps/plan-digests`
- `/usr/local/libexec/vps/reconcile`
- `/usr/local/libexec/vps/validate-compose`
- `/usr/local/libexec/vps/validate-release`
- `/usr/local/libexec/vps/verify-github-evidence`
- `/usr/local/libexec/vps/verify-state`
- `/usr/local/libexec/vps/lib/release_policy.py`
- `/usr/local/share/vps-infra/schemas/production-release.schema.json`

`check`, `check-public-safe` et `doctor` sont des outils d'audit à installer si
le rôle d'exploitation doit les exécuter sur le VPS. `apply-release` est
volontairement absent : aucun chemin de mutation n'est livré dans cette tranche.
Le Python système du VPS doit fournir `jsonschema` Draft 2020-12 ; le contrôleur
production refuse de continuer sans ce validateur.

Le miroir autorisé est `/srv/vps/repository`, avec l'origine exacte
`https://github.com/nclsppr/vps-infra.git`. Le contrôleur ne récupère que
`refs/heads/main` et n'accepte depuis SSH que `deploy <sha40>`.

## États séparés

- `desired/` : manifeste validé et demandé ;
- `active/` : manifeste effectivement appliqué ;
- `plans/` : plans de réconciliation non mutables ;
- `quarantine/commits` et `quarantine/artifacts` : refus persistants ;
- `journal.jsonl` : résultat structuré des tentatives.

Chaque paire `state.json`/`manifest.json` est revalidée par hash, par contenu Git
octet pour octet et par rattachement à `origin/main` avant toute nouvelle
réconciliation. Les clés JSON dupliquées sont refusées.

Persisted manifests are also revalidated with the installed Draft 2020-12 JSON
Schema and the Python release policy. A controller update cannot silently use
only one of these two trust roots.

## Current locks

The manifest requires `activation_policy: locked`. The controller rejects each
unit with `enabled: true`, even when its evidence is structurally valid. A
separate audited revision of the schema, policy, and applicator is necessary to
change this rule.

A disabled platform can contain a candidate declaration. The declaration
consists of the `images`, `integration`, `postgres`, and `readiness_evidence`
fields. The manifest must contain all four fields or none of them. Candidate
images and artifacts must use immutable digests. The platform integration
source revision must be an ancestor of the requested release commit. The
controller validates candidate evidence and applies artifact quarantine before
it records desired state. Candidate metadata does not publish a port, create a
runtime reference in the reconciliation plan, or authorize a service start.

Les projets Compose applicatifs sont également refusés par la CLI tant qu'un
bundle d'intégration vérifié ne fournit pas le set exact des services et leurs
références d'images. Il faudra alors ajouter une allowlist exacte des variables
d'environnement et lier les secrets `_FILE` aux secrets déclarés.

`verify-github-evidence` confirms the repository, branch, commit, run attempt,
and exact `.github/workflows/vps-release.yml` workflow through the public GitHub
API. This check does not yet provide a cryptographic link from the workflow run
to each OCI digest. A complete candidate declaration is therefore not
provenance-complete. The provenance link stays an explicit blocker.

Without `/etc/vps/production-enabled`, `deploy` stays in dry-run mode. It exits
with code 78 after evidence verification, reconciliation, and desired-state
recording. While the installed policy is locked, the controller rejects the
production marker explicitly. It cannot invoke an applicator or create active
state. The locked controller contains no applicator execution path. A future
live path requires a separately audited policy revision.
