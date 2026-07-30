SHELL := /bin/bash
.DEFAULT_GOAL := help

ANSIBLE_INVENTORY ?= ansible/inventories/production/hosts.yml
ANSIBLE_EXTRA_VARS ?=
PLATFORM_ENV ?= platform/.env.example
MISE_EXEC := mise exec --

.PHONY: help setup check check-fast check-yaml check-actions check-ansible \
	check-controller check-public-safe check-platform check-platform-config \
	check-prometheus check-caddy check-json bootstrap converge doctor-local

help: ## Afficher les commandes disponibles.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Installer exactement les outils et dependances verrouilles.
	mise install --locked
	$(MISE_EXEC) uv sync --locked
	cd ansible && ../.venv/bin/ansible-galaxy collection install \
		--requirements-file collections/requirements.yml \
		--collections-path ../.cache/ansible/collections

check: check-fast check-platform ## Executer tout le contrat CI, sans deploiement.

check-fast: check-yaml check-actions check-ansible check-controller check-json ## Executer les validations sans image Docker.

check-yaml: ## Valider les fichiers YAML versionnes.
	$(MISE_EXEC) uv run yamllint --no-warnings .github ansible platform releases

check-actions: ## Valider les workflows GitHub Actions.
	$(MISE_EXEC) actionlint

check-ansible: ## Linter Ansible et verifier la syntaxe des deux playbooks.
	cd ansible && ../.venv/bin/ansible-lint
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/bootstrap.yml
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/site.yml

check-controller: ## Tester le manifeste, le controleur et les scripts shell.
	$(MISE_EXEC) ./scripts/check
	$(MISE_EXEC) shellcheck platform/caddy/entrypoint.sh platform/postgres/initdb/10-platform-exporter.sh

check-public-safe: ## Refuser secrets et inventaires concrets dans le depot public.
	./scripts/check-public-safe --root .

check-json: ## Valider le manifeste par son schema et les documents JSON Grafana.
	$(MISE_EXEC) ./scripts/validate-release --require-json-schema \
		releases/production.yaml
	$(MISE_EXEC) uv run python -m json.tool \
		platform/observability/grafana/dashboards/platform/overview.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool renovate.json >/dev/null

check-platform: check-platform-config check-prometheus check-caddy ## Valider la pile partagee et son image Caddy.

check-platform-config: ## Rendre Compose puis appliquer la politique de production.
	@rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	docker compose --env-file "$(PLATFORM_ENV)" --file platform/compose.yaml \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--structural-only \
		--repository-root "$(CURDIR)" \
		vps-platform "$$rendered"

check-prometheus: ## Valider Prometheus et toutes ses regles dans l'image epinglee.
	@image="$$(sed -n 's/^PROMETHEUS_IMAGE=//p' "$(PLATFORM_ENV)")"; \
	test -n "$$image"; \
	docker run --rm --entrypoint promtool \
		--volume "$(CURDIR)/platform/observability/prometheus:/etc/prometheus:ro" \
		"$$image" check config /etc/prometheus/prometheus.yml

check-caddy: ## Construire Caddy OVH puis valider la configuration complete.
	@builder="$$(sed -n 's/^CADDY_BUILDER_IMAGE=//p' "$(PLATFORM_ENV)")"; \
	runtime="$$(sed -n 's/^CADDY_RUNTIME_IMAGE=//p' "$(PLATFORM_ENV)")"; \
	module="$$(sed -n 's/^CADDY_DNS_MODULE=//p' "$(PLATFORM_ENV)")"; \
	test -n "$$builder" && test -n "$$runtime" && test -n "$$module"; \
	image="vps-infra/caddy-check:$$(git rev-parse --short=12 HEAD 2>/dev/null || printf local)"; \
	docker build \
		--file platform/caddy/Dockerfile \
		--build-arg "CADDY_BUILDER_IMAGE=$$builder" \
		--build-arg "CADDY_RUNTIME_IMAGE=$$runtime" \
		--build-arg "CADDY_DNS_MODULE=$$module" \
		--tag "$$image" platform/caddy; \
	secrets="$$(mktemp -d)"; \
	trap 'rm -rf -- "$$secrets"' EXIT; \
	for name in application-key application-secret consumer-key; do \
		printf 'validation-placeholder\n' >"$$secrets/$$name"; \
	done; \
	docker run --rm \
		--env OVH_APPLICATION_KEY_FILE=/run/secrets/ovh-application-key \
		--env OVH_APPLICATION_SECRET_FILE=/run/secrets/ovh-application-secret \
		--env OVH_CONSUMER_KEY_FILE=/run/secrets/ovh-consumer-key \
		--volume "$$secrets/application-key:/run/secrets/ovh-application-key:ro" \
		--volume "$$secrets/application-secret:/run/secrets/ovh-application-secret:ro" \
		--volume "$$secrets/consumer-key:/run/secrets/ovh-consumer-key:ro" \
		--volume "$(CURDIR)/platform/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
		--volume "$(CURDIR)/platform/caddy/routes:/etc/caddy/routes:ro" \
		"$$image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

bootstrap: ## Creer le compte administrateur sur un Ubuntu OVHcloud neuf.
	@test -f "$(ANSIBLE_INVENTORY)" || { \
		echo "Copier ansible/inventories/production/hosts.example.yml vers hosts.yml." >&2; exit 2; }
	@test -n "$(ANSIBLE_EXTRA_VARS)" && test -f "$(ANSIBLE_EXTRA_VARS)" || { \
		echo "ANSIBLE_EXTRA_VARS doit designer le fichier public local contenant les cles SSH." >&2; exit 2; }
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory "$(abspath $(ANSIBLE_INVENTORY))" \
		--extra-vars "@$(abspath $(ANSIBLE_EXTRA_VARS))" \
		playbooks/bootstrap.yml

converge: ## Durcir et converger l'hote apres preuve de la seconde connexion.
	@test -f "$(ANSIBLE_INVENTORY)" || { echo "Inventaire local absent." >&2; exit 2; }
	@test -n "$(ANSIBLE_EXTRA_VARS)" && test -f "$(ANSIBLE_EXTRA_VARS)" || { \
		echo "ANSIBLE_EXTRA_VARS doit designer le fichier public local contenant les cles SSH." >&2; exit 2; }
	@git fetch --quiet origin main
	@revision="$$(git rev-parse --verify 'origin/main^{commit}')"; \
		printf '%s' "$$revision" | grep -Eq '^[0-9a-f]{40}$$'; \
		cd ansible && ../.venv/bin/ansible-playbook \
			--inventory "$(abspath $(ANSIBLE_INVENTORY))" \
			--extra-vars "@$(abspath $(ANSIBLE_EXTRA_VARS))" \
			--extra-vars "vps_infra_revision=$$revision" \
			playbooks/site.yml

doctor-local: ## Auditer le checkout local sans contacter le VPS.
	./scripts/doctor --local
