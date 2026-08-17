# Plateforme VPS multi-projets

Source de vérité publique pour reconstruire et exploiter un VPS OVHcloud
hébergeant :

- `personal` (`nicolaspieper.com`) ;
- `papersempire` (`papersempire.com`) ;
- `surplasse` et ses frontends ;
- the static Parkventory demo (`parkventory.com`), separate from its disabled
  backend application.

## Architecture retenue

Le socle reste volontairement simple :

1. **Ansible** converge un Ubuntu 26.04 LTS neuf et durcit l’accès ;
2. **Docker Compose** exploite le VPS unique, sans orchestrateur distribué ;
3. une seule pile **plateforme** fournit Caddy, PostgreSQL, Prometheus,
   Grafana et les exporteurs ;
4. chaque application conserve son propre cycle de release ;
5. GitHub Actions construit et publie des artefacts immuables ;
6. le VPS tire uniquement un commit `vps-infra` autorisé et des digests déjà
   validés. Il ne compile jamais les dépôts applicatifs.

Personal, Papers Empire, and the Parkventory demo are static releases served by
the common Caddy service. The Parkventory backend stays disabled. Surplasse
keeps its application images, but not its copy of Caddy, PostgreSQL,
Prometheus, or Grafana. One physical PostgreSQL cluster is shared. Each project
keeps separate databases, roles, secrets, networks, and migrations.

The Parkventory demo is explicitly temporary. Its future React frontend and
Java backend form one Compose application release and must replace the static
promotion through an exclusive cross-contract handoff; the two modes may never
own `parkventory.com` simultaneously.

## État actuel

Le premier socle exécutable est livré :

- bootstrap et convergence Ansible fail-closed ;
- Docker et Compose épinglés, pare-feu, SSH, comptes et répertoires ;
- Compose plateforme durci et images amont épinglées par digest ;
- Caddy with a generated, checksum-locked Go graph and the OVH DNS provider;
- GitHub CLI 2.97.0 with archive and executable checksums, plus a fail-closed
  static OCI materializer for Personal, Papers Empire, and the Parkventory
  demo;
- Codex CLI 0.147.0 from the standalone OpenAI package, with an isolated
  runtime account, a separate bounded SSH gateway, managed permissions, and a
  private persistent App Server Unix socket;
- PostgreSQL 17, observabilité commune et provisioning Grafana ;
- manifeste de production, schéma, vérificateur de preuves GitHub et contrôleur
  de déploiement borné ;
- validations locales et CI, dont détection de secrets pour dépôt public ;
- workflow de production manuel, désactivé tant que les portes de la plateforme
  et des releases ne sont pas éprouvées ;
- Caddy multi-architecture workflow with a native PR build and Trivy gate for
  each architecture. A `main` build scans both published child manifests by
  digest before it creates and verifies GitHub provenance.
- deterministic platform integration publication from an exact runtime
  allowlist. The workflow verifies the manifest and both GHCR layer payloads
  before it creates and verifies GitHub provenance.
- a fail-closed static release reconciler for Personal, Papers Empire, and
  Parkventory. It selects only the canonical branch HEAD after all observed and
  expected checks are green, resolves the coherent site and route tags to
  digests, and uses one bounded Atlas command. It stays disabled until the
  dedicated environment and `VPS_STATIC_DEPLOY_ENABLED=true` are configured.
- immutable application-release admission plus a root-owned transactional
  Compose controller for Surplasse and Parkventory. It verifies every component
  and integration attestation and bundle, but both protected entries remain
  disabled and no application deployment workflow invokes it.

The Atlas host is provisioned from this repository. It passed bootstrap,
repeated convergence, a bounded predictive check, and a complete reboot. The
controlled operator rollout now has this live state:

- the public static edge serves `nicolaspieper.com`, `papersempire.com`, and the
  static Parkventory demo over HTTPS;
- the apex and `www` DNS records for these three sites point to Atlas by IPv4,
  with no public AAAA record;
- PostgreSQL, Prometheus, Grafana, Node Exporter, and PostgreSQL Exporter run as
  the private internal platform;
- only SSH, HTTP, and HTTPS use public host ports. Grafana binds to loopback.
  PostgreSQL and the metrics endpoints have no host port;
- the local PostgreSQL backup and isolated restore-rehearsal timers are active.

This bounded rollout does not enable the dynamic release manifest or invoke the
installed Compose application controller. The manifest keeps every dynamic
application at `enabled: false`; the root controller rejects each one before
runtime validation or network access, and the runtime doctor reports the
missing desired and active release records as expected warnings.
`parkventory.com` is a static demonstration only. `surplasse.com` keeps its
previous DNS target and Atlas does not serve a Surplasse application.

The release contract can validate a complete immutable candidate declaration
while the platform stays disabled. Candidate evidence is checked before the
controller records desired state. The manual `vps-release.yml` workflow now
proves the exact platform candidate subject for two review gates only. It
verifies the seven OCI references, image labels, strict HIGH and CRITICAL
vulnerability scans, the OVH DNS module, and workflow-bound GitHub
attestations. It refuses every CRITICAL finding. It accepts a HIGH finding only
when one unexpired, digest-bound VEX statement matches the service, image,
platform, binary, package, installed version, and CVE exactly. All eight
production blockers remain. A candidate does not
publish a port, start a container, or create active state.

The canonical proof binds the VEX policy digest and its earliest exception
expiry. Evidence verification rejects the proof after that UTC date, even when
the GitHub artifact itself is still retained.

