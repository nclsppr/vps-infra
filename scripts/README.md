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

## Verrous actuels

Le manifeste impose `activation_policy: locked`. Toute unité `enabled: true` est
refusée, même avec des preuves syntaxiquement complètes. Déverrouiller cette
valeur nécessitera une révision auditée du schéma, de la policy et de
l'applicateur.

Les projets Compose applicatifs sont également refusés par la CLI tant qu'un
bundle d'intégration vérifié ne fournit pas le set exact des services et leurs
références d'images. Il faudra alors ajouter une allowlist exacte des variables
d'environnement et lier les secrets `_FILE` aux secrets déclarés.

Pour une future activation, `verify-github-evidence` confirme en ligne le dépôt,
la branche, le SHA, la tentative et le workflow agrégateur exact
`.github/workflows/vps-release.yml`. Cette vérification ne constitue pas encore
une attestation cryptographique liant le run au digest OCI ; ce lien de
provenance reste donc un blocker explicite.

Sans `/etc/vps/production-enabled`, `deploy` reste en dry-run et termine avec le
code 78 après avoir écrit uniquement l'état désiré et le plan. Avec le marqueur,
il exige en plus le vérificateur GitHub et un futur `apply-release` root-owned ;
l'état actif n'est promu qu'après succès complet.
