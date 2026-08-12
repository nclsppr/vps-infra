#!/usr/bin/env python3

from __future__ import annotations

import copy
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST = ROOT / "releases" / "production.yaml"
SCHEMA = ROOT / "schemas" / "production-release.schema.json"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
DIGEST_C = "3" * 64
DIGEST_D = "4" * 64
DIGEST_E = "5" * 64


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE_POLICY = load_script_module("release_policy", SCRIPTS / "lib" / "release_policy.py")
COMPOSE_POLICY = load_script_module("compose_policy", SCRIPTS / "validate-compose")
SURPLASSE_ADAPTER = load_script_module(
    "surplasse_adapter", SCRIPTS / "validate-surplasse-adapter"
)
RECONCILE_POLICY = load_script_module("reconcile_policy", SCRIPTS / "reconcile")


def sample_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def image(repository: str, digest: str = DIGEST_A, tag: str = "test") -> str:
    return f"{repository}:{tag}@sha256:{digest}"


def evidence(repository: str, revision: str = SHA_A, run_id: str = "123456") -> dict:
    return {
        "source_revision": revision,
        "run_id": run_id,
        "run_attempt": 1,
        "workflow_path": ".github/workflows/vps-release.yml",
        "conclusion": "success",
        "url": f"https://github.com/{repository}/actions/runs/{run_id}",
    }


def enable_platform(manifest: dict) -> None:
    manifest["platform"] = {
        "enabled": True,
        "compose_project": "vps-platform",
        "published_ports": ["80/tcp", "443/tcp", "443/udp", "127.0.0.1:3000/tcp"],
        "blocked_by": [],
        "images": {
            "caddy": image("ghcr.io/nclsppr/vps-infra/caddy", DIGEST_C),
            "postgres": image(
                "ghcr.io/nclsppr/vps-infra/postgres",
                DIGEST_C,
                f"sha-{SHA_A}",
            ),
            "prometheus": image("docker.io/prom/prometheus", DIGEST_C),
            "grafana": image("docker.io/grafana/grafana", DIGEST_C),
            "node_exporter": image("docker.io/prom/node-exporter", DIGEST_C),
            "postgres_exporter": image("docker.io/prometheuscommunity/postgres-exporter", DIGEST_C),
        },
        "integration": {
            "source_revision": SHA_A,
            "artifact": image(
                "ghcr.io/nclsppr/vps-infra/platform-integration", DIGEST_E
            ),
        },
        "postgres": {"major": 17, "pgdata": "/var/lib/postgresql/data/pgdata"},
        "readiness_evidence": {
            gate: evidence("nclsppr/vps-infra")
            for gate in RELEASE_POLICY.PLATFORM_READINESS_GATES
        },
    }


def add_platform_candidate(manifest: dict) -> None:
    """Declare a complete platform candidate without enabling runtime state."""

    enable_platform(manifest)
    manifest["platform"]["enabled"] = False
    manifest["platform"]["published_ports"] = []
    manifest["platform"]["blocked_by"] = sorted(
        RELEASE_POLICY.PLATFORM_READINESS_GATES
    )
    manifest["platform"]["readiness_evidence"] = {
        gate: manifest["platform"]["readiness_evidence"][gate]
        for gate in RELEASE_POLICY.PLATFORM_PROOF_BASELINE_GATES
    }


def set_platform_candidate_revision(manifest: dict, revision: str) -> None:
    manifest["platform"]["integration"]["source_revision"] = revision
    for value in manifest["platform"]["readiness_evidence"].values():
        value["source_revision"] = revision


def enable_parkventory(manifest: dict) -> None:
    enable_platform(manifest)
    app = manifest["applications"]["parkventory"]
    app["enabled"] = True
    app["blocked_by"] = []
    app["components"] = {
        "backend": {
            "source_revision": SHA_A,
            "image": image("ghcr.io/nclsppr/parkventory/backend"),
        },
        "frontend": {
            "source_revision": SHA_B,
            "image": image("ghcr.io/nclsppr/parkventory/frontend", DIGEST_B),
        },
    }
    app["integration"] = {
        "source_revision": SHA_A,
        "artifact": image("ghcr.io/nclsppr/parkventory/vps-integration"),
    }
    app["migrations"] = {
        "strategy": "dedicated",
        "runtime_auto_migrate": False,
        "proven": True,
        "evidence": evidence("nclsppr/parkventory"),
    }
    app["readiness_evidence"] = {
        gate: evidence("nclsppr/parkventory")
        for gate in RELEASE_POLICY.PARKVENTORY_READINESS_GATES
    }


def enable_surplasse(manifest: dict) -> None:
    enable_platform(manifest)
    app = manifest["applications"]["surplasse"]
    app["enabled"] = True
    app["blocked_by"] = []
    app["components"] = {
        name: {
            "source_revision": SHA_A,
            "image": image(repository),
        }
        for name, repository in RELEASE_POLICY.COMPOSE_COMPONENT_REPOSITORIES["surplasse"].items()
    }
    app["integration"] = {
        "source_revision": SHA_A,
        "artifact": image(RELEASE_POLICY.INTEGRATION_REPOSITORIES["surplasse"]),
    }
    app["migrations"] = {
        "strategy": "dedicated",
        "runtime_auto_migrate": False,
        "proven": True,
        "evidence": evidence("nclsppr/surplasse"),
    }
    app["readiness_evidence"] = {
        gate: evidence("nclsppr/surplasse")
        for gate in RELEASE_POLICY.SURPLASSE_READINESS_GATES
    }


def enable_static(manifest: dict, name: str) -> None:
    enable_platform(manifest)
    app = manifest["applications"][name]
    app["enabled"] = True
    app["blocked_by"] = []
    app["source_revision"] = SHA_A
    artifact_repo, routes_repo = RELEASE_POLICY.STATIC_ARTIFACT_REPOSITORIES[name]
    app["artifact"] = image(artifact_repo)
    app["route_inventory_artifact"] = image(routes_repo, DIGEST_B)
    app["readiness_evidence"] = {
        gate: evidence(app["source_repository"])
        for gate in RELEASE_POLICY.STATIC_READINESS_GATES
    }


