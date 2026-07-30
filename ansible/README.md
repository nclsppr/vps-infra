# Automatisation de l'hôte

Cette tranche prépare un Ubuntu 24.04 LTS. Elle ne contient aucun secret, ne
connaît aucune IP réelle et ne démarre ni plateforme ni application.

## Préparer le contrôleur

Depuis la racine du dépôt, installer l'unique environnement Python verrouillé,
puis la collection Ansible dans le cache local déclaré par `ansible.cfg` :

```bash
make setup
mise exec -- ansible-galaxy collection install \
  --requirements-file ansible/collections/requirements.yml \
  --collections-path .cache/ansible/collections
cd ansible
cp inventories/production/hosts.example.yml inventories/production/hosts.yml
cp inventories/production/group_vars/bootstrap-public.yml.example \
  /chemin/prive/bootstrap-public.yml
```

`pyproject.toml` et `uv.lock` à la racine sont l'unique contrat de version pour
Python et `ansible-core` ; aucun environnement Ansible secondaire n'est créé
dans ce sous-répertoire.

Sur le VPS, le contrôleur reste dépendance-légère mais exige le paquet Ubuntu
`python3-jsonschema` pour valider le schéma Draft 2020-12 en plus de sa politique
Python sans framework. Il est installé par le rôle `base` ; le contrôleur refuse
la production si cette validation indépendante manque.

Renseigner l'adresse réelle et vérifier manuellement l'empreinte SSH obtenue
via OVHcloud ou la console avant de l'ajouter à `known_hosts`. Ne jamais
désactiver `host_key_checking` et ne pas accepter automatiquement une empreinte
obtenue sur le même chemin réseau que la connexion.

## Exécuter en deux temps

Le bootstrap crée seulement le compte administrateur et sa règle sudo. Il ne
touche ni à sshd ni au pare-feu :

```bash
ansible-playbook playbooks/bootstrap.yml \
  --extra-vars @/chemin/prive/bootstrap-public.yml
```

Conserver cette session ouverte, vérifier une nouvelle connexion SSH avec
`vpsadmin`, puis mettre `ansible_user: vpsadmin` dans l’inventaire. La
convergence exige le SHA complet d’un commit déjà présent sur `origin/main` :

```bash
git fetch origin main
# Noter le SHA complet retourné par : git rev-parse origin/main
ansible-playbook playbooks/site.yml \
  --extra-vars @/chemin/prive/bootstrap-public.yml \
  --extra-vars vps_infra_revision=<sha-complet-de-origin-main>
```

`site.yml` refuse de s'exécuter avec `root` ou `deploy`. Vérifier ensuite un
second passage sans changement et le mode prédictif :

```bash
ansible-playbook playbooks/site.yml \
  --extra-vars @/chemin/prive/bootstrap-public.yml \
  --extra-vars vps_infra_revision=<sha-complet-de-origin-main>
ansible-playbook playbooks/site.yml --check --diff \
  --extra-vars @/chemin/prive/bootstrap-public.yml \
  --extra-vars vps_infra_revision=<sha-complet-de-origin-main>
```

Cette première tranche fixe SSH sur le port 22. Changer ce port dans une seule
convergence serait trompeur et risquerait le verrouillage ; une future migration
devra ouvrir l'ancien et le nouveau port, prouver une seconde connexion, puis
fermer l'ancien.

Après la première prise de contrôle UFW, chaque convergence vérifie l’ensemble
exact des règles numérotées. Une règle manuelle ou héritée, par exemple un
`5432/tcp`, fait échouer le playbook ; elle est affichée mais jamais supprimée
implicitement. L’opérateur doit l’examiner et la retirer explicitement avant de
relancer la convergence.

Le compte `deploy` est verrouillé, absent du groupe `docker` et possède un
shell valide uniquement pour satisfaire OpenSSH. `ForceCommand` redirige toute
clé vers un filtre qui accepte exclusivement `deploy <sha-git-complet>`. Le
contrôleur est installé sous `/usr/local/libexec/vps`, mais le marqueur
`/etc/vps/production-enabled` et l'applicateur `apply-release` restent absents :
il ne peut produire qu'une validation et un plan sans activation de production.

Le rôle initialise aussi le miroir root-owned `/srv/vps/repository` depuis
l’unique origine publique autorisée
`https://github.com/nclsppr/vps-infra.git`. Il prouve que le SHA demandé est
exactement checkouté, appartient à `origin/main` et ne comporte aucune
modification locale avant d’installer les scripts root-owned depuis ce miroir.
Le SHA installé est enregistré dans
`/usr/local/share/vps-infra/controller-revision`. Cette origine et cette
révision ne sont jamais dérivées de la commande SSH reçue.

## Frontières réseau préparées

Ansible crée six réseaux Docker externes aux projets Compose. PostgreSQL rejoint
uniquement `db_surplasse`, `db_parkventory` et le réseau interne
`db_monitoring` (`172.30.31.0/24`). Le PostgreSQL Exporter rejoint
`db_monitoring` pour lire la base et `ops` pour être scrappé. Caddy, Prometheus
et Grafana n'obtiennent ainsi aucun chemin TCP direct vers PostgreSQL via
`ops`. Un réseau existant dont le pilote, le caractère interne, le CIDR ou le
label de gestion diffère fait échouer la convergence sans être supprimé.
