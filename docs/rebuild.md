# Reconstruire le VPS depuis zéro

## Résultat attendu

À partir d’un Ubuntu 26.04 LTS neuf, d’une adresse IP, d’une clé SSH et des secrets
conservés hors de la machine détruite, l’opérateur doit pouvoir :

1. converger l’hôte avec Ansible ;
2. démarrer la plateforme partagée ;
3. réinstaller les releases épinglées ;
4. restaurer les données lorsqu’une sauvegarde sera disponible ;
5. vérifier les domaines et reprendre le trafic.

La création ou la réinstallation de la ressource OVHcloud peut rester manuelle.
Le contrat de ce dépôt commence à un Ubuntu 26.04 LTS joignable en SSH : il ne
suppose ni snapshot, ni `cloud-init`, ni endpoint de provisioning tant que
l’offre exacte n’a pas été observée. Un VPS OVHcloud et une instance Public
Cloud n’ont pas le même plan de contrôle ; leur automatisation ne doit pas être
inventée avant cet inventaire.

Lorsque l’accès API sera disponible, la première passe restera en lecture
seule : type de service, région, image, adresses et capacités de secours. Une
couche OpenTofu ne sera ajoutée que si l’API permet réellement de recréer la
ressource choisie et si elle élimine une étape manuelle mesurée. Ansible suffit
déjà à rendre la configuration du système reproductible après l’accès SSH.

## Ce qui doit survivre hors du VPS

| Élément | Emplacement de récupération |
|---|---|
| playbooks, Compose, Caddy, Prometheus, dashboards et runbooks | dépôt public `vps-infra`, sans donnée sensible |
| versions actives | `releases/production.yaml` dans Git |
| images et artefacts statiques | GHCR, référencés par digest |
| clé SSH administrateur | Termius ou poste de confiance, avec copie de récupération |
| clé age permettant SOPS | gestionnaire de secrets et copie hors ligne |
| jeton GHCR en lecture | gestionnaire de secrets ou procédure de régénération |
| accès Git en lecture à `vps-infra` | HTTPS public ; deploy key seulement si la visibilité devient privée |
| clé du déclencheur SSH de production | secret de l’environnement GitHub `production`, révocable séparément |
| secrets DNS, JWT, Stripe, SMTP, Grafana et bases | fichiers SOPS chiffrés et clé age externe |
| sauvegardes métier | cible hors VPS, à définir dans le chantier sauvegarde |
| inventaire DNS, MX, SPF, DKIM et DMARC | export versionné sans secrets et copie opérateur |

Une clé uniquement stockée sur le VPS n’est pas une stratégie de
reconstruction. Les secrets peuvent être chiffrés dans Git avec SOPS/age, mais
la clé privée age ne rejoint jamais le dépôt.

## Contrat de commandes livré

Les commandes locales suivantes sont présentes et restent explicitement
bornées :

```bash
# Validation sans accès distant
make setup
make check
make doctor-local

# Depuis un poste de confiance, avec fichiers locaux ignorés par Git
make bootstrap ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
make converge ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
make converge-check ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
```

Run `converge-check` only after a successful normal convergence. It predicts
drift from the same isolated `origin/main` snapshot and rejects every
command-line option except the exact `--check --diff` pair.

Le contrôleur `scripts/deploy <sha40>` sait valider et planifier un état désiré,
mais reste en dry-run. L’activation live exige encore l’applicateur root-owned,
les secrets, les artefacts applicatifs et une répétition sur hôte jetable.

## Phase 0 — préparer sans toucher au trafic

Avant la destruction ou la création :

1. vérifier que `vps-infra/main` et la release à restaurer sont accessibles ;
2. vérifier que tous les digests du manifeste existent dans GHCR ;
3. tester le déchiffrement SOPS depuis le poste de récupération ;
4. exporter les zones DNS, surtout les enregistrements mail ;
5. noter les empreintes des clés SSH attendues ;
6. vérifier la disponibilité des sauvegardes métier lorsqu’elles existeront ;
7. réserver une fenêtre de bascule ;
8. garder le DNS actuel actif jusqu’aux probes du nouvel hôte.

Le contrôle doit être possible même si l’ancien VPS est déjà perdu. Sinon un
prérequis dépend encore de la machine que l’on prétend pouvoir reconstruire.
Si le dépôt devient privé, l’opérateur doit aussi pouvoir enregistrer une
nouvelle deploy key en lecture seule.

## Phase 1 — créer l’hôte

1. créer un VPS avec l’image Ubuntu 26.04 LTS validée par le dépôt ;
2. injecter uniquement la clé SSH administrateur ;
3. relever IPv4 et IPv6 ;
4. ouvrir une première session et définir le moyen d’accès à la console de
   secours du fournisseur ;
