# Consignes du dépôt

## Périmètre

Ce dépôt public est la source de vérité de l’hôte VPS, de la plateforme commune
et des versions à déployer. Il ne doit jamais contenir de secret, de clé privée,
d’inventaire de production ni de donnée métier.

## Règles

- La documentation et les messages opérateur sont en français ; le code et les
  identifiants techniques restent en anglais.
- Les images et artefacts de production sont référencés par digest immuable.
- Les builds applicatifs ne s’exécutent pas sur le VPS de production.
- Les changements PostgreSQL, Caddy, migrations et secrets nécessitent une
  validation explicite ; aucun fallback silencieux n’est autorisé.
- Les scripts sont sûrs par défaut : validation ou dry-run avant mutation,
  cibles bornées et refus des variables manquantes.
- Ne jamais créer un vrai inventaire, un fichier `.env`, une clé, un jeton ou un
  fichier déchiffré dans Git.
- Exécuter `make check` avant tout commit.

## État externe

Les adresses OVHcloud, identifiants API, zones DNS, clés SSH et secrets de
production seront fournis séparément. Leur absence est une porte attendue, pas
une invitation à inventer des valeurs.
