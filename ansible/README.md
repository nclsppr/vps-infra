# Automatisation de l'hôte

Ce répertoire configure un hôte Ubuntu 26.04 LTS. Il ne contient aucun secret,
aucune adresse de production ni aucun inventaire de production. La convergence
de l'hôte ne démarre ni la plateforme ni une application.

## Préparer le contrôleur

Exécuter ces commandes depuis la racine du dépôt :

```bash
make setup
cp ansible/inventories/production/hosts.example.yml \
  ansible/inventories/production/hosts.yml
cp ansible/inventories/production/group_vars/bootstrap-public.yml.example \
  /private/path/bootstrap-public.yml
```

Les fichiers racine `pyproject.toml` et `uv.lock` définissent les versions de
Python et d'`ansible-core`. Le répertoire Ansible ne possède pas de second
environnement Python.

Le VPS géré exige le paquet Ubuntu `python3-jsonschema`. Le rôle de base
l'installe. Le contrôleur de déploiement l'utilise pour valider le schéma de
release Draft 2020-12. La validation de production s'arrête si ce contrôle de
schéma indépendant est indisponible.

Confirmer l'empreinte de la clé d'hôte SSH dans la console OVHcloud avant de
l'ajouter à `known_hosts`. Conserver la vérification des clés d'hôte active. Ne
pas accepter une empreinte transmise par le même chemin réseau que la connexion
SSH.

## Amorcer et converger

L'amorçage crée uniquement le compte administrateur et sa règle sudo. Il ne
modifie ni OpenSSH ni le pare-feu. Sa première lecture distante contrôle
`/etc/os-release` et refuse une cible non prise en charge avant l'installation
éventuelle de Python :

```bash
make bootstrap \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

`scripts/bootstrap` n'accepte aucun argument en ligne de commande. Le wrapper
valide les entrées externes de l'opérateur avant de contacter GitHub ou le VPS.
Il exécute ensuite `bootstrap.yml` depuis un export isolé du commit exact
`origin/main`. Ne pas exécuter `bootstrap.yml` directement depuis un worktree.

Conserver la session d'amorçage ouverte. Valider une nouvelle connexion SSH
avec `vpsadmin`. Définir ensuite `ansible_user: vpsadmin` dans l'inventaire.

Exécuter la convergence de l'hôte depuis la racine du dépôt :

```bash
make converge \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

Après une convergence réussie, exécuter le contrôle prédictif borné :

```bash
make converge-check \
  ANSIBLE_EXTRA_VARS=/private/path/bootstrap-public.yml
```

`converge-check` fournit toujours à la fois `--check` et `--diff`. Le wrapper
refuse tout autre argument en ligne de commande. Cette restriction empêche une
option Ansible contrôlée par l'opérateur de modifier le périmètre du playbook
capturé ou la politique d'exécution.

Exécuter ce mode uniquement après une première convergence normale réussie. Il
s'agit d'un contrôle prédictif de dérive, pas d'une répétition de l'amorçage.
En mode check, Ansible ignore la création des réseaux Docker fondée sur des
commandes ainsi que certaines assertions finales sur l'état effectif. Un cache
APT expiré peut également signaler un changement prédit sans actualiser le
cache. Le contrat du dépôt autorise explicitement chaque commande qui doit
s'exécuter hors du mode check et exige que chacune soit en lecture seule.

`scripts/bootstrap` et `scripts/converge` récupèrent `main` avec une refspec de
destination explicite. Chaque wrapper capture une seule fois le commit complet
`origin/main`. Il exporte ce commit dans un nouveau répertoire temporaire. Il y
installe les outils et dépendances verrouillés. Il exécute ensuite son playbook
fixe depuis l'arbre exporté. La convergence transmet aussi le commit capturé
comme `vps_infra_revision`.

L'inventaire doit respecter la structure d'inventaire YAML de
`hosts.example.yml`. Il doit définir exactement un hôte dans le groupe `vps` et
aucun autre groupe. L'hôte doit définir `ansible_host`, `ansible_port` et
`ansible_user`. Il peut définir `ansible_connection` à `ssh` ou `smart`. Le
validateur refuse localhost, les adresses de bouclage, les noms en `.invalid`,
les hôtes supplémentaires, les plugins de connexion, les surcharges de
commandes SSH, les chemins d'interpréteur et les variables de politique.
L'amorçage refuse l'utilisateur de connexion `deploy`. La convergence exige
`vpsadmin`.

Le fichier extra-vars doit être un unique objet YAML. Il doit contenir
exactement `vps_admin_authorized_keys` et `vps_deploy_authorized_keys`. Chaque
valeur doit être une liste de clés publiques OpenSSH normalisées. La liste des
administrateurs ne doit pas être vide. La liste de déploiement peut être vide.
Le validateur refuse les clés de fusion YAML, les clés dupliquées, les variables
qui modifient une version, un chemin, une révision ou une politique, ainsi que
toute autre clé. Une même clé cryptographique ne peut pas apparaître dans les
listes administrateur et déploiement, même avec des commentaires différents.
Les clés RSA doivent posséder au moins 2048 bits. Les clés publiques ne sont pas
des secrets, mais conserver ce fichier hors du dépôt en tant qu'état opérateur.

Chaque wrapper valide les fichiers originaux avant de créer un état temporaire.
Il copie les fichiers dans son répertoire temporaire privé, puis valide de
nouveau les copies avant la récupération Git. Ansible lit uniquement ces copies
privées. Après l'export, le wrapper les revalide avec le validateur issu du même
snapshot `origin/main` que le playbook. Cette séquence empêche la modification
d'un fichier d'entrée entre sa validation et son exécution, ainsi qu'un
désalignement silencieux entre validateur et playbook.