5. lancer le playbook `bootstrap`.

Le bootstrap est minimal et idempotent :

- crée le compte administrateur ;
- copie les clés ;
- installe Python si l’image en a besoin pour Ansible ;
- ne modifie encore ni sshd ni le pare-feu.

L’opérateur prouve ensuite une seconde connexion avec ce compte. `site.yml` ne
redémarre SSH qu’après validation de sa configuration et n’active UFW qu’après
avoir autorisé le port de la session courante. La session initiale reste ouverte
jusqu’à cette preuve.

## Phase 2 — converger l’hôte

Le playbook `site.yml` :

1. applique les mises à jour de sécurité ;
2. configure temps, swap, limites et journalisation ;
3. installe Docker depuis son dépôt officiel et le plugin Compose ;
4. configure la rotation et le redémarrage de Docker ;
5. crée `/srv/vps`, `/srv/vps/releases`, `/srv/www` et `/etc/vps/secrets` ;
6. crée les réseaux externes ;
7. installe le wrapper de déploiement root-owned depuis un SHA `vps-infra`
   prouvé sur `origin/main` ;
8. installs the attestation client used for static sites;
9. installe les fichiers déchiffrés avec les propriétaires et modes attendus ;
10. configure la clé GHCR en lecture seule ;
11. configure l’origine HTTPS publique de `vps-infra`, ou une deploy key si sa
    visibilité a changé ;
12. installe la clé publique du déclencheur GitHub sur le compte de livraison,
    avec commande forcée ;
13. lance les validations hôte.

The current tranche delivers points 1 to 8 and 11 to 13. It uses the public
HTTPS repository and installs a trigger key only when the operator supplies
one. GitHub CLI 2.97.0 is pinned by release archive and executable checksums.
Personal, Papers Empire, and platform integration are public GHCR packages.
Their materializer uses anonymous bounded registry downloads in short-lived
systemd `DynamicUser` executions. One execution fetches each attestation bundle
set. The root orchestrator copies that set and removes the fetch state. A new
sequential offline execution of the same fixed transient unit gives the local
bundle and digest-bound OCI manifest to GitHub CLI. The fixed unit name
prevents concurrent worker creation. No execution receives an operator token.
ORAS remains in the locked local and
CI toolchain; reconstruction does not install it on Atlas.

Before a static activation, supply the exact application source revision, site
digest, route digest, platform integration revision, integration digest, and
Caddy image digest. The integration package and the protected infrastructure
mirror must name the same Caddy image. An attestation proves the exact source
ref and workflow. It does not prove branch protection. Restore the repository
rulesets as a separate reconstruction step before production activation.

The role creates the private secret root but does not materialize a secret. A
GHCR credential for private application images, decrypted secrets, the live
applicator, and their validations remain explicit gates. `site.yml` does not
activate a platform service or an application.

Le compte de livraison n’entre pas dans le groupe `docker`. Une règle restreinte
autorise seulement le wrapper root-owned, par chemin absolu et sans `SETENV`.
Son compte possède un shell système valide pour que la commande forcée
fonctionne, mais aucune session interactive n’est autorisée. L’administrateur
peut utiliser sudo, mais ses actions manuelles ne remplacent jamais Ansible dans
l’état canonique.

## Phase 3 — démarrer la plateforme

La plateforme est restaurée avant les applications :

1. Caddy, avec sa configuration validée mais sans basculer le DNS ;
2. PostgreSQL et ses volumes ;
3. création idempotente des bases, rôles et extensions attendus ;
4. Prometheus et Grafana ;
5. exporteurs communs ;
6. contrôles internes.

Les bases et rôles ne sont pas créés par des commandes SQL copiées dans un
terminal. Un script idempotent, limité à une liste d’applications connue, gère
leur existence. Les mots de passe arrivent par fichiers de secrets.

Avant de monter un volume PostgreSQL, le wrapper compare l’image, la version
majeure et `PGDATA` au manifeste. Le démarrage est refusé si le volume appartient
à une autre version majeure ou si le résultat des matrices exactes 17.10/18.3
et les ADR correspondantes ne sont pas satisfaits.

Sans restauration de sauvegarde, PostgreSQL est vide à ce stade. Il est
interdit de confondre « service sain » et « données récupérées ».

## Phase 4 — restaurer les données

Ce chantier sera spécifié séparément, mais l’ordre de reconstruction doit déjà
réserver sa place :

1. arrêter ou ne pas démarrer les backends qui pourraient migrer ;
2. restaurer chaque base dans le cluster cible ;
3. vérifier propriétaire, extensions et historique Flyway ;
4. exécuter des contrôles de cohérence propres au produit ;
5. seulement ensuite autoriser les migrations vers la release active.

