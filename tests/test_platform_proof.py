#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS / "lib"))
SHA_A = "a" * 40
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


RELEASE_POLICY = load_script_module("platform_proof_release_policy", SCRIPTS / "lib/release_policy.py")
PLATFORM_PROOF = load_script_module("platform_proof_contract", SCRIPTS / "lib/platform_proof.py")
PROVER = load_script_module("platform_proof_prover", SCRIPTS / "prove-platform-candidate")


def image(repository: str, digest: str = DIGEST_A, tag: str = "test") -> str:
    return f"{repository}:{tag}@sha256:{digest}"


def candidate() -> dict:
    return {
        "contract": RELEASE_POLICY.PLATFORM_CANDIDATE_CONTRACT,
        "compose_project": "vps-platform",
        "images": {
            "caddy": image("ghcr.io/nclsppr/vps-infra/caddy"),
            "postgres": image("docker.io/library/postgres", tag="17.10-bookworm"),
            "prometheus": image("docker.io/prom/prometheus"),
            "grafana": image("docker.io/grafana/grafana"),
            "node_exporter": image("docker.io/prom/node-exporter"),
            "postgres_exporter": image(
                "docker.io/prometheuscommunity/postgres-exporter"
            ),
        },
        "integration": {
            "source_revision": SHA_A,
            "artifact": image(
                "ghcr.io/nclsppr/vps-infra/platform-integration",
                DIGEST_B,
            ),
        },
        "postgres": {
            "major": 17,
            "pgdata": "/var/lib/postgresql/data/pgdata",
        },
    }


