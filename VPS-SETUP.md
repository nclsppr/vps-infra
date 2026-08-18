# Plan de mise en œuvre du VPS multi-projets

> **Status: Atlas static edge and internal platform live, dynamic production
> locked.** Atlas passed bootstrap, repeated convergence, predictive check
> mode, and a complete reboot. The three approved static sites use the Atlas
> IPv4 address and HTTPS. The private PostgreSQL and observability platform is
> active. Automatic static reconciliation is enabled and proved by two complete
> scheduled runs and the final central run `32086151183`. No dynamic application
> is active. Atlas has converged the disabled Compose application controller at
> `da04a09bfa9788ae8127b63f9f3a6692bef2551b`; installation is not activation.
> Do not use the archived runbook. Use the
> [static operations runbook](docs/operations/static-release-reconciliation.md)
> and [dated rollout evidence](docs/evidence/2026-08-18-static-reconciliation-rollout.md).

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
- [x] use `parkventory.com` as the Parkventory apex domain.

Public DNS observation on 2026-08-12 confirms OVH authoritative name servers
for `nicolaspieper.com`, `papersempire.com`, `parkventory.com`,
`surplasse.com`, and the legacy `pieper.fr` zone. This observation does not
prove OVH API access or replace the required full zone export.

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
- [x] Codex CLI autonome installé sous un compte dédié sans sudo, Docker,
      SSH direct ni accès aux secrets et releases de production ;
- [x] App Server Codex persistant `atlas-codex-app-server.service` actif sur son
      socket Unix privé, sans port public ;
- [x] logs bornés, redémarrage et reboot vérifiés ;
- [x] second passage Ansible sans changement ;
- [x] bounded `--check --diff` invocation covered by CI and executed on the
      converged host without a predicted change.

Initial Atlas host evidence was collected on 2026-08-12. Both independent
administrator keys opened new sessions, direct root SSH failed, UFW exposed
only the declared ports, Docker had no container, the reboot created a new boot
identifier, normal convergence reported `changed=0`, and predictive check mode
reported `changed=0`. This evidence predates the bounded static-edge and
internal-platform rollout recorded in Phase 7. A full disposable-host platform
rehearsal remains in Phase 6.

A read-only check after the final convergence on 2026-08-18 found
`atlas-codex-app-server.service` active and running on its managed private Unix
socket under the isolated `codex` account. This proves the private service
boundary, not a current Desktop or mobile authentication and pairing session;
those remain operator-controlled procedures.

## Phase 3 — extraire la plateforme commune

This checklist preserves the original extraction acceptance plan. It is not a
standalone live-status page: checked items have repository or rollout evidence,
while unchecked items remain future application requirements even when a
related shared service already exists. Use the README and dated evidence for
the current Atlas state.

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
- [x] publish and promote the first Caddy digest that passes the complete gate;
- [x] importer un fragment de routes par projet statique ;
- [x] servir les releases statiques depuis `/srv/www:ro` ;
- [ ] préserver wildcard, CORS, SSE et fermeture des métriques Surplasse ;
- [x] valider avant tout reload ;
- [x] publier uniquement 80/443.
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

- [x] ajouter une CI de validation ;
- [x] assembler `site/` par allowlist ;
- [x] prouver qu’aucun fichier interne ou d’outillage n’est publié ;
- [x] publier l’archive comme artefact OCI GHCR ;
- [x] generate and probe the complete EN/FR, Work, CV, Blog, article,
      Dashboard, Claude, archive, error-page, and asset inventory;
- [x] probe domain redirects separately;
- [x] conserver Pages jusqu’à la bascule DNS validée.

### Papers Empire

- [x] corriger la documentation de tests contradictoire ;
- [x] épingler les Actions par SHA ;
- [x] conserver le build Retype et `build-lang-pages.mjs` ;
- [x] sonder jeu, langues, Dashboard et documentation ;
- [x] publier le répertoire `site/` comme artefact OCI ;
- [x] préserver exactement l’origine `https://papersempire.com`.

### Parkventory

- [x] build the explicitly labeled static demo for the root path;
- [x] package deterministic static site and route inventory OCI payloads;
- [x] add the bounded static materializer profile and shared Caddy route;
- [x] publish and attest the exact static artifacts from protected `main`;
- [x] deploy the static release, cut over DNS, and run strict public probes;
- [x] keep the backend application disabled during the static demo release;
- [x] repartir d’un commit propre : vérifié sur `21f711c684d3` ;
- [ ] refaire ce contrôle et revoir tout changement de worktree avant de figer
      chaque artefact de production ;
