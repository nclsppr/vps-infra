# Contribuer

## Préparer une modification

1. lire [`PROJECT.md`](PROJECT.md), [`STATUS.md`](STATUS.md) et les décisions du
   périmètre ;
2. rafraîchir `origin/main`, puis créer une branche dédiée depuis ce commit ;
3. installer les versions verrouillées avec `mise trust` puis `make setup` ;
4. modifier une seule unité cohérente ;
5. exécuter `make check` ;
6. mettre à jour [`CHANGELOG.md`](CHANGELOG.md) si l'impact est livrable ;
7. ouvrir une pull request avec les validations, limites et rollback.

Une décision qui change l'architecture, la sécurité, la disponibilité, les
données ou le déploiement exige une ADR. Une procédure existante reste dans son
runbook canonique ; la pull request ne la duplique pas.

## Niveau de preuve

| Changement | Preuve minimale |
| --- | --- |
| Documentation sans effet runtime | catalogue à jour, liens et `make check` |
| Dépendance, image ou Action | version et SHA ou digest, tests, impact et rollback |
| Ansible, Compose, Caddy ou contrôleur | rendu ou dry-run, tests négatifs, cible et rollback |
| PostgreSQL, migration, secret, DNS ou production | checkpoint humain explicite, sauvegarde si applicable, santé et observation |

Les changements de production passent par le manifeste de releases. Un tag
mutable, un secret en clair, une branche non ancrée dans `main` ou un accès
direct non borné au socket Docker est refusé.

## Publication

`main` est protégée. Le dépôt utilise les branches, les pull requests et le
squash merge. Aucune approbation indépendante n'est exigée tant qu'un seul
mainteneur peut relire ; le check `Repository contract`, les conversations
résolues et l'historique linéaire restent obligatoires.
