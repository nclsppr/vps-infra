# ADR-0004 : alignement sélectif sur Project Foundation

## Statut

Acceptée le 2026-08-12.

## Contexte

`vps-infra` est un dépôt public d'infrastructure critique. Il possède déjà des
gates plus précises que plusieurs règles génériques : digests immuables,
production verrouillée, contrôleur fail-closed, ADR, runbooks, CI requise et
protection de branche.

Project Foundation a été audité depuis sa dernière release stable :

| Champ | Valeur |
| --- | --- |
| Source | `https://github.com/nclsppr/project-foundation.git` |
| Tag évalué | `v0.5.2` |
| Objet tag annoté | `d9eadc7ebf5ac74b6bef2180fbdf9eb538f4ecf6` |
| Commit | `708d7374f87060809a805c57abc2cf7e7b66c182` |
| Pack conceptuel | `critical` |
| Profils pertinents | `infrastructure-production`, `dependency-change` |

Le tag est annoté mais non signé. Le dépôt Foundation ne protège ni `main` ni
ses tags par une règle externe. La release `v0.5.2` ne vérifie pas non plus les
hashes d'un snapshot consommateur.

## Décision

Appliquer les pratiques durables sans revendiquer une adoption formelle du
pack Foundation.

Pratiques retenues :

- distinguer état actuel, cible et preuve ;
- identifier une source canonique par sujet ;
- consigner les décisions structurantes dans une ADR ;
- garder une complexité proportionnée au risque ;
- exécuter la même commande agent-neutre en local et en CI ;
- protéger secrets, cibles distantes, sauvegardes et rollback ;
- tracer chaque changement livré dans un changelog ;
- classer chaque Markdown et refuser les orphelins ;
- épingler Actions, outils, images et artefacts ;
- publier chaque tranche par branche et pull request vers `main` protégée.

Adaptations locales :

- `PROJECT.md` route vers les sources existantes au lieu de les recopier ;
- `STATUS.md` porte uniquement l'état daté ;
- `VPS-SETUP.md` reste l'unique roadmap ;
- `docs/deployment.md` et `docs/rebuild.md` restent les runbooks ;
- la pull request et les runs GitHub portent la preuve de livraison ;
- `make check` reste l'unique gate complète ;
- `platform/compose.yaml` reste l'unique Compose canonique.

## Defaults non repris

### Adoption formelle et snapshot vendorisé

Le dépôt ne copie pas `docs/foundation/` et ne crée pas de `FOUNDATION.md`. Sans
lock de hashes et racine de confiance indépendante, ce snapshot pourrait être
modifié avec son checker. Le présent ADR enregistre donc une évaluation et des
choix locaux, pas une conformité.

### Nimbus obligatoire

Le bootstrap `critical` v0.5.2 ajouterait environ 136 fichiers, Node, npm et une
chaîne de dépendances documentaires. Les Markdown sont déjà lisibles dans le
dépôt public et aucun site distinct n'est demandé. Le catalogue exhaustif est
retenu ; le renderer Nimbus ne l'est pas.

### Compose à la racine

Le graphe réel vit sous `platform/compose.yaml`. Ajouter un second graphe ou un
adaptateur uniquement cérémoniel augmenterait les chemins de dérive. Les
contrôles existants rendent ce fichier avec l'environnement d'exemple, valident
digests, healthchecks, ports et durcissement, sans prétendre démarrer une
production dépourvue de secrets.

### Langue anglaise universelle

La documentation et les messages opérateur restent en français. Le code, les
identifiants, schémas et commentaires techniques restent en anglais. Une
future release Foundation qui rendrait l'anglais non dérogeable ne sera pas
adoptée automatiquement.

## Contrôles compensatoires

- `scripts/check-governance` vérifie la classification Markdown, le catalogue,
  les SHA d'Actions et le câblage de la commande CI.
- `make check` exécute cette gate avec les contrôles Ansible, Compose, secrets,
  contrôleur, Caddy et Prometheus.
- GitHub exige `Repository contract` sur une branche `main` à jour et protégée.
- La politique GitHub impose les références SHA pour les Actions.

Ces contrôles réduisent la dérive. Ils ne constituent pas une racine de
confiance indépendante, puisque le dépôt reste administré par un mainteneur
unique.

## Conséquences

### Positives

- Les sources, preuves et limites deviennent explicites sans dupliquer les
  procédures existantes.
- Toute nouvelle documentation et Action passe par une gate déterministe.
- La CI ne peut plus omettre silencieusement une sous-cible ajoutée à
  `make check`.
- Le coût Nimbus et un second Compose sont évités.

### Négatives

- Le dépôt ne peut pas afficher une conformité Foundation formelle.
- Une montée de version Foundation reste une revue manuelle.
- La gouvernance GitHub n'est pas indépendante du propriétaire du dépôt.

## Réexamen

Réexaminer cette décision lors d'une release Foundation publiée qui apporte un
lock d'intégrité stable, si un second mainteneur permet une revue indépendante,
ou si un vrai site documentaire devient nécessaire. Toute évolution doit
préserver le français opérateur et le Compose canonique existant, ou documenter
explicitement leur remplacement.