- [ ] comparer, sélectionner par ADR complémentaire puis implémenter un
      fournisseur OIDC passwordless compatible avec le flux déjà choisi ;
- [ ] conserver l’adaptateur d’identité maison uniquement pour le développement ;
- [ ] sécuriser cookies, CORS, SMTP et Swagger ;
- [x] produire Backend et Frontend immuables ;
- [x] publier le paquet d’intégration VPS par digest ;
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

- [x] publish a production application-only Compose contract without owning the
      shared Caddy, PostgreSQL, Prometheus, or Grafana services;
- [x] externaliser réseaux et URL JDBC dans le bundle de production ;
- [x] remplacer le contrat de publication par le manifeste applicatif VPS ;
- [x] transformer routes Caddy, targets/règles Prometheus, dashboards Grafana,
      migrations et probes en paquet d’intégration OCI ;
- [x] remplacer le `IMAGE_TAG` global par un digest par image ;
- [x] enregistrer une révision source par composant, y compris pour les images
      inchangées ;
- [x] publish the fixed five-image matrix on every canonical `main` push, with
      every component bound to the same source SHA and referenced by digest;
- [x] garder SBOM, provenance, attestations et Trivy ;
- [ ] select a managed transactional email relay and accept its DPA, limits,
      support terms, and costs;
- [ ] preserve the OVH MX records and publish one reviewed SPF record, the
      exact DKIM records, and a DMARC record;
- [ ] prove from Atlas that the Backend can use port `587` with required
      STARTTLS, then prove delivery, bounce handling, and operator alerting;
- [ ] inspect the exact Backend image and the started process to prove the
      effective SMTP and TLS configuration, not only the Compose declaration;
- [x] publish one immutable `application-release` descriptor consumable by
      `vps-infra` admission.

These completed producer tasks do not authorize deployment. The protected
Surplasse entry remains disabled until the host, database, secret, SMTP,
network, route, migration, resource, recovery, and public-proof blockers pass.

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

- [x] aucun tag mutable accepté ;
- [x] origine, SHA et digest cohérents ;
- [x] paquets d’intégration validés, rendus et liés à leur digest ;
- [x] activation Parkventory refusée sans toutes les preuves de readiness ;
- [x] verrou global empêchant deux déploiements simultanés ;
- [x] compte SSH à shell système valide, sans accès interactif et sans groupe
      Docker ;
- [x] commande forcée bornée et règle `sudoers` root-owned sans `SETENV` ;
- [x] empreinte d’hôte préenregistrée ;
- [x] commit d’infrastructure complet et présent sur `main` ;
- [x] pull avant activation ;
- [x] install GitHub CLI 2.97.0 from checksum-locked `amd64` and `arm64`
      archives;
- [x] validate the Personal and Papers Empire OCI envelopes, profile limits,
      unsafe tar cases, and the full archive-to-route-inventory bijection;
- [x] verify exact GitHub attestations for site, route, and platform integration
      manifests with separate sequential systemd `DynamicUser` fetch and
      offline-verifier executions, bounded root-owned local bundles, and no
      operator token;
- [x] keep branch protection as a separate external production gate;
- [x] verify the exact platform integration package and activate its
      application `.caddy.disabled` route only inside the temporary probe;
- [x] fetch, validate, and extract each static release in bounded systemd
      `DynamicUser` units backed by a dedicated tmpfs, copy the exact verified
      files into root-owned trees, and validate the exact Caddy configuration
      before an HTTPS `local_certs` probe of every route through the exact Caddy
      image;
- [x] implement targeted Compose start and health waits in the disabled
      application controller; live use remains blocked;
- [x] fsync static release files and directories bottom-up before the durable
      digest-named rename;
- [x] replace the static `current` symlink atomically and restore the previous
      target if the post-replacement fsync fails;
- [ ] retain a bounded set of previous static and application states for
      operator rollback;
- [x] implement static rollback after strict live TLS failure and application
      transaction recovery; post-migration compatibility remains an activation
      blocker;
- [x] persist exact failed static and application tuples in quarantine until
      explicit operator action;
- [x] add durable prepared transactions and complete active tuples for static
      and application deployments;
- [x] connect the static materializer to the separately bounded ADR-0008 gate,
      with public TLS rollback and transaction recovery;
