#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import sys
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST_PATH = ROOT / "releases" / "production.yaml"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = load_script_module("github_evidence_policy", SCRIPTS / "verify-github-evidence")
RELEASE_POLICY = load_script_module("github_evidence_release_policy", SCRIPTS / "lib" / "release_policy.py")


def sample_manifest() -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not manifest["platform"]["enabled"]:
        manifest["platform"]["blocked_by"] = sorted(RELEASE_POLICY.PLATFORM_READINESS_GATES)
    for name in ("personal", "papersempire"):
        if not manifest["applications"][name]["enabled"]:
            manifest["applications"][name]["blocked_by"] = sorted(
                RELEASE_POLICY.STATIC_READINESS_GATES
            )
    if not manifest["applications"]["surplasse"]["enabled"]:
        manifest["applications"]["surplasse"]["blocked_by"] = sorted(
            RELEASE_POLICY.SURPLASSE_READINESS_GATES
        )
    if not manifest["applications"]["parkventory"]["enabled"]:
        manifest["applications"]["parkventory"]["blocked_by"] = sorted(
            RELEASE_POLICY.PARKVENTORY_READINESS_GATES
        )
    return manifest


def image(repository: str, digest: str = DIGEST_A, tag: str = "test") -> str:
    return f"{repository}:{tag}@sha256:{digest}"


def evidence(
    repository: str,
    revision: str,
    run_id: str,
    *,
    run_attempt: int = 1,
) -> dict:
    return {
        "source_revision": revision,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_path": VERIFY.REQUIRED_WORKFLOW_PATH,
        "conclusion": "success",
        "url": f"https://github.com/{repository}/actions/runs/{run_id}",
    }


def enable_platform(manifest: dict, run_id: str = "101") -> None:
    manifest["platform"] = {
        "enabled": True,
        "compose_project": "vps-platform",
        "published_ports": ["80/tcp", "443/tcp"],
        "blocked_by": [],
        "images": {
            "caddy": image("ghcr.io/nclsppr/vps-infra/caddy"),
            "postgres": image("docker.io/library/postgres", tag="17"),
            "prometheus": image("docker.io/prom/prometheus"),
            "grafana": image("docker.io/grafana/grafana"),
            "node_exporter": image("docker.io/prom/node-exporter"),
            "postgres_exporter": image("docker.io/prometheuscommunity/postgres-exporter"),
        },
        "integration": {
            "source_revision": SHA_A,
            "artifact": image("ghcr.io/nclsppr/vps-infra/platform-integration", DIGEST_B),
        },
        "postgres": {"major": 17, "pgdata": "/var/lib/postgresql/data/pgdata"},
        "readiness_evidence": {
            gate: evidence("nclsppr/vps-infra", SHA_A, run_id)
            for gate in RELEASE_POLICY.PLATFORM_READINESS_GATES
        },
    }


def enable_parkventory(manifest: dict) -> None:
    enable_platform(manifest)
    app = manifest["applications"]["parkventory"]
    app.update(
        {
            "enabled": True,
            "blocked_by": [],
            "components": {
                "backend": {
                    "source_revision": SHA_A,
                    "image": image("ghcr.io/nclsppr/parkventory/backend"),
                },
                "frontend": {
                    "source_revision": SHA_B,
                    "image": image("ghcr.io/nclsppr/parkventory/frontend", DIGEST_B),
                },
            },
            "integration": {
                "source_revision": SHA_A,
                "artifact": image("ghcr.io/nclsppr/parkventory/vps-integration"),
            },
            "migrations": {
                "strategy": "dedicated",
                "runtime_auto_migrate": False,
                "proven": True,
                "evidence": evidence("nclsppr/parkventory", SHA_A, "202"),
            },
            "readiness_evidence": {
                gate: evidence("nclsppr/parkventory", SHA_B, "303")
                for gate in RELEASE_POLICY.PARKVENTORY_READINESS_GATES
            },
        }
    )


def enable_personal(manifest: dict) -> None:
    enable_platform(manifest)
    app = manifest["applications"]["personal"]
    app.update(
        {
            "enabled": True,
            "blocked_by": [],
            "source_revision": SHA_B,
            "artifact": image("ghcr.io/nclsppr/personal/site"),
            "route_inventory_artifact": image("ghcr.io/nclsppr/personal/routes", DIGEST_B),
            "readiness_evidence": {
                gate: evidence("nclsppr/personal", SHA_B, "404")
                for gate in RELEASE_POLICY.STATIC_READINESS_GATES
            },
        }
    )


