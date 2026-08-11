SHELL := /bin/bash
.DEFAULT_GOAL := help

ANSIBLE_INVENTORY ?= ansible/inventories/production/hosts.yml
ANSIBLE_EXTRA_VARS ?=
PLATFORM_ENV ?= platform/.env.example
MISE_EXEC := mise exec --
COMPOSE := $(MISE_EXEC) docker-compose

.PHONY: help setup check check-fast check-yaml check-actions check-ansible \
	check-controller check-public-safe check-platform check-platform-config \
	check-prometheus check-caddy check-json bootstrap converge doctor-local

help: ## Show the available commands.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install the locked tools and dependencies.
	mise install --locked
	$(MISE_EXEC) uv sync --locked
	cd ansible && ../.venv/bin/ansible-galaxy collection install \
		--requirements-file collections/requirements.yml \
		--collections-path ../.cache/ansible/collections

check: check-fast check-platform ## Run the complete CI contract without deployment.

check-fast: check-yaml check-actions check-ansible check-controller check-json ## Run checks that do not require a Docker image.

check-yaml: ## Validate versioned YAML files.
	$(MISE_EXEC) uv run yamllint --no-warnings .github ansible platform releases

check-actions: ## Validate GitHub Actions workflows.
	$(MISE_EXEC) actionlint

check-ansible: ## Lint Ansible and validate both playbooks.
	cd ansible && ../.venv/bin/ansible-lint
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/bootstrap.yml
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/site.yml

check-controller: ## Test the release manifest, controller, and shell scripts.
	$(MISE_EXEC) ./scripts/check
	$(MISE_EXEC) shellcheck platform/caddy/entrypoint.sh platform/postgres/initdb/10-platform-exporter.sh

check-public-safe: ## Reject secrets and production inventories in this public repository.
	./scripts/check-public-safe --root .

check-json: ## Validate the release manifest and Grafana JSON files.
	$(MISE_EXEC) ./scripts/validate-release --require-json-schema \
		releases/production.yaml
	$(MISE_EXEC) uv run python -m json.tool \
		platform/observability/grafana/dashboards/platform/overview.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool renovate.json >/dev/null

check-platform: check-platform-config check-prometheus check-caddy ## Validate the shared platform and its Caddy image.

check-platform-config: ## Render Compose and apply the production policy.
	@rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	$(COMPOSE) --env-file "$(PLATFORM_ENV)" --file platform/compose.yaml \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--structural-only \
		--repository-root "$(CURDIR)" \
		vps-platform "$$rendered"

check-prometheus: ## Validate active and inactive Prometheus rules in the pinned image.
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

check-caddy: ## Build Caddy and validate the inactive and candidate route sets.
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

bootstrap: ## Create the administrator account on a new OVHcloud Ubuntu host.
	@test -f "$(ANSIBLE_INVENTORY)" || { \
		echo "Copy ansible/inventories/production/hosts.example.yml to hosts.yml." >&2; exit 2; }
	@test -n "$(ANSIBLE_EXTRA_VARS)" && test -f "$(ANSIBLE_EXTRA_VARS)" || { \
		echo "ANSIBLE_EXTRA_VARS must identify the local public-key variable file." >&2; exit 2; }
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory "$(abspath $(ANSIBLE_INVENTORY))" \
		--extra-vars "@$(abspath $(ANSIBLE_EXTRA_VARS))" \
		playbooks/bootstrap.yml

converge: ## Run host convergence from an isolated snapshot of origin/main.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge

doctor-local: ## Audit the local checkout without contact with the VPS.
	./scripts/doctor --local
