# Plan de mise en œuvre du VPS multi-projets

> **Status: Atlas host layer proven, production locked.** Atlas passed
> bootstrap, repeated convergence, predictive check mode, and a complete
> reboot. No production secret, DNS change, platform service, application, or
> live applicator is configured. Do not use the archived runbook.

## Décision recommandée

Adopter **Ansible + Docker Compose** avec trois cycles de vie :

1. l’hôte Ubuntu, convergé par Ansible ;
2. une plateforme partagée avec Caddy, PostgreSQL, Prometheus et Grafana ;
3. des releases applicatives indépendantes, toujours construites en CI.

Le déclenchement initial recommandé est GitHub Actions depuis le seul dépôt
`vps-infra`. Le serveur récupère ensuite le commit d’infrastructure et les
digests demandés. Aucun dépôt applicatif ne possède une clé SSH de production.

La conception détaillée vit dans :

- [`docs/architecture.md`](docs/architecture.md) ;
- [`docs/deployment.md`](docs/deployment.md) ;
- [`docs/rebuild.md`](docs/rebuild.md).

## Phase 0 — transformer ce dossier en source de vérité

- [x] créer le dépôt GitHub **public** `nclsppr/vps-infra`, conformément à
      l’ADR-0002 ;
- [x] initialiser Git dans ce dossier et préparer la publication par pull
      request ;
- [x] protéger `main` avec pull request obligatoire, historique linéaire,
      conversations résolues, administrateurs inclus et contrôle
      `Repository contract` requis ;
- [x] déclarer l’environnement GitHub `production`, limité à `main`, avec
      validation humaine et `VPS_DEPLOY_ENABLED=false` ;
- [x] ajouter un `CODEOWNERS` sur `platform/`,
      `ansible/`, `releases/` et les workflows ;
- [x] épingler outils, Actions, paquets hôte et images ;
- [x] configure Renovate to propose upstream digests from
      `platform/.env.example` and Caddy base images from
      `platform/caddy/build.env` after each required approval. Renovate never
      promotes the built Caddy image automatically.
- [ ] confirmer/activer l’application Renovate sur `vps-infra` ; Dependabot
      couvre déjà Actions, Python et Docker chaque semaine, sans prétendre
      mettre à jour les variables d’images ;
- [x] interdire les secrets et inventaires concrets par CI.

Cette phase est nécessaire avant toute installation. Un dossier local non
versionné ne peut pas être la preuve d’une reconstruction.

## Phase 1 — fermer les décisions bloquantes

### Fournisseur et DNS

- [ ] confirmer l’offre, la région et l’image Ubuntu 26.04 LTS exactes chez
      OVHcloud ;
- [x] confirmer le fournisseur DNS de chaque zone ;
- [ ] choisir et épingler les modules DNS Caddy nécessaires à toutes les zones
      dont le certificat doit être émis avant la bascule ;
- [ ] générer des jetons distincts et limités aux seules zones requises ;
- [ ] exporter toutes les zones, surtout les enregistrements mail ;
- [ ] documenter `--resolve` et le chemin de bascule HTTP-01 pour toute zone
      sans DNS-01 ;
- [ ] décider le domaine Parkventory.

Public DNS observation on 2026-08-12 confirms OVH authoritative name servers
for `nicolaspieper.com`, `papersempire.com`, `surplasse.com`, and the legacy
`pieper.fr` zone. This observation does not prove OVH API access or replace the
required full zone export.

### Choisir la version PostgreSQL

- [ ] tester Parkventory avec les images exactes PostgreSQL 17.10 et 18.3 ;
- [ ] vérifier `btree_gist`, les contraintes d’exclusion, V1 seule puis V1→V2 ;
- [ ] tester Surplasse avec l’image exacte PostgreSQL 17.10, pas seulement le
      tag majeur `17` ;
