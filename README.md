# Plateforme VPS multi-projets

Source de vérité publique pour reconstruire et exploiter un VPS OVHcloud
hébergeant :

- `personal` (`nicolaspieper.com`) ;
- `papersempire` (`papersempire.com`) ;
- `surplasse` et ses frontends ;
- `parkventory`, maintenu désactivé jusqu’à ses preuves de readiness.

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

`personal` et `papersempire` seront servis comme releases statiques par le
Caddy commun. Surplasse conservera ses images applicatives, mais plus sa copie
de Caddy, PostgreSQL, Prometheus ou Grafana. Un cluster PostgreSQL physique est
mutualisé ; bases, rôles, secrets, réseaux et migrations restent séparés par
projet.

## État actuel

Le premier socle exécutable est livré :

- bootstrap et convergence Ansible fail-closed ;
- Docker et Compose épinglés, pare-feu, SSH, comptes et répertoires ;
- Compose plateforme durci et images amont épinglées par digest ;
- Caddy avec fournisseur DNS OVH compilé depuis un commit exact ;
- PostgreSQL 17, observabilité commune et provisioning Grafana ;
- manifeste de production, schéma, vérificateur de preuves GitHub et contrôleur
  de déploiement borné ;
- validations locales et CI, dont détection de secrets pour dépôt public ;
- workflow de production manuel, désactivé tant que les portes de la plateforme
  et des releases ne sont pas éprouvées ;
- workflow multi-architecture Caddy : build de preuve en PR, publication GHCR
  et attestation uniquement depuis `main`.

The Atlas host is now provisioned from this repository. It passed bootstrap,
repeated convergence, a bounded predictive check, and a complete reboot. No
DNS zone, application release, platform service, or production data was
changed. The manifest keeps every unit at `enabled: false`. Its root policy
rejects activation, and the controller still has no live applicator.

The release contract can validate a complete immutable candidate declaration
while the platform stays disabled. Candidate evidence is checked before the
controller records desired state. The evidence does not yet prove each digest.
A candidate does not publish a port, start a container, or create active state.

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
- [Livraison et mises à jour](docs/deployment.md)
- [Contrat du contrôleur de release](scripts/README.md)
- [Reconstruction depuis zéro](docs/rebuild.md)
- [Accès OVHcloud à préparer](docs/operations/acces-ovhcloud.md)
- [Contrat des secrets](secrets/README.md)
- [Sources et preuves d’audit](docs/references.md)
- [Plan de mise en œuvre](VPS-SETUP.md)
- [ADR-0001 — Ansible et Compose](docs/decisions/0001-ansible-compose-plateforme-partagee.md)
- [ADR-0002 — dépôt public](docs/decisions/0002-depot-public-sans-etat-sensible.md)
- [ADR-0003 — builds hors production](docs/decisions/0003-builds-hors-du-vps-de-production.md)

L’ancien runbook est conservé pour l’historique dans
[`docs/archive/VPS-SETUP-v0.md`](docs/archive/VPS-SETUP-v0.md). Il ne doit pas
être exécuté.
