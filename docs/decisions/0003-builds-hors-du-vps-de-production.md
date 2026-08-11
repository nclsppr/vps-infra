# ADR-0003 — Builds hors du VPS de production

## Statut

Accepté le 30 juillet 2026.

## Contexte

Construire directement sur le VPS éviterait éventuellement des minutes CI, mais
ferait partager CPU, mémoire, disque, daemon Docker et réseau entre production
et code de build. Une image uniquement locale disparaîtrait avec le VPS.

Les quatre dépôts applicatifs sont publics au moment de la décision. Leurs jobs
sur runners GitHub standards ne consomment donc pas de minutes facturables. Le
dépôt public `vps-infra` bénéficie de la même règle.

## Décision

- GitHub Actions teste et construit les artefacts applicatifs.
- Les composants non modifiés ne sont pas reconstruits.
- Les images et paquets statiques sont publiés dans GHCR par digest.
- Le VPS ne reçoit que des commits et digests déjà validés ; il ne clone ni ne
  compile les sources applicatives.
- Le workflow de déploiement du dépôt VPS reste léger et ne contient aucun
  build Maven, npm ou Docker applicatif.

Un budget GitHub à zéro dollar doit interrompre tout dépassement facturable. La
rétention des artefacts temporaires reste courte ; GHCR conserve les releases
nécessaires au rollback et à la reconstruction.

## Alternative si la facturation change

Le premier recours sera un runner éphémère ou une machine de build séparée, avec
accès à aucun secret de production. Un builder rootless et fortement limité sur
le VPS ne pourra être accepté que par nouvelle ADR, en conservant l’obligation
de pousser l’artefact dans un registre externe avant activation.

Un runner GitHub Actions persistant ayant accès au daemon Docker de production
reste interdit.

## Références

- [GitHub Actions — facturation et usages](https://docs.github.com/en/actions/concepts/billing-and-usage)
- [GitHub — quotas inclus par plan](https://docs.github.com/en/enterprise-cloud@latest/billing/reference/product-usage-included)
- [GitHub Packages — facturation](https://docs.github.com/en/enterprise-cloud@latest/billing/concepts/product-billing/github-packages)
- [GitHub — budgets et alertes](https://docs.github.com/en/billing/concepts/budgets-and-alerts)
