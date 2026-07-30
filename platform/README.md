# Plateforme commune

Ce répertoire contient uniquement le premier socle exécutable. Il ne doit pas
être lancé sur une machine de production tant que l’image Caddy OVH n’a pas été
publiée par digest, que les secrets n’ont pas été matérialisés et que les
réseaux externes n’ont pas été créés par Ansible.

## Versions de départ

Les quatre versions déjà utilisées par le catalogue de production Surplasse
sont conservées exactement :

| Service | Version |
|---|---|
| Caddy | `2.11.4-alpine` |
| PostgreSQL | `17.10-bookworm` |
| Prometheus | `3.13.1-busybox` |
| Grafana | `13.1.1` |

Node Exporter `1.12.1` et PostgreSQL Exporter `0.20.1` ont été ajoutés avec
leurs digests multi-architecture publiés. Les tags restent lisibles, mais les
digests sont l’identité utilisée par Compose.

L’image Caddy amont ne contient pas de fournisseur DNS. Le Dockerfile construit
une image unique avec `caddy-dns/ovh` v1.1.0, épinglé au commit complet
`17fd665136b593153167bf9dfee9a3c0bd2c7ac0`. Cette image doit être
construite en CI, publiée dans GHCR puis référencée avec son digest dans
`CADDY_PLATFORM_IMAGE`. La valeur de `.env.example` sert uniquement à
`docker compose config` et échouera volontairement au démarrage sur la directive
`dns ovh`.

## Frontières réseau

Ansible créera six réseaux externes avec des sous-réseaux stables :

| Réseau | Sous-réseau | Membres plateforme |
|---|---|---|
| `ops` | `172.30.30.0/24` | Caddy, Prometheus, Grafana, exporteurs |
| `db_monitoring` | `172.30.31.0/24` | PostgreSQL et PostgreSQL Exporter uniquement |
| `app_surplasse` | `172.30.10.0/24` | Caddy, Prometheus |
| `db_surplasse` | `172.30.11.0/24` | PostgreSQL |
| `app_parkventory` | `172.30.20.0/24` | Caddy, vide tant que Parkventory est désactivé |
| `db_parkventory` | `172.30.21.0/24` | PostgreSQL, vide côté application |

Les futurs Compose applicatifs doivent fournir des alias uniques tels que
`surplasse-backend`, jamais le nom générique `backend`. Caddy et Prometheus
n’entrent dans aucun réseau `db_*`. Les réseaux réduisent les chemins
accidentels ; `pg_hba.conf`, les bases et les rôles distincts restent la vraie
frontière PostgreSQL.

PostgreSQL ne rejoint jamais `ops` : Caddy, Grafana et Prometheus n’ont donc
aucun chemin TCP direct vers la base. PostgreSQL Exporter fait seul le pont
entre `db_monitoring` (collecte SQL) et `ops` (endpoint métriques).

Seuls Caddy `80/tcp`, `443/tcp` et `443/udp` sont publics. Grafana est publié sur
`127.0.0.1:3000` pour un tunnel SSH. PostgreSQL, Prometheus et les exporteurs ne
publient aucun port hôte.

Ces binds publics sont actuellement IPv4 (`0.0.0.0`). Aucun enregistrement
`AAAA` ne doit viser ce VPS avant l’ajout et la preuve d’une politique Docker et
UFW IPv6 équivalente, puis de probes externes sur les deux familles d’adresses.

## Secrets attendus

Le dépôt ne contient aucune valeur. Les fichiers suivants seront créés par le
mécanisme SOPS/Ansible sous `/etc/vps/secrets/platform` :

| Fichier | Lecteur dans le conteneur |
|---|---|
| `ovh-application-key` | root, Caddy |
| `ovh-application-secret` | root, Caddy |
| `ovh-consumer-key` | root, Caddy |
| `postgres-superuser-password` | root au démarrage PostgreSQL |
| `postgres-exporter-password` | groupe numérique `999`, partagé en lecture entre PostgreSQL et l’exporteur |
| `grafana-admin-password` | UID `472` |
| `grafana-secret-key` | UID `472` |

