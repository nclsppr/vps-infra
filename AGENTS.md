# Consignes du dépôt

## Périmètre et autorité

Ce dépôt public est la source de vérité de l'hôte VPS, de la plateforme commune
et de l'état de release sélectionné. Une procédure documentée ne constitue pas
une autorisation de mutation. Les accès, secrets, achats, DNS et opérations de
production exigent toujours une autorité explicite et une cible résolue.

Avant une modification, lire dans cet ordre :

1. [`PROJECT.md`](PROJECT.md) pour les sources canoniques et les commandes ;
2. [`STATUS.md`](STATUS.md) pour l'état réellement vérifié et les blocages ;
3. les ADR et runbooks qui couvrent le périmètre ;
4. [`CHANGELOG.md`](CHANGELOG.md) pour l'impact déjà livré.

L'alignement sélectif sur Project Foundation est décrit par
[l'ADR-0004](docs/decisions/0004-alignement-selectif-project-foundation.md).
Il ne constitue pas une adoption formelle du pack Foundation.

## Langue

- Rédiger la documentation et les messages destinés aux opérateurs en français.
- Garder le code, les identifiants, schémas et commentaires techniques en
  anglais. Rédiger les sorties destinées aux opérateurs en français.
- Employer des phrases courtes, des termes stables et des dates absolues.
- Ne pas recopier une règle ou une procédure : référencer sa source canonique.

## Règles d'ingénierie

- Référencer chaque image et artefact de production par digest immuable.
- Ne jamais exécuter un build applicatif sur le VPS de production.
- Exiger une validation explicite pour PostgreSQL, Caddy, les migrations, les
  secrets et toute activation de production. Aucun fallback silencieux.
- Concevoir les scripts en mode sûr : validation ou dry-run avant mutation,
  cible bornée, variables obligatoires et arrêt sur ambiguïté.
- Définir le rollback et la preuve de santé avant une mutation distante.
- Ne jamais créer dans Git un inventaire réel, un fichier `.env`, une clé, un
  jeton, un secret déchiffré ou une donnée métier.
- Classer tout Markdown maintenu dans `documentation.json` et régénérer le
  catalogue avec
  `mise exec -- ./scripts/check-governance --write-catalog`.

## Workflow de livraison

1. Inspecter le worktree et préserver les changements sans rapport.
2. Rafraîchir `origin/main` et travailler depuis cette base sur une branche
   dédiée. Ne jamais recycler une branche déjà fusionnée.
3. Garder le diff borné et mettre à jour l'ADR, le statut ou le changelog lorsque
   leur source canonique est affectée.
4. Exécuter `make check` avant chaque commit.
5. Committer puis pousser la tranche validée et ouvrir une pull request vers
   `main`. Le check requis reste `Repository contract`.

La protection de `main` et la CI sont des contrôles nécessaires, mais pas une
racine de confiance indépendante : un même changement peut encore modifier un
checker et le fichier qu'il vérifie. Signaler cette limite au lieu de promettre
une conformité absolue.

## État externe

Les adresses OVHcloud, identifiants API, zones DNS, clés SSH et secrets de
production sont fournis séparément. Leur absence est une porte attendue. Ne
jamais inventer une valeur ni contourner cette porte avec un exemple implicite.
