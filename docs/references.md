# Sources et preuves d’audit

Sources initiales consultées le 30 juillet 2026. Les sources Ubuntu 26.04 et
Docker `resolute` ont été vérifiées le 11 août 2026. Les liens externes pointent
uniquement vers la documentation officielle des outils ou fournisseurs
concernés.

## Instantané des dépôts

Cet instantané sert à rendre les constats reproductibles ; il ne doit pas être
traité comme l’état courant lors d’une mise en œuvre ultérieure.

| Projet | Commit observé | Worktree au dernier contrôle |
|---|---|---|
| Personal | `b3a29db73fef` | propre |
| Papers Empire | `0591dea0ad7e` | propre |
| Parkventory | `21f711c684d3` | propre |
| Surplasse | `fab494ad2940` | fichiers d’agents et de critiques non suivis |

## Preuves dans les projets

### Personal

- `README.md` : site statique, absence de build runtime, domaine et publication
  GitHub Pages ;
- `AGENTS.md` : contrat de publication depuis `main` ;
- `CNAME` : domaine canonique ;
- inspection HTTP : la racine Pages publie actuellement aussi des fichiers
  éditoriaux et d’outillage qui devront être exclus de l’artefact VPS.

### Papers Empire

- `.github/workflows/docs.yml` : assemblage du vrai répertoire `site/` puis
  publication Pages ;
- `package.json` et `package-lock.json` : build Retype ;
- `scripts/build-lang-pages.mjs` : pages de langue et cache-busting par SHA ;
- `docs/architecture.md` et `assets/js/persistence.js` : jeu navigateur et
  persistance `localStorage`.

### Parkventory

- `README.md`, `PROJECT.md`, `STATUS.md` et `RUNBOOK.md` : démo, état de
  livraison et absence explicite de production ;
- `compose.yaml` : Maven `quarkus:dev`, Vite, Mailpit et PostgreSQL 18.3 ;
- `backend/src/main/resources/db/migration/` : Flyway, `btree_gist` et
  contraintes d’exclusion ;
- `.github/workflows/verify.yml` et `pages.yml` : validation et démo Pages,
  sans GHCR ni déploiement VPS ;
- commit `21f711c684d3` observé au dernier contrôle, avec worktree propre ; ce
  commit reste une démo sans artefact ni cible de production ;
- ADR-0003 : flux OIDC passwordless retenu en production, fournisseur encore à
  sélectionner ; l’adaptateur d’identité local n’est pas le fournisseur de
  production.

### Surplasse

- `compose.yaml` et `compose.production.yaml` : graphe actuel, réseaux, volumes,
  durcissement et exposition ;
- `config/deployment/images.env` : Caddy 2.11.4, PostgreSQL 17.10, Prometheus
  3.13.1 et Grafana 13.1.1 épinglés par digest ;
- `.github/workflows/images.yml` : cinq images applicatives GHCR par SHA, scans,
  SBOM, provenance et attestations ;
- `infra/caddy/` : wildcard, DNS-01, CORS, SSE et fermeture des métriques ;
- `infra/observability/` et ADR-0029 : Prometheus/Grafana actuels, cibles,
  règles et dashboard ;
- ADR-0005 : choix PostgreSQL 17 ;
- `docs/operations/deploiement-compose.md` : livraison manuelle, `up --wait`,
  rollback applicatif et limites de restauration.

## Documentation officielle

### Configuration et reconstruction

