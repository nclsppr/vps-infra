# État vérifié

## Référence

| Champ | Valeur |
| --- | --- |
| Vérifié le | 2026-08-12 |
| Base Git auditée | `aea2438699537192a753e865d271ee5a618b25b1` sur `main` |
| Branche de changement | `agent/adopt-foundation` |
| Environnement | checkout local et API GitHub |
| Production | hôte Atlas convergé ; plateforme et applications verrouillées |

Ce fichier sépare les preuves actuelles de l'architecture cible. Le séquencement
détaillé reste dans [`VPS-SETUP.md`](VPS-SETUP.md).

## Résumé

Le dépôt fournit Ansible, la plateforme Compose, un manifeste de release, un
contrôleur fail-closed et une CI requise. Le socle hôte Atlas a passé
l'amorçage, plusieurs convergences, le mode prédictif et un redémarrage complet.
Le contrat accepte désormais une déclaration candidate de plateforme complète,
immuable et toujours désactivée ; le manifeste courant n'en déclare aucune.
Tous ses services et applications restent désactivés. Aucun hôte, DNS, secret
ni déploiement de production n'a été modifié pendant l'alignement Foundation.

## Capacités observées

| Capacité | Preuve | Limite |
| --- | --- | --- |
| Contrat de dépôt | workflow `Validate`, check `Repository contract` vert sur la base auditée | la CI reste modifiable dans le dépôt qu'elle contrôle |
| Protection de `main` | API GitHub : PR, check strict, admins inclus, historique linéaire, conversations résolues | zéro approbation indépendante dans un dépôt mono-mainteneur |
| Socle hôte Atlas | connexions administrateur, UFW, Docker, convergences `changed=0`, mode prédictif et reboot prouvés le 2026-08-12 | aucune répétition complète de la plateforme sur hôte jetable |
| Chaîne d'artefacts | digests, SHA d'Actions, SBOM et attestations Caddy dans les sources | aucune activation applicative réelle |
| Candidat plateforme | déclaration tout ou rien, références immuables, révision d'intégration ancêtre et preuves contrôlées avant l'état désiré | aucun lien cryptographique entre un run et chaque digest OCI ; aucun candidat dans le manifeste courant |
| Contrôleur de release | tests négatifs et état de production verrouillé | applicateur live volontairement absent |
| Documentation | sources Markdown publiques, catalogue exhaustif ajouté par cette tranche | aucun site documentaire séparé |

## État GitHub observé

- Secret scanning et push protection sont actifs ; aucune alerte secret n'était
  ouverte lors de l'audit.
- La pull request Dependabot `#5` et `cryptography 50.0.0` sont incluses dans la
  base auditée. Après recalcul, l'API Dependabot ne signalait plus aucune alerte
  ouverte.
- L'environnement `production` accepte uniquement `main`, exige le reviewer
  unique et interdit le bypass administrateur.
- Aucun run du workflow de déploiement et aucun deployment GitHub n'étaient
  enregistrés.

## Blocages externes attendus

| Blocage | Effet | Condition de reprise |
| --- | --- | --- |
| Export DNS et jetons API non fournis | aucun certificat ni bascule | zones exportées et droits minimaux validés |
| Secrets de production absents | plateforme non activable | stockage, rotation et récupération définis |
| Provenance OCI par digest incomplète | aucun candidat plateforme ne peut être considéré livrable | workflow, labels OCI et attestations liés exactement à chaque digest |
| Répétition plateforme non exécutée | disponibilité applicative et restauration non prouvées | exercice complet sur hôte jetable avec rollback |

## Limites de gouvernance

- L'alignement sur Project Foundation est sélectif, pas une adoption du pack
  `critical` v0.5.2.
- Nimbus n'est pas ajouté : son scaffold et sa chaîne Node seraient une
  complexité sans usage démontré pour ce dépôt.
- `platform/compose.yaml` reste la source canonique ; aucun second Compose racine
  n'est créé uniquement pour satisfaire une convention générique.
- La protection GitHub et les checks locaux ne constituent pas une racine de
  confiance indépendante si un même changement modifie la règle et son test.
- Les wrappers hôte isolent les sous-processus, mais le poste opérateur et les
  exécutables résolus depuis son `PATH` restent une racine de confiance locale.

## Prochaine preuve

La prochaine preuve opérationnelle est une répétition complète de la plateforme
sur un hôte jetable explicitement autorisé, avec restauration et rollback. Une
future déclaration candidate devra aussi lier cryptographiquement chaque digest
à ses preuves de build. Ces preuves doivent précéder toute activation de service
ou bascule DNS.