- [ ] si elle est verte, retenir PostgreSQL 17.10 pour le premier cluster ;
- [ ] aligner explicitement les ADR Parkventory et Surplasse sur la version
      réellement retenue ;
- [ ] sinon, retarder Parkventory ; si le besoin produit l’impose, documenter
      par ADR un second cluster 18 ou, en dernier choix, une migration coordonnée
      vers 18 avec répétition des restaurations ;
- [ ] définir owner, migrateur et runtime pour chaque base.

### Secrets

- [ ] créer une identité age de production ;
- [ ] stocker la clé privée dans un gestionnaire de secrets et une copie de
      récupération hors ligne ;
- [ ] ajouter `.sops.yaml` et les fichiers chiffrés par périmètre ;
- [ ] documenter la rotation et la révocation ;
- [ ] créer un jeton GHCR strictement en lecture ;
- [ ] documenter la création d’une deploy key Git en lecture pour `vps-infra` ;
- [ ] séparer la clé SSH du déclencheur GitHub de toutes les autres identités.

### Alertes

- [ ] choisir le canal qui recevra réellement les alertes ;
- [ ] décider si Alertmanager entre dans le premier palier ou si une sonde
      externe suffit au lancement ;
- [ ] ne pas présenter Grafana seul comme une astreinte.

## Phase 2 — livrer l’automatisation de l’hôte

Créer :

```text
ansible/
  inventories/production/
  playbooks/bootstrap.yml
  playbooks/site.yml
  roles/base/
  roles/ssh/
  roles/firewall/
  roles/docker/
  roles/layout/
  roles/deploy/
```

Critères :

- [x] Ubuntu 26.04 LTS vierge accepté avec une seule clé SSH ;
- [x] seconde connexion prouvée avant durcissement ;
- [x] root et mots de passe SSH refusés ;
- [x] pare-feu limité à SSH/HTTP/HTTPS ;
- [x] Docker officiel et Compose installés ;
- [x] aucune toolchain applicative sur l’hôte ;
- [x] logs bornés, redémarrage et reboot vérifiés ;
- [x] second passage Ansible sans changement ;
- [x] bounded `--check --diff` invocation covered by CI and executed on the
      converged host without a predicted change.

Atlas host evidence was collected on 2026-08-12. Both independent
administrator keys opened new sessions, direct root SSH failed, UFW exposed
only the declared ports, Docker had no container, the reboot created a new boot
identifier, normal convergence reported `changed=0`, and predictive check mode
reported `changed=0`. A full disposable-host platform rehearsal remains in
Phase 6.

## Phase 3 — extraire la plateforme commune

Créer :

```text
platform/
  compose.yaml
  caddy/
  postgres/
  observability/
```

### Caddy

- [ ] partir de la version et du durcissement déjà validés par Surplasse ;
- [x] build the platform image from the complete locked Go graph with the OVH
      module and checksum-verified Alpine security packages;
- [x] reject every HIGH or CRITICAL finding on native `amd64` and `arm64` pull
      request builds, then scan both published child manifests by digest before
      GitHub provenance;
- [ ] publish and promote the first Caddy digest that passes the complete gate;
- [ ] importer un fragment de routes par projet ;
- [ ] servir les releases statiques depuis `/srv/www:ro` ;
- [ ] préserver wildcard, CORS, SSE et fermeture des métriques Surplasse ;
- [ ] valider avant tout reload ;
- [ ] publier uniquement 80/443.
- [x] add a deterministic, secret-free OCI publisher for the exact shared
      platform runtime configuration. It verifies both GHCR layers before
      GitHub provenance and does not activate the platform.

### Configurer PostgreSQL commun

- [ ] définir un seul cluster sans port hôte dans le Compose plateforme ;
- [ ] créer les réseaux `db_surplasse` et `db_parkventory` ;
- [ ] créer bases, rôles et extensions de façon idempotente ;
- [ ] révoquer `CONNECT` et `CREATE` publics, fixer `search_path`,
      `ALTER DEFAULT PRIVILEGES`, SCRAM et les règles `pg_hba.conf` par
      base/rôle/réseau ;
