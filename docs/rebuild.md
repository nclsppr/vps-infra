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
| static promotion policy | `releases/static-production.json` in Git |
| last observed static active tuples | dated evidence in Git; verify against current producer HEAD and GHCR before recovery |
| images et artefacts statiques | GHCR, referenced by immutable digest |
| clé SSH administrateur | Termius ou poste de confiance, avec copie de récupération |
| clé age permettant SOPS | gestionnaire de secrets et copie hors ligne |
| jeton GHCR en lecture | gestionnaire de secrets ou procédure de régénération |
| accès Git en lecture à `vps-infra` | HTTPS public ; deploy key seulement si la visibilité devient privée |
| static SSH trigger key | secret of the GitHub `static-production` environment, separately revocable |
| secret paths, permissions, consumers, target generations, generation bindings, and last observed states | `secrets/registry.json` in Git, without values or content-derived digests |
| DNS, JWT, Stripe, SMTP, Grafana, and database secret values | approved external secret store or the declared regeneration procedure |
| sauvegardes métier | étape locale testée sur Atlas ; cible chiffrée hors VPS encore à sélectionner |
| inventaire DNS, MX, SPF, DKIM et DMARC | export versionné sans secrets et copie opérateur |

A value stored only on Atlas is not a recovery method. The registry can rebuild
its path and permissions, but not its bytes. The repository has no SOPS payload,
no `.sops.yaml` policy, and no proved age recovery identity. SOPS recovery is
therefore blocked. If a later reviewed change adds SOPS, the private age key
must remain outside Git and Atlas with a tested recovery copy.

The baseline read-only audit on 23 August 2026 found only the four platform
secrets and two Surplasse database passwords. All six were materialized, but
their generation is `0` and their binding is `unlinked`. The baseline audit
found no generation marker. No entry had runtime-loaded evidence. All Scaleway
Transactional Email entries were absent. Registry value recovery is
`not-configured`.

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

