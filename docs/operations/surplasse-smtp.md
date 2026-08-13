# Préparer le relais SMTP de Surplasse

Ce runbook prépare le canal email de Surplasse sans MTA sur Atlas. Il n'autorise
aucun achat de compte, modification DNS, création de secret, exécution distante
ou activation de production.

[L'ADR-0006](../decisions/0006-relais-email-transactionnel-surplasse.md)
définit l'architecture.
[L'adaptateur Surplasse verrouillé](../../applications/surplasse/README.md)
définit l'état applicatif actuel.

## État observé le 13 août 2026

Le DNS public a renvoyé les enregistrements suivants :

```text
surplasse.com. MX 1   mx1.mail.ovh.net.
surplasse.com. MX 5   mx2.mail.ovh.net.
surplasse.com. MX 100 mx3.mail.ovh.net.
surplasse.com. TXT    v=spf1 include:mx.ovh.com -all
```

Aucun DMARC public n'a été observé. Une requête sur des sélecteurs DKIM
usuels ne prouve pas qu'aucun sélecteur DKIM n'existe. Exporter la zone depuis
OVH avant toute conclusion ou modification.

## Autorité et entrées requises

L'opérateur doit fournir les entrées suivantes hors de Git :

- le choix explicite du fournisseur ;
- l'acceptation du DPA, des limites, du support et des frais en vigueur ;
- un compte ou projet fournisseur dédié à Surplasse ;
- l'autorisation de vérifier le domaine d'envoi et de modifier SPF, DKIM et
  DMARC pour `surplasse.com` ;
- une boîte de rapports DMARC opérée ;
- des identifiants SMTP dédiés transmis par le canal privé approuvé ;
- une cible Atlas exacte et une autorisation distincte avant toute exécution
  distante.

Scaleway Transactional Email est le candidat préféré pour le pilote. Brevo est
l'alternative lorsque les webhooks de livraison, de rebond et de plainte sont
requis. Confirmer les conditions du service au moment de la sélection.

## Limite actuelle du dépôt

`applications/surplasse/smtp-provider.json` n'existe pas. Le dépôt ne lie, ne
copie et ne valide aucun contrat fournisseur dans la CI. Le script actuel et
les exemples ci-dessous ne peuvent satisfaire aucune porte de release sans ce
contrat revu et un format de preuve.

Ne pas exécuter maintenant les sections DNS ou Atlas. Une tranche ultérieure
doit ajouter le contrat public et définir sa liaison avec la release immuable.

## Définir le futur contrat public

Après la sélection du fournisseur, ajouter
`applications/surplasse/smtp-provider.json` sur une branche dédiée. Ne mettre
aucun identifiant, mot de passe, jeton ou destinataire de test dans ce fichier.
Le futur contrat doit lier la release au relais et aux valeurs DNS exactes de la
console fournisseur.

Le contrat a la forme suivante. Les champs entre chevrons exigent des valeurs
revues. L'exemple n'est volontairement pas un JSON exécutable.

```json
{
  "schema": 1,
  "provider": "<lowercase-provider-id>",
  "domain": "surplasse.com",
  "from": "no-reply@surplasse.com",
  "smtp": {
    "host": "<exact-public-fqdn>",
    "port": 587,
    "security": "starttls-required"
  },
  "dns": {
    "mx": [
      {"priority": 1, "target": "mx1.mail.ovh.net."},
      {"priority": 5, "target": "mx2.mail.ovh.net."},
      {"priority": 100, "target": "mx3.mail.ovh.net."}
    ],
    "provider_spf_mechanism": null,
    "spf": {
      "name": "surplasse.com.",
      "value": "v=spf1 include:mx.ovh.com -all"
    },
    "domain_verification": [
      {
        "name": "surplasse.com.",
        "type": "TXT",
        "value": "<exact-domain-verification-value>"
      }
    ],
    "dkim": [
      {
        "name": "<selector>._domainkey.surplasse.com.",
        "type": "TXT",
        "value": "<exact-dkim-value>"
      }
    ],
    "dmarc": {
      "name": "_dmarc.surplasse.com.",
      "value": "v=DMARC1; p=none; rua=mailto:<operated-mailbox>"
    }
  }
}
```

Si le fournisseur exige un mécanisme SPF, remplacer `null` par son mécanisme
`include:<fqdn>` exact. L'insérer une seule fois avant `-all` dans `spf.value`.
Ne pas inventer de mécanisme `include:`. Brevo indique que SPF et MX ne sont pas
requis pour son authentification standard du domaine. Un fournisseur peut
exiger un CNAME pour la vérification du domaine ou DKIM. Dans ce cas, utiliser
`"type": "CNAME"` et un FQDN absolu terminé par un point comme valeur.

Après la création du contrat, valider sa structure sans accès réseau :

```bash
mise exec -- ./scripts/verify-surplasse-smtp-preflight \
  --contract applications/surplasse/smtp-provider.json \
  --validate-only
```

Cette commande est un diagnostic explicite. Elle n'établit pas que la CI a
validé ou authentifié le contrat.

## Revoir le DNS avant une modification

1. Exporter les enregistrements A, AAAA, CNAME, MX, TXT, CAA, wildcard, les
   redirections et les TTL de la zone complète. Conserver l'export daté et son
   identité hors de Git.
2. Comparer les valeurs SPF et DKIM de la console fournisseur au contrat revu.
3. Modifier le SPF existant uniquement lorsque la documentation ou la console
   du fournisseur fournit un mécanisme exact. Ne jamais publier deux TXT qui
   commencent par `v=spf1`.
4. Ajouter les valeurs exactes de vérification du domaine et DKIM. Ne pas
   déduire un sélecteur.
5. Commencer DMARC avec `p=none` et une boîte de rapports opérée. Un passage à
   `quarantine` ou `reject` exige la revue des rapports et de tous les
   expéditeurs légitimes.
6. Ne modifier aucun MX.

Utiliser la procédure du fournisseur sélectionné :
[Scaleway](https://www.scaleway.com/en/docs/transactional-email/how-to/configure-domain-with-transactional-email/)
ou [Brevo](https://help.brevo.com/hc/en-us/articles/12163873383186-Authenticate-your-domain-with-Brevo-Brevo-code-DKIM-DMARC).

Après une modification DNS autorisée et sa propagation, exécuter un diagnostic
DNS explicite depuis le checkout revu :

```bash
mise exec -- ./scripts/verify-surplasse-smtp-preflight \
  --contract applications/surplasse/smtp-provider.json \
  --dns-only
```

## Comprendre les contrôles du préflight

Le validateur effectue les contrôles suivants :

- Il refuse les noms et adresses de relais réservés, locaux ou non publics. Sa
  liste de domaines à usage spécial utilise l'instantané du registre IANA
  Special-Use Domain Names daté du 2026-05-22.
- Il exige exactement les trois MX OVH revus.
- Il exige un unique SPF public exactement égal au contrat. Il effectue une
  analyse statique du graphe et autorise au plus dix termes DNS. Il suit
  `include` et `redirect` pour appliquer cette limite. Il refuse les cycles,
  les macros, un `+all` ouvert et les réseaux IP invalides ou non publics.
- Il n'effectue pas une évaluation RFC SPF `check_host` complète. Ce diagnostic
  n'évalue aucune adresse IP d'expéditeur candidate. Il ne prouve ni
  l'autorisation des adresses IP du fournisseur ni la livraison d'un message.
- Il exige les enregistrements de vérification du domaine, DKIM et DMARC exacts.
  Une clé DKIM TXT RSA doit utiliser un encodage DER canonique PKCS#1 ou
  SubjectPublicKeyInfo, avoir au moins 2048 bits et utiliser l'exposant 65537.
  Une clé Ed25519 doit contenir exactement 32 octets bruts. Le validateur
  autorise uniquement SHA-256 et refuse le mode de test DKIM `t=y`. Un CNAME
  DKIM doit mener à une unique clé TXT terminale valide.
- Il se connecte au port `587`, exige STARTTLS, valide la chaîne du certificat
  et le nom d'hôte, envoie un second `EHLO` après TLS et exige au moins un
  mécanisme SMTP AUTH compatible avec le Backend : `PLAIN` ou `LOGIN`.
- Il ne lit aucun identifiant et n'envoie aucun message.

Un préflight réussi prouve uniquement les données DNS visibles par l'exécuteur
et un chemin TLS réussi. Il ne prouve ni SMTP AUTH ni la livraison finale. Il
réussit lorsqu'une adresse publique parmi un ensemble borné termine le
handshake. Il ne prouve pas la santé de tout le pool d'adresses du fournisseur.

Le diagnostic ne produit aucune preuve immuable. Il ne lie pas sa sortie au
digest du contrat, à la révision de la release, au digest Backend ou à l'identité
Atlas. Il ne peut pas satisfaire `smtp-atlas-connectivity`. Un futur format de
preuve doit inclure au minimum le digest du contrat, la date, les TTL observés,
l'identité de l'exécuteur, les adresses résolues et les paramètres TLS. Il ne
doit contenir aucun secret.

## Exécuter un futur diagnostic Atlas autorisé

Ne pas exécuter cette section tant que le contrat fournisseur public n'existe
pas, que la release ne le lie pas, que la cible Atlas exacte n'est pas résolue
et que l'exécution distante n'est pas explicitement autorisée.

Utiliser `/usr/bin/python3` et le validateur de la release immuable exacte. Ne
pas utiliser `make`, `mise`, un checkout mutable ou `/usr/local/libexec` sur
Atlas. Remplacer les deux composants de chemin par des valeurs exactes revues :

```bash
sudo /usr/bin/python3 \
  /srv/vps/releases/surplasse/REMPLACER_PAR_REVISION_VPS_INFRA_EXACTE/scripts/verify-surplasse-smtp-preflight \
  --contract /REMPLACER_PAR_CHEMIN_CONTRAT_REVU/smtp-provider.json
```

Le contrat n'est actuellement copié dans aucune release immuable. La tranche
qui sélectionnera le fournisseur doit définir son chemin canonique dans la
release avant que cette commande puisse devenir une commande opérateur
exécutable.

## Matérialiser les identifiants

Créer un identifiant de soumission dédié avec les droits minimaux. Transmettre
l'identifiant et le mot de passe par le canal privé du
[contrat d'entrée de l'adaptateur](../../applications/surplasse/README.md#operator-input-contract).
Ne placer aucune de ces valeurs dans le contrat public, une commande, un log,
une issue ou une pull request. Révoquer toute valeur exposée avant son usage.

L'entrée du matérialiseur doit utiliser le port `587`. Son FQDN doit être égal à
celui du futur contrat revu. L'adaptateur rendu exige
`QUARKUS_MAILER_AUTH_METHODS=PLAIN LOGIN`. Il refuse les autres surcharges
`SMTP_*`, `QUARKUS_MAILER_*`, `QUARKUS_TLS_*` et Java déclarées qui pourraient
désactiver STARTTLS, la validation du certificat ou le vrai envoi. Ce contrôle
ne détecte pas une valeur contenue dans l'image ou exportée par l'entrypoint.

Avant de satisfaire la porte `smtp-effective-runtime-configuration`, inspecter
l'image Backend exacte par digest. Inspecter son environnement, ses fichiers de
configuration et son entrypoint. Lancer le Backend candidat dans un
environnement isolé. Collecter une preuve assainie de son environnement
effectif. La preuve doit confirmer le FQDN du relais, le port et le mode TLS sans
contenir d'identifiant ni de mot de passe.

## Prouver la livraison et l'observabilité

Avant de satisfaire la porte `email-delivery-observability` :

1. Envoyer de vrais liens magiques vers des boîtes de test Gmail, Outlook et
   OVH contrôlées.
2. Relever le délai de livraison. Confirmer l'alignement SPF, DKIM et DMARC pour
   `surplasse.com` dans chaque en-tête `Authentication-Results` reçu.
3. Provoquer un hard bounce vers une adresse de test approuvée par le
   fournisseur. Confirmer l'événement fournisseur.
4. Simuler un refus ou une indisponibilité du relais. Confirmer le comportement
   du Backend.
5. Déclencher l'alerte de livraison. Confirmer sa réception par un opérateur.
6. Conserver uniquement une preuve datée et non sensible. Ne pas conserver de
   lien magique, de corps de message ou d'adresse personnelle du destinataire.

Une remise SMTP acceptée par le Backend n'est pas une preuve de réception.
Authentifier chaque webhook de livraison. Traiter son entrée de manière
idempotente et bornée avant son intégration.
[Brevo documente ses événements transactionnels](https://developers.brevo.com/docs/transactional-webhooks).
Examiner séparément le mécanisme du fournisseur sélectionné.

## Rotation et rollback

La rotation pour une même release n'est pas implémentée. Ne pas recréer le
Backend manuellement. La procédure cible doit créer le nouvel identifiant, le
valider sans log, recréer uniquement le Backend avec une liaison explicite à la
génération, exécuter ses probes et révoquer l'ancien identifiant. Elle doit
restaurer la génération précédente après un échec. Un renommage atomique de
fichier sur l'hôte ne met pas à jour un bind mount Docker déjà ouvert.

En cas d'échec de la validation :

1. Garder ou remettre l'adaptateur dans son état verrouillé.
2. Arrêter les nouvelles tentatives de lien magique si elles peuvent créer une
   fausse confirmation utilisateur.
3. Révoquer l'identifiant défaillant.
4. Restaurer exactement les enregistrements SPF, DKIM et DMARC précédents
   depuis l'export.
5. Confirmer que les trois MX OVH n'ont pas changé.
6. Répéter les preuves DNS, STARTTLS et de livraison avant toute activation.
