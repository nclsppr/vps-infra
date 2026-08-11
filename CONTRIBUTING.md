# Contribuer

1. créer une branche depuis `main` ;
2. ne modifier qu’un périmètre cohérent ;
3. exécuter `mise trust`, `make setup` puis `make check` ;
4. documenter toute décision structurante dans `docs/decisions/` ;
5. ouvrir une pull request avec le résultat des validations et le rollback.

Les changements de production passent par le manifeste de releases. Un tag
mutable, un secret en clair ou un accès direct au socket Docker est refusé.