Une clé administrateur chiffrée doit déjà être déverrouillée dans l'agent SSH de
l'appelant. Le script résout et valide un `SSH_AUTH_SOCK` appartenant à
l'appelant, puis transmet ce socket uniquement à `ansible-playbook`. Git, mise,
l'installation des dépendances et Ansible Galaxy continuent de s'exécuter sans
accès à l'agent.

Les wrappers résolvent d'abord les outils locaux requis depuis le `PATH` de
l'opérateur. Le poste et sa chaîne d'outils constituent donc une racine de
confiance locale explicite ; ne pas exécuter ces commandes depuis un shell ou
un `PATH` non maîtrisé. Les sous-processus Git et Ansible reçoivent ensuite une
liste réduite de variables d'environnement. Ils ignorent les chemins de plugins
Ansible et les variables de configuration Ansible fournis par l'appelant.
Ainsi, une branche divergente, une modification locale suivie, un rôle non
suivi ou un chemin externe de plugin Ansible ne peut pas modifier l'arbre de
playbooks exécuté. Tout échec d'entrée, de récupération, d'archive, de
dépendance ou de collection arrête l'exécution avant qu'Ansible ne contacte
l'hôte.

`site.yml` refuse les utilisateurs de connexion `root` et `deploy`. Exécuter
une seconde convergence et un contrôle prédictif après la première convergence
réussie. Utiliser la même méthode de capture de révision pour chaque commande.
Ne pas exécuter `site.yml` directement depuis un worktree arbitraire.

## Rotation des clés administrateur

La convergence normale ajoute chaque clé administrateur déclarée. Elle ne
supprime aucune clé existante. Elle signale toute clé non déclarée puis
s'arrête. Ce comportement empêche une connexion de contrôle OpenSSH persistante
de masquer une clé de remplacement inutilisable.

Utiliser cette procédure en deux phases :

1. Conserver la clé validée dans `vps_admin_authorized_keys`.
2. Ajouter la clé de remplacement dans la même variable.
3. Exécuter la convergence.
4. Démarrer un nouveau processus SSH avec la clé privée de remplacement.
   Définir `ControlMaster=no` et `ControlPath=none`. Conserver la vérification
   des clés d'hôte active.
5. Utiliser une opération explicite distincte pour retirer l'ancienne clé.
6. Supprimer la clé retirée de `vps_admin_authorized_keys`.
7. Exécuter de nouveau la convergence. Valider encore une nouvelle connexion
   SSH.

Ne pas remplacer toutes les clés au cours d'une seule opération non vérifiée.
Le rôle de convergence normale n'implémente volontairement pas le retrait de
clés.

Le port SSH initial est 22. Une migration de port doit ouvrir l'ancien et le
nouveau port, valider une connexion au nouveau port, puis fermer l'ancien port.

## État du pare-feu

Une fois qu'UFW a pris le contrôle, chaque convergence vérifie l'ensemble exact
des règles numérotées. Une règle manuelle inconnue, telle qu'une règle publique
`5432/tcp`, arrête le playbook. Le playbook signale la règle. Il ne la supprime
pas. L'opérateur doit examiner puis supprimer explicitement cette règle.

La politique Docker `DOCKER-USER` accepte uniquement les ports publiés
d'origine `80/tcp`, `443/tcp` et `443/udp` sur l'interface publique. Elle
utilise les ports de destination d'origine de conntrack, car Docker évalue cette
chaîne après le DNAT. Une règle d'autorisation exige également l'état DNAT de
conntrack et la direction d'origine du paquet. Elle abandonne le transfert
direct hors DNAT ainsi que tout autre nouveau transfert Docker public.

## Contrôleur de déploiement

Le compte `deploy` est verrouillé et n'est pas membre du groupe `docker`. Il
possède un shell valide, car OpenSSH l'exige. `ForceCommand` transmet chaque clé
à un analyseur qui accepte uniquement `deploy <full-git-sha>`.

Les fichiers du contrôleur sont installés sous `/usr/local/libexec/vps`. Le
marqueur `/etc/vps/production-enabled` et l'exécutable `apply-release` sont
absents. Le contrôleur actuel peut valider et planifier. Il ne peut pas activer
la production.

Le rôle deploy initialise le miroir détenu par root dans
`/srv/vps/repository` depuis l'unique origine publique autorisée :

```text
https://github.com/nclsppr/vps-infra.git
```

Il vérifie que le commit demandé est accessible depuis `origin/main`. Il
vérifie également la propreté du checkout avant d'installer les fichiers du
contrôleur détenus par root. Le fichier
`/usr/local/share/vps-infra/controller-revision` enregistre le commit installé.

## Réseaux Docker préparés

Ansible crée six réseaux Docker externes aux propriétés fixes. La plateforme de
base verrouillée rejoint uniquement `ops` et `db_monitoring`. PostgreSQL rejoint
uniquement `db_monitoring`. Caddy et Prometheus rejoignent uniquement `ops`.

Les quatre réseaux applicatifs restent vides tant qu'un paquet d'intégration
applicative validé n'y rattache pas les services requis. Un réseau existant
avec un pilote, un indicateur internal, un CIDR ou une étiquette de gestion
inattendu arrête la convergence. Ansible ne supprime pas ce réseau.