`personal` et `papersempire` n’ont aucun état serveur. Les sauvegardes
`localStorage` restent dans les navigateurs des utilisateurs tant que le domaine
et le schéma ne changent pas.

## Phase 5 — réconcilier les applications

`reconcile RELEASE=production` :

1. lit le manifeste au commit d’infrastructure choisi ;
2. refuse Parkventory tant que `enabled: false` ;
3. récupère et active les deux releases statiques ;
4. tire les images Surplasse ;
5. exécute les migrations contrôlées ;
6. démarre les Compose applicatifs avec `--wait` ;
7. vérifie les cibles Prometheus et les dashboards provisionnés ;
8. enregistre l’état actif.

Le manifeste doit permettre de restaurer une release historique, pas seulement
la plus récente. Les digests garantissent que les octets récupérés ne changent
pas derrière un tag.

## Phase 6 — valider avant le DNS

Les probes utilisent d’abord l’IP du nouveau serveur avec `--resolve` ou un
fichier hosts :

- validation TLS stricte si les certificats ont été obtenus par DNS-01 avant la
  bascule ;
- complete `personal` file inventory: EN/FR, Work, CV, Blog, articles,
  Dashboard, Claude, archive, error pages, and assets;
- separate probes for domain redirects, including the historical domain;
- jeu, langues, Dashboard et documentation de Papers Empire ;
- santé Backend, Onboarding, Commande, Dashboard et documentation Surplasse ;
- fermeture publique des métriques, Swagger et interfaces internes ;
- CORS et SSE Surplasse ;
- cibles Prometheus à `UP` ;
- Grafana accessible uniquement par tunnel ;
- aucun port hôte inattendu ;
- aucune valeur secrète dans les logs.

Le DNS ne change qu’après une vérification complète. Les enregistrements mail ne
sont jamais remplacés par une génération automatique sans comparaison exacte
avec l’export.

Cette séquence suppose DNS-01 pour chaque zone. Si une zone ne le permet pas,
les probes pré-bascule utilisent un hostname de préproduction et une validation
locale avec le bon en-tête `Host` ; l’émission HTTP-01 et la sonde TLS publique
stricte ont alors lieu pendant une bascule contrôlée, avec retour rapide prévu.

## Phase 7 — basculer et observer

1. réduire les TTL avant la fenêtre lorsque c’est possible ;
2. basculer les A ou les nameservers décidés ; ne publier les AAAA qu’après
   preuve explicite du pare-feu, des binds et des probes IPv6 ;
3. vérifier chaque zone auprès des serveurs autoritatifs ;
4. exécuter les probes depuis au moins un réseau externe ;
5. tester l’envoi et la réception mail pour les zones concernées ;
6. observer erreurs, saturation, certificats et métriques ;
7. conserver l’ancien hébergement disponible pendant la fenêtre de retour.

Pour `personal` et `papersempire`, GitHub Pages peut rester la production
jusqu’à cette étape. Le nouveau workflow VPS se valide en parallèle sans
prétendre que le domaine est déjà migré.

## Critères d’acceptation

La reconstruction est prouvée lorsque :

- Ansible passe deux fois et le second passage ne change rien ;
- un reboot complet redémarre la plateforme et les applications ;
- `docker compose config --quiet` est vert partout ;
- seuls les ports explicitement autorisés sont exposés ;
- les digests actifs correspondent au manifeste ;
- les releases statiques correspondent à leur digest d’artefact et checksum,
  avec le SHA source attendu dans les annotations OCI ;
- les healthchecks et probes publiques sont verts ;
- Prometheus voit ses cibles et Grafana retrouve ses dashboards depuis Git ;
- une perte volontaire des volumes Prometheus/Grafana est récupérable par
  reprovisionnement ;
- une restauration PostgreSQL isolée a été testée lorsque les sauvegardes sont
  livrées ;
- le compte rendu contient la durée réelle et les interventions manuelles.

Les objectifs RTO et RPO restent à fixer après le chantier sauvegarde. Une
promesse de durée sans répétition chronométrée serait fictive.

## Exercice périodique

Au moins à chaque changement structurel de l’infrastructure :

1. créer un hôte ou une VM jetable ;
2. exécuter bootstrap, convergence et réconciliation depuis zéro ;
3. utiliser des secrets et données de test ;
4. couper puis redémarrer la machine ;
5. mesurer la durée et relever toute étape manuelle non documentée ;
6. détruire uniquement l’hôte jetable après vérification de ses identifiants.

Lorsqu’il existe sur l’offre retenue, un snapshot fournisseur accélère une
récupération mais ne prouve pas que le socle est reconstruisible. L’exercice
depuis une image officielle vierge reste la preuve de référence.
