# Plateforme VPS multi-projets

Source de vérité publique pour reconstruire et exploiter un VPS OVHcloud
hébergeant :

- `personal` (`nicolaspieper.com`) ;
- `papersempire` (`papersempire.com`) ;
- `surplasse` et ses frontends ;
- the static Parkventory demo (`parkventory.com`), separate from its disabled
  backend application.

## Déployer un site statique

Pour livrer un changement de contenu sur Atlas, modifier le dépôt du site, pas
`vps-infra` :

1. ouvrir une PR vers `main` pour
   [Personal](https://github.com/nclsppr/personal#comment-déployer-sur-atlas),
   [Parkventory](https://github.com/nclsppr/parkventory) ou
   [Papers Empire](https://github.com/nclsppr/papersempire#deploy-to-atlas) ;
2. attendre le check PR `Validate VPS release`, puis fusionner ;
3. vérifier que le workflow producteur `VPS release` du SHA fusionné publie les
   artefacts immuables avec succès ;
4. laisser la réconciliation centrale planifiée les activer sur Atlas, ou la
   déclencher immédiatement sans choisir de digest :

```bash
gh workflow run deploy-static-releases.yml \
  --repo nclsppr/vps-infra \
  --ref main
```

La planification GitHub Actions est configurée toutes les dix minutes mais
reste best-effort et peut être retardée. Une modification de `vps-infra` n'est
nécessaire que pour changer le contrat de déploiement lui-même : profil,
branche ou checks requis, routage Caddy, activation, contrôleur ou politique de
sécurité. Le
[runbook de réconciliation](docs/operations/static-release-reconciliation.md)
explique comment inspecter un run, prouver l'état Atlas, suspendre, récupérer ou
faire un rollback.

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
  runtime account, managed permissions, and a private persistent App Server
  Unix socket for an optional bounded SSH gateway or outbound mobile control;
- PostgreSQL 17, observabilité commune et provisioning Grafana ;
- manifeste de production, schéma, vérificateur de preuves GitHub et contrôleur
  de déploiement borné ;
- validations locales et CI, dont détection de secrets pour dépôt public ;
- a generic manual platform workflow that remains locked, plus a separate
  scheduled static reconciliation workflow that is active;
- Caddy multi-architecture workflow with a native PR build and Trivy gate for
  each architecture. A `main` build scans both published child manifests by
  digest before it creates and verifies GitHub provenance.
- deterministic platform integration publication from an exact runtime
  allowlist. The workflow verifies the manifest and both GHCR layer payloads
  before it creates and verifies GitHub provenance.
- a fail-closed static release reconciler for Personal, Papers Empire, and
  Parkventory. It selects only the canonical branch HEAD after every observed
  check is complete and non-failing and every configured required check is a
  success, resolves the coherent site and route tags to
  digests, and uses one bounded Atlas command. The dedicated
  `static-production` environment is configured with
  `VPS_STATIC_DEPLOY_ENABLED=true`; scheduled operation is proved for all three
  profiles.
- immutable application-release admission plus repository-delivered Ansible
  wiring for a root-owned transactional Compose controller for Surplasse and
  Parkventory. The controller source verifies every component and integration
  attestation and bundle. The 2026-08-18 rollout converged revision
  `da04a09bfa9788ae8127b63f9f3a6692bef2551b` and proved that the root-owned
  `deploy-application` controller and its argument-free gate are installed, and
  `vps-application-recover.service` is loaded, inactive after a successful
  recovery (`Result=success`, `ExecMainStatus=0`). Both protected entries remain
  `enabled: false`; no application deployment workflow invokes the controller.
- a fail-closed Parkventory PostgreSQL 17.10 preparation path. It can create
  separate owner, migrator, and runtime roles plus root-owned file secrets and
  canonical readiness evidence. The path is not run by normal convergence and
  cannot activate Parkventory. Encrypted off-site backup evidence remains an
  explicit unsatisfied gate.

The Atlas host is provisioned from this repository. It passed bootstrap,
repeated convergence, a bounded predictive check, and a complete reboot. The
controlled operator rollout recorded this live state:

- the public static edge serves `nicolaspieper.com`, `papersempire.com`, and the
  static Parkventory demo over HTTPS;
- the apex and `www` DNS records for these three sites point to Atlas by IPv4,
  with no public AAAA record;
- PostgreSQL, Prometheus, Grafana, Node Exporter, and PostgreSQL Exporter run as
  the private internal platform;
- only SSH, HTTP, and HTTPS use public host ports. Grafana binds to loopback.
  PostgreSQL and the metrics endpoints have no host port;
- the local PostgreSQL backup and isolated restore-rehearsal timers are active;
- `atlas-codex-app-server.service` is active and running as the isolated `codex`
  account on its managed private Unix socket, with no public listener.

On 2026-08-18, central reconciliation run
[`32086151183`](https://github.com/nclsppr/vps-infra/actions/runs/32086151183)
contained the resolver and all three successful deploy jobs after the final
controller convergence. Atlas reported the exact immutable tuples for Personal
(`163b9c9643dd9c54e9b1bb5d558d34a670e28e52`), Papers Empire
(`b95f9bdde468aac9d03bd0548c7aa42969e52df7`), and Parkventory
(`db9571cc59d0fcc31c6554af259eda4c29988a6a`) as active and healthy. The
complete site and route digests, controller boundary, and TLS probes are in the
[rollout evidence](docs/evidence/2026-08-18-static-reconciliation-rollout.md).
Use the
[static reconciliation runbook](docs/operations/static-release-reconciliation.md)
for enablement, suspension, run inspection, rollback, recovery, and key
rotation. A green workflow conclusion alone is not proof that all three
profiles were `ready`.

This bounded rollout does not enable the dynamic release manifest or invoke the
Compose application controller. The manifest keeps every dynamic application
at `enabled: false`. The application controller is installed, but no application
workflow invokes it and no desired or active application release exists. The
runtime doctor reports the missing desired and active application release
records as expected warnings.
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
also rejects branch-history rollback. Protected inventories make repeated exact
candidates local health checks instead of registry downloads. A transient
activation unit and a boot oneshot recover unfinished transactions and bounded
probe residue before the systemd-managed public-edge start. Docker's
`restart: unless-stopped` can still restart the existing Caddy container when
the daemon starts, before that ordering is applied. Closing this daemon-level
recovery bypass remains an explicit platform hardening task.
Branch protection remains a separate external gate. The generic locked
controller cannot call `apply-release`, which remains absent. The repository
application controller has its own exact forced-command gate, shared static
lock, transaction journal, quarantine, and boot recovery wiring. Revision
`da04a09bfa9788ae8127b63f9f3a6692bef2551b` and its recovery unit were installed
and proved healthy while idle during the dated rollout. Both application
entries remain disabled and no application release is active. Activation still
requires a reviewed
application entry, database, secrets, observability, edge route, network
cutover, a dedicated workflow, and every blocker in ADR-0010.

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
Actions ne consomment pas de quota de minutes facturables. Les producteurs
statiques reconstruisent leur paquet complet. Surplasse reconstruit et publie
actuellement sa matrice fixe de cinq images à chaque push sur `main`; chaque
référence reste indépendante, liée au SHA global et résolue par digest. Une
optimisation future pourrait sélectionner les composants affectés sans changer
ce contrat. Si la tarification change, le repli prévu est un builder éphémère
séparé du VPS, jamais un runner persistant dans la production.

Cette décision est détaillée dans
[l’ADR-0003](docs/decisions/0003-builds-hors-du-vps-de-production.md).

## Documentation

- [Architecture cible](docs/architecture.md)
- [Automatisation de l’hôte](ansible/README.md)
- [Pile plateforme](platform/README.md)
- [Legacy locked Surplasse preparation adapter](applications/surplasse/README.md)
- [Livraison et mises à jour](docs/deployment.md)
- [Contrat du contrôleur de release](scripts/README.md)
- [Reconstruction depuis zéro](docs/rebuild.md)
- [Accès OVHcloud à préparer](docs/operations/acces-ovhcloud.md)
- [Codex CLI on Atlas](docs/operations/codex-cli.md)
- [Surplasse SMTP relay preparation](docs/operations/surplasse-smtp.md)
- [Sauvegarde PostgreSQL et répétition de restauration](docs/operations/postgresql-backup.md)
- [Static release reconciliation operations](docs/operations/static-release-reconciliation.md)
- [Static reconciliation rollout evidence, 2026-08-18](docs/evidence/2026-08-18-static-reconciliation-rollout.md)
- [Parkventory PostgreSQL preparation](docs/operations/parkventory-postgresql.md)
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
- [ADR-0012 - locked Surplasse DNS cutover controller](docs/decisions/0012-locked-surplasse-dns-cutover-controller.md)

L’ancien runbook est conservé pour l’historique dans
[`docs/archive/VPS-SETUP-v0.md`](docs/archive/VPS-SETUP-v0.md). Il ne doit pas
être exécuté.

The recovered [`manage-ovh-dns` skill prototype](docs/archive/manage-ovh-dns-skill-prototype.md)
is also inactive. It records a proposed security boundary. It does not prove
that the referenced controller exists and it must not be installed or invoked.