def api_run(
    repository: str,
    branch: str,
    revision: str,
    run_id: str,
    *,
    event: str = "push",
    run_attempt: int = 1,
    workflow_id: int = 77,
) -> dict:
    return {
        "id": int(run_id),
        "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "head_sha": revision,
        "head_branch": branch,
        "head_repository": {"full_name": repository},
        "status": "completed",
        "conclusion": "success",
        "event": event,
        "run_attempt": run_attempt,
        "workflow_id": workflow_id,
        "workflow_url": (
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_id}"
        ),
        "path": VERIFY.REQUIRED_WORKFLOW_PATH,
        "repository": {"full_name": repository},
    }


def api_workflow(repository: str, workflow_id: int = 77) -> dict:
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_id}"
    return {
        "id": workflow_id,
        "url": url,
        "path": VERIFY.REQUIRED_WORKFLOW_PATH,
        "state": "active",
    }


def fake_workflow_fetch(repository: str, workflow_url: str, _timeout: float) -> dict:
    workflow_id = int(workflow_url.rsplit("/", 1)[1])
    return api_workflow(repository, workflow_id)


class GitHubEvidenceTests(unittest.TestCase):
    def test_legacy_disabled_manifest_performs_no_network_call(self) -> None:
        manifest = sample_manifest()
        for field in ("images", "integration", "postgres", "readiness_evidence"):
            manifest["platform"].pop(field, None)
        manifest = RELEASE_POLICY.validate_manifest(manifest)

        def unexpected_fetch(_repository: str, _run_id: str, _timeout: float) -> dict:
            self.fail("network fetch must not run for a legacy disabled manifest")

        self.assertEqual(VERIFY.verify_manifest(manifest, fetch_run=unexpected_fetch), 0)

    def test_disabled_platform_candidate_evidence_is_verified(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        manifest["platform"]["enabled"] = False
        manifest["platform"]["published_ports"] = []
        manifest["platform"]["blocked_by"] = sorted(RELEASE_POLICY.PLATFORM_READINESS_GATES)
        calls: list[tuple[str, str, float]] = []

        def fake_fetch(repository: str, run_id: str, timeout: float) -> dict:
            calls.append((repository, run_id, timeout))
            return api_run(repository, "main", SHA_A, run_id)

        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=fake_fetch,
                fetch_workflow=fake_workflow_fetch,
                timeout=4.0,
            ),
            1,
        )
        self.assertEqual(calls, [("nclsppr/vps-infra", "101", 4.0)])

    def test_platform_evidence_is_deduplicated_and_verified(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        calls: list[tuple[str, str, float]] = []

        def fake_fetch(repository: str, run_id: str, timeout: float) -> dict:
            calls.append((repository, run_id, timeout))
            return api_run(repository, "main", SHA_A, run_id)

        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=fake_fetch,
                fetch_workflow=fake_workflow_fetch,
                timeout=3.0,
            ),
            1,
        )
        self.assertEqual(calls, [("nclsppr/vps-infra", "101", 3.0)])

    def test_compose_readiness_and_migration_runs_are_both_verified(self) -> None:
        manifest = sample_manifest()
        enable_parkventory(manifest)
        calls: list[tuple[str, str, float]] = []

        def fake_fetch(repository: str, run_id: str, timeout: float) -> dict:
            calls.append((repository, run_id, timeout))
            revisions = {"101": SHA_A, "202": SHA_A, "303": SHA_B}
            branch = "main"
            return api_run(repository, branch, revisions[run_id], run_id)

        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=fake_fetch,
                fetch_workflow=fake_workflow_fetch,
            ),
            3,
        )
        self.assertEqual(
            {(repository, run_id) for repository, run_id, _timeout in calls},
            {
                ("nclsppr/vps-infra", "101"),
                ("nclsppr/parkventory", "202"),
                ("nclsppr/parkventory", "303"),
            },
        )

    def test_static_readiness_run_uses_the_application_branch(self) -> None:
        manifest = sample_manifest()
        enable_personal(manifest)
        calls: list[tuple[str, str, float]] = []

        def fake_fetch(repository: str, run_id: str, timeout: float) -> dict:
            calls.append((repository, run_id, timeout))
            if repository == "nclsppr/vps-infra":
                return api_run(repository, "main", SHA_A, run_id)
            return api_run(repository, "main", SHA_B, run_id)

        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=fake_fetch,
                fetch_workflow=fake_workflow_fetch,
            ),
            2,
        )
        self.assertEqual(
            {(repository, run_id) for repository, run_id, _timeout in calls},
            {("nclsppr/vps-infra", "101"), ("nclsppr/personal", "404")},
        )

    def test_every_security_relevant_field_must_match(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        valid = api_run("nclsppr/vps-infra", "main", SHA_A, "101")
        mutations = {
            "repository": {"full_name": "nclsppr/other"},
            "head_repository": {"full_name": "someone/fork"},
            "id": 102,
            "html_url": "https://github.com/nclsppr/vps-infra/actions/runs/102",
            "head_sha": SHA_B,
            "status": "in_progress",
            "conclusion": "failure",
            "head_branch": "feature/untrusted",
            "event": "pull_request",
            "run_attempt": 2,
            "workflow_id": 78,
            "workflow_url": "https://api.github.com/repos/nclsppr/other/actions/workflows/77",
            "path": ".github/workflows/untrusted.yml@main",
        }
        for field, invalid_value in mutations.items():
            with self.subTest(field=field):
                run = json.loads(json.dumps(valid))
                run[field] = invalid_value
                with self.assertRaises(VERIFY.EvidenceError):
                    VERIFY.verify_manifest(
                        manifest,
                        fetch_run=lambda _repository, _run_id, _timeout, run=run: run,
                        fetch_workflow=fake_workflow_fetch,
                    )

    def test_declared_attempt_and_workflow_path_are_fail_closed(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        values = manifest["platform"]["readiness_evidence"].values()
        for value in values:
            value["run_attempt"] = 2
        run = api_run("nclsppr/vps-infra", "main", SHA_A, "101", run_attempt=2)
        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=lambda _repository, _run_id, _timeout: run,
                fetch_workflow=fake_workflow_fetch,
            ),
            1,
        )

        first = next(iter(manifest["platform"]["readiness_evidence"].values()))
        first["workflow_path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(VERIFY.EvidenceError, "seul"):
            VERIFY.verify_manifest(
                manifest,
                fetch_run=lambda _repository, _run_id, _timeout: run,
                fetch_workflow=fake_workflow_fetch,
            )

    def test_workflow_metadata_must_match_and_remain_active(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        run = api_run("nclsppr/vps-infra", "main", SHA_A, "101")
        valid = api_workflow("nclsppr/vps-infra")
        for field, invalid_value in {
            "id": 78,
            "url": "https://api.github.com/repos/nclsppr/vps-infra/actions/workflows/78",
            "path": ".github/workflows/other.yml",
            "state": "disabled_manually",
        }.items():
            with self.subTest(field=field):
                workflow = dict(valid)
                workflow[field] = invalid_value
                with self.assertRaises(VERIFY.EvidenceError):
                    VERIFY.verify_manifest(
                        manifest,
                        fetch_run=lambda _repository, _run_id, _timeout: run,
                        fetch_workflow=(
                            lambda _repository, _url, _timeout, workflow=workflow: workflow
                        ),
                    )

    def test_workflow_api_url_cannot_escape_the_expected_repository(self) -> None:
        for url in (
            "https://example.com/repos/nclsppr/vps-infra/actions/workflows/77",
            "https://api.github.com/repos/someone/fork/actions/workflows/77",
            "https://api.github.com/repos/nclsppr/vps-infra/actions/workflows/0",
            "https://api.github.com/repos/nclsppr/vps-infra/actions/workflows/77?x=1",
        ):
            with self.subTest(url=url), self.assertRaises(VERIFY.EvidenceError):
                VERIFY.fetch_github_workflow("nclsppr/vps-infra", url, 1.0)

    def test_workflow_dispatch_is_an_allowed_canonical_event(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        run = api_run(
            "nclsppr/vps-infra",
            "main",
            SHA_A,
            "101",
            event="workflow_dispatch",
        )
        self.assertEqual(
            VERIFY.verify_manifest(
                manifest,
                fetch_run=lambda _repository, _run_id, _timeout: run,
                fetch_workflow=fake_workflow_fetch,
            ),
            1,
        )

    def test_non_dict_api_payload_is_rejected(self) -> None:
        manifest = sample_manifest()
        enable_platform(manifest)
        with self.assertRaisesRegex(VERIFY.EvidenceError, "simulée invalide"):
            VERIFY.verify_manifest(
                manifest,
                fetch_run=lambda _repository, _run_id, _timeout: [],  # type: ignore[return-value]
            )

    def test_timeout_is_bounded_before_any_fetch(self) -> None:
        manifest = RELEASE_POLICY.validate_manifest(sample_manifest())
        for timeout in (0.0, -1.0, 60.1):
            with self.subTest(timeout=timeout), self.assertRaises(VERIFY.EvidenceError):
                VERIFY.verify_manifest(manifest, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
