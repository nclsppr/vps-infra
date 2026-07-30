# ADR-0001 — Ansible et Compose avec une plateforme partagée

## Statut

Accepté le 30 juillet 2026.

## Contexte

Quatre projets doivent cohabiter sur un VPS unique et pouvoir être réinstallés
sur une machine Ubuntu vierge :

- deux sites statiques (`personal` et `papersempire`) ;
- une application Surplasse déjà conteneurisée, qui embarque aujourd’hui son
  propre Caddy, PostgreSQL, Prometheus et Grafana ;
- Parkventory, encore limité à une démo Pages et à une stack Compose de
  développement.

Déployer chaque dépôt avec sa propre pile complète provoquerait un conflit sur
les ports 80/443, plusieurs moteurs PostgreSQL et plusieurs chaînes
d’observabilité. À l’inverse, mutualiser les processus applicatifs, les schémas
ou les comptes de base de données couplerait les versions et augmenterait le
rayon d’impact d’une compromission.

La cible est un VPS OVHcloud dont l’offre exacte reste à inventorier. La
création ou la réinstallation de la machine peut rester manuelle ; tout ce qui
suit la première connexion SSH doit être automatisé. Une éventuelle API de
provisionnement sera intégrée seulement après vérification de ses capacités,
sans la confondre avec les API DNS nécessaires à ACME et à la bascule.

## Options considérées

### Une pile complète par projet

Chaque dépôt possède Caddy, PostgreSQL et sa supervision.

Rejeté : ports publics en conflit, mémoire gaspillée, quatre politiques de
secrets et de sauvegarde, mises à jour répétées et visibilité fragmentée.

### Kubernetes, Nomad, Swarm ou une plateforme de déploiement additionnelle

Un orchestrateur assurerait la convergence et pourrait faciliter les rollbacks.

Rejeté pour le VPS unique actuel : coût d’exploitation et nouvelle couche de
contrôle sans besoin mesuré de réplication, de scheduling multi-nœuds ou de
load-balancing.

### Construction et `git pull` dans chaque dépôt sur le VPS

Le serveur tirerait les branches et compilerait les projets.

Rejeté : branches mutables, outils Java/Node sur la production, résultat
difficile à prouver, rollback fragile et risque de servir accidentellement un
checkout complet. Papers Empire exige en outre un vrai build avant
publication ; un checkout n’est pas son site final.

### Ansible, Docker Compose et une pile plateforme

Ansible converge l’hôte. Compose garde un graphe plateforme et des graphes
applicatifs séparés. Les releases sont construites en CI et épinglées par
digest.

Retenu.

## Décision

1. Le dépôt public `vps-infra` devient la source de vérité non sensible de
   l’hôte, des réseaux, de Caddy, PostgreSQL, Prometheus, Grafana, des versions
   actives et des procédures. Inventaires réels et secrets restent hors Git.
2. Ansible gère Ubuntu, les comptes, SSH, le pare-feu, Docker, les répertoires,
   les unités systemd et les fichiers de configuration. Il doit être rejouable
   sans modifier un hôte déjà conforme.
3. Docker Compose reste l’orchestrateur d’un seul serveur.
4. Un Caddy plateforme possède exclusivement les ports 80/443 et les
   certificats. Les NGINX internes de Surplasse restent de simples serveurs de
   bundles.
5. Un cluster PostgreSQL plateforme héberge une base par projet, avec réseaux,
   rôles, secrets et limites distincts. `personal` et `papersempire` ne
   reçoivent aucune base. Cette mutualisation n’est activée qu’après une matrice
   sur les images exactes et l’alignement des ADR de version.
6. Un Prometheus et un Grafana communs collectent des cibles privées. Les règles
   et dashboards sont versionnés ; la supervision n’est jamais une dépendance
   de disponibilité des applications.
7. Les projets publient des images ou artefacts statiques immuables. Chaque
   composant conserve sa propre révision source. Les routes, règles, dashboards,
   migrations et probes applicatifs sont livrés dans un paquet d’intégration
   OCI validé. Le dépôt VPS épingle uniquement des références `@sha256:…`.
8. Les dépôts applicatifs n’ont aucun secret ni accès SSH vers la production.
   Seul le workflow du dépôt d’infrastructure peut demander un déploiement,
   après validation de l’environnement GitHub `production`.
9. Le serveur récupère l’état d’infrastructure à un commit explicite et les
   artefacts à leur digest. Il ne fait jamais un `git pull` libre suivi d’un
   build applicatif.

## Conséquences

### Positives

- une reconstruction ne dépend pas de la mémoire de l’opérateur ;
- les briques coûteuses ou stateful ne sont déployées qu’une fois ;
- une release applicative ne redémarre ni Caddy, ni PostgreSQL, ni la
  supervision ;
- chaque application conserve son rythme et son rollback ;
- les versions réellement déployées sont lisibles dans un manifeste Git ;
- les sites statiques n’ajoutent aucun runtime.

### Négatives

- Caddy et PostgreSQL deviennent des points de défaillance partagés ;
- une mise à jour majeure PostgreSQL concerne toutes les applications
  consommatrices ;
- le dépôt d’infrastructure est une racine de confiance très sensible ;
- les fragments de routage, règles Prometheus, dashboards et probes ajoutent un
  paquet d’intégration à construire, valider et promouvoir ;
- Compose ne rend pas une mise à jour multi-conteneurs atomique : le wrapper de
  déploiement doit mémoriser la release précédente et effectuer un retour
  contrôlé.

## Portes avant activation en production

- prouver V1 et V1→V2 Parkventory sur PostgreSQL 17.10 et 18.3, ainsi que
  Surplasse sur 17.10 exactement, puis aligner les ADR ;
- si Parkventory ne fonctionne pas sur la version commune, retarder son
  activation ; un second cluster 18 ou une migration coordonnée de Surplasse
  vers 18 exigent chacun une ADR et une répétition de restauration ;
- publier par digest l’image Caddy déjà épinglée avec le module DNS OVH ;
- définir le domaine de Parkventory ;
- livrer une stack de production Parkventory, sélectionner le fournisseur OIDC
  passwordless puis implémenter le flux déjà retenu ;
- choisir le détenteur externe de la clé de déchiffrement des secrets ;
- tester une reconstruction sur un hôte jetable ;
- traiter séparément la sauvegarde et la restauration des données métier.
