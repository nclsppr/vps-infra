# Préparer l’accès OVHcloud

This document records the verified OVHcloud state and the external information
that the remaining rollout still requires. Do not add a real credential or
secret value to this public repository.

## Accès initial au VPS

À fournir par un canal privé :

- l’offre et la région OVHcloud exactes ;
- l’image Ubuntu 26.04 LTS (`resolute`) réellement installée ;
- l’IPv4 et, si activée, l’IPv6 ;
- le nom du compte initial fourni par l’image ;
- le port SSH initial ; l’automatisation actuelle exige `22` et refusera
  d’agir si l’image OVHcloud utilise autre chose, jusqu’à livraison d’une
  migration explicite à deux connexions ;
- l’empreinte de clé hôte observée depuis la console OVHcloud ;
- la confirmation qu’une clé publique administrateur a été injectée ;
- le chemin d’accès à la console KVM ou au mode rescue.

La clé SSH **privée** ne doit jamais être copiée dans Git, une issue ou un log.
Le bootstrap utilise la clé déjà détenue par l’opérateur.

## Verified Atlas host layer

The operator completed the host procedure and the bounded static rollout on
2026-08-12. Atlas runs Ubuntu 26.04 LTS on `amd64`. It has six CPUs and more
than 11 GiB of memory. Bootstrap, fresh administrator connections, SSH
hardening, the exact UFW policy, Docker, repeated convergence, predictive check
mode, and a complete reboot passed. Direct root SSH fails.

The Caddy static edge and the private PostgreSQL, Prometheus, Grafana, Node
Exporter, and PostgreSQL Exporter platform are active. The platform uses
file-based secrets outside Git. No dynamic application container, application
secret set, production marker, or live release applicator is present.

The installed repository mirror records the exact approved `main` revision. A
normal convergence and the bounded `--check --diff` mode both reported
`changed=0` after HTTPS activation.

The first public release supports IPv4 only. Caddy binds to `0.0.0.0`. The
managed `DOCKER-USER` chain matches each original published port after Docker
DNAT and drops every other new public forwarding path. Do not point an `AAAA`
record to the VPS until a reviewed change defines the equivalent IPv6 policy
and verifies external IPv4 and IPv6 probes.

## API OVHcloud : trois identités, jamais un jeton global

L’accès à venir ne doit pas être transformé en un secret omnipotent. Les droits
sont séparés par finalité.

### 1. Inventaire opérateur en lecture seule

Une identité temporaire permet de lire le type de service, la région, l’image,
les IP, les reverse DNS, l’état du VPS et les zones DNS. Elle n’autorise ni
reboot, ni rescue, ni réinstallation, ni snapshot, ni commande, ni facturation,
ni suppression de service. Cette première passe sert à produire un plan fondé
sur l’état réel.

OVHcloud IAM fonctionne par identités, ressources et actions avec refus par
défaut. Lorsque le client choisi le permet, préférer un compte de service OAuth2
à un credential lié au compte humain, borner les ressources aux seuls VPS et
zones concernés et ajouter une expiration.

### 2. Identité ACME permanente de Caddy pour les routes DNS-01

The first Personal, Papers Empire, and Parkventory demo edge uses HTTP-01 and
receives no OVH credential. The following identity is reserved for a later
route that genuinely requires DNS-01, such as a Surplasse wildcard certificate.

Le module `caddy-dns/ovh` retenu consomme alors un endpoint, une application
key, une application secret et une consumer key. Ces trois valeurs sont créées
uniquement pour Caddy et limitées à chaque zone qui doit émettre un certificat
DNS-01.

Le jeu de routes à confirmer par observation sur une zone de test est borné aux
opérations nécessaires à libdns :

```text
GET     /domain/zone
GET     /domain/zone/<zone>
GET     /domain/zone/<zone>/record
POST    /domain/zone/<zone>/record
GET     /domain/zone/<zone>/record/*
DELETE  /domain/zone/<zone>/record/*
POST    /domain/zone/<zone>/refresh
```

Il n’accorde aucun droit VPS, aucun `PUT` A/AAAA et aucun accès aux autres
produits du compte. Si OVHcloud ne permet pas de limiter suffisamment les
records, la prochaine amélioration sera de déléguer les challenges ACME vers
une zone technique dédiée plutôt que d’élargir le credential.

### 3. Identité de bascule DNS temporaire

La modification contrôlée des A utilise une autre identité, créée pour la
fenêtre de migration puis révoquée. Pour la première bascule IPv4, elle supprime
également les anciens AAAA qui pointent vers GitHub Pages. Un nouvel AAAA Atlas
reste interdit jusqu’à la livraison de la porte IPv6 ci-dessus. L’identité lit
l’export, modifie uniquement les records explicitement approuvés et rafraîchit
la zone. Caddy ne reçoit jamais ce credential.