class ReleasePolicyTests(unittest.TestCase):
    def test_duplicate_json_keys_are_rejected_at_top_level_and_nested(self) -> None:
        documents = (
            '{"schema":1,"schema":1}',
            '{"platform":{"enabled":false,"enabled":true}}',
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "duplicate JSON object key"):
                    RELEASE_POLICY.strict_json_loads(document)

    def test_disabled_sample_is_valid_and_contains_no_fake_digest(self) -> None:
        manifest = sample_manifest()
        RELEASE_POLICY.validate_manifest(manifest)
        self.assertEqual(list(RELEASE_POLICY.iter_digests(manifest)), [])
        self.assertFalse(manifest["platform"]["enabled"])
        self.assertTrue(all(not app["enabled"] for app in manifest["applications"].values()))

    def test_complete_disabled_platform_candidate_is_valid_and_lists_all_digests(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        RELEASE_POLICY.validate_manifest(manifest)
        digests = list(RELEASE_POLICY.iter_digests(manifest))
        self.assertEqual(len(digests), 7)
        self.assertEqual(digests.count(f"sha256:{DIGEST_C}"), 6)
        self.assertEqual(digests.count(f"sha256:{DIGEST_E}"), 1)

    def test_partial_disabled_platform_candidate_is_rejected(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        del manifest["platform"]["postgres"]
        with self.assertRaisesRegex(
            RELEASE_POLICY.PolicyError,
            "platform candidate fields must be declared together",
        ):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_json_schema_accepts_only_complete_or_absent_platform_candidate(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        legacy = sample_manifest()
        candidate = sample_manifest()
        add_platform_candidate(candidate)
        partial = copy.deepcopy(candidate)
        del partial["platform"]["integration"]

        self.assertEqual(list(validator.iter_errors(legacy)), [])
        self.assertEqual(list(validator.iter_errors(candidate)), [])
        self.assertTrue(list(validator.iter_errors(partial)))

    def test_release_validator_prints_only_declared_platform_integration_revision(self) -> None:
        candidate = sample_manifest()
        add_platform_candidate(candidate)
        legacy = sample_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidate.json"
            legacy_path = root / "legacy.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            for path, expected in ((candidate_path, SHA_A), (legacy_path, "")):
                with self.subTest(path=path.name):
                    result = subprocess.run(
                        [
                            str(SCRIPTS / "validate-release"),
                            "--require-json-schema",
                            "--print-platform-integration-revision",
                            str(path),
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)

    def test_disabled_platform_candidate_evidence_must_match_integration_revision(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        manifest["platform"]["readiness_evidence"][
            "caddy-ovh-image"
        ]["source_revision"] = SHA_B
        with self.assertRaisesRegex(
            RELEASE_POLICY.PolicyError,
            "must match a declared component or integration revision",
        ):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_disabled_candidate_rejects_unproven_readiness_gate(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        manifest["platform"]["readiness_evidence"][
            "alert-routing-and-delivery"
        ] = evidence("nclsppr/vps-infra")
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "unknown alert-routing"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_disabled_platform_candidate_reconciles_as_runtime_no_op(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            plan = RECONCILE_POLICY.create_plan(
                manifest,
                None,
                SHA_A,
                None,
                Path(temporary) / "quarantine",
            )
        self.assertFalse(plan["changes_required"])
        self.assertTrue(
            all(action["action"] == "unchanged-disabled" for action in plan["actions"])
        )
        self.assertTrue(
            all("desired_references" not in action for action in plan["actions"])
        )

    def test_quarantined_disabled_platform_candidate_digest_is_rejected(self) -> None:
        manifest = sample_manifest()
        add_platform_candidate(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            quarantine = Path(temporary) / "quarantine"
            artifacts = quarantine / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / f"{DIGEST_C}.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RECONCILE_POLICY.ReconcileError, "quarantined"):
                RECONCILE_POLICY.create_plan(
                    manifest,
                    None,
                    SHA_A,
                    None,
                    quarantine,
                )

    def test_wrong_canonical_branch_is_rejected(self) -> None:
        manifest = sample_manifest()
        manifest["applications"]["papersempire"]["source_branch"] = "main"
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "must equal 'master'"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_postgres_pgdata_must_match_the_volume_contract(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        manifest["platform"]["postgres"]["pgdata"] = "/var/lib/postgresql/data"
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "/var/lib/postgresql/data/pgdata"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_postgres_image_must_use_custom_repository_and_revision_tag(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        manifest["platform"]["images"]["postgres"] = image(
            "docker.io/library/postgres", DIGEST_C, "17.10-bookworm"
        )
        with self.assertRaisesRegex(
            RELEASE_POLICY.PolicyError,
            "ghcr.io/nclsppr/vps-infra/postgres",
        ):
            RELEASE_POLICY.validate_manifest(manifest)
        manifest["platform"]["images"]["postgres"] = image(
            "ghcr.io/nclsppr/vps-infra/postgres", DIGEST_C, "latest"
        )
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "source revision"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_enabled_platform_requires_every_readiness_gate(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        del manifest["platform"]["readiness_evidence"]["external-networks-and-cidrs"]
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "missing external-networks-and-cidrs"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_caddy_must_use_the_vps_infra_registry_repository(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        manifest["platform"]["images"]["caddy"] = image("ghcr.io/nclsppr/vps/caddy", DIGEST_C)
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "nclsppr/vps-infra/caddy"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_application_host_port_is_rejected(self) -> None:
        manifest = sample_manifest()
        manifest["applications"]["personal"]["published_ports"] = ["8080/tcp"]
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "may not publish host ports"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_mutable_static_artifact_is_rejected(self) -> None:
        manifest = sample_manifest()
        enable_static(manifest, "personal")
        manifest["applications"]["personal"]["artifact"] = "ghcr.io/nclsppr/personal/site:latest"
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "immutable registry reference"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_static_application_requires_protected_branch_evidence(self) -> None:
        for name in ("personal", "papersempire"):
            with self.subTest(name=name):
                manifest = sample_manifest()
                enable_static(manifest, name)
                del manifest["applications"][name]["readiness_evidence"]["protected-source-branch"]
                with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "protected-source-branch"):
                    RELEASE_POLICY.validate_manifest(manifest)

    def test_surplasse_requires_protected_main_evidence(self) -> None:
        manifest = sample_manifest()
        enable_surplasse(manifest)
        del manifest["applications"]["surplasse"]["readiness_evidence"]["protected-main"]
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "protected-main"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_complete_parkventory_evidence_is_valid(self) -> None:
        manifest = sample_manifest()
        enable_parkventory(manifest)
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "locked forbids"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_missing_parkventory_readiness_gate_is_rejected(self) -> None:
        manifest = sample_manifest()
        enable_parkventory(manifest)
        del manifest["applications"]["parkventory"]["readiness_evidence"]["restore-proof"]
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "missing restore-proof"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_unproven_or_runtime_migration_is_rejected(self) -> None:
        manifest = sample_manifest()
        enable_parkventory(manifest)
        migrations = manifest["applications"]["parkventory"]["migrations"]
        migrations["runtime_auto_migrate"] = True
        migrations["proven"] = False
        with self.assertRaisesRegex(RELEASE_POLICY.PolicyError, "runtime_auto_migrate"):
            RELEASE_POLICY.validate_manifest(manifest)

    def test_quarantined_artifact_blocks_reconciliation(self) -> None:
        manifest = sample_manifest()
        enable_parkventory(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            quarantine = Path(temporary)
            artifacts = quarantine / "artifacts"
            artifacts.mkdir()
            (artifacts / f"{DIGEST_A}.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RECONCILE_POLICY.ReconcileError, "quarantined"):
                RECONCILE_POLICY.create_plan(manifest, None, SHA_A, None, quarantine)

    def test_plan_digests_lists_only_changed_units(self) -> None:
        active = sample_manifest()
        enable_platform(active)
        desired = json.loads(json.dumps(active))
        enable_parkventory(desired)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = RECONCILE_POLICY.create_plan(desired, active, SHA_A, SHA_B, root / "quarantine")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "plan-digests"), str(plan_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(result.stdout.splitlines()), {f"sha256:{DIGEST_A}", f"sha256:{DIGEST_B}"})
            self.assertNotIn(f"sha256:{DIGEST_C}", result.stdout)

    def test_new_infrastructure_revision_updates_each_active_unit(self) -> None:
        active = sample_manifest()
        enable_platform(active)
        desired = json.loads(json.dumps(active))
        plan = RECONCILE_POLICY.create_plan(
            desired,
            active,
            SHA_B,
            SHA_A,
            Path("/nonexistent-quarantine"),
        )
        platform = next(item for item in plan["actions"] if item["unit"] == "platform")
        self.assertEqual(platform["action"], "update")
        self.assertTrue(plan["changes_required"])

    def test_single_component_update_quarantines_only_the_new_digest(self) -> None:
        active = sample_manifest()
        enable_parkventory(active)
        desired = json.loads(json.dumps(active))
        desired["applications"]["parkventory"]["components"]["backend"] = {
            "source_revision": "c" * 40,
            "image": image("ghcr.io/nclsppr/parkventory/backend", DIGEST_D),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = RECONCILE_POLICY.create_plan(desired, active, SHA_B, SHA_A, root / "quarantine")
            park_action = next(item for item in plan["actions"] if item["unit"] == "parkventory")
            self.assertEqual(
                park_action["desired_references"],
                {"backend": image("ghcr.io/nclsppr/parkventory/backend", DIGEST_D)},
            )
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "plan-digests"), str(plan_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [f"sha256:{DIGEST_D}"])

            artifact_quarantine = root / "quarantine" / "artifacts"
            artifact_quarantine.mkdir(parents=True)
            (artifact_quarantine / f"{DIGEST_D}.json").write_text("{}\n", encoding="utf-8")
            rollback_plan = RECONCILE_POLICY.create_plan(active, active, SHA_A, SHA_A, root / "quarantine")
            self.assertFalse(rollback_plan["changes_required"])


def healthcheck() -> dict:
    return {
        "test": ["CMD", "wget", "--spider", "http://127.0.0.1/ready"],
        "interval": "30s",
        "timeout": "5s",
        "retries": 3,
        "start_period": "10s",
    }


def external_networks(project: str) -> dict:
    names = (
        COMPOSE_POLICY.PLATFORM_NETWORKS
        if project == "vps-platform"
        else {f"app_{project}", f"db_{project}"}
    )
    return {name: {"name": name, "external": True} for name in names}


def hardened_service(
    reference: str,
    networks: set[str],
    user: str | None = "10001:10001",
) -> dict:
    return {
        "image": reference,
        "user": user,
        "read_only": True,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "restart": "unless-stopped",
        "pids_limit": 64,
        "cpus": 0.25,
        "mem_limit": "134217728",
        "logging": {"driver": "local", "options": {"max-file": "3", "max-size": "10m"}},
        "stop_grace_period": "30s",
        "tmpfs": [],
        "networks": {name: {} for name in networks},
        "healthcheck": healthcheck(),
    }


def apply_platform_runtime(service_name: str, service: dict) -> None:
    cpus, memory, pids, stop_seconds = COMPOSE_POLICY.PLATFORM_BUDGETS[service_name]
    service["cpus"] = cpus
    service["mem_limit"] = str(memory)
    service["pids_limit"] = pids
    service["tmpfs"] = list(COMPOSE_POLICY.PLATFORM_TMPFS[service_name])
    if stop_seconds is None:
        service.pop("stop_grace_period", None)
    else:
        service["stop_grace_period"] = f"{int(stop_seconds)}s"
    if service_name == "postgresql":
        service["shm_size"] = 268435456
    if service_name == "postgres-exporter":
        service["depends_on"] = {
            "postgresql": {"condition": "service_healthy", "restart": True, "required": True}
        }


def app_document(project: str = "surplasse") -> dict:
    components = COMPOSE_POLICY.APPLICATION_COMPONENTS[project]
    services = {}
    for component in components:
        networks = (
            {f"app_{project}", f"db_{project}"}
            if component == "backend"
            else {f"app_{project}"}
        )
        services[component] = hardened_service(
            image(f"ghcr.io/nclsppr/{project}/{component}"),
            networks,
        )
        services[component]["networks"][f"app_{project}"] = {
            "aliases": [f"{project}-{component}"]
        }
    migrator = hardened_service(
        services["backend"]["image"],
        {f"db_{project}"},
    )
    migrator["profiles"] = ["migration"]
    migrator["restart"] = "no"
    migrator.pop("healthcheck")
    services["migrator"] = migrator
    return {
        "name": project,
        "services": services,
        "networks": external_networks(project),
        "secrets": {},
    }


def surplasse_adapter_document() -> dict:
    document = app_document()
    services = document["services"]
    backend = services["backend"]
    backend["environment"] = {
        "DEPLOYMENT_PROFILE": "production",
        "QUARKUS_DATASOURCE_DEVSERVICES_ENABLED": "false",
        "QUARKUS_DATASOURCE_JDBC_URL": "jdbc:postgresql://postgresql:5432/surplasse",
        "QUARKUS_DATASOURCE_USERNAME": "surplasse_runtime",
        "QUARKUS_DATASOURCE_PASSWORD_FILE":
            "/run/secrets/surplasse_postgres_runtime_password",
        "QUARKUS_FLYWAY_MIGRATE_AT_START": "false",
        "QUARKUS_HTTP_HOST": "0.0.0.0",
        "STRIPE_LIVE_MODE": "true",
        "TRUSTED_PROXIES": "172.30.10.254",
    }
    backend_secrets = {
        "surplasse_jwt_jwks",
        "surplasse_jwt_private_key",
        "surplasse_postgres_runtime_password",
        "surplasse_smtp_password",
        "surplasse_smtp_username",
        "surplasse_stripe_account_webhook_secret",
        "surplasse_stripe_payment_webhook_secret",
        "surplasse_stripe_secret_key",
    }
    backend["secrets"] = [
        {"source": name, "target": f"/run/secrets/{name}"}
        for name in sorted(backend_secrets)
    ]
    backend["healthcheck"]["test"] = [
        "CMD", "/opt/surplasse/scripts/backend-healthcheck.sh"
    ]

    health_paths = {
        "onboarding": "/__health",
        "commande": "/healthz",
        "dashboard": "/healthz",
        "docs": "/healthz",
    }
    for name, path in health_paths.items():
        services[name]["healthcheck"]["test"] = [
            "CMD", "wget", "--quiet", "--spider", f"http://127.0.0.1:8080{path}"
        ]

    migrator = services["migrator"]
    migrator["entrypoint"] = ["/opt/surplasse/scripts/backend-migrate.sh"]
    migrator["environment"] = {
        "DEPLOYMENT_PROFILE": "production",
        "QUARKUS_DATASOURCE_JDBC_URL": "jdbc:postgresql://postgresql:5432/surplasse",
        "QUARKUS_DATASOURCE_USERNAME": "surplasse_migrator",
        "QUARKUS_DATASOURCE_PASSWORD_FILE":
            "/run/secrets/surplasse_postgres_migrator_password",
    }
    migrator["secrets"] = [
        {
            "source": "surplasse_postgres_migrator_password",
            "target": "/run/secrets/surplasse_postgres_migrator_password",
        }
    ]
    return document


def validate_app_document(document: dict) -> None:
    COMPOSE_POLICY.validate_compose(
        document["name"],
        document,
        expected_images={
            name: service["image"] for name, service in document["services"].items()
        },
    )


def validate_platform_document(document: dict) -> None:
    COMPOSE_POLICY.validate_compose(
        "vps-platform",
        document,
        repository_root=Path("/repo"),
        structural_only=True,
    )


def secret_definitions(sources: set[str]) -> dict:
    return {
        source: {
            "name": f"vps-platform_{source}",
            "file": f"/etc/vps/secrets/platform/{source.replace('_', '-')}",
        }
        for source in sources
    }


def platform_caddy() -> dict:
    service = hardened_service(
        image("ghcr.io/nclsppr/vps-infra/caddy"),
        {"ops"},
        user=None,
    )
    service["cap_add"] = ["NET_BIND_SERVICE"]
    service["ports"] = [
        {"target": 80, "published": "80", "protocol": "tcp", "host_ip": "0.0.0.0"},
        {"target": 443, "published": "443", "protocol": "tcp", "host_ip": "0.0.0.0"},
    ]
    service["volumes"] = [
        {
            "type": "bind",
            "source": "/repo/platform/caddy/Caddyfile",
            "target": "/etc/caddy/Caddyfile",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/caddy/routes",
            "target": "/etc/caddy/routes",
            "read_only": True,
        },
        {"type": "bind", "source": "/srv/www", "target": "/srv/www", "read_only": True},
        {"type": "volume", "source": "caddy_data", "target": "/data"},
        {"type": "volume", "source": "caddy_config", "target": "/config"},
    ]
    apply_platform_runtime("caddy", service)
    return service


def platform_document(*, include_grafana: bool = True) -> dict:
    del include_grafana
    postgresql = hardened_service(
        image("ghcr.io/nclsppr/vps-infra/postgres"),
        {"db_monitoring"},
        user="70:70",
    )
    postgresql["secrets"] = [
        {"source": "postgres_superuser_password"},
        {"source": "postgres_exporter_password"},
    ]
    postgresql["volumes"] = [
        {"type": "volume", "source": "postgresql_data", "target": "/var/lib/postgresql/data"},
        {
            "type": "bind",
            "source": "/repo/platform/postgres/postgresql.conf",
            "target": "/etc/postgresql/postgresql.conf",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/postgres/pg_hba.conf",
            "target": "/etc/postgresql/pg_hba.conf",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/postgres/initdb",
            "target": "/docker-entrypoint-initdb.d",
            "read_only": True,
        },
    ]
    prometheus = hardened_service(
        image("docker.io/prom/prometheus"),
        {"ops"},
        user=None,
    )
    prometheus["volumes"] = [
        {
            "type": "bind",
            "source": "/repo/platform/prometheus/prometheus.yml",
            "target": "/etc/prometheus/prometheus.yml",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/prometheus/targets",
            "target": "/etc/prometheus/targets",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/prometheus/rules",
            "target": "/etc/prometheus/rules",
            "read_only": True,
        },
        {"type": "volume", "source": "prometheus_data", "target": "/prometheus"},
    ]
    grafana = hardened_service(
        image("docker.io/grafana/grafana"),
        {"ops"},
        user="472:472",
    )
    grafana["secrets"] = [
        {"source": "grafana_admin_password"},
        {"source": "grafana_secret_key"},
    ]
    grafana["healthcheck"]["test"] = list(
        COMPOSE_POLICY.PLATFORM_HEALTHCHECK_TESTS["grafana"]
    )
    grafana["ports"] = [
        {"target": 3000, "published": "3000", "protocol": "tcp", "host_ip": "127.0.0.1"}
    ]
    grafana["volumes"] = [
        {
            "type": "bind",
            "source": "/repo/platform/grafana/provisioning",
            "target": "/etc/grafana/provisioning",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/repo/platform/grafana/dashboards",
            "target": "/etc/grafana/dashboards",
            "read_only": True,
        },
        {"type": "volume", "source": "grafana_data", "target": "/var/lib/grafana"},
    ]
    node_exporter = hardened_service(
        image("docker.io/prom/node-exporter"),
        {"ops"},
        user=None,
    )
    node_exporter["healthcheck"]["test"] = list(
        COMPOSE_POLICY.PLATFORM_HEALTHCHECK_TESTS["node-exporter"]
    )
    node_exporter["pid"] = "host"
    node_exporter["volumes"] = [
        {"type": "bind", "source": "/proc", "target": "/host/proc", "read_only": True},
        {"type": "bind", "source": "/sys", "target": "/host/sys", "read_only": True},
        {"type": "bind", "source": "/", "target": "/rootfs", "read_only": True},
    ]
    postgres_exporter = hardened_service(
        image("docker.io/prometheuscommunity/postgres-exporter"),
        {"ops", "db_monitoring"},
        user="65534:70",
    )
    postgres_exporter["secrets"] = [{"source": "postgres_exporter_password"}]
    services = {
        "caddy": platform_caddy(),
        "postgresql": postgresql,
        "prometheus": prometheus,
        "grafana": grafana,
        "node-exporter": node_exporter,
        "postgres-exporter": postgres_exporter,
    }
    for service_name, service in services.items():
        apply_platform_runtime(service_name, service)
        if service_name == "postgresql":
            service["networks"] = {
                network: {"aliases": ["postgresql"]}
                for network in service["networks"]
            }
        for secret in service.get("secrets", []):
            secret["target"] = f"/run/secrets/{secret['source']}"
        by_target = {
            volume["target"]: volume for volume in service.get("volumes", [])
        }
        for target, contract in COMPOSE_POLICY.PLATFORM_VOLUME_CONTRACTS[service_name].items():
            volume = by_target[target]
            volume_type, source_spec, read_only, options = contract
            source = (
                f"/repo/{source_spec.removeprefix('repo:')}"
                if source_spec.startswith("repo:")
                else source_spec
            )
            volume.update(type=volume_type, source=source)
            if read_only:
                volume["read_only"] = True
            else:
                volume.pop("read_only", None)
            option_key = "bind" if volume_type == "bind" else "volume"
            volume[option_key] = dict(options)
    sources = {
        secret["source"]
        for service in services.values()
        for secret in service.get("secrets", [])
    }
    return {
        "name": "vps-platform",
        "services": services,
        "networks": external_networks("vps-platform"),
        "secrets": secret_definitions(sources),
        "volumes": {
            name: {"name": stable_name}
            for name, stable_name in COMPOSE_POLICY.PLATFORM_NAMED_VOLUMES.items()
        },
    }


def public_static_edge_document() -> dict:
    caddy = platform_caddy()
    caddy["networks"] = {"edge": {}}
    caddy["healthcheck"] = copy.deepcopy(
        COMPOSE_POLICY.PUBLIC_STATIC_EDGE_HEALTHCHECK
    )
    caddy["ports"].append(
        {
            "target": 443,
            "published": "443",
            "protocol": "udp",
            "host_ip": "0.0.0.0",
        }
    )
    by_target = {volume["target"]: volume for volume in caddy["volumes"]}
    for target, contract in COMPOSE_POLICY.PUBLIC_STATIC_EDGE_VOLUME_CONTRACTS[
        "caddy"
    ].items():
        volume = by_target[target]
        volume_type, source, read_only, options = contract
        volume.update(type=volume_type, source=source)
        if read_only:
            volume["read_only"] = True
        else:
            volume.pop("read_only", None)
        option_key = "bind" if volume_type == "bind" else "volume"
        volume[option_key] = dict(options)
    return {
        "name": "vps-public-static-edge",
        "services": {"caddy": caddy},
        "networks": {"edge": {"external": True, "name": "edge"}},
        "secrets": {},
        "volumes": {
            name: {"name": stable_name}
            for name, stable_name in COMPOSE_POLICY.PUBLIC_STATIC_EDGE_NAMED_VOLUMES.items()
        },
    }


class ComposePolicyTests(unittest.TestCase):
    def run_cli(
        self,
        document: dict,
        expected_images: dict | str | None,
        *options: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compose_path = root / "compose.json"
            compose_path.write_text(json.dumps(document), encoding="utf-8")
            command = [sys.executable, str(SCRIPTS / "validate-compose")]
            if expected_images is not None:
                expected_path = root / "expected-images.json"
                expected_payload = (
                    expected_images
                    if isinstance(expected_images, str)
                    else json.dumps(expected_images)
                )
                expected_path.write_text(expected_payload, encoding="utf-8")
                command.extend(["--expected-images", str(expected_path)])
            command.extend(options)
            command.extend([document["name"], str(compose_path)])
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )

    def test_application_without_host_ports_is_valid(self) -> None:
        document = app_document()
        validate_app_document(document)

    def test_surplasse_adapter_enforces_the_one_shot_migration_boundary(self) -> None:
        document = surplasse_adapter_document()
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        SURPLASSE_ADAPTER.validate_compose(document, expected)

        cases = (
            (
                "runtime-auto-migration",
                lambda value: value["services"]["backend"]["environment"].update(
                    QUARKUS_FLYWAY_MIGRATE_AT_START="true"
                ),
                "MIGRATE_AT_START",
            ),
            (
                "shared-database-role",
                lambda value: value["services"]["migrator"]["environment"].update(
                    QUARKUS_DATASOURCE_USERNAME="surplasse_runtime"
                ),
                "USERNAME",
            ),
            (
                "shared-database-secret",
                lambda value: value["services"]["migrator"]["secrets"][0].update(
                    source="surplasse_postgres_runtime_password",
                    target="/run/secrets/surplasse_postgres_runtime_password",
                ),
                "migrator.secrets",
            ),
            (
                "missing-entrypoint",
                lambda value: value["services"]["migrator"].pop("entrypoint"),
                "one-shot command",
            ),
            (
                "generic-backend-alias",
                lambda value: value["services"]["backend"]["networks"][
                    "app_surplasse"
                ].update(aliases=["backend"]),
                "alias differs",
            ),
            (
                "static-secret",
                lambda value: value["services"]["docs"].update(
                    secrets=[
                        {
                            "source": "surplasse_jwt_jwks",
                            "target": "/run/secrets/surplasse_jwt_jwks",
                        }
                    ]
                ),
                "static runtimes receive no secret",
            ),
        )
        for label, mutate, expected_message in cases:
            with self.subTest(label=label):
                changed = copy.deepcopy(document)
                mutate(changed)
                with self.assertRaisesRegex(
                    SURPLASSE_ADAPTER.AdapterError,
                    expected_message,
                ):
                    SURPLASSE_ADAPTER.validate_compose(changed, expected)

        changed = copy.deepcopy(document)
        changed["services"]["migrator"]["image"] = image(
            "ghcr.io/nclsppr/surplasse/backend", DIGEST_D
        )
        changed_expected = dict(expected)
        changed_expected["migrator"] = changed["services"]["migrator"]["image"]
        with self.assertRaisesRegex(
            SURPLASSE_ADAPTER.AdapterError,
            "must equal the Backend image",
        ):
            SURPLASSE_ADAPTER.validate_compose(changed, changed_expected)

    def test_surplasse_adapter_metadata_matches_disabled_candidates(self) -> None:
        application = ROOT / "applications/surplasse"
        adapter = json.loads((application / "adapter.json").read_text(encoding="utf-8"))
        migrations = json.loads(
            (application / "migrations.json").read_text(encoding="utf-8")
        )
        expected_images = json.loads(
            (application / "expected-images.json").read_text(encoding="utf-8")
        )
        SURPLASSE_ADAPTER.validate_metadata(
            ROOT, adapter, migrations, expected_images
        )

    def test_surplasse_adapter_revision_is_bound_to_every_image_tag(self) -> None:
        application = ROOT / "applications/surplasse"
        adapter = json.loads((application / "adapter.json").read_text(encoding="utf-8"))
        migrations = json.loads(
            (application / "migrations.json").read_text(encoding="utf-8")
        )
        expected_images = json.loads(
            (application / "expected-images.json").read_text(encoding="utf-8")
        )

        changed_adapter = copy.deepcopy(adapter)
        changed_migrations = copy.deepcopy(migrations)
        changed_adapter["source_revision"] = SHA_B
        changed_migrations["source_revision"] = SHA_B
        with self.assertRaisesRegex(
            SURPLASSE_ADAPTER.AdapterError,
            "image tag must equal the adapter source revision",
        ):
            SURPLASSE_ADAPTER.validate_metadata(
                ROOT, changed_adapter, changed_migrations, expected_images
            )

        original_revision = adapter["source_revision"]
        for name in expected_images:
            with self.subTest(name=name):
                changed_images = copy.deepcopy(expected_images)
                changed_images[name] = changed_images[name].replace(
                    f":{original_revision}@sha256:", f":{SHA_B}@sha256:"
                )
                with self.assertRaisesRegex(
                    SURPLASSE_ADAPTER.AdapterError,
                    f"expected-images.{name}: image tag must equal",
                ):
                    SURPLASSE_ADAPTER.validate_metadata(
                        ROOT, adapter, migrations, changed_images
                    )

    def test_surplasse_adapter_requires_exact_component_repositories(self) -> None:
        application = ROOT / "applications/surplasse"
        adapter = json.loads((application / "adapter.json").read_text(encoding="utf-8"))
        migrations = json.loads(
            (application / "migrations.json").read_text(encoding="utf-8")
        )
        expected_images = json.loads(
            (application / "expected-images.json").read_text(encoding="utf-8")
        )
        expected_images["backend"] = expected_images["backend"].replace(
            "ghcr.io/nclsppr/surplasse/backend",
            "ghcr.io/nclsppr/surplasse/backend-copy",
        )
        with self.assertRaisesRegex(
            SURPLASSE_ADAPTER.AdapterError,
            "expected-images.backend: must be the exact immutable",
        ):
            SURPLASSE_ADAPTER.validate_metadata(
                ROOT, adapter, migrations, expected_images
            )

    def test_surplasse_make_target_stops_after_shared_policy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scripts = root / "scripts"
            scripts.mkdir()
            fake_compose = root / "fake-compose"
            shared_validator = scripts / "validate-compose"
            adapter_validator = scripts / "validate-surplasse-adapter"
            adapter_marker = root / "adapter-validator-ran"
            fake_compose.write_text(
                "#!/usr/bin/env bash\nprintf '{}\\n'\n",
                encoding="utf-8",
            )
            shared_validator.write_text(
                "#!/usr/bin/env bash\nexit 73\n",
                encoding="utf-8",
            )
            adapter_validator.write_text(
                f"#!/usr/bin/env bash\ntouch -- {adapter_marker}\n",
                encoding="utf-8",
            )
            for executable in (fake_compose, shared_validator, adapter_validator):
                executable.chmod(0o755)

            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "--file",
                    str(ROOT / "Makefile"),
                    "check-surplasse-adapter",
                    f"COMPOSE={fake_compose}",
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(
                adapter_marker.exists(),
                "the specialized validator ran after the shared policy failed",
            )

    def test_public_static_edge_is_a_caddy_only_verified_unit(self) -> None:
        document = public_static_edge_document()
        expected = {"caddy": document["services"]["caddy"]["image"]}
        COMPOSE_POLICY.validate_compose(
            "vps-public-static-edge",
            document,
            expected_images=expected,
        )

        document["services"]["grafana"] = copy.deepcopy(
            document["services"]["caddy"]
        )
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "exactly caddy"):
            COMPOSE_POLICY.validate_compose(
                "vps-public-static-edge",
                document,
                expected_images=expected,
            )

    def test_public_static_edge_requires_exact_runtime_contract(self) -> None:
        cases = (
            (
                lambda document: document["services"]["caddy"]["ports"].pop(),
                "requires exactly 80/tcp, 443/tcp, and 443/udp",
            ),
            (
                lambda document: document["services"]["caddy"]["volumes"][0].update(
                    source="/tmp/Caddyfile"
                ),
                "expected exact bind source",
            ),
            (
                lambda document: document["services"]["caddy"].update(
                    secrets=[
                        {
                            "source": "ovh_application_secret",
                            "target": "/run/secrets/ovh_application_secret",
                        }
                    ]
                ),
                "must not receive secrets",
            ),
            (
                lambda document: document["services"]["caddy"].update(
                    entrypoint=["/bin/sh"]
                ),
                "forbids runtime overrides: entrypoint",
            ),
            (
                lambda document: document["services"]["caddy"].update(
                    command=["caddy", "run"]
                ),
                "forbids runtime overrides: command",
            ),
            (
                lambda document: document["services"]["caddy"].update(
                    environment={"CADDY_DEBUG": "1"}
                ),
                "forbids runtime overrides: environment",
            ),
            (
                lambda document: document["services"]["caddy"][
                    "healthcheck"
                ].update(interval="16s"),
                "requires the exact reviewed probe",
            ),
            (
                lambda document: document["services"]["caddy"][
                    "healthcheck"
                ].update(test=["CMD", "wget", "http://127.0.0.1/"]),
                "requires the exact reviewed probe",
            ),
            (
                lambda document: document["services"]["caddy"].update(
                    networks={"ops": {}}
                ),
                "networks: expected edge",
            ),
        )
        for mutation, message in cases:
            with self.subTest(message=message):
                document = public_static_edge_document()
                expected = {"caddy": document["services"]["caddy"]["image"]}
                mutation(document)
                if document["services"]["caddy"].get("secrets"):
                    document["secrets"] = {
                        "ovh_application_secret": {
                            "name": "vps-platform_ovh_application_secret",
                            "file": "/etc/vps/secrets/platform/ovh-application-secret",
                        }
                    }
                with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, message):
                    COMPOSE_POLICY.validate_compose(
                        "vps-public-static-edge",
                        document,
                        expected_images=expected,
                    )

    def test_public_static_edge_rejects_named_volume_options(self) -> None:
        cases = (
            {
                "driver": "local",
                "driver_opts": {
                    "type": "none",
                    "o": "bind",
                    "device": "/",
                },
            },
            {"external": True},
            {"labels": {"com.example.scope": "unexpected"}},
        )
        for extra_options in cases:
            with self.subTest(extra_options=extra_options):
                document = public_static_edge_document()
                expected = {"caddy": document["services"]["caddy"]["image"]}
                document["volumes"]["caddy_data"].update(extra_options)
                with self.assertRaisesRegex(
                    COMPOSE_POLICY.ComposePolicyError,
                    "public static edge requires exactly",
                ):
                    COMPOSE_POLICY.validate_compose(
                        "vps-public-static-edge",
                        document,
                        expected_images=expected,
                    )

    def test_cli_accepts_verified_application_images(self) -> None:
        document = app_document()
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        result = self.run_cli(document, expected)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compose policy valid: surplasse", result.stdout)

    def test_cli_rejects_incomplete_or_changed_expected_images(self) -> None:
        document = app_document()
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        missing = dict(expected)
        missing.pop("backend")
        changed = dict(expected)
        changed["backend"] = image(
            "ghcr.io/nclsppr/surplasse/backend", DIGEST_D
        )
        cases = (
            (missing, "expected-image contract is incomplete"),
            (changed, "differs from the verified release-manifest reference"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                result = self.run_cli(document, payload)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_cli_strictly_validates_expected_images_json(self) -> None:
        document = app_document()
        backend_image = document["services"]["backend"]["image"]
        cases = (
            (
                '{"backend":'
                + json.dumps(backend_image)
                + ',"backend":'
                + json.dumps(backend_image)
                + "}",
                "duplicate JSON object key",
            ),
            ("[]", "must be a JSON object"),
            (
                json.dumps({"backend": "ghcr.io/nclsppr/surplasse/backend:latest"}),
                "must be an immutable sha256 image reference",
            ),
            (
                " " * (COMPOSE_POLICY.MAX_EXPECTED_IMAGES_BYTES + 1),
                "file exceeds the",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                result = self.run_cli(document, payload)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_cli_expected_images_and_structural_lint_are_mutually_exclusive(self) -> None:
        document = app_document()
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        result = self.run_cli(document, expected, "--structural-only")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_application_compose_is_locked_without_verified_release_binding(self) -> None:
        document = app_document()
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "integration bundle"):
            COMPOSE_POLICY.validate_compose("surplasse", document)

        document["services"] = {
            "evil": hardened_service(
                image("ghcr.io/nclsppr/surplasse/evil"),
                {"app_surplasse"},
            )
        }
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "requires exactly"):
            COMPOSE_POLICY.validate_compose("surplasse", document)

    def test_application_image_must_match_verified_release_binding(self) -> None:
        document = app_document()
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        document["services"]["backend"]["image"] = image(
            "ghcr.io/nclsppr/surplasse/backend", DIGEST_D
        )
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "differs from"):
            COMPOSE_POLICY.validate_compose(
                "surplasse",
                document,
                expected_images=expected,
            )

    def test_application_published_port_is_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["ports"] = [
            {"target": 8080, "published": "8080", "protocol": "tcp"}
        ]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "may not publish host ports"):
            validate_app_document(document)

    def test_platform_ports_are_narrowly_allowed(self) -> None:
        document = platform_document(include_grafana=True)
        validate_platform_document(document)

    def test_platform_identity_and_audited_probes_are_exact(self) -> None:
        mutations = (
            (
                "postgres-root-entrypoint",
                lambda document: document["services"]["postgresql"].update(user=None),
                "audited platform image contract",
            ),
            (
                "postgres-capability",
                lambda document: document["services"]["postgresql"].update(
                    cap_add=["CHOWN"]
                ),
                "capabilities outside",
            ),
            (
                "postgres-runtime-directory-owner",
                lambda document: document["services"]["postgresql"]["tmpfs"].__setitem__(
                    1,
                    "/var/run/postgresql:size=16m,mode=2775,uid=999,gid=999",
                ),
                "audited platform allowlist",
            ),
            (
                "exporter-group",
                lambda document: document["services"]["postgres-exporter"].update(
                    user="65534:999"
                ),
                "audited platform image contract",
            ),
            (
                "grafana-probe",
                lambda document: document["services"]["grafana"]["healthcheck"].update(
                    test=[
                        "CMD",
                        "wget",
                        "--spider",
                        "http://127.0.0.1:3000/api/health",
                    ]
                ),
                "audited platform probe",
            ),
            (
                "node-exporter-metrics-probe",
                lambda document: document["services"]["node-exporter"]["healthcheck"].update(
                    test=[
                        "CMD",
                        "wget",
                        "--quiet",
                        "--spider",
                        "http://127.0.0.1:9100/metrics",
                    ]
                ),
                "audited platform probe",
            ),
        )
        for label, mutate, expected_message in mutations:
            with self.subTest(label=label):
                document = platform_document()
                mutate(document)
                with self.assertRaisesRegex(
                    COMPOSE_POLICY.ComposePolicyError,
                    expected_message,
                ):
                    validate_platform_document(document)

    def test_platform_images_require_a_verified_binding_outside_structural_lint(self) -> None:
        document = platform_document()
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "structural-only"):
            COMPOSE_POLICY.validate_compose(
                "vps-platform",
                document,
                repository_root=Path("/repo"),
            )
        expected = {
            name: service["image"] for name, service in document["services"].items()
        }
        document["services"]["caddy"]["image"] = image("evil.example/attacker/rootkit")
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "verified release-manifest"):
            COMPOSE_POLICY.validate_compose(
                "vps-platform",
                document,
                expected_images=expected,
                repository_root=Path("/repo"),
            )

    def test_platform_ipv6_bindings_remain_forbidden_until_explicitly_supported(self) -> None:
        document = platform_document()
        document["services"]["caddy"]["ports"][0]["host_ip"] = "::"
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "IPv4 0.0.0.0"):
            validate_platform_document(document)

        document = platform_document()
        document["services"]["grafana"]["ports"][0]["host_ip"] = "::1"
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "IPv4 127.0.0.1"):
            validate_platform_document(document)

    def test_every_platform_mount_source_type_and_option_is_exact(self) -> None:
        mutations = [
            (
                "caddy-source",
                lambda document: document["services"]["caddy"]["volumes"][0].update(
                    source="/tmp/unreviewed-Caddyfile"
                ),
            ),
            (
                "postgres-volume",
                lambda document: document["services"]["postgresql"]["volumes"][0].update(
                    source="other_database"
                ),
            ),
            (
                "root-propagation",
                lambda document: document["services"]["node-exporter"]["volumes"][2].update(
                    bind={"create_host_path": False, "propagation": "rshared"}
                ),
            ),
            (
                "implicit-bind-source-creation",
                lambda document: document["services"]["caddy"]["volumes"][0].update(
                    bind={"create_host_path": True}
                ),
            ),
            (
                "omitted-bind-source-creation-policy",
                lambda document: document["services"]["caddy"]["volumes"][0].update(
                    bind={}
                ),
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = platform_document()
                mutate(document)
                with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "exact platform|expected exact"):
                    validate_platform_document(document)

    def test_missing_healthcheck_is_rejected(self) -> None:
        document = app_document()
        del document["services"]["backend"]["healthcheck"]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "healthcheck: required"):
            validate_app_document(document)

    def test_platform_missing_public_edge_port_is_rejected(self) -> None:
        document = platform_document()
        document["services"]["caddy"]["ports"] = [
            {"target": 443, "published": "443", "protocol": "tcp", "host_ip": "0.0.0.0"}
        ]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "80/tcp and 443/tcp"):
            validate_platform_document(document)

    def test_build_on_vps_is_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["build"] = "."
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "may not build"):
            validate_app_document(document)

    def test_dangerous_runtime_settings_are_rejected(self) -> None:
        mutations = [
            ("root-user", lambda service: service.update(user="0:0"), "explicit root"),
            ("host-pid", lambda service: service.update(pid="host"), "host namespace"),
            ("host-userns", lambda service: service.update(userns_mode="host"), "user namespace"),
            ("host-cgroup", lambda service: service.update(cgroup="host"), "cgroup access"),
            ("cgroup-parent", lambda service: service.update(cgroup_parent="/system.slice"), "cgroup access"),
            ("docker-api", lambda service: service.update(use_api_socket=True), "Docker API"),
            ("sys-admin", lambda service: service.update(cap_add=["SYS_ADMIN"]), "capabilities outside"),
            (
                "unconfined-seccomp",
                lambda service: service.update(
                    security_opt=["no-new-privileges:true", "seccomp=unconfined"]
                ),
                "unconfined",
            ),
            ("writable-root", lambda service: service.update(read_only=False), "read_only"),
            ("missing-cap-drop", lambda service: service.pop("cap_drop"), "cap_drop"),
            ("missing-resource-limit", lambda service: service.pop("mem_limit"), "mem_limit"),
        ]
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                document = app_document()
                mutate(document["services"]["backend"])
                with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, message):
                    validate_app_document(document)

    def test_bind_mounts_and_cross_project_networks_are_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["volumes"] = [
            {"type": "bind", "source": "/", "target": "/rootfs", "read_only": False}
        ]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "may not use host"):
            validate_app_document(document)
        document = app_document()
        document["services"]["backend"]["networks"] = {"ops": {}}
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "expected app_surplasse"):
            validate_app_document(document)

    def test_trivial_healthcheck_is_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["healthcheck"]["test"] = ["CMD", "true"]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "always-success"):
            validate_app_document(document)

    def test_lifecycle_scaling_host_and_dns_bypasses_are_rejected(self) -> None:
        mutations = [
            ("post-start", {"post_start": [{"command": "id"}]}, "lifecycle hooks"),
            ("pre-stop", {"pre_stop": [{"command": "id"}]}, "lifecycle hooks"),
            ("scale", {"scale": 0}, "scaling overrides"),
            ("deploy-replicas", {"deploy": {"replicas": 0}}, "scaling overrides"),
            ("host-gateway", {"extra_hosts": ["host.docker.internal:host-gateway"]}, "host and DNS"),
            ("dns", {"dns": ["1.1.1.1"]}, "host and DNS"),
            ("oom-kill", {"oom_kill_disable": True}, "oom_kill_disable"),
        ]
        for label, addition, expected in mutations:
            with self.subTest(label=label):
                document = app_document()
                document["services"]["backend"].update(addition)
                with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, expected):
                    validate_app_document(document)

    def test_hidden_long_running_profile_and_oversized_resources_are_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["profiles"] = ["never"]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "may not be hidden"):
            validate_app_document(document)

        for key, value in (("cpus", 128.0), ("pids_limit", 100000), ("mem_limit", str(64 << 30))):
            with self.subTest(key=key):
                document = app_document()
                document["services"]["backend"][key] = value
                with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "policy budget"):
                    validate_app_document(document)

    def test_unbounded_logging_and_healthcheck_are_rejected(self) -> None:
        document = app_document()
        document["services"]["backend"]["logging"] = {"driver": "json-file"}
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "bounded local"):
            validate_app_document(document)

        document = app_document()
        document["services"]["backend"]["healthcheck"]["interval"] = "24h"
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "between 1s and 300s"):
            validate_app_document(document)

    def test_secret_traversal_and_unknown_top_level_are_rejected(self) -> None:
        document = platform_document()
        document["secrets"]["postgres_superuser_password"]["file"] = (
            "/etc/vps/secrets/platform/../platform/postgres-superuser-password"
        )
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "exact normalized path"):
            validate_platform_document(document)

        document = app_document()
        document["include"] = ["unreviewed.yaml"]
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "unsupported top-level"):
            validate_app_document(document)

    def test_network_options_and_secret_target_overrides_are_rejected(self) -> None:
        document = platform_document()
        document["services"]["postgresql"]["networks"]["db_monitoring"] = {
            "aliases": ["postgresql"],
            "ipv4_address": "172.30.11.10",
        }
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "exact contract"):
            validate_platform_document(document)

        document = platform_document()
        document["services"]["postgresql"]["secrets"][0].update(
            target="/etc/passwd",
            uid="0",
            gid="0",
            mode=0o777,
        )
        with self.assertRaisesRegex(COMPOSE_POLICY.ComposePolicyError, "source and target keys"):
            validate_platform_document(document)