- [x] converge the reviewed static gate on Atlas, provision its dedicated
      GitHub environment identity, and set `VPS_STATIC_DEPLOY_ENABLED=true`;
- [ ] retain old static releases with a reviewed garbage-collection policy;
- [x] configure the real `static-production` environment, serialized matrix,
      global concurrency, dedicated identity, and production switch.

Atlas has converged the Compose application controller from revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b`. The root controller and gate are
installed; `vps-application-recover.service` is loaded, inactive after its
successful run (`Result=success`, `ExecMainStatus=0`). The two legacy entries in
`releases/production.yaml` remain `enabled: false`; the separate canonical
application contract admits Surplasse and keeps Parkventory disabled. No
application workflow or live application activation exists.

## Phase 6 — répétition générale

Atlas proves the checked production steps below. This does not replace the
remaining full-stack rehearsal on a disposable VPS or VM.

- [x] run bootstrap and convergence;
- [x] verify an idempotent second convergence;
- [x] deploy the bounded public edge and private internal platform;
- [x] deploy the three approved static sites;
- [x] run an isolated restore rehearsal from a verified PostgreSQL backup;
- [ ] déployer Surplasse ;
- [x] keep dynamic Parkventory disabled because its gates are not all green;
- [ ] reboot the complete current platform and application topology;
- [x] verify public ports, static TLS, private metrics, and Grafana;
- [ ] verify CORS and SSE through the future dynamic Surplasse routes;
- [ ] simuler une release défectueuse et son rollback ;
- [ ] chronométrer la reconstruction et corriger le runbook.

## Phase 7 — bascule de production

- [ ] export the complete current `pieper.fr` zone and record the previous web
      A, AAAA, and TTL values separately from the protected mail and DNSSEC
      records;
- [ ] lower only the TTL values of `pieper.fr`, `www.pieper.fr`,
      `nicolas.pieper.fr`, and `www.nicolas.pieper.fr`, without changing their
      targets;
- [ ] wait at least the previous web TTL after every authoritative server shows
      the lower TTL, before changing the edge state or any web target;
- [ ] activate and probe the `precutover` edge state: keep every established
      HTTPS site valid and expose the four pending `.fr` aliases only as direct
      Atlas HTTP `308` redirects;
- [x] deploy and probe the three static releases before the DNS change;
- [x] probe the new host with forced IPv4 resolution;
- [x] create a verified PostgreSQL backup and complete an isolated restore
      rehearsal;
- [x] point the apex and `www` A records for `nicolaspieper.com`,
      `papersempire.com`, and `parkventory.com` to Atlas. Remove their previous
      AAAA records. Do not add an Atlas AAAA record before IPv6 edge proof;
- [ ] point only the web A records for `pieper.fr`, `www.pieper.fr`,
      `nicolas.pieper.fr`, and `www.nicolas.pieper.fr` to Atlas. Remove their
      previous AAAA records and preserve the complete mail and DNSSEC state;
- [ ] run the bounded HTTPS activation immediately after the exact DNS answers
      are visible, and retain the `precutover` release plus the DNS export for
      rollback;
- [ ] verify that each `.fr` alias returns one path-preserving `308` to
      `https://nicolaspieper.com` over both HTTP and valid HTTPS;
- [ ] compare the exact pre/post-cutover MX, TXT, NS, DS, and DNSKEY answers for
      `pieper.fr`, then complete one iCloud send and receive test without
      changing the two pre-existing SPF records during this web-only cutover;
- [x] preserve and verify the existing mail records;
- [x] run external DNS, HTTP, HTTPS, certificate, redirect, and port probes;
- [x] observe PostgreSQL, Prometheus targets, Grafana health, and container
      restart counts;
- [x] retain the pre-cutover DNS snapshot and the previous hosting targets for
      rollback during the TTL window.

This cutover applies only to the three static sites. `surplasse.com` keeps its
previous DNS target. Dynamic Surplasse and Parkventory remain disabled.

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
package Personal, Papers Empire, and the Parkventory demo; extract Surplasse
        ↓
répéter le déploiement complet depuis zéro
        ↓
basculer les projets validés
        ↓
Parkventory backend when it is production-ready
```

Cette séquence évite de bloquer la plateforme sur Parkventory et évite aussi de
modifier Surplasse avant que ses nouveaux services communs existent. Les phases
3 et 4 produisent les configurations et artefacts ; aucune activation réelle ne
précède le contrôleur borné de la phase 5.