# Static edge, before and after the reviewed DNS cutover
make prepare-public-static-edge ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
make activate-public-static-edge ANSIBLE_EXTRA_VARS=/chemin/prive/bootstrap-public.yml
```

Run `converge-check` only after a successful normal convergence. It predicts
drift from the same isolated `origin/main` snapshot and rejects every
command-line option except the exact `--check --diff` pair.

The generic `scripts/deploy <sha40>` controller validates and plans desired
state but remains dry-run. Static activation uses the separate operational
controller documented in
[`operations/static-release-reconciliation.md`](operations/static-release-reconciliation.md).
The 2026-08-18 rollout converged the Compose application controller and root
gate from revision `da04a09bfa9788ae8127b63f9f3a6692bef2551b`. Canonical
Surplasse admission is now enabled, Parkventory remains disabled, and no
application workflow or active release follows from admission alone.

## Phase 0 — préparer sans toucher au trafic

Avant la destruction ou la création :

1. vérifier que `vps-infra/main` et la release à restaurer sont accessibles ;
2. vérifier que tous les digests du manifeste existent dans GHCR ;
3. compare every required registry entry with its external recovery source or
   declared regeneration procedure; do not assume that SOPS is available;
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
9. materializes only a secret with an implemented and authorized input path; it
   does not decrypt SOPS;
10. configure la clé GHCR en lecture seule ;
11. configure l’origine HTTPS publique de `vps-infra`, ou une deploy key si sa
    visibilité a changé ;
12. installe la clé publique du déclencheur GitHub sur le compte de livraison,
    avec commande forcée ;
13. lance les validations hôte.

The current tranche delivers points 1 to 8 and 11 to 13. It uses the public
HTTPS repository and installs a trigger key only when the operator supplies
one. GitHub CLI 2.97.0 is pinned by release archive and executable checksums.
Personal, Papers Empire, the Parkventory static demo, and platform integration
are public GHCR packages. Their materializer uses anonymous bounded registry
downloads in short-lived systemd `DynamicUser` executions. One execution fetches each attestation bundle
set. A separate network execution uses the pinned GitHub CLI and its embedded
TUF bootstrap roots to obtain one current trusted root per deployment. The root
orchestrator also requires its versioned SHA-256. A trust-root rotation fails
closed until a reviewed repository update changes that digest. The orchestrator
validates and copies these inputs, then removes each fetch state. A
new sequential offline execution of the same fixed transient unit gives the
local bundle, copied trusted root, and digest-bound OCI manifest to GitHub CLI.
The fixed unit name prevents concurrent worker creation. No execution receives
an operator token. ORAS remains in the locked local and
CI toolchain; reconstruction does not install it on Atlas.

Host convergence installs one operator tool outside that CI toolchain: the
standalone Codex CLI package. It uses no Node.js or npm and runs only as the
isolated `codex` account through the enforced bounded launcher. On the proved
2026-08-18 Atlas convergence, `atlas-codex-app-server.service` was active and
running on its managed private Unix socket. An optional unprivileged SSH gateway
requires an external public key. Direct mobile control instead uses the App
Server's outbound ChatGPT relay and opens no inbound port. Persistent state is
capped by a dedicated 6 GiB filesystem. Authentication and mobile pairing are
not part of reconstruction. After convergence, an operator may restore access
with the ChatGPT-only device flow and the manual pairing procedure documented in
[`operations/codex-cli.md`](operations/codex-cli.md).

The input-free static reconciliation workflow resolves the exact application
source revision, site digest, route digest, platform integration revision,
integration digest, and Caddy image digest. An operator does not supply or
override these values. The integration package and the protected
infrastructure mirror must name the same Caddy image. An attestation proves the
exact source ref and workflow. It does not prove branch protection. Restore the
repository rulesets as a separate reconstruction step before production
activation.

Normal host convergence creates the private secret root. It copies the Mon
Florian OpenAI key only when the operator supplies an explicit private source.
It has no general secret restore step. The internal platform and Surplasse
preparation playbooks separately generated the six values in the baseline
audit. A GHCR credential for private application images, recovered operator
secrets, the live applicator, and their validations remain explicit gates.
`site.yml` does not activate a platform service or an application.

Le compte de livraison n’entre pas dans le groupe `docker`. Une règle restreinte
autorise seulement le wrapper root-owned, par chemin absolu et sans `SETENV`.
Son compte possède un shell système valide pour que la commande forcée
fonctionne, mais aucune session interactive n’est autorisée. L’administrateur
peut utiliser sudo, mais ses actions manuelles ne remplacent jamais Ansible dans
l’état canonique.

## Phase 3 — démarrer la plateforme

Before a service starts, compare its required entries with the registry. Create
or recover each value through its declared materializer. Commit the target
generation before the operation. The materializer must publish a non-secret
generation marker last with the exact file set. Run a read-only audit of the
file metadata and marker. Mark the file materialized after the metadata audit.
Advance the generation only after the marker audit. Do not mark it
runtime-loaded until the current service has loaded that marker-bound
generation and passed its probes. A Docker file bind mount can retain the old
inode after an atomic host-file replacement, so a rotation must recreate the
affected service. The two Parkventory materializers complete this generation
step for their exact registered sets. The Mon Florian helper completes it for
its two closed identifiers. Its initial adoption path requires a separate
read-only preflight and does not replace an existing file. Other materializers
remain unlinked.

La plateforme est restaurée avant les applications :

1. Caddy en mode HTTP-only, avec sa configuration validée mais sans basculer le DNS ;
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

Le contrôleur local crée des dumps logiques et répète leur restauration dans
un conteneur jetable. La copie hors site reste à sélectionner. Une
reconstruction réelle doit conserver cet ordre :

1. arrêter ou ne pas démarrer les backends qui pourraient migrer ;
2. restaurer chaque base dans le cluster cible ;
3. vérifier propriétaire, extensions et historique Flyway ;
4. exécuter des contrôles de cohérence propres au produit ;
5. seulement ensuite autoriser les migrations vers la release active.

Personal, Papers Empire, and the Parkventory demo have no server state. Browser
`localStorage` remains on the client while its origin and schema stay stable.

## Phase 5 - reconcile releases

For static recovery:

1. converge the reviewed static controller revision and public edge;
2. install the dedicated deploy public key and independently verified host key;
3. configure the `static-production` environment while deployment is suspended;
4. confirm that the current canonical producer heads have only complete,
   non-failing observed checks, successful configured required checks, and
   immutable artifacts;
5. enable reconciliation and dispatch the input-free workflow;
6. require all three deploy jobs, protected active tuples, empty transactions,
   and strict public probes.

Do not manually request the old tuple recorded in dated evidence. Normal
reconstruction selects the current eligible canonical heads. If the current
producer content must be reverted, merge a new descendant revert first.

For a future Compose application, prepare the exact edge route and application
network before migration. Do not run a migration until every ADR-0010 blocker,
including post-migration compatibility or explicit forward-recovery behavior,
is enforced. Current reconstruction leaves Surplasse and dynamic Parkventory
disabled.

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

For Personal, Papers Empire, and the Parkventory demo, the first deployment
uses HTTP-01 without an OVH credential. The preparation phase serves explicit
HTTP routes. The cutover then points the apex and `www` A records to Atlas and
removes all previous AAAA records before the HTTPS activation phase can start.
Strict public TLS probes run during that controlled cutover, with the captured
DNS state available for rollback.

## Phase 7 — basculer et observer

1. réduire les TTL avant la fenêtre lorsque c’est possible ;
2. basculer les A ou les nameservers décidés, supprimer explicitement les
   anciens AAAA, puis ne publier un AAAA Atlas qu’après preuve explicite du
   pare-feu, des binds et des probes IPv6 ;
3. vérifier chaque zone auprès des serveurs autoritatifs ;
4. exécuter les probes depuis au moins un réseau externe ;
5. tester l’envoi et la réception mail pour les zones concernées ;
6. observer erreurs, saturation, certificats et métriques ;
7. conserver l’ancien hébergement disponible pendant la fenêtre de retour.

GitHub Pages can keep serving each current static surface until this step. The
VPS workflows can be validated in parallel without claiming that a domain has
already moved.

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
- the registry matches a fresh read-only Atlas file and generation-marker
  audit, and each active consumer has runtime-loaded evidence for its
  marker-bound generation;
- every `restore-from-external-store` entry has a tested recovery source, and no
  step claims SOPS recovery while the age gate remains open;
- registry `value_recovery_state` is `verified`;
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
6. compare the resulting secret metadata with the registry and record any new
   test generation outside the production registry;
7. détruire uniquement l’hôte jetable après vérification de ses identifiants.

Lorsqu’il existe sur l’offre retenue, un snapshot fournisseur accélère une
récupération mais ne prouve pas que le socle est reconstruisible. L’exercice
depuis une image officielle vierge reste la preuve de référence.