class PublicSafetyTests(unittest.TestCase):
    def run_check(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPTS / "check-public-safe"), "--root", str(root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_documented_placeholders_do_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            (root / "docs" / "example.md").write_text(
                "OVH_APPLICATION_SECRET=<secret>\n"
                "Escaped BEGIN-[PRIVATE-KEY] is only a marker.\n",
                encoding="utf-8",
            )
            result = self.run_check(root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_env_private_key_and_inventory_are_rejected(self) -> None:
        cases = {
            ".env": "SAFE=value\n",
            "config.txt": "-----BEGIN " + "PRIVATE KEY-----\nnot-a-real-key\n",
            "ansible/inventories/production/hosts.yaml": "all: {}\n",
        }
        for relative, content in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                result = self.run_check(root)
                self.assertEqual(result.returncode, 1)

    def test_real_token_in_markdown_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "accidental token: " + "github_pat_" + ("A" * 24) + "\n",
                encoding="utf-8",
            )
            result = self.run_check(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("GitHub token", result.stderr)

    def test_generic_sensitive_assignments_are_rejected(self) -> None:
        cases = {
            "VPS_SSH_KEY": "unencrypted-private-material",
            "POSTGRES_SUPERUSER_PASSWORD": "plaintext-password",
            "GF_SECURITY_SECRET_KEY": "plaintext-secret-key",
            "DEPLOY_TOKEN": "plaintext-deploy-token",
            "DATABASE_URL": "postgresql://admin:plaintext@db/prod",
            "FALLBACK_SECRET": "${FALLBACK_SECRET:-plaintext}",
            "password": "lowercase-plaintext",
            "JINJA_PASSWORD": "{{ 'plaintext' }}",
        }
        for key, value in cases.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "config.example").write_text(f"{key}={value}\n", encoding="utf-8")
                result = self.run_check(root)
                self.assertEqual(result.returncode, 1)
                self.assertIn("credential-like assignment", result.stderr)

    def test_file_references_and_explicit_placeholders_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.example").write_text(
                "POSTGRES_SUPERUSER_PASSWORD_FILE=/run/secrets/postgres-password\n"
                "GF_SECURITY_SECRET_KEY=${GF_SECRET_FROM_ENV}\n"
                "DEPLOY_TOKEN=<secret>\n"
                "VPS_SSH_KEY=${{ secrets.VPS_SSH_KEY }}\n"
                "DATABASE_URL=REDACTED\n"
                "SERVICE_PASSWORD={{ vault_service_password }}\n",
                encoding="utf-8",
            )
            result = self.run_check(root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_git_mode_scans_tracked_and_untracked_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "tracked.txt").write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            (root / "untracked.txt").write_text("also safe\n", encoding="utf-8")
            result = self.run_check(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2 repository files inspected", result.stdout)
            (root / "untracked.txt").write_text(
                "accidental token: " + "github_pat_" + ("B" * 24) + "\n",
                encoding="utf-8",
            )
            result = self.run_check(root)
            self.assertEqual(result.returncode, 1)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.geteuid() == 0:
            if os.environ.get("VPS_CONTROLLER_ROOT_SAFE_SKIP") == "1":
                raise unittest.SkipTest(
                    "controller integration tests require an unprivileged user"
                )
            raise PermissionError(
                "ControllerTests require an unprivileged user; run tests/run"
            )

    def create_repository(self, root: Path) -> tuple[Path, Path, str]:
        source = root / "source"
        remote = root / "remote.git"
        repository = root / "repository"
        (source / "releases").mkdir(parents=True)
        (source / "releases" / "production.yaml").write_text(
            MANIFEST.read_text(encoding="utf-8"), encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "releases/production.yaml"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=VPS tests",
                "-c",
                "user.email=vps-tests@example.invalid",
                "commit",
                "-q",
                "-m",
                "test release",
            ],
            check=True,
        )
        sha = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(
            ["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True
        )
        subprocess.run(
            ["git", "-C", str(source), "push", "-q", "-u", "origin", "main"], check=True
        )
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(remote), str(repository)],
            check=True,
        )
        return source, repository, sha

    def controller_env(self, root: Path, repository: Path) -> dict[str, str]:
        evidence_verifier = root / "evidence-verifier"
        evidence_verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        evidence_verifier.chmod(0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "VPS_CONTROLLER_TESTING": "1",
                "VPS_REPOSITORY_DIR": str(repository),
                "VPS_STATE_DIR": str(root / "state"),
                "VPS_LOCK_DIR": str(root / "lock" / "deploy.lock"),
                "VPS_PRODUCTION_ENABLE_FILE": str(root / "production-enabled"),
                "VPS_VALIDATE_RELEASE": str(SCRIPTS / "validate-release"),
                "VPS_RECONCILE": str(SCRIPTS / "reconcile"),
                "VPS_STATE_VERIFIER": str(SCRIPTS / "verify-state"),
                "VPS_EVIDENCE_VERIFIER": str(evidence_verifier),
                "VPS_EXPECTED_ORIGIN": subprocess.check_output(
                    ["git", "-C", str(repository), "remote", "get-url", "origin"], text=True
                ).strip(),
                "VPS_SCHEMA_FILE": str(SCHEMA),
                "GIT_CONFIG_PARAMETERS": "'protocol.file.allow=never'",
            }
        )
        return environment

    def test_dry_run_records_desired_but_never_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            environment["DRY_RUN"] = "0"
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 78, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["mode"], "dry-run")
            self.assertEqual(response["result"], "validated")
            self.assertTrue((root / "state" / "desired" / "state.json").is_file())
            self.assertFalse((root / "state" / "active").exists())
            plan = json.loads((root / "state" / "plans" / f"{sha}.json").read_text())
            self.assertFalse(plan["automatic_migrations"])

    def test_candidate_evidence_is_verified_before_desired_state_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, repository, base_sha = self.create_repository(root)
            manifest = sample_manifest()
            add_platform_candidate(manifest)
            set_platform_candidate_revision(manifest, base_sha)
            (source / "releases" / "production.yaml").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "releases/production.yaml"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "platform candidate",
                ],
                check=True,
            )
            sha = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(
                ["git", "-C", str(source), "push", "-q", "origin", "main"],
                check=True,
            )
            environment = self.controller_env(root, repository)
            evidence_verifier = root / "candidate-evidence-verifier"
            evidence_verifier.write_text(
                "#!/bin/sh\n"
                "if [ -e \"${VPS_STATE_DIR}/desired\" ]; then exit 91; fi\n"
                "touch \"${VPS_STATE_DIR}/candidate-evidence-verified\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            evidence_verifier.chmod(0o700)
            environment["VPS_EVIDENCE_VERIFIER"] = str(evidence_verifier)
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertTrue((root / "state" / "candidate-evidence-verified").is_file())
            self.assertTrue((root / "state" / "desired" / "state.json").is_file())
            self.assertFalse((root / "state" / "active").exists())
            plan = json.loads(
                (root / "state" / "plans" / f"{sha}.json").read_text(encoding="utf-8")
            )
            self.assertFalse(plan["changes_required"])
            self.assertTrue(
                all(action["action"] == "unchanged-disabled" for action in plan["actions"])
            )
            self.assertTrue(
                all("desired_references" not in action for action in plan["actions"])
            )

    def test_candidate_integration_revision_must_be_ancestor_of_requested_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, repository, _ = self.create_repository(root)
            subprocess.run(
                ["git", "-C", str(source), "switch", "-q", "-c", "integration"],
                check=True,
            )
            (source / "integration-source.txt").write_text("candidate source\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(source), "add", "integration-source.txt"], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "unrelated integration source",
                ],
                check=True,
            )
            unrelated_sha = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(
                ["git", "-C", str(source), "push", "-q", "origin", "integration"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "-q",
                    "origin",
                    "integration:refs/remotes/origin/integration",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "switch", "-q", "main"], check=True
            )
            manifest = sample_manifest()
            add_platform_candidate(manifest)
            set_platform_candidate_revision(manifest, unrelated_sha)
            (source / "releases" / "production.yaml").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "releases/production.yaml"], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "candidate with unrelated source",
                ],
                check=True,
            )
            requested_sha = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(
                ["git", "-C", str(source), "push", "-q", "origin", "main"], check=True
            )
            environment = self.controller_env(root, repository)
            verifier = root / "unexpected-evidence-verifier"
            verifier.write_text(
                f"#!/bin/sh\ntouch {root / 'evidence-called'}\nexit 0\n",
                encoding="utf-8",
            )
            verifier.chmod(0o700)
            environment["VPS_EVIDENCE_VERIFIER"] = str(verifier)
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), requested_sha],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not an ancestor", result.stderr)
            self.assertFalse((root / "evidence-called").exists())
            self.assertFalse((root / "state" / "desired").exists())
            self.assertFalse((root / "state" / "plans" / f"{requested_sha}.json").exists())

    def test_evidence_failure_leaves_no_desired_or_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            evidence_verifier = root / "reject-evidence"
            evidence_verifier.write_text(
                "#!/bin/sh\n"
                "if [ -e \"${VPS_STATE_DIR}/desired\" ]; then exit 91; fi\n"
                "exit 23\n",
                encoding="utf-8",
            )
            evidence_verifier.chmod(0o700)
            environment["VPS_EVIDENCE_VERIFIER"] = str(evidence_verifier)
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("evidence verification failed", result.stderr)
            self.assertFalse((root / "state" / "desired").exists())
            self.assertFalse((root / "state" / "active").exists())
            self.assertFalse((root / "state" / "plans" / f"{sha}.json").exists())

    def test_existing_lock_and_quarantine_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            lock = Path(environment["VPS_LOCK_DIR"])
            lock.mkdir(parents=True)
            (lock / "owner").write_text("12345\n", encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha], env=environment, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 75)
            lock.rename(root / "old-lock")
            quarantine = root / "state" / "quarantine" / "commits"
            quarantine.mkdir(parents=True, exist_ok=True)
            (quarantine / f"{sha}.json").write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha], env=environment, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("quarantined", result.stderr)

    def test_duplicate_or_incomplete_persisted_state_blocks_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            first = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(first.returncode, 78, first.stderr)
            state_file = root / "state" / "desired" / "state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state_file.write_text(
                json.dumps({"schema": state["schema"]})[:-1]
                + f',"schema":1,"commit":"{state["commit"]}",'
                + f'"manifest_sha256":"{state["manifest_sha256"]}",'
                + f'"recorded_at":"{state["recorded_at"]}"}}\n',
                encoding="utf-8",
            )
            second = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("state integrity", second.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            first = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(first.returncode, 78, first.stderr)
            restrictive_schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
            restrictive_schema["properties"]["environment"]["const"] = "staging"
            restrictive_schema_path = root / "restrictive-schema.json"
            restrictive_schema_path.write_text(
                json.dumps(restrictive_schema), encoding="utf-8"
            )
            environment["VPS_SCHEMA_FILE"] = str(restrictive_schema_path)
            second = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("state integrity", second.stderr)
            self.assertIn("persisted manifest failed release validation", second.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            active = root / "state" / "active"
            active.mkdir(parents=True)
            (active / "state.json").write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state pair is incomplete", result.stderr)

    def test_locked_policy_refuses_marker_without_creating_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            Path(environment["VPS_PRODUCTION_ENABLE_FILE"]).touch()
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha], env=environment, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("activation_policy=locked", result.stderr)
            self.assertFalse((root / "state" / "desired").exists())
            self.assertFalse((root / "state" / "active").exists())

    def test_locked_policy_never_calls_an_installed_applicator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            Path(environment["VPS_PRODUCTION_ENABLE_FILE"]).touch()
            applicator = root / "apply-release"
            applicator.write_text(
                f"#!/bin/sh\ntouch {root / 'applicator-called'}\nexit 0\n",
                encoding="utf-8",
            )
            applicator.chmod(0o700)
            environment["VPS_APPLY_EXECUTABLE"] = str(applicator)
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha], env=environment, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("activation_policy=locked", result.stderr)
            self.assertFalse((root / "applicator-called").exists())
            self.assertFalse((root / "state" / "desired").exists())
            self.assertFalse((root / "state" / "active").exists())

    def test_bounded_fetch_makes_a_new_main_commit_deployable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, repository, _ = self.create_repository(root)
            manifest = json.loads((source / "releases" / "production.yaml").read_text())
            (source / "releases" / "production.yaml").write_text(
                json.dumps(manifest, indent=4) + "\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(source), "add", "releases/production.yaml"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "new desired state",
                ],
                check=True,
            )
            new_sha = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "main"], check=True)
            before = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "-e", f"{new_sha}^{{commit}}"],
                check=False,
                stderr=subprocess.DEVNULL,
            )
            self.assertNotEqual(before.returncode, 0)
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), new_sha],
                env=self.controller_env(root, repository),
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 78, result.stderr)
            after = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "-e", f"{new_sha}^{{commit}}"],
                check=False,
            )
            self.assertEqual(after.returncode, 0)

    def test_origin_url_is_not_accepted_from_repository_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, repository, sha = self.create_repository(root)
            environment = self.controller_env(root, repository)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "set-url", "origin", str(root / "other.git")],
                check=True,
            )
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), sha], env=environment, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("origin URL differs", result.stderr)

    def test_reconcile_refusal_does_not_promote_requested_state_to_desired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, repository, _ = self.create_repository(root)
            manifest = sample_manifest()
            enable_parkventory(manifest)
            (source / "releases" / "production.yaml").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(source), "add", "releases/production.yaml"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "quarantined desired state",
                ],
                check=True,
            )
            requested = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "main"], check=True)
            environment = self.controller_env(root, repository)
            artifact_quarantine = root / "state" / "quarantine" / "artifacts"
            artifact_quarantine.mkdir(parents=True)
            (artifact_quarantine / f"{DIGEST_A}.json").write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "deploy"), requested],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("activation_policy", result.stderr)
            self.assertFalse((root / "state" / "desired" / "state.json").exists())

if __name__ == "__main__":
    unittest.main(verbosity=2)