- [ ] verrouiller la version majeure, l’image exacte et `PGDATA` dans le
      manifeste ;
- [ ] fixer limites de connexions et budgets mémoire par application ;
- [ ] ajouter le compte de lecture de l’exporter ;
- [ ] interdire tout DDL au runtime et fournir un job migrateur dédié ;
- [ ] séparer son cycle de mise à jour de ceux des backends.

### Observabilité

- [ ] extraire Prometheus, Grafana, règles et dashboard de Surplasse ;
- [ ] généraliser les targets et labels ;
- [ ] ajouter Node Exporter et PostgreSQL Exporter ;
- [ ] provisionner Grafana depuis Git ;
- [ ] garder Prometheus privé et Grafana sur loopback/tunnel ;
- [ ] limiter rétention, CPU, mémoire et logs ;
- [ ] prouver que l’arrêt de la supervision ne touche aucune application.

## Phase 4 — rendre les quatre projets livrables

### Personal

- [ ] ajouter une CI de validation ;
- [ ] assembler `site/` par allowlist ;
- [ ] prouver qu’aucun fichier interne ou d’outillage n’est publié ;
- [ ] publier l’archive comme artefact OCI GHCR ;
- [ ] générer et sonder l’inventaire complet EN/FR, Work, CV, Blog et articles,
      Dashboard, Claude, archive, erreurs, assets et redirections ;
- [ ] conserver Pages jusqu’à la bascule DNS validée.

### Papers Empire

- [ ] corriger la documentation de tests contradictoire ;
- [ ] épingler les Actions par SHA ;
- [ ] conserver le build Retype et `build-lang-pages.mjs` ;
- [ ] sonder jeu, langues, Dashboard et documentation ;
- [ ] publier le répertoire `site/` comme artefact OCI ;
- [ ] préserver exactement l’origine `https://papersempire.com`.

### Parkventory

- [x] repartir d’un commit propre : vérifié sur `21f711c684d3` ;
- [ ] refaire ce contrôle et revoir tout changement de worktree avant de figer
      chaque artefact de production ;
- [ ] comparer, sélectionner par ADR complémentaire puis implémenter un
      fournisseur OIDC passwordless compatible avec le flux déjà choisi ;
- [ ] conserver l’adaptateur d’identité maison uniquement pour le développement ;
- [ ] sécuriser cookies, CORS, SMTP et Swagger ;
- [ ] produire Backend et Frontend immuables ;
- [ ] publier le paquet d’intégration VPS par digest ;
- [ ] supprimer ports et montages de développement ;
- [ ] ajouter durcissement, limites et healthchecks ;
- [ ] ajouter Micrometer Prometheus et logs JSON ;
- [ ] séparer migrateur et runtime ;
- [ ] satisfaire la matrice PostgreSQL ;
- [ ] forcer RLS et prouver par tests négatifs l’isolation tenant A/B ;
- [ ] prouver une restauration sur une base jetable ;
- [ ] consommer les secrets de production par fichiers ;
- [ ] versionner domaine, DNS, healthchecks et smoke public ;
- [ ] protéger `main` ;
- [ ] joindre une preuve immuable pour chaque clé de `blocked_by` et faire
      valider ces preuves automatiquement ;
- [ ] seulement alors passer `enabled: true`.

### Surplasse

- [ ] retirer `edge`, `postgresql`, `prometheus` et `grafana` de la pile
      applicative ;
- [ ] externaliser réseaux et URL JDBC ;
- [ ] adapter le wrapper ou remplacer son contrat par le manifeste VPS ;
- [ ] transformer routes Caddy, targets/règles Prometheus, dashboards Grafana,
      migrations et probes en paquet d’intégration OCI ;
- [ ] remplacer le `IMAGE_TAG` global par un digest par image ;
- [ ] enregistrer une révision source par composant, y compris pour les images
      inchangées ;