- [Canonical — notes de version Ubuntu 26.04 LTS](https://documentation.ubuntu.com/release-notes/26.04/) :
  version `Resolute Raccoon`, publiée le 23 avril 2026 et prise en charge
  pendant cinq ans.
- [OVHcloud — cycle de vie des images](https://docs.ovhcloud.com/en/guides/public-cloud/compute/image-life-cycle) :
  disponibilité d’Ubuntu 26.04 LTS dans le catalogue d’images OVHcloud.
- [Ansible — Playbooks](https://docs.ansible.com/projects/ansible-core/devel/playbook_guide/playbooks_intro.html) :
  playbooks versionnables, idempotence et mode `--check`.
- [OVHcloud — documentation VPS](https://help.ovhcloud.com/csm/fr-documentation-bare-metal-cloud-virtual-private-servers?id=kb_browse_cat&kb_id=203c4f65551974502d4c6e78b7421996) :
  installation, KVM, SSH, snapshots et opérations propres à la gamme VPS.
- [OVHcloud — diagnostic VPS](https://help.ovhcloud.com/csm/fr-documentation-bare-metal-cloud-virtual-private-servers-troubleshooting?id=kb_browse_cat&kb_id=203c4f65551974502d4c6e78b7421996) :
  mode rescue et récupération lorsque l’accès normal ne fonctionne plus.
- [OVHcloud — politiques IAM par API](https://help.ovhcloud.com/csm/en-customer-iam-policies-api?id=kb_article_view&sysparm_article=KB0056805) :
  identités, ressources, actions, refus par défaut et expiration des droits.
- [OVHcloud — comptes de service API](https://help.ovhcloud.com/csm/de-manage-service-account?id=kb_article_view&sysparm_article=KB0059328) :
  credentials OAuth2 non liés au cycle de vie d’un utilisateur, à évaluer pour
  l’automatisation opérateur lorsque le client utilisé les prend en charge.
- [Console API OVHcloud — domaines et zones DNS](https://api.eu.ovhcloud.com/console/?branch=v1&section=/domain) :
  routes de lecture, création/suppression de records et rafraîchissement d’une
  zone ; les droits Caddy seront bornés aux records TXT ACME nécessaires.

### Docker et Compose

- [Docker — installer Docker Engine sur Ubuntu](https://docs.docker.com/engine/install/ubuntu/) :
  prise en charge d’Ubuntu Resolute 26.04 LTS sur `amd64` et `arm64`.
- [Docker — index APT Ubuntu Resolute](https://download.docker.com/linux/ubuntu/dists/resolute/Release) :
  architectures publiées et racine des index utilisés pour vérifier les
  versions épinglées.
- [Docker — utiliser Compose en production](https://docs.docker.com/compose/how-tos/production/) :
  configuration de production et recréation ciblée des services.
- [Docker — réseaux Compose](https://docs.docker.com/compose/how-tos/networking/) :
  résolution par nom et réseaux externes inter-projets.
- [Docker — `compose up`](https://docs.docker.com/reference/cli/docker/compose/up/) :
  recréation des services modifiés et attente des états running/healthy avec
  `--wait`.
- [Docker — couches et stockage](https://docs.docker.com/engine/storage/drivers/) :
  réutilisation des couches identiques entre images.

### GitHub Actions et artefacts

- [GitHub — publier des images Docker](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images) :
  publication GHCR, permissions minimales et recommandation d’épingler les
  Actions par SHA.
- [GitHub — contrôler les déploiements](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments) :
  environnements, approbations, restrictions et historique.
- [GitHub — attestations d’artefacts](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations) :
  provenance et vérification des images, avec limites selon le plan et la
  visibilité du dépôt.
- [GitHub - vérifier des attestations hors connexion](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/verify-attestations-offline) :
  acquisition d'une racine de confiance actuelle, bundle local et option
  `--custom-trusted-root` pour une vérification sans réseau.
- [GitHub — usage sécurisé des Actions](https://docs.github.com/en/actions/reference/security/secure-use) :
  risques des runners auto-hébergés persistants et recommandations de sécurité.
- [GitHub — permissions des packages](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages) :
  téléchargement d'un package associé au dépôt avec le `GITHUB_TOKEN` du
  workflow, y compris lorsqu'il est privé.
- [Trivy - container image scanning](https://trivy.dev/docs/latest/guide/target/container_image/) :
  scan strict d’une image locale ou distante par digest.
- [Trivy - private registries](https://trivy.dev/docs/latest/advanced/private-registries/) :
  authentification limitée au registre via la configuration Docker.
- [ORAS — registres OCI compatibles](https://oras.land/docs/compatible_oci_registries/) :
  push et pull de fichiers dans GHCR.
- [ORAS - release 1.3.3](https://github.com/oras-project/oras/releases/tag/v1.3.3) :
  version locked by `mise` for local and CI artifact publication.
- [ORAS - manifest fetch](https://oras.land/docs/commands/oras_manifest_fetch/) and
  [blob fetch](https://oras.land/docs/commands/oras_blob_fetch/) : exact
  manifest and layer retrieval without an application build.

### État désiré et mises à jour

- [Renovate — digest pinning Docker](https://docs.renovatebot.com/docker/) :
  tags mutables, références immuables par digest et mises à jour par PR.
- [Renovate — Docker Compose](https://docs.renovatebot.com/modules/manager/docker-compose/) :
  découverte des images dans les fichiers Compose.

### Données et observabilité

- [PostgreSQL 17 — `CREATE ROLE`](https://www.postgresql.org/docs/17/sql-createrole.html) :
  attributs et séparation des rôles.
- [PostgreSQL 17 — `CREATE DATABASE`](https://www.postgresql.org/docs/17/sql-createdatabase.html) :
  propriétaire et création des bases.
- [Prometheus — configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/) :
  scrape configs, règles et rechargement.
- [Prometheus — file-based service discovery](https://prometheus.io/docs/guides/file-sd/) :
  cibles versionnées ou générées sans exposer le socket Docker.
- [Grafana — provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/) :
  sources et dashboards pilotés par fichiers.
- [SOPS — chiffrement avec age](https://github.com/getsops/sops#23encrypting-using-age) :
  fichiers chiffrés dans Git et identités de déchiffrement externes.
