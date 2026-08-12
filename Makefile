SHELL := /bin/bash
.DEFAULT_GOAL := help

ANSIBLE_INVENTORY ?= ansible/inventories/production/hosts.yml
ANSIBLE_EXTRA_VARS ?=
PLATFORM_ENV ?= platform/.env.example
CADDY_BUILD_ENV ?= platform/caddy/build.env
POSTGRES_BUILD_ENV ?= platform/postgres/build.env
MISE_EXEC := mise exec --
COMPOSE := $(MISE_EXEC) docker-compose

.PHONY: help setup check check-fast check-yaml check-actions check-ansible \
	check-controller check-public-safe check-platform check-platform-config \
	check-public-static-edge check-surplasse-adapter check-prometheus check-caddy check-postgres-image \
	check-json bootstrap \
	converge converge-check prepare-public-static-edge \
	activate-public-static-edge stop-public-static-edge \
	start-internal-platform stop-internal-platform \
	doctor-local

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
	$(MISE_EXEC) uv run yamllint --no-warnings .github ansible applications platform releases

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
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/public-static-edge.yml
	cd ansible && ../.venv/bin/ansible-playbook \
		--inventory inventories/production/hosts.example.yml \
		--syntax-check playbooks/internal-platform.yml

check-controller: ## Test the release manifest, controller, and shell scripts.
	$(MISE_EXEC) ./scripts/check
	$(MISE_EXEC) shellcheck scripts/validate-caddy-build-inputs \
		scripts/verify-caddy-image scripts/validate-postgres-build-inputs \
		scripts/verify-postgres-image \
		platform/caddy/entrypoint.sh platform/postgres/initdb/10-platform-exporter.sh

check-public-safe: ## Reject secrets and production inventories in this public repository.
	./scripts/check-public-safe --root .

check-json: ## Validate the release manifest and Grafana JSON files.
	$(MISE_EXEC) ./scripts/validate-release --require-json-schema \
		releases/production.yaml
	$(MISE_EXEC) uv run python -m json.tool \
		platform/observability/grafana/dashboards/platform/overview.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		schemas/static-route-inventory-v1.schema.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		schemas/platform-vex-v1.schema.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		policies/platform-vex-v1.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		applications/surplasse/adapter.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		applications/surplasse/expected-images.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool \
		applications/surplasse/migrations.json >/dev/null
	$(MISE_EXEC) uv run python -m json.tool renovate.json >/dev/null

check-platform: check-platform-config check-public-static-edge check-surplasse-adapter check-prometheus check-caddy check-postgres-image ## Validate the shared platform, public edge, application candidates, and custom images.

check-platform-config: ## Render Compose and apply the production policy.
	@rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	$(COMPOSE) --env-file "$(PLATFORM_ENV)" --file platform/compose.yaml \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--expected-images platform/expected-images.json \
		--repository-root "$(CURDIR)" \
		vps-platform "$$rendered"

check-public-static-edge: ## Validate the isolated Caddy-only public edge.
	@rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	$(COMPOSE) --file platform/public-static-edge/compose.yaml \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--expected-images platform/public-static-edge/expected-images.json \
		vps-public-static-edge "$$rendered"

check-surplasse-adapter: ## Validate the locked Surplasse application candidate.
	@set -Eeuo pipefail; \
	rendered="$$(mktemp)"; \
	trap 'rm -f -- "$$rendered"' EXIT; \
	$(COMPOSE) --env-file applications/surplasse/.env.example \
		--file applications/surplasse/compose.yaml --profile migration \
		config --format json >"$$rendered"; \
	./scripts/validate-compose \
		--expected-images applications/surplasse/expected-images.json \
		surplasse "$$rendered"; \
	./scripts/validate-surplasse-adapter "$$rendered"

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
	@set -Eeuo pipefail; \
	./scripts/validate-caddy-build-inputs "$(CADDY_BUILD_ENV)"; \
	builder="$$(sed -n 's/^CADDY_BUILDER_IMAGE=//p' "$(CADDY_BUILD_ENV)")"; \
	runtime="$$(sed -n 's/^CADDY_RUNTIME_IMAGE=//p' "$(CADDY_BUILD_ENV)")"; \
	test -n "$$builder" && test -n "$$runtime"; \
	image="vps-infra/caddy-check:$$(git rev-parse --short=12 HEAD 2>/dev/null || printf local)"; \
	docker build \
		--file platform/caddy/Dockerfile \
		--build-arg "CADDY_BUILDER_IMAGE=$$builder" \
		--build-arg "CADDY_RUNTIME_IMAGE=$$runtime" \
		--tag "$$image" platform/caddy; \
	./scripts/verify-caddy-image "$$image"

check-postgres-image: ## Build and verify the non-root PostgreSQL runtime image.
	@set -Eeuo pipefail; \
	./scripts/validate-postgres-build-inputs "$(POSTGRES_BUILD_ENV)"; \
	base="$$(sed -n 's/^POSTGRES_BASE_IMAGE=//p' "$(POSTGRES_BUILD_ENV)")"; \
	test -n "$$base"; \
	base_tag="$${base#*:}"; \
	version="$${base_tag%%-*}"; \
	image="vps-infra/postgres-check:$$(git rev-parse --short=12 HEAD 2>/dev/null || printf local)"; \
	docker build \
		--file platform/postgres/Dockerfile \
		--build-arg "POSTGRES_BASE_IMAGE=$$base" \
		--tag "$$image" platform/postgres; \
	POSTGRES_DOCKER_PLATFORM="linux/$$(docker image inspect \
		--format '{{.Architecture}}' "$$image")" \
		./scripts/verify-postgres-image "$$image" "$$version"

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

converge-check: ## Predict host changes from an isolated snapshot of origin/main.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --check --diff

prepare-public-static-edge: ## Start the static edge in HTTP-only preflight mode.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --prepare-public-static-edge

activate-public-static-edge: ## Activate HTTPS only after the exact DNS cutover.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --activate-public-static-edge

stop-public-static-edge: ## Stop only the static Caddy edge and preserve its data.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --stop-public-static-edge

start-internal-platform: ## Start PostgreSQL and observability without Caddy.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --start-internal-platform

stop-internal-platform: ## Stop internal services and preserve their data volumes.
	ANSIBLE_INVENTORY="$(abspath $(ANSIBLE_INVENTORY))" \
	ANSIBLE_EXTRA_VARS="$(abspath $(ANSIBLE_EXTRA_VARS))" \
		./scripts/converge --stop-internal-platform

doctor-local: ## Audit the local checkout without contact with the VPS.
	./scripts/doctor --local
