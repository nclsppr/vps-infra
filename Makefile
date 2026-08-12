SHELL := /bin/bash
.DEFAULT_GOAL := help

ANSIBLE_INVENTORY ?= ansible/inventories/production/hosts.yml
ANSIBLE_EXTRA_VARS ?=
PLATFORM_ENV ?= platform/.env.example
MISE_EXEC := mise exec --
COMPOSE := $(MISE_EXEC) docker-compose

.PHONY: help setup check check-fast check-yaml check-actions check-ansible \
	check-controller check-public-safe check-platform check-platform-config \
	check-prometheus check-caddy check-json bootstrap converge converge-check \
	doctor-local

help: ## Afficher les commandes disponibles.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Installer les outils et dépendances verrouillés.
	mise install --locked
	$(MISE_EXEC) uv sync --locked
	cd ansible && ../.venv/bin/ansible-galaxy collection install \
		--requirements-file collections/requirements.yml \
		--collections-path ../.cache/ansible/collections

check: check-fast check-platform ## Exécuter le contrat CI complet sans déploiement.

check-fast: check-yaml check-actions check-ansible check-controller check-json ## Exécuter les contrôles sans image Docker.

check-yaml: ## Valider les fichiers YAML versionnés.
	$(MISE_EXEC) uv run yamllint --no-warnings .github ansible platform releases

check-actions: ## Valider les workflows GitHub Actions.
	$(MISE_EXEC) actionlint

check-ansible: ## Linter Ansible et valider les deux playbooks.
	cd ansible && ../.venv/bin/ansible-lint
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/bootstrap.yml
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/site.yml

check-controller: ## Tester le manifeste de release, le contrôleur et les scripts shell.
	$(MISE_EXEC) ./scripts/check
	$(MISE_EXEC) shellcheck platform/caddy/entrypoint.sh platform/postgres/initdb/10-platform-exporter.sh

check-public-safe: ## Refuser les secrets et inventaires de production dans ce dépôt public.
	./scripts/check-public-safe --root .

check-json: ## Valider le manifeste de release et les fichiers JSON Grafana.
	$(MISE_EXEC) ./scripts/validate-release --require-json-schema \
		releases/production.yaml
	$(MISE_EXEC) uv run python -m json.tool \
		platform/observability/grafana/dashboards/platform/overview.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool renovate.json >/dev/null

check-platform: check-platform-config check-prometheus check-caddy ## Valider la plateforme commune et son image Caddy.

check-platform-config: ## Produire la configuration Compose et appliquer la politique de production.
	@rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	$(COMPOSE) --env-file "$(PLATFORM_ENV)" --file platform/compose.yaml \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--structural-only \
		--repository-root "$(CURDIR)" \
		vps-platform "$$rendered"

check-prometheus: ## Valider les règles Prometheus actives et inactives dans l'image épinglée.
	@image="$$(sed -n 's/^PROMETHEUS_IMAGE=//p' "$(PLATFORM_ENV)")"; \
		test -n "$$image"; \
		docker run --rm --entrypoint promtool \
			--volume "$(CURDIR)/platform/observability/prometheus:/etc/prometheus:ro" \
			"$$image" check config /etc/prometheus/prometheus.yml; \
		for candidate in "$(CURDIR)"/platform/observability/prometheus/rules/*.yml.disabled; do \
			test -f "$$candidate"; \
			docker run --rm --entrypoint promtool \
				--volume "$$candidate:/tmp/candidate.yml:ro" \
				"$$image" check rules /tmp/candidate.yml; \
		done

check-caddy: ## Construire Caddy et valider les routes inactives et candidates.
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
		candidate_routes="$$(mktemp -d)"; \
		trap 'rm -rf -- "$$secrets" "$$candidate_routes"' EXIT; \
		for source in "$(CURDIR)"/platform/caddy/routes/*.caddy.disabled; do \
			test -f "$$source"; \
			cp -- "$$source" "$$candidate_routes/$$(basename "$${source%.disabled}")"; \
		done; \
		docker run --rm \
			--volume "$(CURDIR)/platform/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
			--volume "$(CURDIR)/platform/caddy/routes:/etc/caddy/routes:ro" \
			"$$image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; \
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
			--volume "$$candidate_routes:/etc/caddy/routes:ro" \
			"$$image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

bootstrap: ## Amorcer l'hôte depuis un snapshot isolé de origin/main.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/bootstrap

converge: ## Converger l'hôte depuis un snapshot isolé de origin/main.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge

converge-check: ## Prévoir les changements depuis un snapshot isolé de origin/main.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --check --diff

doctor-local: ## Auditer le checkout local sans contacter le VPS.
	./scripts/doctor --local
