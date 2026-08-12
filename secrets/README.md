# Contrat des secrets

Ce répertoire documente le contrat ; il ne contient et ne contiendra aucune
valeur déchiffrée. L’identité age de production et sa copie de récupération
doivent d’abord être créées hors du VPS. Une `.sops.yaml` ne sera ajoutée
qu’avec le destinataire public réel et après preuve de récupération de la clé
privée.

## Plateforme

Les fichiers déchiffrés seront matérialisés sous
`/etc/vps/secrets/platform/` :

| Fichier | Propriétaire et mode attendus |
|---|---|
| `ovh-application-key` | `root:root 0400` |
| `ovh-application-secret` | `root:root 0400` |
| `ovh-consumer-key` | `root:root 0400` |
| `postgres-superuser-password` | `root:70 0440` |
| `postgres-exporter-password` | `root:70 0440` |
| `grafana-admin-password` | `root:472 0440` |
| `grafana-secret-key` | `root:472 0440` |

Le parent `/etc/vps/secrets/platform` reste `root:root 0700`. Le groupe `70`
permet la lecture des deux mots de passe par PostgreSQL (`70:70`) et du seul
mot de passe exporteur par PostgreSQL Exporter (`65534:70`). Le groupe `472`
permet à Grafana (`472:472`) de lire ses deux fichiers sans les rendre publics.

Le contrôleur interne crée ces quatre valeurs une seule fois. Il refuse les
liens symboliques, les liens physiques supplémentaires, un format divergent ou
des permissions existantes différentes. Il ne remplace jamais une valeur déjà
valide et ne l'écrit pas dans les sorties Ansible.

## Règles

- une identité et un fichier distincts par périmètre lorsque leurs droits
  diffèrent ;
- aucun secret dans l’environnement Git, une issue, une PR ou un log CI ;
- rotation après toute exposition, même si le commit est ensuite supprimé ;
- déchiffrement depuis un poste de confiance, puis copie Ansible sans sortie de
  contenu ;
- références Compose uniquement par fichiers sous `/run/secrets` ;
- sauvegarde de récupération de la clé age testée avant toute activation.

## Surplasse

The Surplasse preparation controller now creates separate migrator and runtime
database passwords. It also installs a helper that validates and materializes
the complete operator bundle under `/etc/vps/secrets/surplasse`. The exact file
list, metadata, offline format checks, and JWT key-pair proof are in
[`applications/surplasse/README.md`](../applications/surplasse/README.md).

The adapter remains locked. A locally valid OVH token shape does not prove its
IAM scope, and valid Stripe values do not prove the reviewed Connect release
gate. Do not activate from the presence of files alone.
