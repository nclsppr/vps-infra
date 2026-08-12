#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
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
PLATFORM_VEX = load_script_module("platform_vex_contract", SCRIPTS / "lib/platform_vex.py")
PROVER = load_script_module("platform_proof_prover", SCRIPTS / "prove-platform-candidate")


def image(repository: str, digest: str = DIGEST_A, tag: str = "test") -> str:
    return f"{repository}:{tag}@sha256:{digest}"


def candidate() -> dict:
    return {
        "contract": RELEASE_POLICY.PLATFORM_CANDIDATE_CONTRACT,
        "compose_project": "vps-platform",
        "images": {
            "caddy": image("ghcr.io/nclsppr/vps-infra/caddy"),
            "postgres": image(
                "ghcr.io/nclsppr/vps-infra/postgres",
                tag=f"sha-{SHA_A}",
            ),
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


def trivy_report(statement: dict) -> dict:
    return {
        "Results": [
            {
                "Target": statement["target"],
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": statement["vulnerability_id"],
                        "PkgName": statement["package"]["name"],
                        "PkgIdentifier": {"PURL": statement["package"]["purl"]},
                        "InstalledVersion": statement["package"]["installed_version"],
                        "Severity": statement["severity"],
                    }
                ],
            }
        ]
    }


def vex_metadata() -> dict[str, str]:
    return PLATFORM_VEX.load_vex_policy(
        ROOT / "policies/platform-vex-v1.json",
        ROOT / "schemas/platform-vex-v1.schema.json",
        on_date=date(2026, 8, 12),
    ).metadata()


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
                image(
                    "ghcr.io/nclsppr/vps-infra/postgres",
                    DIGEST_A,
                    f"sha-{'b' * 40}",
                ),
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
            vulnerability_policy=vex_metadata(),
        )
        payload = PLATFORM_PROOF.platform_proof_bytes(proof)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertEqual(
            PLATFORM_PROOF.platform_proof_digest(proof),
            "sha256:e11281a595ffbcdf5784529b436daaeb3131e5da0739f93c7b73dae98c321dcb",
        )
        attempt_two = PLATFORM_PROOF.build_platform_proof(
            value,
            source_revision=SHA_A,
            run_id="123",
            run_attempt=2,
            verified_gates=sorted(RELEASE_POLICY.PLATFORM_PROOF_BASELINE_GATES),
            vulnerability_policy=vex_metadata(),
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
            vulnerability_policy=vex_metadata(),
        )
        validator = Draft202012Validator(proof_schema, registry=registry)
        validator.validate(proof)
        proof["controls"]["high-critical-scans"] = "skipped"
        self.assertTrue(list(validator.iter_errors(proof)))

    def test_exact_manifest_verification_rejects_registry_digest_substitution(self) -> None:
        reference = image(
            "ghcr.io/nclsppr/vps-infra/postgres",
            tag=f"sha-{SHA_A}",
        )
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
        reference = image("ghcr.io/nclsppr/vps-infra/caddy", tag=f"sha-{SHA_A}")
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
        self.assertEqual(PROVER._single_image_revision("caddy", {SHA_A}), SHA_A)
        with self.assertRaisesRegex(PROVER.ProofError, "one source revision"):
            PROVER._single_image_revision("caddy", {SHA_A, "b" * 40})
        with self.assertRaisesRegex(PROVER.ProofError, "one source revision"):
            PROVER._single_image_revision("caddy", set())

    def test_trivy_command_is_digest_bound_and_always_writes_json(self) -> None:
        reference = image(
            "ghcr.io/nclsppr/vps-infra/postgres",
            tag=f"sha-{SHA_A}",
        )

        def fake_command(argv: list[str], **_kwargs) -> bytes:
            self.assertIn(reference, argv)
            self.assertNotIn("--ignore-unfixed", argv)
            self.assertIn("HIGH,CRITICAL", argv)
            self.assertEqual(argv[argv.index("--exit-code") + 1], "0")
            report_path = Path(argv[argv.index("--output") + 1])
            report_path.write_text(
                '{"Results":[{"Vulnerabilities":[]}]}',
                encoding="utf-8",
            )
            return b""

        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            policy = PROVER.VexPolicy(
                {},
                policy_digest=f"sha256:{DIGEST_A}",
                valid_until=date(2026, 8, 26),
            )
            with mock.patch.object(PROVER, "_command", side_effect=fake_command):
                PROVER._scan(
                    "postgres",
                    reference,
                    "linux/amd64",
                    report,
                    policy,
                )

            def finding_command(argv: list[str], **_kwargs) -> bytes:
                report_path = Path(argv[argv.index("--output") + 1])
                report_path.write_text(
                    json.dumps(
                        {
                            "Results": [
                                {
                                    "Target": "bin/postgres",
                                    "Vulnerabilities": [
                                        {
                                            "VulnerabilityID": "CVE-2026-99999",
                                            "PkgName": "stdlib",
                                            "PkgIdentifier": {
                                                "PURL": "pkg:golang/stdlib@v1.26.4"
                                            },
                                            "InstalledVersion": "v1.26.4",
                                            "Severity": "HIGH",
                                        }
                                    ],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return b""

            with mock.patch.object(PROVER, "_command", side_effect=finding_command):
                with self.assertRaisesRegex(PROVER.ProofError, "uncovered HIGH"):
                    PROVER._scan(
                        "postgres",
                        reference,
                        "linux/amd64",
                        report,
                        policy,
                    )

    def test_vex_policy_matches_every_exact_current_finding(self) -> None:
        policy_path = ROOT / "policies/platform-vex-v1.json"
        schema_path = ROOT / "schemas/platform-vex-v1.schema.json"
        raw_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw_policy["statements"]), 4)
        expected_expirations = {
            ("grafana", "CVE-2026-21728"): date(2026, 9, 11),
            ("grafana", "CVE-2026-28377"): date(2026, 9, 11),
            ("postgres_exporter", "CVE-2026-56852"): date(2026, 8, 26),
            ("postgres_exporter", "CVE-2026-39822"): date(2026, 8, 26),
        }
        self.assertEqual(
            {
                (statement["service"], statement["vulnerability_id"])
                for statement in raw_policy["statements"]
            },
            set(expected_expirations),
        )
        for statement in raw_policy["statements"]:
            key = (statement["service"], statement["vulnerability_id"])
            self.assertLessEqual(
                date.fromisoformat(statement["expires_on"]),
                expected_expirations[key],
            )
        self.assertNotIn(
            "CVE-2026-42505",
            {
                statement["vulnerability_id"]
                for statement in raw_policy["statements"]
            },
        )
        policy = PLATFORM_VEX.load_vex_policy(
            policy_path,
            schema_path,
            on_date=date(2026, 8, 12),
        )
        for statement in raw_policy["statements"]:
            policy.evaluate_report(
                service=statement["service"],
                image=statement["image"],
                platform=statement["platform"],
                report=trivy_report(statement),
            )
        metadata = policy.proof_metadata()
        self.assertEqual(metadata["valid_until"], "2026-08-26")
        self.assertEqual(
            metadata["digest"],
            "sha256:"
            + hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        )

    def test_vex_policy_rejects_expiry_divergence_and_unused_exceptions(self) -> None:
        source = json.loads(
            (ROOT / "policies/platform-vex-v1.json").read_text(encoding="utf-8")
        )
        schema_path = ROOT / "schemas/platform-vex-v1.schema.json"
        with tempfile.TemporaryDirectory() as temporary:
            policy_path = Path(temporary) / "policy.json"
            policy_path.write_text(json.dumps(source), encoding="utf-8")
            policy = PLATFORM_VEX.load_vex_policy(
                policy_path,
                schema_path,
                on_date=date(2026, 8, 12),
            )
            with self.assertRaisesRegex(PLATFORM_VEX.VexError, "unused VEX"):
                policy.assert_all_used()

            with self.subTest("expired"):
                source["statements"][0]["expires_on"] = "2026-08-11"
                policy_path.write_text(json.dumps(source), encoding="utf-8")
                with self.assertRaisesRegex(PLATFORM_VEX.VexError, "expired VEX"):
                    PLATFORM_VEX.load_vex_policy(
                        policy_path,
                        schema_path,
                        on_date=date(2026, 8, 12),
                    )

    def test_vex_schema_rejects_unknown_fields_status_and_justification(self) -> None:
        baseline = json.loads(
            (ROOT / "policies/platform-vex-v1.json").read_text(encoding="utf-8")
        )
        schema_path = ROOT / "schemas/platform-vex-v1.schema.json"
        mutations = {
            "unknown field": lambda value: value["statements"][0].__setitem__(
                "ignore", True
            ),
            "status": lambda value: value["statements"][0].__setitem__(
                "status", "affected"
            ),
            "justification": lambda value: value["statements"][0].__setitem__(
                "justification", "component_not_present"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            policy_path = Path(temporary) / "policy.json"
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    changed = json.loads(json.dumps(baseline))
                    mutate(changed)
                    policy_path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(
                        PLATFORM_VEX.VexError,
                        "does not match its schema",
                    ):
                        PLATFORM_VEX.load_vex_policy(
                            policy_path,
                            schema_path,
                            on_date=date(2026, 8, 12),
                        )

            unapproved = json.loads(json.dumps(baseline))
            unapproved["statements"][0]["vulnerability_id"] = "CVE-2026-99999"
            policy_path.write_text(json.dumps(unapproved), encoding="utf-8")
            with self.assertRaisesRegex(
                PLATFORM_VEX.VexError,
                "unapproved VEX exception",
            ):
                PLATFORM_VEX.load_vex_policy(
                    policy_path,
                    schema_path,
                    on_date=date(2026, 8, 12),
                )

    def test_vex_policy_rejects_reference_package_cve_and_critical_divergence(self) -> None:
        source = json.loads(
            (ROOT / "policies/platform-vex-v1.json").read_text(encoding="utf-8")
        )
        statement = source["statements"][0]
        schema_path = ROOT / "schemas/platform-vex-v1.schema.json"
        with tempfile.TemporaryDirectory() as temporary:
            policy_path = Path(temporary) / "policy.json"
            policy_path.write_text(json.dumps(source), encoding="utf-8")
            for label, mutate in {
                "reference": lambda report: None,
                "package": lambda report: report["Results"][0]["Vulnerabilities"][0].__setitem__(
                    "PkgName", "github.com/grafana/not-tempo"
                ),
                "CVE": lambda report: report["Results"][0]["Vulnerabilities"][0].__setitem__(
                    "VulnerabilityID", "CVE-2026-99999"
                ),
            }.items():
                with self.subTest(label=label):
                    policy = PLATFORM_VEX.load_vex_policy(
                        policy_path,
                        schema_path,
                        on_date=date(2026, 8, 12),
                    )
                    report = trivy_report(statement)
                    mutate(report)
                    evaluated_image = (
                        statement["image"].rsplit("@", maxsplit=1)[0]
                        + f"@sha256:{DIGEST_B}"
                        if label == "reference"
                        else statement["image"]
                    )
                    with self.assertRaisesRegex(PLATFORM_VEX.VexError, "uncovered HIGH"):
                        policy.evaluate_report(
                            service=statement["service"],
                            image=evaluated_image,
                            platform=statement["platform"],
                            report=report,
                        )

            critical_policy = PLATFORM_VEX.load_vex_policy(
                policy_path,
                schema_path,
                on_date=date(2026, 8, 12),
            )
            critical_report = trivy_report(statement)
            critical_report["Results"][0]["Vulnerabilities"][0]["Severity"] = "CRITICAL"
            with self.assertRaisesRegex(PLATFORM_VEX.VexError, "not eligible"):
                critical_policy.evaluate_report(
                    service=statement["service"],
                    image=statement["image"],
                    platform=statement["platform"],
                    report=critical_report,
                )

    def test_postgres_labels_and_attestation_policy_are_first_party_bound(self) -> None:
        reference = image(
            "ghcr.io/nclsppr/vps-infra/postgres",
            tag=f"sha-{SHA_A}",
        )
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
        self.assertEqual(PROVER._verify_labels("postgres", reference, config), SHA_A)
        wrong_tag = image(
            "ghcr.io/nclsppr/vps-infra/postgres",
            tag=f"sha-{'b' * 40}",
        )
        with self.assertRaisesRegex(PROVER.ProofError, "match its sha tag"):
            PROVER._verify_labels("postgres", wrong_tag, config)
        self.assertEqual(
            PROVER.FIRST_PARTY_IMAGE_WORKFLOWS["postgres"],
            "nclsppr/vps-infra/.github/workflows/postgres-image.yml",
        )

    def test_attestations_are_workflow_bound_and_reject_self_hosted_runners(self) -> None:
        commands: list[list[str]] = []

        def fake_command(argv: list[str], **_kwargs) -> bytes:
            commands.append(argv)
            return b"[{}]"

        with mock.patch.object(PROVER, "_command", side_effect=fake_command):
            PROVER._verify_attestation(
                image("ghcr.io/nclsppr/vps-infra/platform-integration", DIGEST_B),
                source_revision=SHA_A,
                signer_workflow=(
                    "nclsppr/vps-infra/.github/workflows/platform-integration.yml"
                ),
            )
        command = commands[0]
        self.assertIn("--deny-self-hosted-runners", command)
        self.assertEqual(
            command[command.index("--source-ref") + 1],
            "refs/heads/main",
        )
        self.assertIn("--signer-workflow", command)
        self.assertIn(
            "nclsppr/vps-infra/.github/workflows/platform-integration.yml",
            command,
        )

    def test_proof_output_is_exclusive_and_bounded_to_working_directory(self) -> None:
        payload = b"proof\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            working = root / "working"
            outside.mkdir()
            working.mkdir()
            with mock.patch.object(Path, "cwd", return_value=working):
                output = working / "proof.json"
                PROVER._write_proof_exclusive(output, payload)
                self.assertEqual(output.read_bytes(), payload)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(PROVER.ProofError, "already exists"):
                    PROVER._write_proof_exclusive(output, payload)

                target = outside / "target.json"
                target.write_bytes(b"unchanged\n")
                symlink = working / "symlink.json"
                symlink.symlink_to(target)
                with self.assertRaisesRegex(PROVER.ProofError, "already exists"):
                    PROVER._write_proof_exclusive(symlink, payload)
                self.assertEqual(target.read_bytes(), b"unchanged\n")

                with self.assertRaisesRegex(PROVER.ProofError, "working directory"):
                    PROVER._write_proof_exclusive(outside / "escape.json", payload)


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
        self.assertIn("docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8", workflow_text)
        self.assertIn("docker.io/tonistiigi/binfmt:", workflow_text)


if __name__ == "__main__":
    unittest.main()