Toutes les valeurs sont matérialisées hors Git. Il faudra confirmer l’endpoint
OVHcloud (`ovh-eu` ou autre), les zones réellement hébergées chez OVHcloud et
les routes observées avant de finaliser chaque politique.

Any API credential copied into a chat is compromised. Revoke it immediately.
Do not reuse it for Caddy, deployment automation, or a later DNS change. Create
a new short-lived and zone-scoped identity when another mutation is required.

The ready Surplasse implementation is specified in
[`surplasse-dns-cutover.md`](surplasse-dns-cutover.md) and
[ADR-0012](../decisions/0012-locked-surplasse-dns-cutover-controller.md), as
superseded by
[ADR-0015](../decisions/0015-surplasse-tester-dns-cutover-policy.md). It uses a
dedicated credential directory. Installing the ready policy does not call
OVHcloud. Only an explicit controller command can open that directory.

## DNS à exporter avant toute mutation

Pour chaque zone :

- A, AAAA, CNAME et wildcard ;
- MX, SPF, DKIM et DMARC ;
- CAA ;
- TTL actuels ;
- délégation et serveurs autoritatifs ;
- redirections historiques, notamment `pieper.fr`, `www.pieper.fr`,
  `nicolas.pieper.fr` et `www.nicolas.pieper.fr` ;
- origine canonique exacte `https://papersempire.com`.
- Parkventory apex origin `https://parkventory.com` and its `www` redirect.

L’automatisation comparera cet export à l’état API. Elle ne réécrira jamais les
enregistrements mail implicitement.

Before the 2026-08-12 cutover, the operator captured the complete visible zone
tables outside Git. After the cutover, every OVH authoritative name server and
the `1.1.1.1` and `8.8.8.8` resolvers returned the Atlas IPv4 address for the
apex and `www` names of `nicolaspieper.com`, `papersempire.com`, and
`parkventory.com`. These names have no AAAA answer. The existing mail records
remain unchanged. `surplasse.com` still points to its previous host.

This is historical evidence. On 2026-08-24, Papers Empire moved its
authoritative zone and web delivery to Cloudflare; it is no longer part of the
Atlas DNS or public-edge mutation set. Its OVH mail records were preserved in
the Cloudflare zone.

The captured tables support rollback, but they do not replace a reusable API
export of A, AAAA, CNAME, MX, SPF, DKIM, DMARC, CAA, wildcard, redirect, and TTL
state.

The `pieper.fr` web cutover changes only the A and AAAA records for the apex,
`www`, and `nicolas` names. Each A answer must contain only the
Atlas IPv4 address and each AAAA answer must be empty before HTTPS activation.
Preserve the zone delegation, DNSSEC, MX, TXT, DKIM, DMARC, CAA, and every other
record. Do not replace the apex with a CNAME and do not change mail records as
part of the web cutover.

Treat TTL preparation as a separate web-only change. Lower the TTL for those
three names without changing a target, confirm the lower value at every
authoritative server, and then wait at least the previously published TTL before
installing the pre-cutover edge state or changing A and AAAA answers. Keep both
the previous targets and TTLs in the out-of-Git export. A rollback restores only
those exact web records; it never synthesizes a new zone or rewrites protected
mail and DNSSEC state. Before declaring success, compare the exact MX, TXT, NS,
DS, and DNSKEY answers with that export and complete an iCloud send and receive
test. The web edge emits no HSTS on these aliases, which keeps DNS rollback
available during the propagation window.

## Activation GitHub

Après le premier bootstrap réussi, créer dans l’environnement GitHub
`production` :

- `VPS_HOST` ;
- `VPS_USER` pour le compte à commande forcée ;
- `VPS_SSH_KEY` limitée à ce compte ;
- `VPS_KNOWN_HOSTS`, obtenue et vérifiée hors workflow.

La variable `VPS_DEPLOY_ENABLED` reste absente ou différente de `true` jusqu’à
ce que le bootstrap, un second passage Ansible et l’exercice de rollback soient
verts.

Le dépôt et ses workflows sont publics. Aucun secret d’environnement GitHub ne
doit être disponible aux workflows de pull requests provenant de forks.

## Première session

Lors de la remise des accès :

1. vérifier l’empreinte SSH via deux canaux ;
2. confirmer en lecture seule `VERSION_ID=26.04` et
   `VERSION_CODENAME=resolute`, puis collecter disques, mémoire, CPU, réseau et
   règles de pare-feu OVHcloud ;
3. lancer `bootstrap.yml` en gardant la session initiale ouverte ;
4. prouver une seconde connexion avant de durcir SSH ;
5. exécuter `site.yml` deux fois et exiger zéro changement au second passage ;
6. run the bounded `make converge-check` mode and review every diff ;
7. garder `VPS_DEPLOY_ENABLED=false` tant que les probes ne sont pas validées.

Check mode is not a bootstrap simulation. It runs only after successful normal
convergence because command-based resource creation and some effective-state
assertions cannot be predicted completely.
