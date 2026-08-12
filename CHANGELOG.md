# Journal des changements

Ce journal décrit l'impact observable des changements livrés. Git conserve le
diff exhaustif ; les ADR conservent les décisions structurantes.

## Non publié

### Ajouté

- Alignement sélectif sur les pratiques de Project Foundation `v0.5.2`, avec
  contrat stable, état vérifié, catalogue documentaire et règles de livraison.
- Gate locale contre les Actions GitHub non épinglées et contre la dérive du
  catalogue Markdown, y compris les syntaxes YAML équivalentes et les liens
  symboliques.

### Modifié

- La CI appelle désormais la commande canonique `make check` sans en recopier
  les sous-cibles.
- Les entrées opérateur et l'amorçage de l'hôte sont bornés avant toute action
  distante, revalidés depuis le commit exact de `origin/main`, puis exécutés
  depuis ce même snapshot.
- Les guides Ansible et plateforme ainsi que l'aide Make sont désormais en
  français, conformément au contrat opérateur du dépôt.

### Sécurité

- Le contrôle public détecte aussi les secrets génériques structurés en JSON ou
  YAML, les clés TOML, les valeurs multilignes et les identifiants OVH, en plus
  des formats fournisseur et des affectations existantes.
- L'amorçage refuse un OS non pris en charge avant toute mutation ; les clés
  administrateur et déploiement doivent être cryptographiquement disjointes et
  les clés RSA posséder au moins 2048 bits.

## 2026-08-12

### Vérifié

- Le socle hôte Atlas a passé l'amorçage, deux identités administrateur,
  plusieurs convergences, le mode prédictif `changed=0` et un redémarrage
  complet, sans activer la plateforme ni une application.
- Le contrat de release sait contrôler une déclaration candidate de plateforme
  complète et immuable avant l'état désiré, tout en refusant l'activation. Le
  manifeste courant n'en déclare aucune et la provenance de chaque digest reste
  à prouver.
- Les zones déclarées utilisent des serveurs de noms OVH autoritaires ; cette
  observation publique ne remplace pas leur export API complet.

## 2026-08-11

### Ajouté

- Socle reproductible initial : Ubuntu, Ansible, Docker Compose, Caddy,
  PostgreSQL, observabilité, contrôleur de release et validations GitHub.
- Convergence isolée depuis `origin/main` et mode prédictif `--check --diff`.

### Sécurité

- Production verrouillée, artefacts immuables, secrets hors Git et branche
  `main` protégée par le check `Repository contract`.