- [ ] ne publier que les composants affectés ;
- [ ] garder SBOM, provenance, attestations et Trivy ;
- [ ] ajouter la promotion vers `vps-infra`.

## Phase 5 — livrer le contrôleur de releases

Créer :

```text
releases/production.yaml
scripts/deploy
scripts/deploy-static
scripts/reconcile
scripts/doctor
systemd/
.github/workflows/validate.yml
.github/workflows/deploy.yml
```

Critères :

- [ ] aucun tag mutable accepté ;
- [ ] origine, SHA et digest cohérents ;
- [ ] paquets d’intégration validés, rendus et rollbackés avec leur digest ;
- [ ] activation Parkventory refusée sans toutes les preuves de readiness ;
- [ ] verrou global empêchant deux déploiements simultanés ;
- [ ] compte SSH à shell système valide, sans accès interactif et sans groupe
      Docker ;
- [ ] commande forcée bornée et règle `sudoers` root-owned sans `SETENV` ;
- [ ] empreinte d’hôte préenregistrée ;
- [ ] commit d’infrastructure complet et présent sur `main` ;
- [ ] pull avant activation ;
- [ ] `compose up --wait` ciblé ;
- [ ] bascule statique atomique ;
- [ ] état précédent conservé ;
- [ ] rollback automatique des statiques et des échecs applicatifs
      pré-migration ; arrêt explicite après migration sans compatibilité prouvée ;
- [ ] digests en échec placés en quarantaine jusqu’au revert ou à une action
      explicite ;
- [ ] journal de déploiement durable ;
- [x] environnement GitHub et concurrence globale configurés ; l’interrupteur
      de production reste volontairement à `false` jusqu’aux preuves de la
      phase 6.

## Phase 6 — répétition générale

Sur un VPS ou une VM jetable :

- [ ] lancer bootstrap puis convergence ;
- [ ] vérifier un second passage idempotent ;
- [ ] déployer la plateforme ;
- [ ] déployer les sites statiques ;
- [ ] restaurer une base de test ou initialiser des bases vides ;
- [ ] déployer Surplasse ;
- [ ] garder Parkventory désactivé si ses portes ne sont pas toutes vertes ;
- [ ] redémarrer complètement la machine ;
- [ ] vérifier ports, TLS, CORS, SSE, métriques et dashboards ;
- [ ] simuler une release défectueuse et son rollback ;
- [ ] chronométrer la reconstruction et corriger le runbook.

## Phase 7 — bascule de production

- [ ] baisser les TTL à l’avance ;
- [ ] déployer sans changer le DNS ;
- [ ] sonder le nouvel hôte avec résolution forcée ;
- [ ] sauvegarder et vérifier les données selon le chantier dédié ;
- [ ] basculer les A ou nameservers ; ne publier les AAAA qu’après preuve du
      pare-feu, des binds et des probes IPv6 ;
- [ ] vérifier les enregistrements mail sans aucune réécriture implicite ;
- [ ] lancer des probes externes ;
- [ ] observer la plateforme ;
- [ ] conserver un chemin de retour pendant la fenêtre convenue.

## Ordre recommandé

```text
versionner vps-infra
        ↓
Ansible hôte + secrets récupérables
        ↓
définir Caddy + PostgreSQL + observabilité communs
        ↓
livrer le contrôleur de promotion/déploiement
        ↓
packager personal/papersempire et extraire Surplasse
        ↓
répéter le déploiement complet depuis zéro
        ↓
basculer les projets validés
        ↓
Parkventory lorsqu’il est production-ready
```

Cette séquence évite de bloquer la plateforme sur Parkventory et évite aussi de
modifier Surplasse avant que ses nouveaux services communs existent. Les phases
3 et 4 produisent les configurations et artefacts ; aucune activation réelle ne
précède le contrôleur borné de la phase 5.
