# ADR-0002 — Dépôt public sans état sensible

## Statut

Accepté le 30 juillet 2026.

## Contexte

Le dépôt `nclsppr/vps-infra` a été créé avec une visibilité publique. Cette
visibilité rend les validations sur runners GitHub standards gratuites et
supprime le besoin d’un credential Git pour reconstruire l’hôte.

Le dépôt décrit cependant une infrastructure de production. Une publication
accidentelle de clé, d’inventaire réel ou de secret rendrait ces gains sans
objet et imposerait une rotation immédiate.

## Décision

Le dépôt reste public tant que les règles suivantes sont vérifiées :

- aucun inventaire réel, adresse d’administration, nom de compte opérateur ou
  empreinte SSH de production n’est versionné ;
- aucune clé privée, valeur déchiffrée, variable `.env` ou donnée métier n’est
  versionnée ;
- les exemples utilisent uniquement des domaines et adresses réservés à la
  documentation ;
- les secrets de production sont matérialisés depuis un poste de confiance ;
  seuls des fichiers SOPS intégralement chiffrés pourront éventuellement être
  ajoutés après création et sauvegarde de l’identité age ;
- les workflows ont des permissions minimales et toutes les Actions tierces
  sont épinglées par SHA ;
- les modifications sensibles passent par pull request et validation CI.

Le VPS lit le dépôt en HTTPS sans authentification. GHCR et le déclencheur SSH
conservent des identités distinctes et strictement limitées.

## Conséquences

- les builds et validations des dépôts publics n’utilisent pas le quota Actions
  des dépôts privés ;
- la reconstruction a un secret de moins ;
- la topologie générale, les versions et les procédures sont publiques ;
- le contrôle automatisé de fuite de secrets devient une condition de merge ;
- un passage en privé reste possible sans changer l’architecture, mais exige
  alors une deploy key Git en lecture seule sur le VPS.

## Réexamen

Réexaminer cette décision si une contrainte contractuelle impose la
confidentialité de la topologie, si un fichier nécessaire ne peut pas être rendu
public-safe, ou si les règles de facturation GitHub changent matériellement.
