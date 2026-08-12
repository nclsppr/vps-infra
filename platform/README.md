# Plateforme commune

Ce répertoire contient le socle de la plateforme commune. Ne pas le démarrer
sur un hôte de production avant que le contrôleur ait validé chaque secret
requis, réseau externe et référence d'image immuable.

## Versions des images

La plateforme utilise les versions de services suivantes :

| Service | Version |
|---|---|
| Caddy | `2.11.4-alpine` |
| PostgreSQL | `17.10-bookworm` |
| Prometheus | `3.13.1-busybox` |
| Grafana | `13.1.1` |
| Node Exporter | `1.12.1` |
| PostgreSQL Exporter | `0.20.1` |

Chaque référence d'image Compose contient un tag lisible et un digest immuable.
L'image Caddy amont ne contient aucun fournisseur DNS OVH. La CI construit une
image Caddy avec `caddy-dns/ovh` v1.1.0 au commit
`17fd665136b593153167bf9dfee9a3c0bd2c7ac0`. La production doit utiliser
l'image publiée par digest dans `CADDY_PLATFORM_IMAGE`.

## État des applications

Le manifeste de release de production désactive les quatre applications. La
plateforme de base ne possède donc aucune route applicative, cible de collecte
ni règle d'alerte active. Chaque fichier candidat porte un suffixe `.disabled` :

```text
platform/caddy/routes/
  papersempire.caddy.disabled
  parkventory.caddy.disabled
  personal.caddy.disabled
  surplasse.caddy.disabled

platform/observability/prometheus/
  targets/surplasse.yml.disabled
  rules/surplasse.yml.disabled
```

`scripts/validate-application-state` exige que l'état des fichiers corresponde
à `releases/production.yaml`. Il refuse un fichier inconnu et un fichier actif
pour une application désactivée. Ce socle verrouillé refuse également toute
application activée. Un paquet d'intégration validé doit mettre à jour le
validateur et activer chaque fichier requis dans la même modification de
release versionnée. Une variable d'environnement ne peut pas activer un
fichier.

Le service Caddy de base ne reçoit ni identifiant OVH ni réseau applicatif. Le
point d'entrée Caddy exige les trois fichiers d'identifiants OVH uniquement
lorsque `surplasse.caddy` est actif. Cette exigence fait échouer une activation
incomplète de Surplasse avant le démarrage de Caddy.

## Frontières réseau

Ansible crée six réseaux Docker externes. La plateforme de base rejoint
uniquement les deux réseaux de plateforme :

| Réseau | Sous-réseau | Membres de la plateforme de base |
|---|---|---|
| `ops` | `172.30.30.0/24` | Caddy, Prometheus, Grafana et les exporters |
| `db_monitoring` | `172.30.31.0/24` | PostgreSQL et PostgreSQL Exporter |
| `app_surplasse` | `172.30.10.0/24` | Aucun |
| `db_surplasse` | `172.30.11.0/24` | Aucun |
| `app_parkventory` | `172.30.20.0/24` | Aucun |
| `db_parkventory` | `172.30.21.0/24` | Aucun |

Un paquet d'intégration applicative validé rattache uniquement les services
requis à un réseau applicatif. Il doit utiliser un alias unique tel que
`surplasse-backend`. Il ne doit pas utiliser un alias générique tel que
`backend`.

PostgreSQL ne rejoint pas `ops`. Caddy, Grafana et Prometheus n'ont aucun chemin
TCP direct vers PostgreSQL. PostgreSQL Exporter rejoint `db_monitoring` pour
l'accès SQL et `ops` pour l'accès aux métriques. Les rôles de base de données et
`pg_hba.conf` imposent la frontière d'autorisation de la base de données.

Caddy publie `80/tcp`, `443/tcp` et `443/udp`. Grafana se lie à
`127.0.0.1:3000` pour un tunnel SSH. Aucun autre service de la plateforme ne
publie de port hôte. Les liaisons publiques utilisent IPv4. Ne pas publier
d'enregistrement `AAAA` pour cet hôte tant que le dépôt n'a pas défini et
validé une politique IPv6 équivalente.

## Secrets

Le dépôt ne contient aucune valeur secrète. SOPS et Ansible créeront ces
fichiers de la plateforme de base sous `/etc/vps/secrets/platform` :

| Fichier | Lecteur dans le conteneur |
|---|---|
| `postgres-superuser-password` | Processus de démarrage de PostgreSQL |
| `postgres-exporter-password` | Groupe numérique `999` |
| `grafana-admin-password` | UID `472` |
| `grafana-secret-key` | UID `472` |

Le répertoire parent a le mode `0700` et appartient à `root`. Un secret avec un
seul lecteur a le mode `0400`. Le mot de passe PostgreSQL Exporter a le mode
`0440` et appartient à `root:999`. Le processus d'initialisation de PostgreSQL
s'exécute avec `999:999`. L'exporter s'exécute avec `65534:999`.

Une intégration Surplasse active exige également trois fichiers d'identifiants
DNS OVH à portée limitée. Le paquet d'intégration doit ajouter ces secrets
Compose et les variables de fichier associées. Ne pas les ajouter à la
plateforme de base désactivée.

## PostgreSQL

Le nom de volume `vps-platform-postgresql-17-data` identifie la version majeure.
Un passage à PostgreSQL 18 exige un plan de migration. L'amorçage crée seulement
le rôle de lecture `postgres_exporter`.

Un contrôleur idempotent distinct doit créer une base de données applicative et
ses rôles. Il doit utiliser un propriétaire `NOLOGIN` ainsi que des rôles de
migration et d'exécution distincts. Parkventory ne possède ni base de données
ni rôle tant que ses portes de préparation à la production sont incomplètes.

Les valeurs de mémoire actuelles exigent un VPS disposant d'au moins 4 Gio de
mémoire. Ansible doit produire les valeurs propres à l'hôte après confirmation
de sa capacité par l'opérateur.

## Validation locale

Exécuter le contrat complet depuis la racine du dépôt :

```bash
make check
```

Le contrat effectue les contrôles suivants sur la plateforme :

- Il produit la configuration Compose et applique la politique structurelle de
  production.
- Il valide la configuration Prometheus active.
- Il valide chaque règle Prometheus candidate inactive.
- Il valide Caddy avec l'ensemble des routes inactives.
- Il valide toutes les routes Caddy candidates avec des identifiants OVH
  fictifs.

Les contrôles ne démarrent pas la plateforme. Ils n'appellent pas l'API OVH. Le
mode Compose `--structural-only` est valide uniquement pour l'analyse locale.
Un contrôleur de production doit utiliser les références exactes du manifeste
de release et du paquet d'intégration validé.

Les règles d'alerte Prometheus ne possèdent ni Alertmanager configuré ni canal
de notification. L'activation de la production exige un chemin d'alerte externe
testé.