The deploy role installs the static materializer as a root-owned primitive and
exposes a separate allowlisted `deploy-static-live` forced command. It
binds each site, route inventory, and platform integration artifact to an exact
GitHub attestation. It performs bounded unprivileged parsing and verifies the
exact integration route through temporary HTTPS Caddy before it changes the
`current` symlink. The live form journals the transaction, rechecks the
canonical source HEAD, probes the actual edge with strict public TLS, records
the complete active tuple, and restores the previous release before it
classifies a failed probe. It quarantines the candidate only while the same
source HEAD and Caddy runtime still hold. A bounded unprivileged ancestry check
also rejects branch-history rollback. Protected inventories make repeated exact candidates local health
checks instead of registry downloads. A transient activation unit and a boot
oneshot recover unfinished transactions and bounded probe residue before the
public edge can start.
Branch protection remains a separate external gate. The generic locked
controller cannot call `apply-release`, which remains absent. The separate
application controller has its own exact forced-command gate, shared static
lock, transaction journal, quarantine, and boot recovery. It remains inert
until a reviewed application entry is enabled and its database, secrets,
observability, edge route, and network cutover are prepared.

## Démarrage local

Prérequis : `mise`, Git, Docker Engine avec le plugin Compose et GNU Make.

```bash
mise trust
make setup
make check
make doctor-local
```

`make check` ne déploie rien. Il valide les workflows, Ansible, le manifeste,
les politiques de sécurité, Compose, Prometheus, Grafana et construit une image
Caddy locale pour prouver la présence du module OVH.

Le premier accès distant utilisera des fichiers locaux ignorés par Git :

```bash
cp ansible/inventories/production/hosts.example.yml \
  ansible/inventories/production/hosts.yml
cp ansible/inventories/production/group_vars/bootstrap-public.yml.example \
  /chemin/prive/bootstrap-public.yml

make bootstrap \
  ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml

# Après avoir prouvé une seconde connexion avec le compte administrateur :
# make converge récupère origin/main et n'installe que ce SHA prouvé.
make converge \
  ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml

# After successful convergence, predict drift without remote mutation.
make converge-check \
  ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
```

Les valeurs réelles OVHcloud, l’inventaire, les clés et les secrets ne sont
jamais commités.

## Builds et coût GitHub Actions

Compiler directement sur le VPS de production économiserait éventuellement des
minutes CI, mais mêlerait code non fiable, daemon Docker, CPU, disque et secrets
de production. Une image seulement locale disparaîtrait aussi avec le VPS.

Les dépôts concernés étant publics aujourd’hui, les runners standards GitHub
Actions ne consomment pas de quota de minutes facturables. La stratégie retenue
est donc de ne reconstruire que les composants modifiés, publier leurs digests
dans GHCR, puis exécuter un déploiement VPS très court. Si cette tarification
change, le repli prévu est un builder éphémère séparé du VPS, jamais un runner
persistant dans la production.

Cette décision est détaillée dans
[l’ADR-0003](docs/decisions/0003-builds-hors-du-vps-de-production.md).

## Documentation

- [Architecture cible](docs/architecture.md)
- [Automatisation de l’hôte](ansible/README.md)
- [Pile plateforme](platform/README.md)
- [Adaptateur Surplasse verrouillé](applications/surplasse/README.md)
- [Livraison et mises à jour](docs/deployment.md)
- [Contrat du contrôleur de release](scripts/README.md)
- [Reconstruction depuis zéro](docs/rebuild.md)
- [Accès OVHcloud à préparer](docs/operations/acces-ovhcloud.md)
- [Codex CLI on Atlas](docs/operations/codex-cli.md)
- [Surplasse SMTP relay preparation](docs/operations/surplasse-smtp.md)
- [Sauvegarde PostgreSQL et répétition de restauration](docs/operations/postgresql-backup.md)
- [Contrat des secrets](secrets/README.md)
- [Sources et preuves d’audit](docs/references.md)
- [Plan de mise en œuvre](VPS-SETUP.md)
- [ADR-0001 — Ansible et Compose](docs/decisions/0001-ansible-compose-plateforme-partagee.md)
- [ADR-0002 — dépôt public](docs/decisions/0002-depot-public-sans-etat-sensible.md)
- [ADR-0003 — builds hors production](docs/decisions/0003-builds-hors-du-vps-de-production.md)
- [ADR-0004 - Parkventory static demo](docs/decisions/0004-parkventory-static-demo.md)
- [ADR-0008 - automatic static release reconciliation](docs/decisions/0008-automatic-static-release-reconciliation.md)
- [ADR-0005 - dedicated Codex CLI account](docs/decisions/0005-dedicated-codex-cli-account.md)
- [ADR-0006 - private Codex App Server](docs/decisions/0006-private-codex-app-server.md)
- [ADR-0007 - managed transactional email relay for Surplasse](docs/decisions/0007-relais-email-transactionnel-surplasse.md)
- [ADR-0009 - immutable application release admission](docs/decisions/0009-immutable-application-release-admission.md)
- [ADR-0010 - disabled transactional application controller](docs/decisions/0010-disabled-transactional-application-controller.md)

L’ancien runbook est conservé pour l’historique dans
[`docs/archive/VPS-SETUP-v0.md`](docs/archive/VPS-SETUP-v0.md). Il ne doit pas
être exécuté.

The recovered [`manage-ovh-dns` skill prototype](docs/archive/manage-ovh-dns-skill-prototype.md)
is also inactive. It records a proposed security boundary. It does not prove
that the referenced controller exists and it must not be installed or invoked.
