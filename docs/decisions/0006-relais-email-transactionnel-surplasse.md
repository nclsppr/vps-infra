# ADR-0006 — Relais email transactionnel managé pour Surplasse

## Statut

Accepté le 13 août 2026 pour l'architecture cible. Aucun fournisseur n'est
sélectionné. Cette décision n'autorise aucun achat de compte, modification DNS,
changement de secret ou activation de production.

## Contexte

Surplasse envoie les liens magiques d'authentification par email. Une remise
SMTP acceptée par le relais ne prouve ni la livraison finale, ni l'alignement
SPF, DKIM et DMARC. Elle ne prouve pas non plus le traitement des rebonds et des
plaintes.

Le Backend est un client SMTP. Son adaptateur Atlas exige un hôte, un port, un
identifiant et un mot de passe. Atlas expose uniquement SSH, HTTP et HTTPS. La
plateforme commune ne contient aucun agent de transfert de courrier (MTA). Le
domaine `surplasse.com` reçoit actuellement ses emails par trois MX OVH. Son SPF
autorise uniquement OVH.

Postfix ou Exim sur Atlas ajouterait une file persistante, un service public,
des exigences de sauvegarde et des exigences de supervision. Cette installation
ne fournirait pas à elle seule la réputation IP, le reverse DNS, la signature
DKIM ou le traitement des rebonds et des plaintes. Elle ferait aussi dépendre
l'email d'authentification de l'unique VPS.

## Options considérées

### MTA public sur Atlas

Rejeté. Cette option dépasse le besoin applicatif. Elle couple le canal
d'authentification à Atlas et à son adresse IP.

### Postfix local comme relais

Rejeté. Quarkus peut joindre directement un relais authentifié. Une file locale
ajouterait un état et un point de panne. Elle n'améliorerait pas la livraison
externe.

### Relais email transactionnel managé

Retenu. Le fournisseur gère la soumission SMTP, la réputation d'envoi et les
événements de livraison. Atlas n'expose aucun port entrant supplémentaire.

## Décision

1. Le Backend se connecte directement à un relais email transactionnel managé
   sur le port `587`. Il exige STARTTLS, la validation normale du certificat et
   la validation du nom d'hôte.
2. L'expéditeur applicatif reste exactement `no-reply@surplasse.com`.
3. Seuls l'identifiant et le mot de passe SMTP sont secrets. L'identité du
   fournisseur, l'hôte du relais, le port et les enregistrements DNS attendus
   forment un contrat public revu. Ce contrat n'existe pas dans ce dépôt. Une
   tranche ultérieure doit l'ajouter et le faire revoir avant l'activation du
   fournisseur.
4. Les MX OVH restent inchangés. Ajouter un mécanisme SPF fournisseur seulement
   lorsque le fournisseur exige un mécanisme exact. Conserver un seul SPF.
   Utiliser uniquement les valeurs de vérification de domaine et DKIM fournies
   par la console du fournisseur. Publier DMARC avec une boîte de rapports
   opérée.
5. Ne pas ajouter de MTA, de port entrant `25`, de volume de file SMTP ou de
   secret fournisseur à la plateforme Atlas.
6. Conserver cinq portes de preuve indépendantes :
   `transactional-email-provider`, `email-domain-authentication`,
   `smtp-atlas-connectivity`, `smtp-effective-runtime-configuration` et
   `email-delivery-observability`.
7. Une preuve GitHub Actions générique ne peut satisfaire aucune porte externe.
   Surplasse reste désactivé jusqu'à ce que des formats de preuve revus lient
   chaque preuve au contrat fournisseur, au digest Backend, à la date et à
   l'identité Atlas.

[Scaleway Transactional Email](https://www.scaleway.com/en/docs/transactional-email/reference-content/smtp-configuration/)
est le candidat préféré pour le pilote. Il accepte la soumission SMTP sur le
port `587` avec STARTTLS. Avant la création du compte, examiner et accepter le
plan, le quota, le DPA, les frais, le support et les conditions de SLA en
vigueur. [Brevo](https://developers.brevo.com/docs/smtp-integration) est
l'alternative lorsque ses webhooks d'événements transactionnels sont requis. Ne
pas ajouter de mécanisme SPF fournisseur lorsque le fournisseur sélectionné ne
l'exige pas. Cet ADR ne sélectionne et ne provisionne aucun fournisseur.

## Limites du préflight

Le dépôt contient `verify-surplasse-smtp-preflight`. Le script exige un contrat
fournisseur qui n'existe pas. Il n'effectue aucune mutation de production.

La validation SPF est une analyse statique bornée. Elle analyse le graphe SPF et
refuse plus de dix termes DNS. Elle suit les enregistrements `include` et
`redirect` pour appliquer cette limite. Ce n'est pas une évaluation RFC SPF
`check_host` complète. Elle ne prouve ni l'autorisation d'une adresse IP d'envoi
ni la livraison d'un message.

Pour une clé DKIM TXT, le validateur accepte une clé publique RSA DER canonique
au format PKCS#1 ou SubjectPublicKeyInfo. La clé RSA doit avoir une taille
minimale de 2048 bits et l'exposant 65537. Le validateur accepte aussi une clé
publique Ed25519 brute de 32 octets. Il autorise uniquement SHA-256 et refuse le
mode de test DKIM `t=y`. Un CNAME DKIM doit mener à une unique clé TXT terminale
valide.

Le validateur refuse les noms DNS réservés. Ses données de domaines à usage
spécial utilisent l'instantané du registre IANA Special-Use Domain Names daté du
2026-05-22. Une modification ultérieure du registre exige une mise à jour revue
du validateur.

## Conséquences

### Positives

- Atlas n'a aucune nouvelle surface entrante ni service mail avec état.
- Un compte de soumission dédié limite le périmètre des secrets.
- Le DNS, le transport TLS, la configuration effective et la livraison finale
  utilisent des preuves distinctes.
- Un changement de fournisseur exige une modification revue du contrat public
  et une rotation bornée des identifiants.

### Négatives

- L'authentification Surplasse dépend d'un service tiers.
- Les opérateurs doivent examiner les limites, les frais, la résidence des
  données, le support et les conditions contractuelles.
- Le Backend mesure actuellement uniquement la remise au relais. Une preuve
  fournisseur et une alerte externe doivent couvrir les rebonds, les plaintes
  et les retards.

## Rollback

Avant une modification DNS autorisée, exporter la zone complète et relever
chaque TTL. En cas d'échec de la validation, garder Surplasse désactivé. Révoquer
l'identifiant SMTP dédié. Restaurer exactement les enregistrements SPF, DKIM et
DMARC précédents. Ne pas modifier les MX OVH. Un changement de fournisseur
exige une nouvelle revue. Ne pas utiliser de fallback SMTP silencieux.

## Vérification

Le [runbook SMTP de Surplasse](../operations/surplasse-smtp.md) définit la
procédure. L'activation exige toutes les preuves suivantes :

- un contrat fournisseur public revu qui ne contient aucun secret ;
- les MX OVH inchangés et un unique SPF exact ;
- les valeurs DKIM et DMARC exactes ;
- une connexion Atlas au FQDN public du relais sur le port `587`, avec STARTTLS,
  la validation de la chaîne de certification et celle du nom d'hôte ;
- l'inspection de l'image Backend par digest et de l'environnement assaini du
  processus lancé, sans surcharge SMTP ou TLS embarquée ;
- des liens magiques reçus sur Gmail, Outlook et OVH, avec des en-têtes
  `Authentication-Results` alignés ;
- un hard bounce observé, une indisponibilité du relais simulée et une alerte
  opérateur reçue.