Le répertoire parent reste `0700 root`. Les secrets à lecteur unique sont
`0400` et appartiennent au lecteur numérique indiqué. Le mot de passe exporteur
est l’exception : `0440 root:999`; l’init PostgreSQL s’exécute en `999:999` et
l’exporteur en `65534:999`. Cela compense le fait que les secrets Compose locaux
sont des bind mounts et ne réécrivent pas toujours `uid/gid/mode`.

Le compte OVH devra être limité aux opérations DNS nécessaires sur les zones
concernées. Aucun endpoint, application key, secret ou consumer key réel n’est
déduit ici. `ovh-eu` n’est qu’un endpoint explicite de configuration à confirmer
avec le compte fourni.

## PostgreSQL

Le volume `vps-platform-postgresql-17-data` encode volontairement la version
majeure. Un changement vers PostgreSQL 18 n’est pas une simple mise à jour
d’image. Le bootstrap ne crée que le rôle de lecture `postgres_exporter`.

Les bases et rôles Surplasse/Parkventory seront provisionnés par un contrôleur
idempotent séparé, avec owner `NOLOGIN`, migrateur et runtime distincts. Le
fichier `pg_hba.conf` est déjà fermé aux autres couples base/rôle/réseau.
Parkventory reste sans base et sans rôle tant que ses portes de production ne
sont pas satisfaites.

Les réglages mémoire sont un point de départ pour un VPS d’au moins 4 Gio. Ils
devront être rendus depuis l’inventaire Ansible lorsque la taille OVH sera
connue, avant tout démarrage réel.

## Validation locale sans déploiement

```bash
docker compose \
  --env-file platform/.env.example \
  --file platform/compose.yaml \
  config --quiet

docker run --rm \
  --entrypoint promtool \
  -v "$PWD/platform/observability/prometheus:/etc/prometheus:ro" \
  "$(sed -n 's/^PROMETHEUS_IMAGE=//p' platform/.env.example)" \
  check config /etc/prometheus/prometheus.yml

jq --exit-status empty \
  platform/observability/grafana/dashboards/platform/overview.json
```

Pour valider Caddy, il faut construire l’image locale avec les arguments de
`.env.example`, fournir trois fichiers factices montés en lecture seule, puis
exécuter `caddy validate`. Cette validation n’effectue aucun appel à l’API OVH,
mais elle exige le module compilé. Aucun `docker compose up` ne fait partie de
ce bootstrap local.

Le validateur Compose reçoit alors `--structural-only` : il prouve la structure
locale et les sources de mounts, mais n’autorise jamais une image en production.
Le contrôleur live devra recevoir les références exactes depuis le manifeste et
le digest du bundle d’intégration vérifié ; il lui est interdit d’utiliser ce
mode de lint.

## Routes actives

- Personal : apex, `www` et `nicolas.pieper.fr` redirigés vers
  `https://nicolaspieper.com` ; release sous `/srv/www/personal/current`.
- Papers Empire : apex `https://papersempire.com` uniquement afin de préserver
  l’origine de son `localStorage` ; release sous
  `/srv/www/papersempire/current`.
- Surplasse : apex, API, Dashboard, documentation et wildcard, avec DNS-01 OVH,
  refus public de tout `/q/*` (métriques, health détaillée, OpenAPI et Swagger),
  règles CORS existantes et flush immédiat pour les flux SSE. Les deux endpoints Stripe Connect répondent explicitement `503`
  tant qu’un adaptateur de production n’a pas remplacé le serveur de
  développement qui les fournit aujourd’hui.
- Parkventory : aucun fragment importé. Le fichier `.disabled` ne devient pas
  actif via une variable d’environnement.

Les répertoires `current` ne sont pas créés par Compose. Le contrôleur de
déploiement y basculera atomiquement les artefacts statiques vérifiés.

Les règles Prometheus sont validées mais aucun Alertmanager ni canal de
notification n’est encore configuré. Elles ne constituent donc pas une
astreinte et la plateforme reste bloquée sur le choix puis le test d’un canal
d’alerte avant activation de production.
