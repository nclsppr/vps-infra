# Contrat documentaire

Les Markdown du dépôt restent les sources éditoriales. GitHub est le rendu
canonique actuel ; aucune copie générée n'est une seconde source normative.

## Classification

`documentation.json` classe chaque Markdown maintenu exactement une fois :

- `public` : documentation destinée aux lecteurs et opérateurs ;
- `internal` : gouvernance et état de travail du mainteneur ;
- `reference` : inventaires de sources et catalogue généré ;
- `archive` : historique conservé, jamais exécutable comme état actuel.

Le dépôt étant public, la visibilité `internal` ne rend pas un fichier privé.
Aucun secret, inventaire réel ni donnée métier ne peut donc y être placé.

## Commandes

```bash
mise exec -- ./scripts/check-governance --write-catalog
make check
```

La première commande régénère `DOCUMENTATION-CATALOG.md`. La seconde refuse un
Markdown orphelin, une classification multiple, un catalogue obsolète et une
Action GitHub non épinglée par SHA complet.

## Publication

Nimbus n'est pas adopté pour ce dépôt. L'ADR-0004 documente ce choix : ajouter
un runtime Node, un scaffold volumineux et une nouvelle chaîne de dépendances
n'améliorerait pas aujourd'hui l'exploitation du VPS. Ce choix doit être
réévalué uniquement si un site documentaire distinct devient un besoin réel.
