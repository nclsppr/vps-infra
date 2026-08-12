## Objet

<!-- Décrire l'état observé, le résultat visé et pourquoi ce diff est borné. -->

## Risque et autorité

- [ ] documentation ou gouvernance uniquement
- [ ] dépendance ou chaîne d'approvisionnement
- [ ] infrastructure ou production
- [ ] PostgreSQL, Caddy, migration, secret ou DNS avec checkpoint explicite

Autorité et cible exacte :

<!-- Indiquer « aucune mutation externe » si le changement reste dans Git. -->

## Validation

- [ ] `make check`
- [ ] la CI appelle la même commande canonique
- [ ] aucun secret, inventaire réel, identifiant d'hôte ou donnée métier ajouté
- [ ] chaque Action externe est épinglée par SHA complet
- [ ] documentation, catalogue, changelog, statut et ADR alignés si applicables
- [ ] limites de la preuve indiquées

Résultats observés :

<!-- Commandes, environnement et résultat. Ne jamais coller de secret. -->

## Déploiement et rollback

Déploiement :

<!-- « Aucun » pour un changement sans effet runtime. -->

Rollback et critères de déclenchement :

<!-- Référence immuable précédente, procédure canonique et contrôle de santé. -->
