# Contrat du projet

## Identité

| Champ | Valeur |
| --- | --- |
| Nom | `vps-infra` |
| Propriétaire | `nclsppr` |
| Classe | Critique |
| Dépôt canonique | `https://github.com/nclsppr/vps-infra` |
| Branche canonique | `main` protégée, livraison par pull request |
| Surface de production | hôte Atlas convergé ; plateforme et applications non activées |

## Problème traité

Le dépôt permet de reconstruire, valider et exploiter un VPS multi-projets à
partir d'une configuration publique sans y stocker de secret, d'inventaire réel
ni de donnée métier. Les artefacts applicatifs sont construits hors production,
puis sélectionnés par référence immuable.

## Utilisateurs

| Utilisateur | Besoin | Risque principal |
| --- | --- | --- |
| Opérateur de la plateforme | reconstruire, converger, diagnostiquer et restaurer | mutation du mauvais hôte ou perte d'accès |
| Mainteneur applicatif | proposer un artefact vérifié sans accès au VPS | promotion d'un artefact non prouvé |
| Relecteur | relier intention, diff, validation et rollback | garantie déclarée mais non exécutée |

## Sources canoniques

| Sujet | Source | Nature |
| --- | --- | --- |
| Architecture | [`docs/architecture.md`](docs/architecture.md) | normative |
| État vérifié | [`STATUS.md`](STATUS.md) | snapshot daté |
| Séquencement | [`VPS-SETUP.md`](VPS-SETUP.md) | roadmap canonique |
| État de release | [`releases/production.yaml`](releases/production.yaml) | machine-readable |
| Automatisation hôte | [`ansible/`](ansible/) | exécutable |
| Plateforme | [`platform/compose.yaml`](platform/compose.yaml) | exécutable |
| Livraison | [`docs/deployment.md`](docs/deployment.md) | normative |
| Reconstruction | [`docs/rebuild.md`](docs/rebuild.md) | runbook |
| Contrôleur | [`scripts/README.md`](scripts/README.md) | contrat exécutable |
| Secrets | [`secrets/README.md`](secrets/README.md) | normative |
| Décisions | [`docs/decisions/`](docs/decisions/) | normative |
| Historique livré | [`CHANGELOG.md`](CHANGELOG.md) | historique |
| Documentation | [`DOCUMENTATION.md`](DOCUMENTATION.md) et [`documentation.json`](documentation.json) | normative |
| Règles d'intervention | [`AGENTS.md`](AGENTS.md) | adaptateur local |

Le README oriente vers ces sources. Il ne remplace ni l'architecture, ni l'état
vérifié, ni la roadmap.

## Commandes canoniques

| Action | Commande | Effet attendu |
| --- | --- | --- |
| Installer | `make setup` | installe les versions et collections verrouillées |
| Vérifier | `make check` | exécute le contrat local et CI sans déployer |
| Diagnostiquer localement | `make doctor-local` | inspecte le checkout sans contacter le VPS |
| Prévoir une convergence | `make converge-check` | exécute Ansible en `--check --diff` depuis `origin/main` |
| Amorcer un hôte | `make bootstrap` | opération distante bornée depuis `origin/main` |
| Converger un hôte | `make converge` | opération distante bornée depuis `origin/main` |
| Déployer | workflow manuel `Deploy production` | reste bloqué tant que la production n'est pas activée |

`make check` est l'unique contrat de validation complet. La CI doit appeler cette
commande littéralement afin qu'une nouvelle gate ne puisse pas être omise par
duplication.

## Politique de livraison

- `main` exige une pull request, un historique linéaire, les conversations
  résolues et le check strict `Repository contract`.
- Les Actions tierces sont épinglées par SHA complet et les images par digest.
- Le squash merge produit une unité de livraison ; son impact observable est
  consigné dans le changelog.
- L'absence d'une approbation obligatoire est un choix mono-mainteneur. Elle ne
  vaut pas revue indépendante.
- Une modification Git n'autorise aucune mutation de production implicite.

## Alignement Foundation

Les pratiques retenues depuis Project Foundation `v0.5.2` sont détaillées par
[l'ADR-0004](docs/decisions/0004-alignement-selectif-project-foundation.md).
Le dépôt ne revendique pas une conformité formelle à ce pack.