class PlatformProofTests(unittest.TestCase):
    def test_subject_is_order_independent_and_binds_every_candidate_field(self) -> None:
        baseline = candidate()
        subject = RELEASE_POLICY.platform_candidate_subject(baseline)
        reordered = json.loads(json.dumps(baseline, sort_keys=True))
        self.assertEqual(RELEASE_POLICY.platform_candidate_subject(reordered), subject)

        mutations = {
            "integration digest": lambda value: value["integration"].__setitem__(
                "artifact",
                image("ghcr.io/nclsppr/vps-infra/platform-integration", DIGEST_A),
            ),
            "integration revision": lambda value: value["integration"].__setitem__(
                "source_revision", "b" * 40
            ),
            "postgres tag": lambda value: value["images"].__setitem__(
                "postgres",
                image("docker.io/library/postgres", DIGEST_A, "17.11-bookworm"),
            ),
        }
        for service, reference in baseline["images"].items():
            repository_and_tag = reference.split("@", maxsplit=1)[0]
            mutations[f"{service} digest"] = (
                lambda value, service=service, repository_and_tag=repository_and_tag: value[
                    "images"
                ].__setitem__(service, f"{repository_and_tag}@sha256:{DIGEST_B}")
            )
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(baseline))
                mutate(changed)
                self.assertNotEqual(
                    RELEASE_POLICY.platform_candidate_subject(changed),
                    subject,
                )

        for field, invalid_value in {
            "major": 18,
            "pgdata": "/var/lib/postgresql/data",
        }.items():
            with self.subTest(postgres_field=field):
                changed = json.loads(json.dumps(baseline))
                changed["postgres"][field] = invalid_value
                with self.assertRaises(RELEASE_POLICY.PolicyError):
                    RELEASE_POLICY.platform_candidate_subject(changed)

    def test_canonical_proof_binds_run_attempt_and_has_stable_digest(self) -> None:
        value = candidate()
        proof = PLATFORM_PROOF.build_platform_proof(
            value,
            source_revision=SHA_A,
            run_id="123",
            run_attempt=1,
            verified_gates=sorted(RELEASE_POLICY.PLATFORM_PROOF_BASELINE_GATES),
        )
        payload = PLATFORM_PROOF.platform_proof_bytes(proof)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertEqual(
            PLATFORM_PROOF.platform_proof_digest(proof),
            "sha256:5c5e1089c758d05c8a881a007e8b65b595a49a8b550820adc0ce240eb4b825e3",
        )
        attempt_two = PLATFORM_PROOF.build_platform_proof(
            value,
            source_revision=SHA_A,
            run_id="123",
            run_attempt=2,
            verified_gates=sorted(RELEASE_POLICY.PLATFORM_PROOF_BASELINE_GATES),
        )
        self.assertNotEqual(
            PLATFORM_PROOF.platform_proof_digest(attempt_two),
            PLATFORM_PROOF.platform_proof_digest(proof),
        )

    def test_candidate_and_proof_json_schemas_reject_unknown_fields(self) -> None:
        candidate_schema = json.loads(
            (ROOT / "schemas/platform-candidate.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(candidate_schema).validate(candidate())
        invalid = candidate()
        invalid["secret"] = "must-not-exist"
        self.assertTrue(list(Draft202012Validator(candidate_schema).iter_errors(invalid)))
        proof_schema = json.loads(
            (ROOT / "schemas/platform-proof.schema.json").read_text(encoding="utf-8")
        )
        registry = Registry().with_resource(
            candidate_schema["$id"],
            Resource.from_contents(candidate_schema),
        )
        proof = PLATFORM_PROOF.build_platform_proof(
            candidate(),
            source_revision=SHA_A,
            run_id="123",
            run_attempt=1,
            verified_gates=sorted(RELEASE_POLICY.PLATFORM_PROOF_BASELINE_GATES),
        )
        validator = Draft202012Validator(proof_schema, registry=registry)
        validator.validate(proof)
        proof["controls"]["fixed-high-critical-scans"] = "skipped"
        self.assertTrue(list(validator.iter_errors(proof)))

    def test_exact_manifest_verification_rejects_registry_digest_substitution(self) -> None:
        reference = image("docker.io/library/postgres", tag="17.10-bookworm")
        descriptor = json.dumps(
            {
                "digest": f"sha256:{DIGEST_B}",
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 2,
            }
        ).encode()
        with mock.patch.object(PROVER, "_command", return_value=descriptor):
            with self.assertRaisesRegex(PROVER.ProofError, "returned digest"):
                PROVER._inspect_exact_manifest(reference)

    def test_print_subject_mode_is_offline_and_rejects_extra_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate()), encoding="utf-8")
            result = subprocess.run(
                [
                    str(SCRIPTS / "prove-platform-candidate"),
                    "--print-subject",
                    str(candidate_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                RELEASE_POLICY.platform_candidate_subject(candidate()),
            )
            refused = subprocess.run(
                [
                    str(SCRIPTS / "prove-platform-candidate"),
                    "--print-subject",
                    "--run-id",
                    "1",
                    str(candidate_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("cannot be combined", refused.stderr)

    def test_caddy_labels_require_exact_source_and_full_revision(self) -> None:
        reference = image("ghcr.io/nclsppr/vps-infra/caddy")
        config = {
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": (
                        "https://github.com/nclsppr/vps-infra"
                    ),
                    "org.opencontainers.image.revision": SHA_A,
                }
            }
        }
        self.assertEqual(PROVER._verify_labels("caddy", reference, config), SHA_A)
        config["config"]["Labels"]["org.opencontainers.image.revision"] = "main"
        with self.assertRaisesRegex(PROVER.ProofError, "full commit"):
            PROVER._verify_labels("caddy", reference, config)

    def test_caddy_platform_revisions_must_match(self) -> None:
        self.assertEqual(PROVER._single_caddy_revision({SHA_A}), SHA_A)
        with self.assertRaisesRegex(PROVER.ProofError, "one source revision"):
            PROVER._single_caddy_revision({SHA_A, "b" * 40})
        with self.assertRaisesRegex(PROVER.ProofError, "one source revision"):
            PROVER._single_caddy_revision(set())

    def test_trivy_command_is_digest_bound_and_rejects_findings(self) -> None:
        reference = image("docker.io/library/postgres", tag="17.10-bookworm")

        def fake_command(argv: list[str], **_kwargs) -> bytes:
            self.assertIn(reference, argv)
            self.assertIn("--ignore-unfixed", argv)
            self.assertIn("HIGH,CRITICAL", argv)
            report_path = Path(argv[argv.index("--output") + 1])
            report_path.write_text(
                '{"Results":[{"Vulnerabilities":[]}]}',
                encoding="utf-8",
            )
            return b""

        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            with mock.patch.object(PROVER, "_command", side_effect=fake_command):
                PROVER._scan(reference, "linux/amd64", report)

            def finding_command(argv: list[str], **_kwargs) -> bytes:
                report_path = Path(argv[argv.index("--output") + 1])
                report_path.write_text(
                    '{"Results":[{"Vulnerabilities":[{"VulnerabilityID":"CVE-X"}]}]}',
                    encoding="utf-8",
                )
                return b""

            with mock.patch.object(PROVER, "_command", side_effect=finding_command):
                with self.assertRaisesRegex(PROVER.ProofError, "HIGH or CRITICAL"):
                    PROVER._scan(reference, "linux/amd64", report)


class PlatformProofWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_locked_and_uploads_one_raw_proof(self) -> None:
        path = ROOT / ".github/workflows/vps-release.yml"
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(set(workflow["jobs"]), {"prove-platform-candidate"})
        job = workflow["jobs"]["prove-platform-candidate"]
        self.assertNotIn("if", job)
        steps = job["steps"]
        upload = next(step for step in steps if step.get("id") == "upload")
        self.assertEqual(
            upload["uses"],
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        )
        self.assertEqual(upload["with"]["archive"], "false")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["overwrite"], "false")
        workflow_text = path.read_text(encoding="utf-8")
        self.assertIn(
            'test "${normalized_digest}" = "${EXPECTED_DIGEST}"',
            workflow_text,
        )
        for forbidden in (
            "continue-on-error",
            "docker compose up",
            "ssh ",
            "production-enabled",
        ):
            self.assertNotIn(forbidden, workflow_text)
        self.assertEqual(
            set(re.findall(r"secrets\.([A-Z_]+)", workflow_text)),
            {"GITHUB_TOKEN"},
        )


if __name__ == "__main__":
    unittest.main()
