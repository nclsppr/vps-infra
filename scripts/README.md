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

Les manifestes persistés sont aussi revalidés avec le schéma JSON Draft 2020-12
installé et la politique de release Python. Une mise à jour du contrôleur ne
peut pas utiliser silencieusement une seule de ces deux racines de confiance.

## Verrous actuels

Le manifeste exige `activation_policy: locked`. Le contrôleur refuse chaque
unité à `enabled: true`, même lorsque ses preuves sont structurellement
valides. Modifier cette règle exige une révision séparément auditée du schéma,
de la politique et de l'applicateur.

Une plateforme désactivée peut contenir une déclaration candidate. Elle réunit
les champs `images`, `integration`, `postgres` et `readiness_evidence`. Le
manifeste doit contenir les quatre champs ou aucun. Les images et artefacts
candidats utilisent des digests immuables. La révision source de l'intégration
plateforme doit être un ancêtre du commit de release demandé. Le contrôleur
valide les preuves candidates et applique la quarantaine des artefacts avant
d'enregistrer l'état désiré. Les métadonnées candidates ne publient aucun port,
ne créent aucune référence runtime dans le plan de réconciliation et
n'autorisent aucun démarrage de service.

Les projets Compose applicatifs sont également refusés par la CLI tant qu'un
bundle d'intégration vérifié ne fournit pas le set exact des services et leurs
références d'images. Il faudra alors ajouter une allowlist exacte des variables
d'environnement et lier les secrets `_FILE` aux secrets déclarés.

`verify-github-evidence` confirme le dépôt, la branche, le commit, la tentative
du run et le workflow exact `.github/workflows/vps-release.yml` via l'API GitHub
publique. Ce contrôle ne fournit pas encore de lien cryptographique entre le
run et chaque digest OCI. Une déclaration candidate complète n'a donc pas une
provenance complète. Ce lien reste un blocage explicite.

Sans `/etc/vps/production-enabled`, `deploy` reste en dry-run et termine avec le
code 78 après vérification des preuves, réconciliation et enregistrement de
l'état désiré. Tant que la politique installée est verrouillée, le contrôleur
refuse explicitement le marqueur de production. Il ne peut appeler aucun
applicateur ni créer d'état actif. Le contrôleur verrouillé ne contient aucun
chemin d'exécution d'un applicateur. Un futur chemin live exige une révision de
politique séparément auditée.
