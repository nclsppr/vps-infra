#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import re
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


APPLICATION_STATE = load_script_module(
    "application_state_policy",
    SCRIPTS / "validate-application-state",
)


class SupplyChainContractTests(unittest.TestCase):
    def test_parkventory_application_workflow_is_manual_inert_and_gated(self) -> None:
        path = ROOT / ".github/workflows/deploy-parkventory-application.yml"
        workflow_text = path.read_text(encoding="utf-8")
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertIn(workflow["on"]["workflow_dispatch"], {None, ""})
        self.assertEqual(workflow["concurrency"]["group"], "production-vps")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(
            workflow["jobs"]["deploy"]["environment"]["name"],
            "application-production",
        )
        self.assertEqual(
            workflow["jobs"]["deploy"]["strategy"]["max-parallel"],
            "1",
        )
        self.assertIn("github.ref == 'refs/heads/main'", workflow_text)
        self.assertIn("--application parkventory", workflow_text)
        self.assertIn("VPS_APPLICATION_DEPLOY_ENABLED != 'true'", workflow_text)
        self.assertIn("VPS_APPLICATION_DEPLOY_ENABLED == 'true'", workflow_text)
        self.assertIn(
            "deploy-application-live parkventory ${SOURCE_REVISION} "
            "${RELEASE_REFERENCE}",
            workflow_text,
        )
        self.assertNotIn("schedule:", workflow_text)
        self.assertNotIn("pull_request:", workflow_text)
        self.assertNotIn("push:", workflow_text)

        application_contract = json.loads(
            (ROOT / "releases/application-production.json").read_text(
                encoding="utf-8"
            )
        )
        static_contract = json.loads(
            (ROOT / "releases/static-production.json").read_text(encoding="utf-8")
        )
        self.assertFalse(application_contract["applications"]["parkventory"]["enabled"])
        self.assertTrue(static_contract["applications"]["parkventory"]["enabled"])

    def test_surplasse_public_edge_adversarial_tests_are_canonical(self) -> None:
        runner = (ROOT / "tests/run").read_text(encoding="utf-8")
        for invocation in (
            'python3 "$TESTS_DIR/test_surplasse_public_edge_candidate.py"',
            'python3 "$TESTS_DIR/test_surplasse_public_edge_controller.py"',
        ):
            self.assertEqual(runner.count(invocation), 1)

    def test_caddy_build_inputs_are_separate_from_runtime_state(self) -> None:
        def assignments(path: Path) -> dict[str, str]:
            values: dict[str, str] = {}
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                key, value = line.split("=", maxsplit=1)
                self.assertNotIn(key, values)
                values[key] = value
            return values

        build_values = assignments(ROOT / "platform/caddy/build.env")
        self.assertEqual(
            set(build_values),
            {
                "CADDY_BUILDER_IMAGE",
                "CADDY_RUNTIME_IMAGE",
            },
        )
        image_pattern = re.compile(
            r"^[a-z0-9./_-]+:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}$"
        )
        self.assertRegex(build_values["CADDY_BUILDER_IMAGE"], image_pattern)
        self.assertRegex(build_values["CADDY_RUNTIME_IMAGE"], image_pattern)
        self.assertTrue(
            build_values["CADDY_BUILDER_IMAGE"].startswith(
                "docker.io/library/caddy:"
            )
        )
        self.assertTrue(
            build_values["CADDY_RUNTIME_IMAGE"].startswith(
                "docker.io/library/caddy:"
            )
        )
        def image_tag(reference: str) -> str:
            tagged_reference = reference.split("@", maxsplit=1)[0]
            return tagged_reference.rsplit(":", maxsplit=1)[1]

        builder_tag = image_tag(build_values["CADDY_BUILDER_IMAGE"])
        runtime_tag = image_tag(build_values["CADDY_RUNTIME_IMAGE"])
        builder_version = re.fullmatch(
            r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-builder-alpine",
            builder_tag,
        )
        runtime_version = re.fullmatch(
            r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-alpine",
            runtime_tag,
        )
        self.assertIsNotNone(builder_version)
        self.assertIsNotNone(runtime_version)
        assert builder_version is not None
        assert runtime_version is not None
        self.assertEqual(
            builder_version.group("version"),
            runtime_version.group("version"),
        )
        runtime_values = assignments(ROOT / "platform/.env.example")
        self.assertIn("CADDY_PLATFORM_IMAGE", runtime_values)
        self.assertNotIn("CADDY_BUILDER_IMAGE", runtime_values)
        self.assertNotIn("CADDY_RUNTIME_IMAGE", runtime_values)
        self.assertNotIn("CADDY_DNS_MODULE", runtime_values)

    def test_caddy_build_input_validator_enforces_publication_contract(self) -> None:
        validator = ROOT / "scripts/validate-caddy-build-inputs"
        build_env = ROOT / "platform/caddy/build.env"
        self.assertTrue(os.access(validator, os.X_OK))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shutil.copytree(ROOT / "platform/caddy/build", root / "build")
            shutil.copytree(ROOT / "platform/caddy/patches", root / "patches")
            source = build_env.read_text(encoding="utf-8")
            copied_build_env = root / "valid.env"
            copied_build_env.write_text(source, encoding="utf-8")
            github_output = root / "github-output"
            github_output.touch()
            valid = subprocess.run(
                [
                    str(validator),
                    "--github-output",
                    str(github_output),
                    str(copied_build_env),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            output_lines = github_output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(output_lines), 2)
            self.assertTrue(output_lines[0].startswith("builder="))
            self.assertTrue(output_lines[1].startswith("runtime="))

            invalid_cases = {
                "wrong-repository.env": source.replace(
                    "CADDY_BUILDER_IMAGE=docker.io/library/caddy:",
                    "CADDY_BUILDER_IMAGE=registry.example/caddy:",
                ),
                "version-mismatch.env": source.replace(
                    "CADDY_RUNTIME_IMAGE=docker.io/library/caddy:2.11.4-alpine",
                    "CADDY_RUNTIME_IMAGE=docker.io/library/caddy:2.11.5-alpine",
                ),
            }
            for name, content in invalid_cases.items():
                candidate = root / name
                candidate.write_text(content, encoding="utf-8")
                rejected = subprocess.run(
                    [str(validator), str(candidate)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(rejected.returncode, 0, name)

            copied_go_mod = root / "build/go.mod"
            valid_go_mod = copied_go_mod.read_text(encoding="utf-8")
            copied_go_mod.write_text(
                valid_go_mod.replace(
                    "github.com/google/cel-go v0.29.2",
                    "github.com/google/cel-go v0.28.1",
                ),
                encoding="utf-8",
            )
            vulnerable_graph = subprocess.run(
                [str(validator), str(copied_build_env)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(vulnerable_graph.returncode, 0)
            copied_go_mod.write_text(valid_go_mod, encoding="utf-8")

            copied_patch = root / "patches/caddy-cel-go-v0.29.patch"
            copied_patch.write_text(
                copied_patch.read_text(encoding="utf-8") + "# tampered\n",
                encoding="utf-8",
            )
            tampered_patch = subprocess.run(
                [str(validator), str(copied_build_env)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tampered_patch.returncode, 0)

    def test_caddy_workflow_uses_only_build_inputs(self) -> None:
        workflow_text = (ROOT / ".github/workflows/caddy-image.yml").read_text(
            encoding="utf-8"
        )
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
        self.assertEqual(
            workflow["env"]["CADDY_BUILD_ENV"],
            "platform/caddy/build.env",
        )
        for event in ("pull_request", "push"):
            paths = workflow["on"][event]["paths"]
            self.assertEqual(
                paths,
                [
                    ".github/workflows/caddy-image.yml",
                    "platform/caddy/.dockerignore",
                    "platform/caddy/Dockerfile",
                    "platform/caddy/build.env",
                    "platform/caddy/build/**",
                    "platform/caddy/patches/**",
                    "platform/caddy/entrypoint.sh",
                    "scripts/validate-caddy-build-inputs",
                    "scripts/verify-caddy-image",
                ],
            )

        self.assertEqual(
            (ROOT / "platform/caddy/.dockerignore")
            .read_text(encoding="utf-8")
            .splitlines(),
            [
                "**",
                "!Dockerfile",
                "!build/",
                "!build/**",
                "!entrypoint.sh",
                "!patches/",
                "!patches/**",
            ],
        )

        input_steps = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("name") == "Read and validate the pinned build inputs"
        ]
        self.assertEqual(len(input_steps), 2)
        for step in input_steps:
            command = step["run"]
            self.assertIn("./scripts/validate-caddy-build-inputs", command)
            self.assertIn('--github-output "${GITHUB_OUTPUT}"', command)
            self.assertEqual(command.count('"${CADDY_BUILD_ENV}"'), 1)
            self.assertNotIn("platform/.env.example", command)

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("CADDY_BUILD_ENV ?= platform/caddy/build.env", makefile)
        check_caddy = makefile.split("check-caddy:", maxsplit=1)[1].split(
            "\nbootstrap:", maxsplit=1
        )[0]
        validator_position = check_caddy.index("./scripts/validate-caddy-build-inputs")
        builder_position = check_caddy.index("CADDY_BUILDER_IMAGE")
        self.assertLess(validator_position, builder_position)
        self.assertEqual(check_caddy.count('"$(CADDY_BUILD_ENV)"'), 3)
        self.assertNotIn("$(PLATFORM_ENV)", check_caddy)

    def test_caddy_build_and_security_gate_are_content_locked(self) -> None:
        dockerfile = (ROOT / "platform/caddy/Dockerfile").read_text(
            encoding="utf-8"
        )
        main_file = (ROOT / "platform/caddy/build/main.go").read_text(
            encoding="utf-8"
        )
        go_mod = (ROOT / "platform/caddy/build/go.mod").read_text(encoding="utf-8")
        go_sum = (ROOT / "platform/caddy/build/go.sum").read_text(encoding="utf-8")
        cel_patch = (
            ROOT / "platform/caddy/patches/caddy-cel-go-v0.29.patch"
        ).read_text(encoding="utf-8")

        self.assertIn("// Code generated by xcaddy v0.4.5. DO NOT EDIT.", main_file)
        self.assertIn('_ "github.com/caddy-dns/ovh"', main_file)
        self.assertIn("github.com/caddyserver/caddy/v2 v2.11.4", go_mod)
        self.assertIn("github.com/caddy-dns/ovh v1.1.0", go_mod)
        self.assertIn("github.com/google/cel-go v0.29.2", go_mod)
        self.assertIn("b2693fb63a30e6d7be0972c3645e9a2c0a500e93", cel_patch)
        self.assertEqual(cel_patch.count("interpreter.InterpretableV2"), 2)
        self.assertIn(
            "replace golang.org/x/text => golang.org/x/text v0.39.0", go_mod
        )
        self.assertIn(
            "replace google.golang.org/grpc => google.golang.org/grpc v1.82.1",
            go_mod,
        )
        for checksum in (
            "github.com/caddyserver/caddy/v2 v2.11.4 h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=",
            "github.com/caddy-dns/ovh v1.1.0 h1:CiqT4b3y/lK4j7FQTsUggdru4GqsY6yU8W0zStS54MI=",
            "github.com/google/cel-go v0.29.2 h1:ZtDxkeiMmz0mxbKDYiNkE5Lk7V5edMRcaaDf2jX002k=",
            "golang.org/x/text v0.39.0 h1:UbZz4pLOvn600D6Oh6GGEI6VAmndrEBLv8/6BEXzyus=",
            "google.golang.org/grpc v1.82.1 h1:NnAxzGRA0677vCa4BUkOAnO5+FfQqVl9iUXeD0IqcGE=",
        ):
            self.assertIn(checksum, go_sum)

        self.assertNotIn("xcaddy build", dockerfile)
        self.assertIn("GOTOOLCHAIN=local", dockerfile)
        self.assertIn("go mod verify", dockerfile)
        self.assertIn(
            "git apply --check --unidiff-zero /tmp/caddy-cel-go-v0.29.patch",
            dockerfile,
        )
        self.assertIn(
            "e4a3578e3307cb0d97b3d140aff2a8d14fb2147d0fd5122c1d94e523c3ed89bb",
            dockerfile,
        )
        self.assertIn("go build -mod=readonly", dockerfile)
        self.assertEqual(dockerfile.count("ADD --checksum=sha256:"), 6)
        self.assertIn("apk add --no-cache --no-network --upgrade", dockerfile)
        self.assertEqual(dockerfile.count("/c-ares-1.34.8-r0.apk"), 2)
        self.assertEqual(dockerfile.count("/curl-8.20.0-r0.apk"), 2)
        self.assertEqual(dockerfile.count("/libcurl-8.20.0-r0.apk"), 2)

        workflow = yaml.load(
            (ROOT / ".github/workflows/caddy-image.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            workflow["env"]["TRIVY_IMAGE"],
            "ghcr.io/aquasecurity/trivy:0.73.0@sha256:"
            "7cced7cae583819fc7806d4cbc0dbbc7cad18b99f7d3e235192e6da8c091045c",
        )
        verify_steps = workflow["jobs"]["verify"]["steps"]
        verify_names = [step["name"] for step in verify_steps]
        self.assertLess(
            verify_names.index("Build without registry credentials"),
            verify_names.index("Verify the exact Caddy image"),
        )
        self.assertLess(
            verify_names.index("Verify the exact Caddy image"),
            verify_names.index("Reject high or critical vulnerabilities"),
        )
        verify_build = next(
            step
            for step in verify_steps
            if step["name"] == "Build without registry credentials"
        )
        self.assertEqual(verify_build["with"]["load"], "true")
        self.assertEqual(verify_build["with"]["provenance"], "false")
        self.assertEqual(verify_build["with"]["sbom"], "false")

        publish_steps = workflow["jobs"]["publish"]["steps"]
        publish_names = [step["name"] for step in publish_steps]
        ordered_names = (
            "Build and publish the exact image",
            "Verify the published manifest and OCI labels",
            "Verify both published Caddy images",
            "Scan both published manifests by digest",
            "Attest the pushed multi-architecture manifest",
            "Verify GitHub provenance",
            "Publish digest in the workflow summary",
        )
        self.assertEqual(
            [publish_names.index(name) for name in ordered_names],
            sorted(publish_names.index(name) for name in ordered_names),
        )
        publish_build = next(
            step
            for step in publish_steps
            if step["name"] == "Build and publish the exact image"
        )
        self.assertEqual(publish_build["with"]["provenance"], "false")
        self.assertEqual(publish_build["with"]["sbom"], "false")
        published_scan_step = next(
            step
            for step in publish_steps
            if step["name"] == "Scan both published manifests by digest"
        )
        self.assertEqual(
            published_scan_step["env"]["GHCR_TOKEN"],
            "${{ secrets.GITHUB_TOKEN }}",
        )
        self.assertEqual(
            published_scan_step["env"]["GHCR_USERNAME"],
            "${{ github.actor }}",
        )
        published_scan = published_scan_step["run"]
        self.assertIn('"${IMAGE_NAME}@${digest}"', published_scan)
        self.assertIn('"${TRIVY_IMAGE}" registry login', published_scan)
        self.assertIn("--password-stdin", published_scan)
        self.assertIn('--user "${trivy_runner_identity}"', published_scan)
        self.assertIn(
            '--volume "${trivy_auth_directory}:/tmp/trivy-auth:ro"',
            published_scan,
        )
        self.assertIn(
            '--volume "${trivy_cache_directory}:/tmp/trivy-cache"',
            published_scan,
        )
        self.assertIn("--env DOCKER_CONFIG=/tmp/trivy-auth", published_scan)
        self.assertIn("--cache-dir /tmp/trivy-cache", published_scan)
        self.assertNotIn("/var/run/docker.sock", published_scan)
        for scan_name in (
            "Reject high or critical vulnerabilities",
            "Scan both published manifests by digest",
        ):
            scan = next(
                step
                for job in workflow["jobs"].values()
                for step in job["steps"]
                if step.get("name") == scan_name
            )["run"]
            self.assertIn('"${TRIVY_IMAGE}" image', scan)
            self.assertIn("--severity HIGH,CRITICAL", scan)
            self.assertNotIn("--ignore-unfixed", scan)
            self.assertIn("--exit-code 1", scan)
            self.assertNotIn("--ignorefile", scan)
        self.assertFalse((ROOT / ".trivyignore").exists())
        provenance = next(
            step
            for step in publish_steps
            if step["name"] == "Verify GitHub provenance"
        )["run"]
        self.assertIn("gh attestation verify", provenance)

    def test_postgres_build_input_validator_enforces_exact_upstream_image(self) -> None:
        validator = ROOT / "scripts/validate-postgres-build-inputs"
        build_env = ROOT / "platform/postgres/build.env"
        self.assertTrue(os.access(validator, os.X_OK))
        source = build_env.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate = root / "build.env"
            candidate.write_text(source, encoding="utf-8")
            github_output = root / "github-output"
            github_output.touch()
            valid = subprocess.run(
                [
                    str(validator),
                    "--github-output",
                    str(github_output),
                    str(candidate),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            output_lines = github_output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(output_lines), 2)
            self.assertTrue(output_lines[0].startswith("base="))
            self.assertEqual(output_lines[1], "version=17.10")

            invalid_cases = {
                "wrong-repository.env": source.replace(
                    "docker.io/library/postgres:",
                    "registry.example/postgres:",
                ),
                "wrong-major.env": source.replace("postgres:17.", "postgres:18."),
                "duplicate.env": source + source,
                "mutable.env": source.split("@", maxsplit=1)[0] + "\n",
            }
            for name, content in invalid_cases.items():
                candidate.write_text(content, encoding="utf-8")
                rejected = subprocess.run(
                    [str(validator), str(candidate)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(rejected.returncode, 0, name)

    def test_postgres_runtime_image_is_gosu_free_and_non_root(self) -> None:
        dockerfile = (ROOT / "platform/postgres/Dockerfile").read_text(
            encoding="utf-8"
        )
        verifier = (ROOT / "scripts/verify-postgres-image").read_text(
            encoding="utf-8"
        )
        self.assertIn("FROM ${POSTGRES_BASE_IMAGE}", dockerfile)
        self.assertIn('test "$(id -u postgres)" = 70', dockerfile)
        self.assertIn('test "$(id -g postgres)" = 70', dockerfile)
        self.assertIn("rm /usr/local/bin/gosu", dockerfile)
        self.assertTrue(dockerfile.rstrip().endswith("USER 70:70"))
        self.assertIn('[[ "$identity" == "70:70" ]]', verifier)
        self.assertIn('[[ "$expected_version" =~ ^17\\.[0-9]+$ ]]', verifier)
        self.assertNotIn('PostgreSQL) 17.10"', verifier)
        self.assertIn("! command -v gosu", verifier)
        self.assertIn("--network none", verifier)
        self.assertIn("--read-only", verifier)
        self.assertIn("--cap-drop ALL", verifier)
        self.assertIn("--security-opt no-new-privileges:true", verifier)
        self.assertIn("--data-checksums", verifier)
        self.assertIn('[[ "$pid_one" == "postgres" ]]', verifier)
        self.assertIn("data did not survive container replacement", verifier)
        self.assertIn("secret appeared in logs", verifier)

    def test_postgres_workflow_verifies_scans_then_attests_exact_children(self) -> None:
        workflow_text = (ROOT / ".github/workflows/postgres-image.yml").read_text(
            encoding="utf-8"
        )
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
        expected_paths = [
            ".github/workflows/postgres-image.yml",
            "platform/postgres/.dockerignore",
            "platform/postgres/Dockerfile",
            "platform/postgres/build.env",
            "scripts/validate-postgres-build-inputs",
            "scripts/verify-postgres-image",
        ]
        for event in ("pull_request", "push"):
            self.assertEqual(workflow["on"][event]["paths"], expected_paths)

        matrix = workflow["jobs"]["verify"]["strategy"]["matrix"]["include"]
        self.assertEqual(
            {(entry["platform"], entry["runner"]) for entry in matrix},
            {
                ("linux/amd64", "ubuntu-24.04"),
                ("linux/arm64", "ubuntu-24.04-arm"),
            },
        )
        publish_steps = workflow["jobs"]["publish"]["steps"]
        publish_names = [step["name"] for step in publish_steps]
        ordered_names = (
            "Build and publish the exact image",
            "Verify the published manifest and OCI labels",
            "Verify both published images",
            "Scan the published manifest by digest",
            "Attest the pushed multi-architecture manifest",
            "Verify GitHub provenance",
        )
        self.assertEqual(
            [publish_names.index(name) for name in ordered_names],
            sorted(publish_names.index(name) for name in ordered_names),
        )
        manifest_check = next(
            step
            for step in publish_steps
            if step["name"] == "Verify the published manifest and OCI labels"
        )["run"]
        self.assertIn('.platform.architecture == "amd64"', manifest_check)
        self.assertIn('.platform.architecture == "arm64"', manifest_check)
        self.assertIn("(.manifests | length) == 2", manifest_check)
        self.assertIn('.config.User == "70:70"', manifest_check)
        self.assertIn("org.opencontainers.image.revision", manifest_check)
        self.assertIn("org.opencontainers.image.source", manifest_check)
        scan = next(
            step
            for step in publish_steps
            if step["name"] == "Scan the published manifest by digest"
        )["run"]
        self.assertIn('for digest in "${AMD64_DIGEST}" "${ARM64_DIGEST}"', scan)
        self.assertIn('"${IMAGE_NAME}@${digest}"', scan)
        self.assertIn("--severity HIGH,CRITICAL", scan)
        self.assertIn("--exit-code 1", scan)
        self.assertNotIn("--ignore-unfixed", scan)
        self.assertNotIn("--ignorefile", scan)
        self.assertNotIn("self-hosted", workflow_text)

    def test_required_ci_runs_the_complete_canonical_check(self) -> None:
        validate = yaml.load(
            (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(list(validate["jobs"]), ["check"])
        validate_job = validate["jobs"]["check"]
        self.assertEqual(validate_job["name"], "Repository contract")
        for bypass in (
            "if",
            "continue-on-error",
            "env",
            "defaults",
            "strategy",
            "container",
        ):
            self.assertNotIn(bypass, validate_job)
        validate_steps = [
            step
            for step in validate_job["steps"]
            if step.get("name") == "Validate repository"
        ]
        self.assertEqual(len(validate_steps), 1)
        validate_step = validate_steps[0]
        self.assertEqual(validate_step["run"], "make check")
        for bypass in (
            "if",
            "continue-on-error",
            "working-directory",
            "env",
            "shell",
        ):
            self.assertNotIn(bypass, validate_step)

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        check_dependencies = re.search(r"(?m)^check: ([^#\n]+)", makefile)
        platform_dependencies = re.search(
            r"(?m)^check-platform: ([^#\n]+)", makefile
        )
        self.assertIsNotNone(check_dependencies)
        self.assertIsNotNone(platform_dependencies)
        assert check_dependencies is not None
        assert platform_dependencies is not None
        self.assertEqual(
            check_dependencies.group(1).split(),
            ["check-fast", "check-platform"],
        )
        self.assertEqual(
            platform_dependencies.group(1).split(),
            [
                "check-platform-config",
                "check-public-static-edge",
                "check-surplasse-public-edge-candidate",
                "check-surplasse-adapter",
                "check-parkventory-postgres",
                "check-parkventory-monitoring-candidate",
                "check-prometheus",
                "check-caddy",
                "check-postgres-image",
            ],
        )

    def test_smtp_decision_and_runbook_are_linked_without_reusing_adr_0006(
        self,
    ) -> None:
        decisions = sorted((ROOT / "docs/decisions").glob("[0-9][0-9][0-9][0-9]-*.md"))
        by_number: dict[str, list[str]] = {}
        for decision in decisions:
            by_number.setdefault(decision.name[:4], []).append(decision.name)
        self.assertEqual(
            by_number["0006"],
            ["0006-private-codex-app-server.md"],
        )
        self.assertEqual(
            by_number["0007"],
            ["0007-relais-email-transactionnel-surplasse.md"],
        )

        repository_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        adapter_readme = (ROOT / "applications/surplasse/README.md").read_text(
            encoding="utf-8"
        )
        smtp_decision = (
            ROOT / "docs/decisions/0007-relais-email-transactionnel-surplasse.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "docs/decisions/0007-relais-email-transactionnel-surplasse.md",
            repository_readme,
        )
        self.assertIn("docs/operations/surplasse-smtp.md", repository_readme)
        self.assertIn(
            "../../docs/operations/surplasse-smtp.md",
            adapter_readme,
        )
        self.assertIn("../operations/surplasse-smtp.md", smtp_decision)

    def test_renovate_never_promotes_custom_images(self) -> None:
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        managers = config["customManagers"]
        self.assertEqual(len(managers), 3)
        self.assertEqual(
            managers[0]["managerFilePatterns"],
            [r"/^platform\/\.env\.example$/"],
        )
        self.assertEqual(
            managers[1]["managerFilePatterns"],
            [r"/^platform\/caddy\/build\.env$/"],
        )
        self.assertEqual(
            managers[2]["managerFilePatterns"],
            [r"/^platform\/postgres\/build\.env$/"],
        )

        matchers = [
            re.compile(pattern.replace("(?<", "(?P<"))
            for manager in managers
            for pattern in manager["matchStrings"]
        ]

        def is_managed(line: str) -> bool:
            return any(pattern.fullmatch(line) for pattern in matchers)

        digest = "a" * 64
        for env_name in (
            "PROMETHEUS_IMAGE",
            "GRAFANA_IMAGE",
            "NODE_EXPORTER_IMAGE",
            "POSTGRES_EXPORTER_IMAGE",
        ):
            self.assertTrue(
                is_managed(
                    f"{env_name}=docker.io/library/example:1.2.3@sha256:{digest}"
                ),
                env_name,
            )
        for env_name in ("CADDY_BUILDER_IMAGE", "CADDY_RUNTIME_IMAGE"):
            self.assertTrue(
                is_managed(
                    f"{env_name}=docker.io/library/caddy:1.2.3-alpine@sha256:{digest}"
                ),
                env_name,
            )
        self.assertFalse(
            is_managed(
                "CADDY_RUNTIME_IMAGE="
                f"docker.io/library/example:1.2.3-alpine@sha256:{digest}"
            )
        )
        self.assertTrue(
            is_managed(
                "POSTGRES_BASE_IMAGE="
                f"docker.io/library/postgres:17.10-alpine3.24@sha256:{digest}"
            )
        )
        self.assertFalse(
            is_managed(
                "POSTGRES_IMAGE="
                f"ghcr.io/nclsppr/vps-infra/postgres:sha-test@sha256:{digest}"
            )
        )
        self.assertFalse(
            is_managed(
                "CADDY_PLATFORM_IMAGE="
                f"ghcr.io/nclsppr/vps-infra/caddy:sha-test@sha256:{digest}"
            )
        )

        package_rules = config["packageRules"]
        caddy_inputs = next(
            rule
            for rule in package_rules
            if rule.get("groupName") == "Caddy build inputs"
        )
        self.assertIs(caddy_inputs["dependencyDashboardApproval"], True)
        self.assertEqual(caddy_inputs["matchFileNames"], ["platform/caddy/build.env"])
        self.assertEqual(
            caddy_inputs["matchPackageNames"],
            ["docker.io/library/caddy"],
        )
        self.assertEqual(caddy_inputs["minimumGroupSize"], 2)
        custom_image = next(
            rule
            for rule in package_rules
            if "ghcr.io/nclsppr/vps-infra/caddy"
            in rule.get("matchPackageNames", [])
        )
        self.assertIs(custom_image["enabled"], False)
        custom_postgres_image = next(
            rule
            for rule in package_rules
            if "ghcr.io/nclsppr/vps-infra/postgres"
            in rule.get("matchPackageNames", [])
        )
        self.assertIs(custom_postgres_image["enabled"], False)

    def test_engine_pin_is_fixed_and_held_packages_can_change(self) -> None:
        variables = yaml.safe_load(
            (ROOT / "ansible/inventories/production/group_vars/all.yml").read_text(
                encoding="utf-8"
            )
        )
        packages = {item["name"]: item["version"] for item in variables["vps_docker_packages"]}
        self.assertEqual(packages["docker-ce-cli"], packages["docker-ce"])
        engine_match = re.fullmatch(
            r"5:(\d+)\.(\d+)\.(\d+)-1~ubuntu\.26\.04~resolute",
            packages["docker-ce"],
        )
        self.assertIsNotNone(engine_match)
        assert engine_match is not None
        engine_version = tuple(int(part) for part in engine_match.groups())
        self.assertGreaterEqual(engine_version, (29, 7, 2))

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/docker/tasks/main.yml").read_text(encoding="utf-8")
        )
        install = next(
            task
            for task in tasks
            if task["name"] == "Install exact Docker Engine and Compose versions"
        )
        self.assertIs(install["ansible.builtin.apt"]["allow_change_held_packages"], True)
        self.assertIs(install["ansible.builtin.apt"]["allow_downgrade"], False)

    def test_builder_runtime_images_are_immutable(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/caddy-image.yml").read_text(encoding="utf-8")
        )
        image_pattern = re.compile(
            r"^[a-z0-9./_-]+:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}$"
        )
        buildkit_image = workflow["env"]["BUILDKIT_IMAGE"]
        binfmt_image = workflow["env"]["BINFMT_IMAGE"]
        self.assertRegex(buildkit_image, image_pattern)
        self.assertRegex(binfmt_image, image_pattern)
        self.assertNotIn(":latest@", binfmt_image)
        self.assertNotIn(":buildx-stable-1@", buildkit_image)

        steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
        buildx_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/setup-buildx-action@")
        ]
        qemu_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/setup-qemu-action@")
        ]
        self.assertGreaterEqual(len(buildx_steps), 2)
        self.assertEqual(len(qemu_steps), 1)
        for step in buildx_steps:
            self.assertEqual(
                step["with"]["driver-opts"].strip(),
                "image=${{ env.BUILDKIT_IMAGE }}",
            )
        self.assertEqual(qemu_steps[0]["with"]["image"], "${{ env.BINFMT_IMAGE }}")
        self.assertEqual(qemu_steps[0]["with"]["platforms"], "arm64")


class SecurityBoundaryContractTests(unittest.TestCase):
    def test_forced_static_deployment_contract_is_exact(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_deploy_controller_path"],
            "/usr/local/libexec/vps/deploy",
        )
        self.assertEqual(
            defaults["vps_deploy_static_path"],
            "/usr/local/libexec/vps/deploy-static",
        )
        self.assertEqual(
            defaults["vps_deploy_static_gate_path"],
            "/usr/local/libexec/vps/deploy-static-live-gate",
        )
        self.assertEqual(
            defaults["vps_static_state_dir"],
            "/var/lib/vps-static",
        )
        self.assertEqual(
            defaults["vps_static_recovery_unit"],
            "vps-static-recover.service",
        )
        self.assertIn(
            "deploy-static-live-gate",
            defaults["vps_deploy_executables"],
        )

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        create_directories = next(
            task
            for task in tasks
            if task["name"] == "Create root-owned controller directories"
        )
        directory_modes = {
            entry["path"]: entry["mode"] for entry in create_directories["loop"]
        }
        for path in (
            "{{ vps_static_state_dir }}",
            "{{ vps_static_state_dir }}/active",
            "{{ vps_static_state_dir }}/inventories",
            "{{ vps_static_state_dir }}/quarantine",
            "{{ vps_static_state_dir }}/transactions",
        ):
            self.assertEqual(directory_modes[path], "0700")
        directory_task = create_directories["ansible.builtin.file"]
        self.assertEqual(directory_task["owner"], "root")
        self.assertEqual(directory_task["group"], "root")

        by_name = {task["name"]: task for task in tasks}
        recovery_install = by_name[
            "Install the static transaction recovery unit"
        ]["ansible.builtin.template"]
        self.assertEqual(
            recovery_install,
            {
                "src": "vps-static-recover.service.j2",
                "dest": "/etc/systemd/system/{{ vps_static_recovery_unit }}",
                "owner": "root",
                "group": "root",
                "mode": "0644",
            },
        )
        recovery_enable = by_name[
            "Enable static transaction recovery at boot"
        ]
        self.assertEqual(
            recovery_enable["ansible.builtin.systemd_service"],
            {
                "name": "{{ vps_static_recovery_unit }}",
                "enabled": True,
                "daemon_reload": True,
            },
        )
        self.assertEqual(recovery_enable["when"], "not ansible_check_mode")

        recovery_unit = (
            ROOT
            / "ansible/roles/deploy/templates/vps-static-recover.service.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", recovery_unit)
        self.assertIn("User=root", recovery_unit)
        self.assertIn("Group=root", recovery_unit)
        self.assertIn(
            "RequiresMountsFor=/srv/www /var/lib/vps-static",
            recovery_unit,
        )
        self.assertIn("Before=vps-public-static-edge.service", recovery_unit)
        self.assertIn(
            "ExecStart={{ vps_deploy_static_path }} --recover-live",
            recovery_unit,
        )
        self.assertIn("TimeoutStartSec=300", recovery_unit)
        self.assertIn("WantedBy=multi-user.target", recovery_unit)
        self.assertIn("After=local-fs.target docker.service", recovery_unit)
        self.assertIn("Requires=docker.service", recovery_unit)
        self.assertNotIn("RemainAfterExit", recovery_unit)

        parser = (SCRIPTS / "parse-forced-command").read_text(encoding="utf-8")
        wrapper = (SCRIPTS / "forced-command").read_text(encoding="utf-8")
        gate_source = (SCRIPTS / "deploy-static-live-gate").read_text(
            encoding="utf-8"
        )
        for repository in (
            "ghcr.io/nclsppr/personal/site",
            "ghcr.io/nclsppr/personal/routes",
            "ghcr.io/nclsppr/papersempire/site",
            "ghcr.io/nclsppr/papersempire/routes",
            "ghcr.io/nclsppr/parkventory-static-site",
            "ghcr.io/nclsppr/parkventory-static-routes",
            "ghcr.io/nclsppr/vps-infra/platform-integration",
            "ghcr.io/nclsppr/vps-infra/caddy",
        ):
            self.assertIn(repository, parser)
            self.assertIn(repository, gate_source)
        self.assertNotIn("eval", wrapper)
        self.assertIn("set -f", wrapper)
        self.assertIn("LC_ALL=C", parser)
        self.assertIn("LC_ALL=C", wrapper)
        self.assertEqual(wrapper.count("/usr/bin/sudo -n --"), 3)
        self.assertIn(
            "exec /usr/bin/sudo -n -- /usr/local/libexec/vps/deploy \"$sha\"",
            wrapper,
        )
        self.assertIn(
            "printf '%s\\n' \"$original_command\" |",
            wrapper,
        )
        self.assertIn(
            "/usr/bin/sudo -n -- "
            "/usr/local/libexec/vps/deploy-static-live-gate",
            wrapper,
        )
        self.assertIn(
            "/usr/bin/sudo -n -- "
            "/usr/local/libexec/vps/deploy-application-live-gate",
            wrapper,
        )
        self.assertNotIn("/usr/local/libexec/vps/deploy-static \\", wrapper)
        self.assertIn("subprocess.run", gate_source)
        self.assertNotIn("os.execve", gate_source)

        template = (
            ROOT / "ansible/roles/deploy/templates/deploy.sudoers.j2"
        ).read_text(encoding="utf-8")
        rendered = Environment().from_string(template).render(
            **defaults,
            vps_deploy_user="deploy",
        )
        aliases = [
            line
            for line in rendered.splitlines()
            if line.startswith("Cmnd_Alias ")
        ]
        self.assertEqual(len(aliases), 3)
        self.assertEqual(
            aliases,
            [
                "Cmnd_Alias VPS_DEPLOY_SHA = /usr/local/libexec/vps/deploy",
                "Cmnd_Alias VPS_DEPLOY_STATIC_LIVE = "
                "/usr/local/libexec/vps/deploy-static-live-gate \"\"",
                "Cmnd_Alias VPS_DEPLOY_APPLICATION_LIVE = "
                "/usr/local/libexec/vps/deploy-application-live-gate \"\"",
            ],
        )
        self.assertNotIn("*", rendered)
        self.assertNotIn("^", rendered)
        privilege = rendered.splitlines()[-1]
        self.assertIn("NOPASSWD:NOSETENV:", privilege)
        self.assertNotIn("ALL=(ALL", rendered)

        validated_executables = set()
        for executable_name in ("visudo-rs", "visudo"):
            visudo = shutil.which(executable_name)
            if visudo is None:
                continue
            resolved_visudo = str(Path(visudo).resolve())
            if resolved_visudo in validated_executables:
                continue
            validated_executables.add(resolved_visudo)
            with tempfile.TemporaryDirectory() as temporary_directory:
                sudoers = Path(temporary_directory) / "deploy.sudoers"
                sudoers.write_text(rendered + "\n", encoding="utf-8")
                validation = subprocess.run(
                    [visudo, "-cf", str(sudoers)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_disabled_application_controller_is_installed_with_recovery(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_deploy_application_path"],
            "/usr/local/libexec/vps/deploy-application",
        )
        self.assertEqual(
            defaults["vps_deploy_application_gate_path"],
            "/usr/local/libexec/vps/deploy-application-live-gate",
        )
        self.assertEqual(defaults["vps_application_root"], "/srv/applications")
        self.assertEqual(
            defaults["vps_application_config_dir"],
            "/etc/vps/applications",
        )
        self.assertEqual(
            defaults["vps_application_state_dir"],
            "/var/lib/vps-application",
        )
        self.assertEqual(
            defaults["vps_application_recovery_unit"],
            "vps-application-recover.service",
        )
        self.assertEqual(
            defaults["vps_monflorian_secret_materializer_path"],
            "/usr/local/libexec/vps/materialize-monflorian-secret",
        )
        self.assertEqual(defaults["vps_monflorian_adopt_existing_ids"], [])
        self.assertEqual(defaults["vps_monflorian_openai_api_key_source"], "")
        self.assertEqual(
            defaults["vps_monflorian_openai_api_key_path"],
            "/etc/vps/secrets/monflorian/monflorian-openai-api-key",
        )
        self.assertEqual(defaults["vps_monflorian_private_access_source"], "")
        self.assertEqual(
            defaults["vps_monflorian_private_access_path"],
            "/etc/vps/secrets/monflorian/monflorian-private-access.caddy",
        )
        self.assertIn(
            "materialize-monflorian-secret",
            defaults["vps_deploy_root_helpers"],
        )
        self.assertIn("deploy-application", defaults["vps_deploy_executables"])
        self.assertIn(
            "deploy-application-live-gate",
            defaults["vps_deploy_executables"],
        )

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        by_name = {task["name"]: task for task in tasks}
        directories = {
            item["path"]: item["mode"]
            for item in by_name[
                "Create root-owned controller directories"
            ]["loop"]
        }
        for path in (
            "{{ vps_application_config_dir }}",
            "{{ vps_application_state_dir }}",
            "{{ vps_application_state_dir }}/active",
            "{{ vps_application_state_dir }}/inventories",
            "{{ vps_application_state_dir }}/quarantine",
            "{{ vps_application_state_dir }}/transactions",
        ):
            self.assertEqual(directories[path], "0700")
        for application in ("surplasse", "parkventory", "monflorian"):
            self.assertEqual(
                directories[f"{{{{ vps_application_root }}}}/{application}"],
                "0755",
            )
            self.assertEqual(
                directories[
                    f"{{{{ vps_application_root }}}}/{application}/releases"
                ],
                "0755",
            )
        self.assertEqual(directories["/etc/vps/secrets/monflorian"], "0700")

        input_contract = by_name[
            "Validate the private Mon Florian secret input contracts"
        ]
        self.assertTrue(input_contract["no_log"])
        input_assertions = input_contract["ansible.builtin.assert"]["that"]
        normalized_input_assertions = {
            " ".join(assertion.split()) for assertion in input_assertions
        }
        self.assertIn(
            "vps_monflorian_openai_api_key_source == '' or "
            "vps_monflorian_openai_api_key_source is match('^/')",
            input_assertions,
        )
        self.assertIn(
            "vps_monflorian_private_access_source == '' or "
            "vps_monflorian_private_access_source is match('^/')",
            input_assertions,
        )
        self.assertIn(
            "vps_monflorian_adopt_existing_ids | "
            "difference(['monflorian.openai-api-key', "
            "'monflorian.private-access']) | length == 0",
            normalized_input_assertions,
        )

        adoption_preflight = by_name[
            "Preflight each selected Mon Florian existing-file adoption"
        ]
        self.assertEqual(
            adoption_preflight["ansible.builtin.command"]["argv"],
            [
                "{{ vps_monflorian_secret_materializer_path }}",
                "--check-adopt-existing",
                "{{ item }}",
            ],
        )
        self.assertFalse(adoption_preflight["check_mode"])
        self.assertFalse(adoption_preflight["changed_when"])
        self.assertNotIn("when", adoption_preflight)
        self.assertNotIn("no_log", adoption_preflight)

        adoption = by_name[
            "Adopt each explicitly selected existing Mon Florian secret"
        ]
        self.assertEqual(
            adoption["ansible.builtin.command"]["argv"],
            [
                "{{ vps_monflorian_secret_materializer_path }}",
                "--adopt-existing",
                "{{ item }}",
            ],
        )
        self.assertEqual(
            adoption["loop"],
            "{{ vps_monflorian_adopt_existing_ids }}",
        )
        self.assertEqual(adoption["when"], "not ansible_check_mode")
        self.assertNotIn("check_mode", adoption)
        self.assertNotIn("no_log", adoption)

        materialize = by_name[
            "Materialize each supplied Mon Florian singleton secret"
        ]
        self.assertEqual(
            materialize["ansible.builtin.include_tasks"],
            "materialize-monflorian-secret.yml",
        )
        self.assertEqual(
            materialize["loop"],
            [
                {
                    "identifier": "monflorian.openai-api-key",
                    "source": "{{ vps_monflorian_openai_api_key_source }}",
                },
                {
                    "identifier": "monflorian.private-access",
                    "source": "{{ vps_monflorian_private_access_source }}",
                },
            ],
        )
        self.assertTrue(materialize["no_log"])
        self.assertEqual(
            materialize["when"],
            [
                "not ansible_check_mode",
                "vps_monflorian_secret_input.source | length > 0",
            ],
        )

        materializer_tasks = yaml.safe_load(
            (
                ROOT
                / "ansible/roles/deploy/tasks/materialize-monflorian-secret.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(len(materializer_tasks), 1)
        materializer_block = materializer_tasks[0]
        self.assertTrue(materializer_block["no_log"])
        materializer_by_name = {
            task["name"]: task for task in materializer_block["block"]
        }
        staged_copy = materializer_by_name[
            "Stage the private Mon Florian source"
        ]["ansible.builtin.copy"]
        self.assertEqual(staged_copy["group"], "root")
        self.assertEqual(staged_copy["mode"], "0400")
        install_command = materializer_by_name[
            "Materialize the staged Mon Florian singleton file set"
        ]
        self.assertNotIn("check_mode", install_command)
        self.assertEqual(
            install_command["ansible.builtin.command"]["argv"],
            [
                "{{ vps_monflorian_secret_materializer_path }}",
                "--install-from",
                "{{ vps_monflorian_secret_stage.path }}/source",
                "{{ vps_monflorian_secret_input.identifier }}",
            ],
        )
        cleanup = materializer_block["always"][0]
        self.assertEqual(
            cleanup["ansible.builtin.file"]["state"],
            "absent",
        )

        audit = by_name[
            "Audit Mon Florian secret metadata and generation markers"
        ]
        self.assertEqual(
            audit["ansible.builtin.command"]["argv"],
            ["{{ vps_monflorian_secret_materializer_path }}", "--check"],
        )
        self.assertFalse(audit["check_mode"])
        self.assertFalse(audit["changed_when"])
        self.assertNotIn("no_log", audit)
        self.assertTrue(
            by_name["Prove the public Mon Florian secret audit contract"]
            ["ansible.builtin.assert"]["that"]
        )

        helper = ROOT / "scripts/materialize-monflorian-secret"
        self.assertTrue(os.access(helper, os.X_OK))
        self.assertIn(
            'MATERIALIZER = "materialize-monflorian-secret"',
            helper.read_text(encoding="utf-8"),
        )
        recovery_install = by_name[
            "Install the application transaction recovery unit"
        ]["ansible.builtin.template"]
        self.assertEqual(
            recovery_install,
            {
                "src": "vps-application-recover.service.j2",
                "dest": (
                    "/etc/systemd/system/"
                    "{{ vps_application_recovery_unit }}"
                ),
                "owner": "root",
                "group": "root",
                "mode": "0644",
            },
        )
        recovery_enable = by_name[
            "Enable application transaction recovery at boot"
        ]
        self.assertEqual(
            recovery_enable["ansible.builtin.systemd_service"],
            {
                "name": "{{ vps_application_recovery_unit }}",
                "enabled": True,
                "daemon_reload": True,
            },
        )
        self.assertEqual(recovery_enable["when"], "not ansible_check_mode")

        recovery_unit = (
            ROOT
            / "ansible/roles/deploy/templates/vps-application-recover.service.j2"
        ).read_text(encoding="utf-8")
        for expected in (
            "Type=oneshot",
            "User=root",
            "Group=root",
            "After=local-fs.target docker.service",
            "Before=vps-public-static-edge.service",
            "Requires=docker.service",
            "ExecStart={{ vps_deploy_application_path }} --recover-live",
            "MemoryMax=1G",
            "MemorySwapMax=0",
            "TasksMax=512",
            "LimitFSIZE=64M",
            "WantedBy=multi-user.target",
        ):
            self.assertIn(expected, recovery_unit)
        self.assertIn(
            "After=local-fs.target docker.service vps-static-recover.service",
            recovery_unit,
        )
        self.assertIn(
            "Requires=docker.service vps-static-recover.service",
            recovery_unit,
        )
        self.assertNotIn("RemainAfterExit", recovery_unit)

        parser = (SCRIPTS / "parse-forced-command").read_text(encoding="utf-8")
        wrapper = (SCRIPTS / "forced-command").read_text(encoding="utf-8")
        gate = (SCRIPTS / "deploy-application-live-gate").read_text(
            encoding="utf-8"
        )
        for repository in (
            "ghcr.io/nclsppr/surplasse/application-release",
            "ghcr.io/nclsppr/parkventory/application-release",
        ):
            self.assertIn(repository, parser)
            self.assertIn(repository, gate)
        self.assertIn('"deploy-application-live "*', parser)
        self.assertIn('"deploy-application-live "*', wrapper)
        self.assertIn("RuntimeDirectoryMode=0700", gate)
        self.assertIn("MemoryMax=1G", gate)
        self.assertIn("MemorySwapMax=0", gate)
        self.assertIn("TasksMax=512", gate)
        self.assertIn("LimitFSIZE=64M", gate)
        self.assertIn("ExecStopPost=", gate)
        self.assertIn("--recover-live", gate)

    def test_static_live_gate_revalidates_bounded_canonical_stdin(self) -> None:
        gate_path = SCRIPTS / "deploy-static-live-gate"
        self.assertTrue(os.access(gate_path, os.X_OK))
        gate = load_script_module("deploy_static_live_gate", gate_path)
        self.assertEqual(gate.MAX_INPUT_BYTES, 1024)
        self.assertEqual(gate.INPUT_TIMEOUT_SECONDS, 5)
        self.assertEqual(gate.ACTIVATION_TIMEOUT_SECONDS, 2100)
        self.assertEqual(gate.RECOVERY_TIMEOUT_SECONDS, 300)
        self.assertEqual(gate.SYSTEMD_RUN_WAIT_TIMEOUT_SECONDS, 2460)
        self.assertEqual(gate.SYSTEMD_RUN_PATH, "/usr/bin/systemd-run")

        sha = "a" * 40
        digest = "b" * 64
        integration = (
            "ghcr.io/nclsppr/vps-infra/platform-integration@sha256:" + digest
        )
        caddy = (
            "ghcr.io/nclsppr/vps-infra/caddy:release_1.2-3@sha256:" + digest
        )
        repositories = {
            "personal": (
                "ghcr.io/nclsppr/personal/site",
                "ghcr.io/nclsppr/personal/routes",
            ),
            "papersempire": (
                "ghcr.io/nclsppr/papersempire/site",
                "ghcr.io/nclsppr/papersempire/routes",
            ),
            "parkventory": (
                "ghcr.io/nclsppr/parkventory-static-site",
                "ghcr.io/nclsppr/parkventory-static-routes",
            ),
        }
        valid_commands = {}
        for application, (
            site_repository,
            routes_repository,
        ) in repositories.items():
            command = (
                f"deploy-static-live {application} {sha} "
                f"{site_repository}@sha256:{digest} "
                f"{routes_repository}@sha256:{digest} {sha} {integration} {caddy}"
            )
            valid_commands[application] = command
            self.assertEqual(
                gate.parse_request((command + "\n").encode("ascii")),
                command.split(" ")[1:],
            )

        personal = valid_commands["personal"]
        invalid_payloads = (
            b"",
            personal.encode("ascii"),
            (personal + "\n\n").encode("ascii"),
            (personal + "\r\n").encode("ascii"),
            (personal + " \n").encode("ascii"),
            (
                personal.replace(
                    "deploy-static-live ",
                    "deploy-static-live  ",
                )
                + "\n"
            ).encode("ascii"),
            (personal + " extra\n").encode("ascii"),
            (personal.replace(" personal ", " unknown ") + "\n").encode(
                "ascii"
            ),
            (
                personal.replace(
                    "ghcr.io/nclsppr/personal/site",
                    "ghcr.io/nclsppr/papersempire/site",
                )
                + "\n"
            ).encode("ascii"),
            (personal[:-1] + "é\n").encode("utf-8"),
            b"x" * (gate.MAX_INPUT_BYTES + 1),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(gate.GateError):
                    gate.parse_request(payload)

        with mock.patch.object(gate.os, "isatty", return_value=True):
            with self.assertRaisesRegex(gate.GateError, "terminal"):
                gate.read_request()

        with mock.patch.object(gate.sys, "argv", [str(gate_path), "extra"]):
            with mock.patch.object(gate.sys, "stderr"):
                with self.assertRaisesRegex(SystemExit, "64"):
                    gate.main()

        with (
            mock.patch.object(gate.sys, "argv", [str(gate_path)]),
            mock.patch.object(gate.os, "geteuid", return_value=501),
            mock.patch.object(gate.sys, "stderr"),
        ):
            with self.assertRaisesRegex(SystemExit, "64"):
                gate.main()

        oversized_stdin = mock.Mock()
        oversized_stdin.buffer = io.BytesIO(
            b"x" * (gate.MAX_INPUT_BYTES + 1)
        )
        with (
            mock.patch.object(gate.os, "isatty", return_value=False),
            mock.patch.object(gate.sys, "stdin", oversized_stdin),
        ):
            with self.assertRaisesRegex(gate.GateError, "input limit"):
                gate.read_request()

        personal_payload = (personal + "\n").encode("ascii")
        materializer_command = [
            "/usr/local/libexec/vps/deploy-static",
            "--activate-live",
            *personal.split(" ")[1:],
        ]
        unit_suffix = "c" * 24
        activation_command = [
            "/usr/bin/systemd-run",
            "--system",
            "--no-ask-password",
            f"--unit=vps-static-live-personal-{unit_suffix}.service",
            "--service-type=exec",
            "--wait",
            "--collect",
            "--quiet",
            "--expand-environment=no",
            "--uid=root",
            "--gid=root",
            "--working-directory=/",
        ]
        for property_value in (
            "KillMode=control-group",
            "SendSIGKILL=yes",
            "FinalKillSignal=SIGKILL",
            "Restart=no",
            "UMask=0077",
            "StandardInput=null",
            "StandardOutput=journal",
            "StandardError=journal",
            "TimeoutStartSec=2100s",
            "RuntimeMaxSec=2100s",
            "RuntimeRandomizedExtraSec=0",
            "RuntimeDirectory=vps-static-live-personal-" + unit_suffix,
            "RuntimeDirectoryMode=0700",
            "RuntimeDirectoryPreserve=no",
            "TimeoutStopSec=300s",
            (
                "ExecStopPost=/usr/local/libexec/vps/deploy-static "
                "--recover-live personal"
            ),
            "Environment=HOME=/root",
            "Environment=LANG=C",
            "Environment=LC_ALL=C",
            (
                "Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:"
                "/usr/bin:/sbin:/bin"
            ),
            (
                "Environment=VPS_STATIC_RUNTIME_DIRECTORY=/run/"
                "vps-static-live-personal-" + unit_suffix
            ),
        ):
            activation_command.extend(("--property", property_value))
        activation_command.extend(("--", *materializer_command))
        with mock.patch.object(
            gate.secrets,
            "token_hex",
            return_value=unit_suffix,
        ):
            self.assertEqual(
                gate.build_activation_command(personal.split(" ")[1:]),
                activation_command,
            )
        with mock.patch.object(
            gate.secrets,
            "token_hex",
            return_value="not-a-token",
        ):
            with self.assertRaisesRegex(gate.GateError, "unit name"):
                gate.build_activation_command(personal.split(" ")[1:])
        self.assertNotIn("--pipe", activation_command)
        self.assertNotIn("--pty", activation_command)
        recovery_command = [
            "/usr/local/libexec/vps/deploy-static",
            "--recover-live",
            "personal",
        ]
        expected_subprocess_options = {
            "check": False,
            "close_fds": True,
            "env": {
                "HOME": "/root",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": (
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                ),
            },
            "shell": False,
            "stdin": gate.subprocess.DEVNULL,
        }

        inherited_environment = {"ATTACKER_CONTROLLED": "value"}
        with (
            mock.patch.object(gate.sys, "argv", [str(gate_path)]),
            mock.patch.object(gate, "read_request", return_value=personal_payload),
            mock.patch.object(gate.os, "geteuid", return_value=0),
            mock.patch.object(gate.os, "environ", inherited_environment),
            mock.patch.object(gate.os, "umask"),
            mock.patch.object(
                gate.secrets,
                "token_hex",
                return_value=unit_suffix,
            ),
            mock.patch.object(
                gate.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    activation_command,
                    0,
                ),
            ) as run,
        ):
            gate.main()
        self.assertEqual(inherited_environment, gate.SAFE_ENVIRONMENT)
        run.assert_called_once_with(
            activation_command,
            **expected_subprocess_options,
            timeout=gate.SYSTEMD_RUN_WAIT_TIMEOUT_SECONDS,
        )

        failed_environment = {"ATTACKER_CONTROLLED": "value"}
        with (
            mock.patch.object(gate.sys, "argv", [str(gate_path)]),
            mock.patch.object(gate, "read_request", return_value=personal_payload),
            mock.patch.object(gate.os, "geteuid", return_value=0),
            mock.patch.object(gate.os, "environ", failed_environment),
            mock.patch.object(gate.os, "umask"),
            mock.patch.object(gate.sys, "stderr"),
            mock.patch.object(
                gate.secrets,
                "token_hex",
                return_value=unit_suffix,
            ),
            mock.patch.object(
                gate.subprocess,
                "run",
                side_effect=(
                    subprocess.CompletedProcess(activation_command, 1),
                    subprocess.CompletedProcess(recovery_command, 0),
                ),
            ) as run,
        ):
            with self.assertRaisesRegex(SystemExit, "70"):
                gate.main()
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    activation_command,
                    **expected_subprocess_options,
                    timeout=gate.SYSTEMD_RUN_WAIT_TIMEOUT_SECONDS,
                ),
                mock.call(
                    recovery_command,
                    **expected_subprocess_options,
                    timeout=gate.RECOVERY_TIMEOUT_SECONDS,
                ),
            ],
        )

    def test_shared_platform_images_and_non_root_database_are_exact(self) -> None:
        environment = {}
        for raw_line in (ROOT / "platform/.env.example").read_text(
            encoding="utf-8"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", maxsplit=1)
            self.assertNotIn(key, environment)
            environment[key] = value

        expected_images = json.loads(
            (ROOT / "platform/expected-images.json").read_text(encoding="utf-8")
        )
        image_variables = {
            "caddy": "CADDY_PLATFORM_IMAGE",
            "grafana": "GRAFANA_IMAGE",
            "node-exporter": "NODE_EXPORTER_IMAGE",
            "postgres-exporter": "POSTGRES_EXPORTER_IMAGE",
            "postgresql": "POSTGRES_IMAGE",
            "prometheus": "PROMETHEUS_IMAGE",
        }
        self.assertEqual(set(expected_images), set(image_variables))
        for service_name, variable_name in image_variables.items():
            reference = expected_images[service_name]
            self.assertEqual(reference, environment[variable_name])
            self.assertRegex(reference, r"^[^\s]+@sha256:[0-9a-f]{64}$")

        compose = yaml.safe_load(
            (ROOT / "platform/compose.yaml").read_text(encoding="utf-8")
        )
        postgresql = compose["services"]["postgresql"]
        self.assertEqual(postgresql["user"], "70:70")
        self.assertNotIn("cap_add", postgresql)
        self.assertIn(
            "/var/run/postgresql:size=16m,mode=2775,uid=70,gid=70",
            postgresql["tmpfs"],
        )
        self.assertEqual(
            compose["services"]["postgres-exporter"]["user"],
            "65534:70",
        )
        self.assertEqual(
            compose["services"]["grafana"]["healthcheck"]["test"],
            [
                "CMD",
                "curl",
                "--fail",
                "--silent",
                "http://127.0.0.1:3000/api/health",
            ],
        )

        prometheus = yaml.safe_load(
            (
                ROOT
                / "platform/observability/prometheus/prometheus.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "caddy",
            {job["job_name"] for job in prometheus["scrape_configs"]},
        )

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        platform_check = makefile.split("check-platform-config:", maxsplit=1)[1].split(
            "check-public-static-edge:", maxsplit=1
        )[0]
        self.assertIn(
            "--expected-images platform/expected-images.json",
            platform_check,
        )
        self.assertNotIn("--structural-only", platform_check)

    def test_public_static_edge_is_caddy_only_and_reversible(self) -> None:
        edge_root = ROOT / "platform/public-static-edge"
        compose = yaml.safe_load((edge_root / "compose.yaml").read_text(encoding="utf-8"))
        self.assertEqual(compose["name"], "vps-public-static-edge")
        self.assertEqual(set(compose["services"]), {"caddy"})
        caddy = compose["services"]["caddy"]
        self.assertRegex(
            caddy["image"],
            r"^ghcr\.io/nclsppr/vps-infra/caddy:[^@]+@sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn("environment", caddy)
        self.assertNotIn("secrets", caddy)
        self.assertEqual(
            set(compose["networks"]),
            {"app_parkventory", "edge"},
        )
        self.assertEqual(
            set(caddy["networks"]),
            {"app_parkventory", "edge"},
        )
        self.assertEqual(
            caddy["networks"]["app_parkventory"],
            {"ipv4_address": "172.30.20.254"},
        )
        self.assertNotIn("ops", caddy["networks"])
        route_mounts = {
            volume["target"]: volume
            for volume in caddy["volumes"]
            if volume.get("type") == "bind"
        }
        self.assertEqual(
            route_mounts["/etc/caddy/routes/parkventory.caddy"]["source"],
            "/var/lib/vps-public-edge-parkventory/route.caddy",
        )
        self.assertEqual(
            {
                (port["host_ip"], int(port["published"]), port["protocol"])
                for port in caddy["ports"]
            },
            {
                ("0.0.0.0", 80, "tcp"),
                ("0.0.0.0", 443, "tcp"),
                ("0.0.0.0", 443, "udp"),
            },
        )

        expected_images = json.loads(
            (edge_root / "expected-images.json").read_text(encoding="utf-8")
        )
        self.assertEqual(expected_images, {"caddy": caddy["image"]})
        caddy_verifier = (ROOT / "scripts/verify-caddy-image").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "for route_set in routes-prepare routes-precutover routes-activate",
            caddy_verifier,
        )
        self.assertEqual(
            {
                path.name
                for path in edge_root.iterdir()
                if path.is_dir() and path.name.startswith("routes-")
            },
            {"routes-prepare", "routes-precutover", "routes-activate"},
        )
        route_directories = (
            "routes-prepare",
            "routes-precutover",
            "routes-activate",
        )
        for route_directory in route_directories:
            self.assertEqual(
                {path.name for path in (edge_root / route_directory).iterdir()},
                {"personal.caddy", "papersempire.caddy", "parkventory.caddy"},
            )
        route_text = "\n".join(
            path.read_text(encoding="utf-8")
            for route_directory in route_directories
            for path in sorted((edge_root / route_directory).iterdir())
        )
        self.assertIn("nicolaspieper.com", route_text)
        self.assertIn("papersempire.com", route_text)
        self.assertIn("parkventory.com", route_text)
        self.assertNotIn("surplasse", route_text.lower())
        self.assertNotIn("grafana", route_text.lower())
        self.assertIn("nicolas.pieper.fr", route_text)
        self.assertNotIn("www.nicolas.pieper.fr", route_text)
        prepare_routes = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((edge_root / "routes-prepare").iterdir())
        )
        activate_routes = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((edge_root / "routes-activate").iterdir())
        )
        precutover_routes = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((edge_root / "routes-precutover").iterdir())
        )
        for domain in (
            "nicolaspieper.com",
            "www.nicolaspieper.com",
            "pieper.fr",
            "www.pieper.fr",
            "nicolas.pieper.fr",
            "papersempire.com",
            "www.papersempire.com",
            "parkventory.com",
            "www.parkventory.com",
        ):
            self.assertIn(f"http://{domain}", prepare_routes)
        self.assertNotIn("http://nicolaspieper.com", activate_routes)
        self.assertNotIn("http://papersempire.com", activate_routes)
        self.assertNotIn("http://parkventory.com", activate_routes)
        self.assertNotIn("http://nicolaspieper.com", precutover_routes)
        self.assertNotIn("http://papersempire.com", precutover_routes)
        self.assertNotIn("http://parkventory.com", precutover_routes)
        self.assertEqual(
            (edge_root / "routes-precutover/papersempire.caddy").read_bytes(),
            (edge_root / "routes-activate/papersempire.caddy").read_bytes(),
        )
        self.assertEqual(
            (edge_root / "routes-precutover/parkventory.caddy").read_bytes(),
            (edge_root / "routes-activate/parkventory.caddy").read_bytes(),
        )
        personal_activate_route = (
            edge_root / "routes-activate/personal.caddy"
        ).read_text(encoding="utf-8")
        for domain in (
            "www.nicolaspieper.com",
            "pieper.fr",
            "www.pieper.fr",
            "nicolas.pieper.fr",
        ):
            self.assertIn(f"http://{domain}", personal_activate_route)
        self.assertIn(
            "redir https://nicolaspieper.com{uri} 308",
            personal_activate_route,
        )
        caddyfile = (edge_root / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("metrics /metrics", caddyfile)
        self.assertIn('respond "Not found.\\n" 404', caddyfile)
        self.assertIn("http:// {", caddyfile)
        redirect_headers = caddyfile.split("(redirect_security_headers)", 1)[1]
        redirect_headers = redirect_headers.split("(static_site)", 1)[0]
        self.assertNotIn("Strict-Transport-Security", redirect_headers)
        self.assertIn("-Server", redirect_headers)
        self.assertIn('X-Content-Type-Options "nosniff"', redirect_headers)
        self.assertIn(
            'Referrer-Policy "strict-origin-when-cross-origin"',
            redirect_headers,
        )
        self.assertEqual(
            personal_activate_route.count("import redirect_security_headers"),
            2,
        )
        personal_precutover_route = (
            edge_root / "routes-precutover/personal.caddy"
        ).read_text(encoding="utf-8")
        precutover_site_labels = {
            label
            for line in personal_precutover_route.splitlines()
            if line and not line[0].isspace() and line.endswith(" {")
            for label in line.removesuffix(" {").split(", ")
        }
        self.assertIn("nicolaspieper.com", precutover_site_labels)
        self.assertIn("www.nicolaspieper.com", precutover_site_labels)
        for domain in (
            "pieper.fr",
            "www.pieper.fr",
            "nicolas.pieper.fr",
        ):
            self.assertIn(f"http://{domain}", precutover_site_labels)
            self.assertNotIn(domain, precutover_site_labels)
        self.assertEqual(
            personal_precutover_route.count("import redirect_security_headers"),
            2,
        )
        edge_defaults = yaml.safe_load(
            (
                ROOT
                / "ansible/roles/public_static_edge/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            edge_defaults["vps_public_static_edge_route_sources"],
            {
                "prepare": "routes-prepare",
                "precutover": "routes-precutover",
                "activate": "routes-activate",
            },
        )
        self.assertEqual(
            edge_defaults["vps_public_static_edge_direct_http_redirects"],
            [
                {
                    "source": "www.nicolaspieper.com",
                    "target": "nicolaspieper.com",
                },
                {"source": "pieper.fr", "target": "nicolaspieper.com"},
                {"source": "www.pieper.fr", "target": "nicolaspieper.com"},
                {"source": "nicolas.pieper.fr", "target": "nicolaspieper.com"},
            ],
        )

        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/public-static-edge.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [entry["role"] for entry in playbook[0]["roles"]],
            ["public_static_edge", "surplasse_dns_cutover"],
        )
        role_text = (
            ROOT / "ansible/roles/public_static_edge/tasks/main.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("/usr/local/libexec/vps/validate-compose", role_text)
        self.assertIn(
            '"{{ vps_public_static_edge_release_dir }}/validate-compose"',
            role_text,
        )
        self.assertIn("Pull the exact verified Caddy image", role_text)
        self.assertIn("Validate Caddy with the staged production files", role_text)
        self.assertIn(
            "item.stat.lnk_target is match('^releases/sha256-[0-9a-f]{64}$')",
            role_text,
        )
        self.assertNotIn(
            "item.stat.lnk_source is match('^releases/sha256-[0-9a-f]{64}$')",
            role_text,
        )
        self.assertIn("Read the effective Docker ingress firewall activity", role_text)
        self.assertIn(
            "vps_public_static_edge_ingress_firewall_activity.stdout == 'active'",
            role_text,
        )
        self.assertIn("Remove an interrupted public edge staging tree", role_text)
        self.assertIn("Atomically install the immutable public edge release", role_text)
        self.assertIn("Prepare the locked public edge base transaction", role_text)
        self.assertIn("Commit the probed public edge base transaction", role_text)
        self.assertIn("Roll back the durable public edge base transaction", role_text)
        self.assertIn(
            "Verify the switched public static edge from host and operator networks",
            role_text,
        )
        verification_position = role_text.index(
            "Verify the switched public static edge from host and operator networks"
        )
        rescue_position = role_text.index("      rescue:", verification_position)
        self.assertLess(verification_position, rescue_position)
        self.assertNotIn(
            "Unconditionally reconcile the public static edge Compose project",
            role_text,
        )
        self.assertNotIn("- --force-recreate", role_text)
        self.assertNotIn("vps_public_static_edge_previous_release", role_text)
        self.assertNotIn(
            "Install the selected static Parkventory route before handoff",
            role_text,
        )
        self.assertNotIn("map(attribute='Source')", role_text)
        self.assertIn(
            "Stop the public edge through the shared locked controller",
            role_text,
        )
        self.assertNotIn(
            "Stop every residual public edge project container",
            role_text,
        )
        self.assertIn("failed_when: false", role_text)
        self.assertNotIn("production-enabled", role_text)
        self.assertNotIn("apply-release", role_text)
        self.assertNotIn("OVH_", role_text)
        self.assertIn("Inspect the dedicated public edge network", role_text)
        self.assertIn("172.30.32.0/24", role_text)

        role_tasks = yaml.safe_load(role_text)
        stopped_task = next(
            task
            for task in role_tasks
            if task["name"]
            == "Stop and prove the isolated public static edge is absent"
        )
        stopped_mutator = stopped_task["block"][0]
        self.assertEqual(
            stopped_mutator["ansible.builtin.command"]["argv"],
            [
                "{{ vps_public_static_edge_application_controller }}",
                "--stop-public-edge-base",
            ],
        )
        self.assertFalse(
            any(
                "ansible.builtin.systemd_service" in task
                or task.get("ansible.builtin.command", {})
                .get("argv", [None, None])[1:2]
                == ["stop"]
                for task in stopped_task["block"]
            )
        )
        stage_task = next(
            task
            for task in role_tasks
            if task["name"] == "Stage, switch, and verify the isolated public static edge"
        )
        switch_task = next(
            task
            for task in stage_task["block"]
            if task["name"] == "Switch and reconcile the public static edge"
        )
        forward_names = [task["name"] for task in switch_task["block"]]
        self.assertEqual(
            forward_names,
            [
                "Prepare the locked public edge base transaction",
                "Verify the switched public static edge from host and operator networks",
                "Commit the probed public edge base transaction",
            ],
        )
        forward_tasks = {task["name"]: task for task in switch_task["block"]}
        self.assertEqual(
            forward_tasks["Prepare the locked public edge base transaction"]
            ["ansible.builtin.command"]["argv"][1],
            "--prepare-public-edge-base",
        )
        self.assertEqual(
            forward_tasks["Commit the probed public edge base transaction"]
            ["ansible.builtin.command"]["argv"][1],
            "--commit-public-edge-base",
        )
        rescue_tasks = {task["name"]: task for task in switch_task["rescue"]}
        rollback = rescue_tasks["Roll back the durable public edge base transaction"]
        self.assertEqual(
            rollback["ansible.builtin.command"]["argv"][1],
            "--rollback-public-edge-base",
        )
        rescue_names = [task["name"] for task in switch_task["rescue"]]
        self.assertEqual(
            rescue_names,
            [
                "Roll back the durable public edge base transaction",
                "Refuse the failed public edge reconciliation",
            ],
        )
        self.assertEqual(
            rollback["when"],
            [
                "vps_public_static_edge_base_prepare is defined",
                "vps_public_static_edge_base_prepare is succeeded",
            ],
        )

        application_controller = (ROOT / "scripts/deploy-application").read_text(
            encoding="utf-8"
        )
        for token in (
            "PUBLIC_EDGE_BASE_TRANSACTION",
            "previous_route",
            "previous_release",
            "previous_unit_active",
            "previous_unit_enabled",
            "write_public_edge_base_transaction(transaction)",
            "with deployment_lock():",
            "--prepare-public-edge-base",
            "--commit-public-edge-base",
            "--rollback-public-edge-base",
            "--stop-public-edge-base",
        ):
            self.assertIn(token, application_controller)
        for writer in (
            ROOT / "scripts/deploy-static",
            ROOT / "scripts/deploy-surplasse-public-edge",
        ):
            writer_text = writer.read_text(encoding="utf-8")
            self.assertIn("base-transaction.json", writer_text)
            self.assertIn("refuse_public_edge_base_transaction", writer_text)

        runtime_verification = (
            ROOT / "ansible/roles/public_static_edge/tasks/verify-runtime.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("NetworkSettings.Networks.keys()", runtime_verification)
        self.assertIn("vps_public_static_edge_network", runtime_verification)
        self.assertIn("inspect-bind-identities.yml", runtime_verification)
        self.assertIn(
            "vps_public_static_edge_bind_identities_match | bool",
            runtime_verification,
        )
        self.assertNotIn("map(attribute='Source')", runtime_verification)

        bind_verification = (
            ROOT
            / "ansible/roles/public_static_edge/tasks/inspect-bind-identities.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("/proc/{{", bind_verification)
        self.assertIn("}}/root{{ item }}", bind_verification)
        self.assertIn("follow: false", bind_verification)
        self.assertIn("get_attributes: false", bind_verification)
        self.assertIn("get_checksum: false", bind_verification)
        self.assertIn("get_mime: false", bind_verification)
        self.assertEqual(bind_verification.count(".stat.dev =="), 3)
        self.assertEqual(bind_verification.count(".stat.inode =="), 3)
        self.assertIn("State.Pid | int) > 0", bind_verification)
        self.assertNotIn("map(attribute='Source')", bind_verification)

        runtime_verification = (
            ROOT / "ansible/roles/public_static_edge/tasks/verify-runtime.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("delegate_to: localhost", runtime_verification)
        self.assertIn("validate_certs: true", runtime_verification)
        self.assertIn("http://{{ ansible_default_ipv4.address }}/", runtime_verification)
        self.assertIn(
            "Probe pending one-hop HTTP redirects directly on Atlas before DNS cutover",
            runtime_verification,
        )
        self.assertIn(
            "vps_public_static_edge_state == 'precutover'",
            runtime_verification,
        )
        self.assertIn("source=atlas-precutover", runtime_verification)
        self.assertIn(
            "pre_cutover_http_redirect_probe.server is not defined",
            runtime_verification,
        )
        self.assertIn(
            "external_http_redirect_probe.server is not defined",
            runtime_verification,
        )
        self.assertIn(
            "external_https_redirect_probe.server is not defined",
            runtime_verification,
        )
        self.assertGreaterEqual(
            runtime_verification.count("strict_transport_security"),
            2,
        )
        self.assertGreaterEqual(
            runtime_verification.count("x_content_type_options"),
            2,
        )
        self.assertIn("Refuse an unconfigured HTTP host", runtime_verification)
        self.assertIn("Host: unconfigured.invalid", runtime_verification)
        self.assertIn("status_code: 404", runtime_verification)

        authoritative_dns = (
            ROOT
            / "ansible/roles/public_static_edge/tasks/verify-authoritative-dns.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("+comments", authoritative_dns)
        self.assertIn("status: NOERROR", authoritative_dns)
        self.assertIn("AAAA", authoritative_dns)
        self.assertNotIn("authoritative_aaaa.stdout | trim == ''", authoritative_dns)
        base_defaults = (ROOT / "ansible/roles/base/defaults/main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("bind9-dnsutils", base_defaults)

        unit = (
            ROOT
            / "ansible/roles/public_static_edge/templates/vps-public-static-edge.service.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("ExecStop=/usr/bin/docker compose", unit)
        self.assertIn(" stop --timeout 30 caddy", unit)
        self.assertNotIn("--force-recreate", unit)
        self.assertIn(
            "After=network-online.target docker.service "
            "vps-docker-ingress-firewall.service "
            "vps-application-recover.service vps-static-recover.service",
            unit,
        )
        self.assertIn(
            "Requires=docker.service vps-docker-ingress-firewall.service "
            "vps-application-recover.service vps-static-recover.service",
            unit,
        )
        self.assertNotIn("--volumes", unit)

    def test_internal_platform_controller_is_bounded_and_reversible(self) -> None:
        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/internal-platform.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [entry["role"] for entry in playbook[0]["roles"]],
            ["internal_platform"],
        )
        defaults = yaml.safe_load(
            (
                ROOT / "ansible/roles/internal_platform/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        selected_services = {
            "postgresql",
            "prometheus",
            "grafana",
            "node-exporter",
            "postgres-exporter",
        }
        self.assertEqual(
            set(defaults["vps_internal_platform_services"]),
            selected_services,
        )
        self.assertNotIn("caddy", defaults["vps_internal_platform_services"])
        self.assertEqual(
            defaults["vps_internal_platform_runtime_dir"],
            "/srv/vps/runtime/internal-platform/platform",
        )
        self.assertEqual(
            set(defaults["vps_internal_platform_network_contract"]["postgresql"]),
            {"db_monitoring", "db_parkventory", "db_surplasse"},
        )

        role = (
            ROOT / "ansible/roles/internal_platform/tasks/main.yml"
        ).read_text(encoding="utf-8")
        unit = (
            ROOT
            / "ansible/roles/internal_platform/templates/vps-internal-platform.service.j2"
        ).read_text(encoding="utf-8")
        runtime = (
            ROOT / "ansible/roles/internal_platform/tasks/verify-runtime.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Refuse to remove an unselected shared-project container", role)
        self.assertIn("difference(vps_internal_platform_services)", role)
        orphan_guard = role.index(
            "Refuse to remove an unselected shared-project container"
        )
        systemd_restart = role.index("Reconcile exactly the five internal services")
        self.assertLess(orphan_guard, systemd_restart)
        self.assertIn("Pull exactly the five selected internal service images", role)
        self.assertIn("Atomically install the immutable internal platform release", role)
        self.assertIn("Atomically activate the staged internal platform release", role)
        self.assertIn("Atomically restore the previous internal platform release", role)
        self.assertIn("Named data volumes were preserved", role)
        self.assertNotIn("docker compose down", role)
        self.assertNotIn("--volumes", role)
        self.assertNotIn("prune", role)
        self.assertNotIn(" OVH_", role)
        self.assertNotIn("production-enabled", role)
        self.assertEqual(
            defaults["vps_internal_platform_expected_images_file"],
            "platform/expected-images.json",
        )
        self.assertIn("--expected-images", role)
        self.assertIn("regex_replace('^docker\\\\.io/', '')", role)
        self.assertNotIn(" caddy", unit)
        self.assertNotIn("caddy ", unit)
        self.assertIn("--remove-orphans", unit)
        for service in selected_services:
            self.assertIn(service, unit)
        self.assertNotIn(" down ", unit)
        self.assertNotIn("--volumes", unit)
        self.assertIn('"sport = :{{ item }}"', runtime)
        for port in (3000, 5432, 9090, 9100, 9187):
            self.assertIn(f"    - {port}", runtime)
        self.assertIn("search('127[.]0[.]0[.]1:3000[ \\t]')", runtime)
        self.assertNotIn("\\\\b", runtime)
        self.assertIn("pg_up 1", runtime)
        self.assertIn("node_uname_info", runtime)
        self.assertIn("labels.job', 'equalto', 'caddy'", runtime)
        self.assertIn("RepoDigests", runtime)
        self.assertIn("regex_replace('^docker\\\\.io/', '')", runtime)
        self.assertIn("'db_surplasse'].Aliases", runtime)
        self.assertIn("'db_parkventory'].Aliases", runtime)
        self.assertIn("172.30.11.0/24", role)
        self.assertIn("172.30.21.0/24", role)

        materializer_helper = ROOT / "scripts/materialize-internal-platform-secrets"
        helper_text = materializer_helper.read_text(encoding="utf-8")
        self.assertTrue(os.access(materializer_helper, os.X_OK))
        self.assertIn("os.O_EXCL", helper_text)
        self.assertIn("os.O_NOFOLLOW", helper_text)
        self.assertIn("validate_secret(descriptor, spec, owner)", helper_text)
        self.assertIn('SecretSpec("postgres-superuser-password", 70)', helper_text)
        self.assertIn('SecretSpec("grafana-secret-key", 472)', helper_text)

    def test_surplasse_guard_accepts_the_active_oneshot_platform_unit(self) -> None:
        unit = (
            ROOT
            / "ansible/roles/internal_platform/templates/vps-internal-platform.service.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("RemainAfterExit=yes", unit)

        role = yaml.safe_load(
            (ROOT / "ansible/roles/surplasse/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        stage = next(
            task
            for task in role
            if task["name"] == "Stage a fail-closed Surplasse release"
        )
        preparation_tasks = {task["name"]: task for task in stage["block"]}

        activity = preparation_tasks["Read the internal platform unit activity"]
        self.assertEqual(
            activity["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/systemctl",
                "is-active",
                "{{ vps_surplasse_internal_platform_unit }}",
            ],
        )
        self.assertEqual(
            activity["register"],
            "vps_surplasse_internal_platform_unit_activity",
        )
        self.assertFalse(activity["changed_when"])

        guard = preparation_tasks[
            "Require the active and enabled shared internal platform"
        ]["ansible.builtin.assert"]["that"]
        self.assertIn(
            "vps_surplasse_internal_platform_unit_activity.stdout == 'active'",
            guard,
        )
        self.assertIn(
            "ansible_facts.services[vps_surplasse_internal_platform_unit].status "
            "== 'enabled'",
            guard,
        )
        self.assertNotIn(
            "ansible_facts.services[vps_surplasse_internal_platform_unit].state "
            "== 'running'",
            guard,
        )

        environment = Environment()
        platform_unit = "vps-internal-platform.service"

        def guard_results(
            *, activity_stdout: str, platform_status: str, docker_state: str
        ) -> list[bool]:
            variables = {
                "ansible_facts": {
                    "services": {
                        "docker.service": {"state": docker_state},
                        platform_unit: {
                            "state": "stopped",
                            "status": platform_status,
                        },
                    }
                },
                "vps_surplasse_internal_platform_unit": platform_unit,
                "vps_surplasse_internal_platform_unit_activity": {
                    "stdout": activity_stdout
                },
            }
            return [
                bool(environment.compile_expression(assertion)(**variables))
                for assertion in guard
            ]

        self.assertTrue(
            all(
                guard_results(
                    activity_stdout="active",
                    platform_status="enabled",
                    docker_state="running",
                )
            )
        )
        for inactive_boundary in (
            {
                "activity_stdout": "inactive",
                "platform_status": "enabled",
                "docker_state": "running",
            },
            {
                "activity_stdout": "active",
                "platform_status": "disabled",
                "docker_state": "running",
            },
            {
                "activity_stdout": "active",
                "platform_status": "enabled",
                "docker_state": "stopped",
            },
        ):
            self.assertFalse(all(guard_results(**inactive_boundary)))

        runtime_verification = (
            ROOT / "ansible/roles/internal_platform/tasks/verify-runtime.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(".State.Health.Status == 'healthy'", runtime_verification)

    def test_parkventory_guard_accepts_the_active_oneshot_platform_unit(
        self,
    ) -> None:
        tasks = yaml.safe_load(
            (
                ROOT / "ansible/roles/parkventory_postgres/tasks/main.yml"
            ).read_text(encoding="utf-8")
        )
        by_name = {task["name"]: task for task in tasks}
        guard = by_name[
            "Require the healthy internal platform before database inspection"
        ]["ansible.builtin.assert"]["that"]
        environment = Environment()

        def guard_results(
            *, activity_stdout: str, platform_status: str, docker_state: str
        ) -> list[bool]:
            variables = {
                "ansible_facts": {
                    "services": {
                        "docker.service": {"state": docker_state},
                        "vps-internal-platform.service": {
                            "state": "stopped",
                            "status": platform_status,
                        },
                    }
                },
                "vps_parkventory_internal_platform_active": {
                    "stdout": activity_stdout
                },
            }
            return [
                bool(environment.compile_expression(assertion)(**variables))
                for assertion in guard
            ]

        self.assertTrue(
            all(
                guard_results(
                    activity_stdout="active",
                    platform_status="enabled",
                    docker_state="running",
                )
            )
        )
        for inactive_boundary in (
            {
                "activity_stdout": "inactive",
                "platform_status": "enabled",
                "docker_state": "running",
            },
            {
                "activity_stdout": "active",
                "platform_status": "disabled",
                "docker_state": "running",
            },
            {
                "activity_stdout": "active",
                "platform_status": "enabled",
                "docker_state": "stopped",
            },
        ):
            self.assertFalse(all(guard_results(**inactive_boundary)))

    def test_internal_platform_secret_materialization_is_idempotent(self) -> None:
        helper = ROOT / "scripts/materialize-internal-platform-secrets"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "secrets"
            root.mkdir(mode=0o700)
            environment = os.environ.copy()
            environment["VPS_INTERNAL_SECRET_TESTING"] = "1"
            first = subprocess.run(
                [str(helper), "--test-root", str(root)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            expected_names = {
                "postgres-superuser-password",
                "postgres-exporter-password",
                "grafana-admin-password",
                "grafana-secret-key",
            }
            self.assertEqual({path.name for path in root.iterdir()}, expected_names)
            before = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in root.iterdir()
            }
            for path in root.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
                self.assertEqual(path.stat().st_nlink, 1)
                self.assertRegex(path.read_bytes(), rb"[A-Za-z0-9_-]{64}\n")

            second = subprocess.run(
                [str(helper), "--test-root", str(root)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in root.iterdir()
            }
            self.assertEqual(before, after)

            victim = root / "grafana-secret-key"
            victim.unlink()
            outside = Path(temporary_directory) / "outside"
            outside.write_text("unchanged\n", encoding="utf-8")
            victim.symlink_to(outside)
            refused = subprocess.run(
                [str(helper), "--test-root", str(root)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    def test_internal_secret_materialization_rejects_invalid_existing_files(self) -> None:
        helper = ROOT / "scripts/materialize-internal-platform-secrets"
        environment = os.environ.copy()
        environment["VPS_INTERNAL_SECRET_TESTING"] = "1"
        invalid_cases = {
            "hardlink": None,
            "empty": b"",
            "wrong-mode": b"A" * 64 + b"\n",
            "wrong-format": b"!" * 64 + b"\n",
            "too-long": b"A" * 65 + b"\n",
        }
        for case_name, content in invalid_cases.items():
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "secrets"
                root.mkdir(mode=0o700)
                valid = subprocess.run(
                    [str(helper), "--test-root", str(root)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(valid.returncode, 0, valid.stderr)
                control = root / "postgres-superuser-password"
                control_before = control.read_bytes()
                victim = root / "grafana-secret-key"
                victim.unlink()
                if case_name == "hardlink":
                    source = Path(temporary) / "hardlink-source"
                    source.write_bytes(b"A" * 64 + b"\n")
                    source.chmod(0o440)
                    os.link(source, victim)
                else:
                    assert content is not None
                    victim.write_bytes(content)
                    victim.chmod(0o400 if case_name == "wrong-mode" else 0o440)

                refused = subprocess.run(
                    [str(helper), "--test-root", str(root)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(refused.returncode, 0)
                self.assertEqual(control.read_bytes(), control_before)

    def test_public_static_edge_network_is_a_managed_host_boundary(self) -> None:
        variables = yaml.safe_load(
            (ROOT / "ansible/inventories/production/group_vars/all.yml").read_text(
                encoding="utf-8"
            )
        )
        networks = {
            network["name"]: {
                "driver": network["driver"],
                "internal": network["internal"],
                "subnet": network["subnet"],
            }
            for network in variables["vps_docker_networks"]
        }
        self.assertEqual(
            networks["edge"],
            {
                "driver": "bridge",
                "internal": False,
                "subnet": "172.30.32.0/24",
            },
        )
        self.assertEqual(
            networks["app_monflorian"],
            {
                "driver": "bridge",
                "internal": False,
                "subnet": "172.30.40.0/24",
            },
        )
        self.assertEqual(len(networks), 8)
        self.assertEqual(
            len({network["subnet"] for network in networks.values()}),
            len(networks),
        )

    def test_dynamic_static_jobs_use_one_bounded_persistent_tmpfs(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/layout/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_static_jobs_backing_path"],
            "/run/private/vps-static-jobs",
        )
        self.assertEqual(
            defaults["vps_static_jobs_mount_unit"],
            r"run-private-vps\x2dstatic\x2djobs.mount",
        )
        self.assertEqual(defaults["vps_static_jobs_tmpfs_size_mib"], 384)
        self.assertEqual(defaults["vps_static_jobs_tmpfs_inodes"], 16384)

        unit_template = (
            ROOT
            / "ansible/roles/layout/templates/run-private-vps-static-jobs.mount.j2"
        ).read_text(encoding="utf-8")
        rendered_unit = Templar(
            loader=DataLoader(), variables=defaults
        ).template(trust_as_template(unit_template))
        self.assertIn("What=tmpfs\n", rendered_unit)
        self.assertIn(
            "Where=/run/private/vps-static-jobs\n",
            rendered_unit,
        )
        self.assertIn("Type=tmpfs\n", rendered_unit)
        self.assertIn(
            "Options=nodev,nosuid,noexec,mode=0700,size=384M,"
            "nr_inodes=16384\n",
            rendered_unit,
        )
        self.assertIn("DirectoryMode=0700\n", rendered_unit)
        self.assertIn("WantedBy=local-fs.target\n", rendered_unit)

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/layout/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        by_name = {task["name"]: task for task in tasks}
        contract_assertions = by_name[
            "Validate managed directory and network names"
        ]["ansible.builtin.assert"]["that"]
        mount_unit_assertion = next(
            assertion
            for assertion in contract_assertions
            if "vps_static_jobs_mount_unit" in assertion
        )
        self.assertIs(
            Environment().compile_expression(mount_unit_assertion)(**defaults),
            True,
        )
        content_probe = by_name[
            "Inspect content that would be shadowed by the static job mount"
        ]["ansible.builtin.command"]["argv"]
        self.assertEqual(
            content_probe[:2],
            ["/usr/bin/find", "{{ vps_static_jobs_backing_path }}"],
        )
        self.assertIn("-mindepth", content_probe)
        self.assertIn("-maxdepth", content_probe)
        self.assertIn("-quit", content_probe)
        required_options = by_name[
            "Define the required static job backing mount options"
        ]["ansible.builtin.set_fact"]["vps_static_jobs_required_options"]
        divergence = json.dumps(
            [
                required_options,
                by_name["Refuse a divergent active static job backing mount"],
            ],
            sort_keys=True,
        )
        for required in (
            "rw",
            "nosuid",
            "nodev",
            "noexec",
            "nr_inodes=",
            "mode=700",
        ):
            self.assertIn(required, divergence)
        mount_assertions = by_name[
            "Refuse a divergent active static job backing mount"
        ]["ansible.builtin.assert"]["that"]
        options_assertion = mount_assertions[3]
        live_options = [
            "rw",
            "nosuid",
            "nodev",
            "noexec",
            "relatime",
            "size=393216k",
            "nr_inodes=16384",
            "mode=700",
            "inode64",
        ]
        options_templar = Templar(
            loader=DataLoader(),
            variables={
                **defaults,
                "vps_static_jobs_active_options": live_options,
                "vps_static_jobs_required_options": live_options[:-1],
            },
        )
        self.assertIs(
            options_templar.template(
                trust_as_template("{{ " + options_assertion + " }}")
            ),
            True,
        )
        options_templar.available_variables[
            "vps_static_jobs_active_options"
        ] = [option for option in live_options if option != "noexec"] + ["exec"]
        self.assertIs(
            options_templar.template(
                trust_as_template("{{ " + options_assertion + " }}")
            ),
            False,
        )
        shadow_refusal = json.dumps(
            by_name["Refuse unsafe or shadowed static job backing content"],
            sort_keys=True,
        )
        self.assertIn("backing_content.stdout", shadow_refusal)
        self.assertIn("explicit migration", shadow_refusal)

        template_task = by_name[
            "Install the persistent static job tmpfs mount unit"
        ]["ansible.builtin.template"]
        self.assertEqual(
            template_task["dest"],
            "/etc/systemd/system/{{ vps_static_jobs_mount_unit }}",
        )

        mount_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"]
            == "Enable and start the static job tmpfs before installing the deploy controller"
        )
        retire_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"] == "Remove the obsolete fixed static worker account"
        )
        self.assertLess(mount_index, retire_index)
        mount_task = tasks[mount_index]["ansible.builtin.systemd_service"]
        self.assertEqual(mount_task["name"], "{{ vps_static_jobs_mount_unit }}")
        self.assertIs(mount_task["enabled"], True)
        self.assertEqual(mount_task["state"], "started")
        self.assertIs(mount_task["daemon_reload"], True)
        self.assertEqual(tasks[mount_index]["when"], "not ansible_check_mode")

        site = yaml.safe_load(
            (ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        )
        roles = [entry["role"] for entry in site[0]["roles"]]
        self.assertLess(roles.index("layout"), roles.index("deploy"))

    def test_repository_check_anchors_platform_candidate_to_full_git_history(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        )
        checkout = next(
            step
            for step in workflow["jobs"]["check"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(checkout["with"]["fetch-depth"], 0)

        repository_check = (SCRIPTS / "check").read_text(encoding="utf-8")
        self.assertIn("--print-platform-integration-revision", repository_check)
        self.assertIn("merge-base --is-ancestor", repository_check)
        self.assertIn('"$platform_integration_revision" HEAD', repository_check)

    def test_assert_conditions_are_yaml_strings(self) -> None:
        def inspect_assertions(node: object, path: Path) -> None:
            if isinstance(node, dict):
                assertion = node.get("ansible.builtin.assert")
                if isinstance(assertion, dict):
                    conditions = assertion.get("that", [])
                    if isinstance(conditions, str):
                        conditions = [conditions]
                    for condition in conditions:
                        self.assertIsInstance(
                            condition,
                            str,
                            f"{path}: assert condition parsed as {type(condition).__name__}",
                        )
                for value in node.values():
                    inspect_assertions(value, path)
            elif isinstance(node, list):
                for value in node:
                    inspect_assertions(value, path)

        for path in (ROOT / "ansible").rglob("*.yml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            inspect_assertions(document, path.relative_to(ROOT))

    def test_ufw_validation_accepts_rendered_ipv4_and_ipv6_rules(self) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/firewall/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/firewall/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        extraction = next(
            task
            for task in tasks
            if task["name"] == "Extract rules outside the exact managed ingress allowlist"
        )["ansible.builtin.set_fact"]
        assertions = next(
            task
            for task in tasks
            if task["name"]
            == "Assert UFW is active with the exact managed ingress allowlist"
        )["ansible.builtin.assert"]["that"]
        output_lines = [
            "Status: active",
            "",
            "     To                         Action      From",
            "     --                         ------      ----",
            "[ 1] 22/tcp                     ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 2] 80/tcp                     ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 3] 443/tcp                    ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 4] 443/udp                    ALLOW IN    Anywhere                   # vps-infra managed",
            "[ 5] 22/tcp (v6)                ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 6] 80/tcp (v6)                ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 7] 443/tcp (v6)               ALLOW IN    Anywhere (v6)              # vps-infra managed",
            "[ 8] 443/udp (v6)               ALLOW IN    Anywhere (v6)              # vps-infra managed",
        ]
        rendered_rules = output_lines[4:]
        templar = Templar(
            loader=DataLoader(),
            variables={
                "vps_ufw_numbered_status": {"stdout_lines": output_lines},
                "vps_firewall_managed_rule_pattern": defaults[
                    "vps_firewall_managed_rule_pattern"
                ],
            }
        )

        rule_lines = templar.template(
            trust_as_template(extraction["vps_ufw_rule_lines"])
        )
        unexpected_rule_lines = templar.template(
            trust_as_template(extraction["vps_ufw_unexpected_rule_lines"])
        )
        self.assertEqual(rule_lines, rendered_rules)
        self.assertEqual(unexpected_rule_lines, [])

        for condition, ipv4_rule, ipv6_rule in zip(
            assertions[3:],
            rendered_rules[:4],
            rendered_rules[4:],
            strict=True,
        ):
            for rule in (ipv4_rule, ipv6_rule):
                result = Templar(
                    loader=DataLoader(),
                    variables={"vps_ufw_rule_lines": [rule]}
                ).template(trust_as_template("{{ " + condition + " }}"))
                self.assertIs(result, True)

    def test_firewall_matches_original_published_ports(self) -> None:
        firewall = (
            ROOT / "ansible/roles/firewall/templates/docker-ingress-firewall.sh.j2"
        ).read_text(encoding="utf-8")
        normalized = firewall.replace("\\\n", "")
        rules = [
            'ipt -A "$managed_chain" -m conntrack '
            "--ctstate RELATED,ESTABLISHED -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p tcp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 80 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p tcp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 443 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -p udp   '
            "-m conntrack --ctstate NEW   "
            "-m conntrack --ctstate DNAT --ctdir ORIGINAL "
            "--ctorigdstport 443 -j ACCEPT",
            'ipt -A "$managed_chain" -i "$public_interface" -j DROP',
            'ipt -A "$managed_chain" -j RETURN',
        ]
        positions = [normalized.index(rule) for rule in rules]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("--dports", normalized)
        self.assertNotIn(" --dport ", normalized)
        self.assertNotIn("--ctstate NEW,DNAT", normalized)
        self.assertEqual(normalized.count("--ctstate NEW"), 3)
        self.assertEqual(normalized.count("--ctstate DNAT"), 3)
        self.assertEqual(normalized.count("--ctdir ORIGINAL"), 3)
        self.assertEqual(
            normalized.count('ipt -I DOCKER-USER 1 -j "$managed_chain"'),
            1,
        )
        self.assertEqual(
            normalized.count(
                'ipt -I DOCKER-USER 2 -i "$public_interface" -j DROP'
            ),
            1,
        )

    def test_docker_apt_refresh_runs_only_after_key_repository_or_pin_change(
        self,
    ) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/docker/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        handlers = yaml.safe_load(
            (ROOT / "ansible/roles/docker/handlers/main.yml").read_text(
                encoding="utf-8"
            )
        )
        key_index, key = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"]
            == "Download the Docker signing key with a pinned checksum"
        )
        repository_index, repository = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"] == "Configure the official Docker APT repository"
        )
        pin_index, pin = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"] == "Pin the selected Docker package versions"
        )
        refresh = next(
            handler
            for handler in handlers
            if handler["name"] == "Refresh Docker APT metadata"
        )
        flush_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"]
            == "Apply Docker repository handlers before package installation"
        )
        install_index = next(
            index
            for index, task in enumerate(tasks)
            if task["name"] == "Install exact Docker Engine and Compose versions"
        )

        self.assertEqual(key["notify"], "Refresh Docker APT metadata")
        self.assertEqual(repository["notify"], "Refresh Docker APT metadata")
        self.assertEqual(pin["notify"], "Refresh Docker APT metadata")
        self.assertIs(refresh["ansible.builtin.apt"]["update_cache"], True)
        self.assertFalse(
            any(
                task.get("ansible.builtin.apt", {}).get("update_cache") is True
                for task in tasks
            )
        )
        self.assertLess(key_index, repository_index)
        self.assertLess(repository_index, pin_index)
        self.assertLess(pin_index, flush_index)
        self.assertLess(flush_index, install_index)

    def test_normal_convergence_never_retires_an_administrator_key(self) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/ssh/tasks/main.yml").read_text(encoding="utf-8")
        )
        key_mutations = [
            task for task in tasks if "ansible.builtin.lineinfile" in task
        ]
        self.assertEqual(len(key_mutations), 1)
        key_mutation = key_mutations[0]["ansible.builtin.lineinfile"]
        self.assertIn("/.ssh/authorized_keys", key_mutation["path"])
        self.assertNotIn("state", key_mutation)

        drift_check = next(
            task
            for task in tasks
            if task["name"] == "Refuse undeclared administrator key drift"
        )
        drift_contract = json.dumps(drift_check, sort_keys=True)
        self.assertIn("difference", drift_contract)
        self.assertIn("separate explicit operation", drift_contract)

    def test_deployment_key_rotation_is_fail_closed_at_every_boundary(self) -> None:
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )

        def task_named(name: str) -> tuple[int, dict[str, object]]:
            return next(
                (index, task)
                for index, task in enumerate(tasks)
                if task["name"] == name
            )

        refusal_index, refusal = next(
            (index, task)
            for index, task in enumerate(tasks)
            if task["name"]
            == "Refuse convergence without a bounded deployment key set"
        )
        syntax_index, syntax = task_named("Validate deployment public key syntax")
        parse_index, parse = task_named(
            "Cryptographically parse deployment public keys"
        )
        overlap_index, overlap = task_named(
            "Measure overlap with a currently installed deployment key"
        )
        rotation_index, rotation = task_named(
            "Authorize only first install overlap or explicit unused recovery"
        )
        consume_index, consume = task_named(
            "Consume the exceptional deployment key recovery nonce"
        )
        raced_index, raced = task_named(
            "Refuse a raced or reused deployment key recovery nonce"
        )
        install_index, install = task_named("Install forced-command deployment keys")
        assertions = refusal["ansible.builtin.assert"]
        contract = json.dumps(assertions, sort_keys=True)

        self.assertIn("vps_deploy_authorized_keys is sequence", contract)
        self.assertIn("vps_deploy_authorized_keys is not string", contract)
        self.assertIn("vps_deploy_authorized_keys | length > 0", contract)
        self.assertIn("vps_deploy_authorized_keys | length <= 4", contract)
        self.assertIn("vps_deploy_authorized_keys | unique | length", contract)
        self.assertIn("Refus de vider les clés", assertions["fail_msg"])
        self.assertNotIn("when", refusal)
        self.assertNotIn("ignore_errors", refusal)

        parse_command = parse["ansible.builtin.command"]
        self.assertEqual(
            parse_command["argv"],
            ["/usr/bin/ssh-keygen", "-l", "-E", "sha256", "-f", "/dev/stdin"],
        )
        self.assertIs(parse["check_mode"], False)
        self.assertIs(parse["changed_when"], False)
        self.assertIs(parse_command["stdin_add_newline"], False)

        overlap_contract = json.dumps(overlap, sort_keys=True)
        rotation_contract = json.dumps(rotation, sort_keys=True)
        self.assertIn("intersect", overlap_contract)
        self.assertIn("deploy_first_key_install", rotation_contract)
        self.assertIn("deploy_key_overlap_count", rotation_contract)
        self.assertIn("vps_deploy_key_recovery_nonce", rotation_contract)
        self.assertIn("vps_deploy_authorized_keys | length == 1", rotation_contract)
        self.assertEqual(rotation["register"], "deploy_key_rotation_guard")
        self.assertNotIn("ignore_errors", rotation)

        consume_command = consume["ansible.builtin.command"]
        self.assertEqual(consume_command["argv"][0:2], ["/usr/bin/python3", "-c"])
        self.assertIn("os.O_EXCL", consume_command["argv"][2])
        self.assertIn("os.fsync", consume_command["argv"][2])
        self.assertIn("os.O_DIRECTORY", consume_command["argv"][2])
        self.assertLess(
            consume_command["argv"][2].index("os.fsync(marker.fileno())"),
            consume_command["argv"][2].index("os.O_DIRECTORY"),
        )
        self.assertLess(
            consume_command["argv"][2].index("os.O_DIRECTORY"),
            consume_command["argv"][2].index("os.fsync(directory)"),
        )
        self.assertIn("installed=", consume_command["argv"][4])
        self.assertIn("desired=", consume_command["argv"][4])
        with tempfile.TemporaryDirectory() as marker_directory:
            marker_path = Path(marker_directory) / "one-use"
            marker_argv = [
                *consume_command["argv"][0:3],
                str(marker_path),
                "revision=test\ninstalled=old\ndesired=new\n",
            ]
            first_consumption = subprocess.run(
                marker_argv,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            second_consumption = subprocess.run(
                marker_argv,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(first_consumption.returncode, 0)
            self.assertNotEqual(second_consumption.returncode, 0)
            self.assertEqual(
                marker_path.read_text(encoding="utf-8"),
                "revision=test\ninstalled=old\ndesired=new\n",
            )
        self.assertIs(consume["changed_when"], True)
        self.assertIn("not ansible_check_mode", consume["when"])
        self.assertIn(
            "ansible_check_mode or",
            json.dumps(raced["ansible.builtin.assert"], sort_keys=True),
        )
        self.assertLess(refusal_index, syntax_index)
        self.assertLess(syntax_index, parse_index)
        self.assertLess(parse_index, overlap_index)
        self.assertLess(overlap_index, rotation_index)
        self.assertLess(rotation_index, consume_index)
        self.assertLess(consume_index, raced_index)
        self.assertLess(raced_index, install_index)

        install_template = install["ansible.builtin.template"]
        validate = install_template["validate"]
        self.assertEqual(validate.count("%s"), 1)
        self.assertTrue(validate.endswith("vps-deploy-authorized-keys-validator %s"))
        self.assertIn("/bin/sh -c 'set -eu;", validate)
        self.assertIn("restrict,command=", validate)
        self.assertIn("ssh-keygen -l -E sha256", validate)
        self.assertIn("[ \"$count\" -ge 1 ]", validate)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            key_path = temporary_root / "deploy-test"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key_path),
                ],
                check=True,
            )
            public_key = key_path.with_suffix(".pub").read_text(
                encoding="utf-8"
            ).strip()
            prefix = (
                'restrict,command="/usr/local/libexec/vps/forced-command" '
            )
            candidates = {
                "valid": (f"{prefix}{public_key}\n", 0),
                "empty": ("", 1),
                "mixed-invalid": (
                    f"not-a-key\n{prefix}{public_key}\n",
                    1,
                ),
                "wrong-prefix": (f"command=\"/bin/sh\" {public_key}\n", 1),
            }
            for name, (content, expected_success) in candidates.items():
                candidate = temporary_root / name
                candidate.write_text(content, encoding="utf-8")
                validation = subprocess.run(
                    shlex.split(validate.replace("%s", str(candidate))),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertEqual(
                    validation.returncode == 0,
                    expected_success == 0,
                    name,
                )

        template = (
            ROOT / "ansible/roles/deploy/templates/authorized_keys.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("deploy_key_rotation_guard is defined", template)
        self.assertIn("deploy_key_recovery_marker_creation is defined", template)
        self.assertIn("ansible_check_mode | default(false) | bool", template)
        self.assertIn("vps-deploy-authorized-keys-render-refused", template)

        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        )
        pre_tasks = playbook[0]["pre_tasks"]
        preflight = pre_tasks[0]["ansible.builtin.assert"]
        preflight_contract = json.dumps(preflight, sort_keys=True)
        self.assertIn("vps_deploy_authorized_keys | length > 0", preflight_contract)
        self.assertIn("vps_deploy_authorized_keys | length <= 4", preflight_contract)
        self.assertIn(
            "vps_deploy_authorized_keys | unique | length", preflight_contract
        )

        preflight_parse = next(
            task
            for task in pre_tasks
            if task["name"]
            == "Cryptographically parse deployment public keys before any role"
        )
        self.assertIs(preflight_parse["check_mode"], False)
        self.assertIs(preflight_parse["changed_when"], False)
        self.assertNotIn("delegate_to", preflight_parse)
        self.assertEqual(
            preflight_parse["ansible.builtin.command"]["argv"],
            ["/usr/bin/ssh-keygen", "-l", "-E", "sha256", "-f", "/dev/stdin"],
        )
        preflight_nonce = next(
            task
            for task in pre_tasks
            if task["name"]
            == "Validate the one-use deployment key recovery nonce contract"
        )
        nonce_contract = json.dumps(
            preflight_nonce["ansible.builtin.assert"], sort_keys=True
        )
        self.assertIn("vps_deploy_key_recovery_nonce | default('')", nonce_contract)
        deploy_defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(deploy_defaults["vps_deploy_key_recovery_nonce"], "")

    def test_deployment_key_identity_filters_use_real_ansible_backreferences(
        self,
    ) -> None:
        role_tasks = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        )
        pre_tasks = playbook[0]["pre_tasks"]

        def task_named(tasks: list[dict[str, object]], name: str) -> dict[str, object]:
            return next(task for task in tasks if task["name"] == name)

        def render(expression: str, variables: dict[str, object]) -> object:
            return Templar(
                loader=DataLoader(),
                variables=variables,
            ).template(trust_as_template(expression))

        site_normalization = task_named(
            pre_tasks,
            "Normalize desired deployment key identities before any role",
        )["ansible.builtin.set_fact"]["site_deploy_key_identities"]
        role_normalization = task_named(
            role_tasks,
            "Normalize installed and desired deployment key records",
        )["ansible.builtin.set_fact"]["deploy_desired_key_identities"]
        site_duplicate_condition = task_named(
            pre_tasks,
            "Refuse duplicate deployment key identities before any role",
        )["ansible.builtin.assert"]["that"][0]
        role_duplicate_condition = task_named(
            role_tasks,
            "Refuse duplicate desired deployment key identities",
        )["ansible.builtin.assert"]["that"][0]

        distinct_keys = [
            "ssh-ed25519 AAAA1111 first deploy key",
            "sk-ssh-ed25519@openssh.com BBBB2222 second deploy key",
        ]
        distinct_identities = [
            "ssh-ed25519 AAAA1111",
            "sk-ssh-ed25519@openssh.com BBBB2222",
        ]
        duplicate_keys = [
            "ssh-ed25519 CCCC3333 old comment",
            "ssh-ed25519 CCCC3333 new comment",
        ]
        duplicate_identities = [
            "ssh-ed25519 CCCC3333",
            "ssh-ed25519 CCCC3333",
        ]

        for expression in (site_normalization, role_normalization):
            self.assertEqual(
                render(
                    expression,
                    {"vps_deploy_authorized_keys": distinct_keys},
                ),
                distinct_identities,
            )
            self.assertEqual(
                render(
                    expression,
                    {"vps_deploy_authorized_keys": duplicate_keys},
                ),
                duplicate_identities,
            )

        self.assertIs(
            render(
                "{{ " + site_duplicate_condition + " }}",
                {"site_deploy_key_identities": distinct_identities},
            ),
            True,
        )
        self.assertIs(
            render(
                "{{ " + role_duplicate_condition + " }}",
                {"deploy_desired_key_identities": distinct_identities},
            ),
            True,
        )
        self.assertIs(
            render(
                "{{ " + site_duplicate_condition + " }}",
                {"site_deploy_key_identities": duplicate_identities},
            ),
            False,
        )
        self.assertIs(
            render(
                "{{ " + role_duplicate_condition + " }}",
                {"deploy_desired_key_identities": duplicate_identities},
            ),
            False,
        )

        installed_extraction = task_named(
            role_tasks,
            "Extract only exact installed forced-command key identities",
        )["ansible.builtin.set_fact"]["deploy_installed_key_identities"]
        forced_command = (
            'restrict,command="/usr/local/libexec/vps/forced-command" '
        )
        installed_lines = [
            f"{forced_command}{distinct_keys[0]}",
            f"{forced_command}{distinct_keys[1]}",
            (
                'restrict,command="/usr/local/libexec/vps/forced-command-extra" '
                "ssh-ed25519 DDDD4444 wrong command"
            ),
            (
                'no-port-forwarding,command="/usr/local/libexec/vps/forced-command" '
                "ssh-ed25519 EEEE5555 wrong options"
            ),
        ]
        self.assertEqual(
            render(
                installed_extraction,
                {"deploy_installed_key_lines": installed_lines},
            ),
            distinct_identities,
        )

    def test_total_loss_recovery_uses_one_nonce_and_a_negative_auth_probe(
        self,
    ) -> None:
        runbook = (
            ROOT / "docs/operations/static-release-reconciliation.md"
        ).read_text(encoding="utf-8")

        self.assertIn("vps_deploy_key_recovery_nonce", runbook)
        self.assertIn("openssl rand -hex 32", runbook)
        self.assertIn("The same\nnonce cannot be reused", runbook)
        self.assertIn("vps_deploy_key_recovery_nonce: \"\"", runbook)
        self.assertIn("neither consumes the nonce nor\nreplaces", runbook)
        self.assertIn("point-of-mutation template validator", runbook)
        self.assertIn("-o IdentityAgent=none", runbook)
        self.assertIn("-o ControlMaster=no", runbook)
        self.assertIn("'deploy auth-proof'", runbook)
        self.assertIn("proof_status\" -eq 255", runbook)
        self.assertIn("proof_status\" -ne 64", runbook)
        self.assertIn(
            "forced-command parser: malformed deploy command",
            runbook,
        )

    def test_root_test_mode_stops_before_controller_paths_are_selected(self) -> None:
        controller = (SCRIPTS / "deploy").read_text(encoding="utf-8")
        guard = controller.index("root cannot use controller test mode")
        production_path = controller.index("readonly state_dir=/var/lib/vps-controller")
        first_mutation = controller.index("install -d -m 0700")
        self.assertLess(guard, production_path)
        self.assertLess(guard, first_mutation)
        self.assertIn("GIT_CONFIG_PARAMETERS", controller)

    def test_convergence_uses_an_isolated_origin_main_archive(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        self.assertIn("./scripts/converge", makefile)
        self.assertIn("./scripts/converge --check --diff", makefile)
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", converge)
        self.assertIn("archive --format=tar \"$revision\"", converge)
        self.assertIn("cd \"$checkout\"", converge)
        self.assertIn(
            'isolated_inventory="$isolated_inventory_directory/hosts.yml"',
            converge,
        )
        self.assertIn(
            'isolated_inventory_group_vars="$isolated_inventory_directory/'
            'group_vars/all.yml"',
            converge,
        )
        self.assertIn(
            '"$install_executable" -m 0600 -- "$inventory_source" '
            '"$isolated_inventory"',
            converge,
        )
        self.assertIn('--inventory "$isolated_inventory"', converge)
        self.assertNotIn('--inventory "$inventory"', converge)
        self.assertNotIn('--inventory "$inventory_source"', converge)
        self.assertIn('--extra-vars "vps_infra_revision=$revision"', converge)
        self.assertIn('"${ansible_playbook_options[@]}"', converge)

    def test_tasks_forced_outside_check_mode_are_read_only(self) -> None:
        expected = {
            (
                "base",
                "Detect whether the swap file has a swap signature",
            ): [
                "/usr/sbin/blkid",
                "-o",
                "value",
                "-s",
                "TYPE",
                "{{ vps_swap_path }}",
            ],
            ("codex_cli", "Read an existing Codex convergence lease"): [
                "/usr/bin/systemctl",
                "show",
                "{{ vps_codex_convergence_unit }}",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=Result",
            ],
            ("base", "Read active swap devices"): [
                "/usr/bin/awk",
                "NR>1 {print $1}",
                "/proc/swaps",
            ],
            ("codex_cli", "Read the selected Codex release target"): [
                "/usr/bin/readlink",
                "--canonicalize",
                "{{ vps_codex_install_root }}/current",
            ],
            ("codex_cli", "Read active Atlas Codex session state before mutation"): [
                "/usr/bin/systemctl",
                "is-active",
                "atlas-codex-session.service",
            ],
            ("codex_cli", "Read pinned Ubuntu bubblewrap package metadata"): [
                "/usr/bin/apt-cache",
                "show",
                "bubblewrap={{ vps_codex_bubblewrap_version }}",
            ],
            (
                "codex_cli",
                "Read the installed bubblewrap version before reconciliation",
            ): [
                "/usr/bin/dpkg-query",
                "--show",
                "--showformat=${db:Status-Status} ${Version}",
                "bubblewrap",
            ],
            (
                "codex_cli",
                "Read the installed bubblewrap version after reconciliation",
            ): [
                "/usr/bin/dpkg-query",
                "--show",
                "--showformat=${Version}",
                "bubblewrap",
            ],
            ("codex_cli", "Inspect distribution bubblewrap file capabilities"): [
                "/usr/sbin/getcap",
                "/usr/bin/bwrap",
            ],
            ("codex_cli", "Read effective aggregate Codex resource limits"): [
                "/usr/bin/systemctl",
                "show",
                "atlas-codex.slice",
                "--property=CPUQuotaPerSecUSec",
                "--property=MemoryHigh",
                "--property=MemoryMax",
                "--property=MemorySwapMax",
                "--property=TasksMax",
                "--property=CPUWeight",
                "--property=IOWeight",
            ],
            ("codex_cli", "Inspect an active Codex storage mount"): [
                "/usr/bin/findmnt",
                "--json",
                "--mountpoint",
                "{{ vps_codex_storage_root }}",
                "--output",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
            ],
            (
                "codex_cli",
                "Inspect content that would be shadowed by Codex storage",
            ): [
                "/usr/bin/find",
                "{{ vps_codex_storage_root }}",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            ("codex_cli", "Read the active Codex storage backing file"): [
                "/usr/sbin/losetup",
                "--noheadings",
                "--output",
                "BACK-FILE",
                "{{ vps_codex_active_storage_mount.source }}",
            ],
            ("codex_cli", "Read the bounded Codex storage filesystem type"): [
                "/usr/sbin/blkid",
                "-o",
                "value",
                "-s",
                "TYPE",
                "{{ vps_codex_storage_image_path }}",
            ],
            ("codex_cli", "Read existing fstab entries for the Codex mountpoint"): [
                "/usr/bin/awk",
                '$1 !~ /^#/ && $2 == "{{ vps_codex_storage_root }}" {print $0}',
                "/etc/fstab",
            ],
            ("codex_cli", "Read free space before allocating Codex storage"): [
                "/usr/bin/df",
                "--block-size=1M",
                "--output=avail",
                "{{ vps_codex_storage_image_path | dirname }}",
            ],
            ("codex_cli", "Reinspect the activated Codex storage mount"): [
                "/usr/bin/findmnt",
                "--json",
                "--mountpoint",
                "{{ vps_codex_storage_root }}",
                "--output",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
            ],
            ("codex_cli", "Read the activated Codex storage backing file"): [
                "/usr/sbin/losetup",
                "--noheadings",
                "--output",
                "BACK-FILE",
                "{{ vps_codex_activated_storage_mount.source }}",
            ],
            ("codex_cli", "Read the Codex account groups"): [
                "/usr/bin/id",
                "-nG",
                "{{ vps_codex_user }}",
            ],
            ("codex_cli", "Read the Codex remote gateway account groups"): [
                "/usr/bin/id",
                "-nG",
                "{{ vps_codex_remote_user }}",
            ],
            (
                "codex_cli",
                "Prove ordinary direct Codex invocation uses the guarded entry point",
            ): [
                "/usr/local/bin/codex",
                "--version",
            ],
            ("codex_cli", "Probe prohibited Codex account capabilities"): [
                "/bin/bash",
                "-c",
                "test ! -r /etc/vps/secrets && "
                "test ! -x /etc/vps/secrets && "
                "test ! -r /home/vpsadmin && "
                "test ! -r /srv/vps/repository && "
                "test ! -r /run/containerd/containerd.sock && "
                "test ! -r /var/run/docker.sock && "
                "test -x {{ vps_codex_storage_root }} && "
                "test -w {{ vps_codex_workspace_root }} && "
                "! /usr/bin/sudo -n /usr/bin/true",
            ],
            ("codex_cli", "Probe prohibited Codex remote gateway capabilities"): [
                "/bin/bash",
                "-c",
                "test ! -r {{ vps_codex_home }}/.codex/auth.json && "
                "test ! -r {{ vps_codex_home }}/.codex/config.toml && "
                "test ! -r /etc/vps/secrets && "
                "test ! -r /srv/vps/repository && "
                "test ! -r /run/containerd/containerd.sock && "
                "test ! -r /var/run/docker.sock && "
                "! /usr/bin/sudo -n /usr/bin/true",
            ],
            ("deploy", "Read the infrastructure mirror origin"): [
                "/usr/bin/git",
                "-C",
                "{{ vps_deploy_repository_dir }}",
                "remote",
                "get-url",
                "origin",
            ],
            ("deploy", "Read deployment account groups"): [
                "/usr/bin/id",
                "-nG",
                "{{ vps_deploy_user }}",
            ],
            ("deploy", "Cryptographically parse deployment public keys"): [
                "/usr/bin/ssh-keygen",
                "-l",
                "-E",
                "sha256",
                "-f",
                "/dev/stdin",
            ],
            ("deploy", "Inspect whether the deployment account already exists"): [
                "/usr/bin/getent",
                "passwd",
                "{{ vps_deploy_user }}",
            ],
            (
                "deploy",
                "Preflight each selected Mon Florian existing-file adoption",
            ): [
                "{{ vps_monflorian_secret_materializer_path }}",
                "--check-adopt-existing",
                "{{ item }}",
            ],
            (
                "deploy",
                "Audit Mon Florian secret metadata and generation markers",
            ): [
                "{{ vps_monflorian_secret_materializer_path }}",
                "--check",
            ],
            ("docker", "Read the Docker key fingerprints"): [
                "/usr/bin/gpg",
                "--batch",
                "--show-keys",
                "--with-colons",
                "/etc/apt/keyrings/docker.asc",
            ],
            ("docker", "Read installed Docker package versions"): [
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${Version}",
                "{{ item.name }}",
            ],
            ("firewall", "Read effective UFW policy"): [
                "/usr/sbin/ufw",
                "status",
                "verbose",
            ],
            ("firewall", "Read every numbered UFW rule"): [
                "/usr/sbin/ufw",
                "status",
                "numbered",
            ],
            ("layout", "Inspect managed external Docker networks"): [
                "/usr/bin/docker",
                "network",
                "inspect",
                "{{ item.name }}",
            ],
            (
                "parkventory_postgres",
                "Inspect the exact private Parkventory database network",
            ): [
                "/usr/bin/docker",
                "network",
                "inspect",
                "db_parkventory",
            ],
            (
                "parkventory_postgres",
                "Read the effective internal platform activation state",
            ): [
                "/usr/bin/systemctl",
                "is-active",
                "vps-internal-platform.service",
            ],
            (
                "parkventory_postgres",
                "Run the reviewed-source Parkventory secret plan in check mode",
            ): [
                "/usr/bin/python3",
                "-c",
                "{{ lookup('ansible.builtin.file', playbook_dir ~ "
                "'/../../scripts/materialize-parkventory-secrets', "
                "rstrip=false) }}",
                "--dry-run",
            ],
            (
                "parkventory_postgres",
                "Run the reviewed-source Parkventory PostgreSQL plan in check mode",
            ): [
                "/usr/bin/python3",
                "-c",
                "{{ lookup('ansible.builtin.file', playbook_dir ~ "
                "'/../../scripts/provision-parkventory-postgres', "
                "rstrip=false) }}",
                "--dry-run",
                "--embedded-contract",
            ],
            ("layout", "Inspect an active static job backing mount"): [
                "/usr/bin/findmnt",
                "--json",
                "--mountpoint",
                "{{ vps_static_jobs_backing_path }}",
                "--output",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
            ],
            (
                "layout",
                "Inspect content that would be shadowed by the static job mount",
            ): [
                "/usr/bin/find",
                "{{ vps_static_jobs_backing_path }}",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
                "-quit",
            ],
            ("ssh", "Read effective deploy SSH policy"): [
                "/usr/sbin/sshd",
                "-T",
                "-C",
                "user={{ vps_deploy_user }},host=localhost,addr=127.0.0.1",
            ],
            ("ssh", "Read effective administrator SSH policy"): [
                "/usr/sbin/sshd",
                "-T",
                "-C",
                "user={{ vps_admin_user }},host=localhost,addr=127.0.0.1",
            ],
            ("ssh", "Read effective Codex remote SSH policy"): [
                "/usr/sbin/sshd",
                "-T",
                "-C",
                "user={{ vps_codex_remote_user }},host=localhost,addr=127.0.0.1",
            ],
        }
        observed: dict[tuple[str, str], list[str]] = {}
        task_files = sorted((ROOT / "ansible/roles").glob("*/tasks/main.yml"))
        task_files.extend(
            [
                ROOT / "ansible/roles/codex_cli/tasks/converge.yml",
                ROOT / "ansible/roles/codex_cli/tasks/activate.yml",
            ]
        )
        for task_file in task_files:
            role = task_file.parent.parent.name
            for task in yaml.safe_load(task_file.read_text(encoding="utf-8")):
                if task.get("check_mode") is not False:
                    continue
                module_keys = {
                    key for key in task if key.startswith("ansible.builtin.")
                }
                self.assertEqual(module_keys, {"ansible.builtin.command"})
                self.assertIs(task.get("changed_when"), False)
                observed[(role, task["name"])] = task["ansible.builtin.command"][
                    "argv"
                ]

        self.assertEqual(observed, expected)

    def test_convergence_keeps_the_ssh_control_path_short(self) -> None:
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        self.assertIn("umask 077", converge)
        self.assertIn(
            'temporary_root=$("$mktemp_executable" -d '
            "/tmp/vps-c.XXXXXXXX)",
            converge,
        )
        representative_control_path = (
            Path("/tmp").resolve()
            / "vps-c.12345678/cp"
            / (
                # OpenSSH expands %C to a 40-character SHA-1 and briefly
                # appends a dot plus 16 random characters before rename.
                "0123456789abcdef0123456789abcdef01234567"
                ".ABCDEFGHIJKLMNOP"
            )
        )
        self.assertLess(len(os.fsencode(representative_control_path)), 104)
        self.assertIn(
            '"ANSIBLE_SSH_CONTROL_PATH_DIR=$control_path_dir"',
            converge,
        )
        self.assertEqual(converge.count("ANSIBLE_SSH_CONTROL_PATH_DIR"), 1)
        self.assertIn(
            'mkdir -m 0700 -- "$checkout" "$isolated_home" "$control_path_dir"',
            converge,
        )

    def test_convergence_explicitly_trusts_the_captured_mise_config(self) -> None:
        converge = (SCRIPTS / "converge").read_text(encoding="utf-8")
        trust = 'clean_command "$mise_executable" trust "$checkout/mise.toml"'
        locked_install = 'clean_command "$mise_executable" install --locked'
        install = "install_locked_tools"
        self.assertIn(trust, converge)
        self.assertIn(locked_install, converge)
        bootstrap = converge[converge.index('(\n  cd "$checkout"') :]
        self.assertLess(bootstrap.index(trust), bootstrap.index(install))

    def test_convergence_executes_the_captured_remote_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "work", root], check=True)

            (root / "ansible/collections").mkdir(parents=True)
            (root / "ansible/playbooks").mkdir(parents=True)
            (root / "ansible/inventories/production/group_vars").mkdir(
                parents=True
            )
            (root / "ansible/ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
            (root / "ansible/collections/requirements.yml").write_text(
                "collections: []\n",
                encoding="utf-8",
            )
            (root / "ansible/playbooks/site.yml").write_text(
                "---\n- hosts: all\n  gather_facts: false\n",
                encoding="utf-8",
            )
            (root / "ansible/inventories/production/group_vars/all.yml").write_text(
                "source_marker: remote-main\n",
                encoding="utf-8",
            )
            for name in ("mise.toml", "mise.lock", "pyproject.toml", "uv.lock"):
                (root / name).write_text(f"remote {name}\n", encoding="utf-8")
            (root / "marker.txt").write_text("remote-main\n", encoding="utf-8")
            subprocess.run(["git", "-C", root, "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "remote main",
                ],
                check=True,
            )
            remote_sha = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()

            (root / "marker.txt").write_text("divergent-worktree\n", encoding="utf-8")
            (root / "ansible/inventories/production/group_vars/all.yml").write_text(
                "source_marker: divergent-worktree\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "add",
                    "marker.txt",
                    "ansible/inventories/production/group_vars/all.yml",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "divergent worktree",
                ],
                check=True,
            )
            divergent_sha = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            archived_private_inventory = (
                root / "ansible/inventories/production/hosts.yml"
            )
            archived_private_inventory.write_text("all: {}\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "add",
                    "ansible/inventories/production/hosts.yml",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=VPS tests",
                    "-c",
                    "user.email=vps-tests@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "invalid archived inventory",
                ],
                check=True,
            )
            archived_inventory_sha = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            (root / "ansible/roles/rogue/tasks").mkdir(parents=True)
            (root / "ansible/roles/rogue/tasks/main.yml").write_text(
                "---\n- debug: msg=rogue\n",
                encoding="utf-8",
            )

            expected_origin = "https://github.com/nclsppr/vps-infra.git"
            subprocess.run(
                ["git", "-C", root, "remote", "add", "origin", expected_origin],
                check=True,
            )
            self.assertNotEqual(remote_sha, divergent_sha)
            self.assertFalse(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        root,
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/remotes/origin/main",
                    ],
                    check=False,
                ).returncode
                == 0
            )

            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            converge = scripts_dir / "converge"
            shutil.copy2(SCRIPTS / "converge", converge)
            converge.chmod(0o755)

            support = Path(temporary) / "support"
            fake_bin = support / "bin"
            fake_bin.mkdir(parents=True)
            log = support / "execution.log"
            remote_revision_file = support / "remote-revision"
            remote_revision_file.write_text(f"{remote_sha}\n", encoding="utf-8")
            galaxy_template = support / "ansible-galaxy"
            playbook_template = support / "ansible-playbook"

            def write_executable(path: Path, content: str) -> None:
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)

            quoted_root = shlex.quote(str(root))
            quoted_log = shlex.quote(str(log))
            quoted_remote_revision_file = shlex.quote(str(remote_revision_file))
            quoted_divergent_sha = shlex.quote(divergent_sha)
            real_git = shlex.quote(shutil.which("git") or "/usr/bin/git")
            fetch_signature = (
                " --force --no-tags --no-recurse-submodules origin "
                "+refs/heads/main:refs/remotes/origin/main "
            )
            write_executable(
                fake_bin / "git",
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
case " $* " in
  *" fetch "*)
    printf '%s\\n' "$*" >>{quoted_log}
    case " $* " in
      *"{fetch_signature}"*) ;;
      *) exit 91 ;;
    esac
    [ ! -e {quoted_root}/fail-fetch ] || exit 42
    IFS= read -r selected_remote_revision < {quoted_remote_revision_file}
    {real_git} -C {quoted_root} update-ref \
      refs/remotes/origin/main "$selected_remote_revision"
    exit 0
    ;;
  *" rev-parse "*"refs/remotes/origin/main"*)
    revision=$({real_git} "$@")
    {real_git} -C {quoted_root} update-ref refs/remotes/origin/main {quoted_divergent_sha}
    printf '%s\\n' "$revision"
    exit 0
    ;;
esac
exec {real_git} "$@"
""",
            )
            write_executable(
                galaxy_template,
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
printf 'galaxy=%s\\n' "$PWD $*" >>{quoted_log}
""",
            )
            write_executable(
                playbook_template,
                f"""#!/bin/sh
set -eu
[ -z "${{ANSIBLE_LIBRARY+x}}" ]
[ -n "${{SSH_AUTH_SOCK+x}}" ]
[ -d "$ANSIBLE_SSH_CONTROL_PATH_DIR" ]
inventory_path=
expect_inventory=false
for argument in "$@"; do
  if [ "$expect_inventory" = true ]; then
    inventory_path=$argument
    expect_inventory=false
    continue
  fi
  [ "$argument" != --inventory ] || expect_inventory=true
done
[ "$expect_inventory" = false ]
[ -n "$inventory_path" ]
inventory_directory=$(CDPATH='' cd -- "$(dirname -- "$inventory_path")" && pwd -P)
[ "$inventory_directory/$(basename -- "$inventory_path")" = \
  "$PWD/inventories/production/hosts.yml" ]
inventory_mode=$(stat -c '%a' "$inventory_path" 2>/dev/null || \
  stat -f '%Lp' "$inventory_path")
[ "$inventory_mode" = 600 ]
IFS= read -r marker < ../marker.txt
IFS= read -r inventory_marker < "$inventory_path"
IFS= read -r group_vars_marker < inventories/production/group_vars/all.yml
printf 'playbook_directory=%s\\n' "$PWD" >>{quoted_log}
printf 'marker=%s\\n' "$marker" >>{quoted_log}
printf 'inventory_path=%s\\n' "$inventory_path" >>{quoted_log}
printf 'inventory_mode=%s\\n' "$inventory_mode" >>{quoted_log}
printf 'inventory_marker=%s\\n' "$inventory_marker" >>{quoted_log}
printf 'group_vars_marker=%s\\n' "$group_vars_marker" >>{quoted_log}
printf 'control_path_dir=%s\\n' "$ANSIBLE_SSH_CONTROL_PATH_DIR" >>{quoted_log}
printf 'ssh_auth_sock=%s\\n' "$SSH_AUTH_SOCK" >>{quoted_log}
printf 'arguments=%s\\n' "$*" >>{quoted_log}
""",
            )
            write_executable(
                fake_bin / "mise",
                f"""#!/bin/sh
set -eu
[ -z "${{SSH_AUTH_SOCK+x}}" ]
[ -z "${{ANSIBLE_SSH_CONTROL_PATH_DIR+x}}" ]
printf 'mise=%s\\n' "$PWD $*" >>{quoted_log}
if [ "${{1:-}}" = install ] && [ -e {quoted_root}/retry-mise-install ]; then
  mkdir -p "$XDG_CACHE_HOME"
  attempt_file="$XDG_CACHE_HOME/mise-install-attempt"
  attempt=1
  if [ -f "$attempt_file" ]; then
    IFS= read -r previous_attempt < "$attempt_file"
    attempt=$((previous_attempt + 1))
  fi
  printf '%s\\n' "$attempt" > "$attempt_file"
  printf 'mise_install_attempt=%s home=%s cache=%s\\n' \
    "$attempt" "$HOME" "$XDG_CACHE_HOME" >>{quoted_log}
  [ "$attempt" -ge 3 ] || exit 35
fi
if [ "${{1:-}}" = install ] && [ -e {quoted_root}/fail-mise-install ]; then
  printf 'mise_install_permanent_failure home=%s cache=%s\\n' \
    "$HOME" "$XDG_CACHE_HOME" >>{quoted_log}
  exit 36
fi
if [ "${{1:-}}" = exec ]; then
  mkdir -p .venv/bin
  cp {shlex.quote(str(galaxy_template))} .venv/bin/ansible-galaxy
  cp {shlex.quote(str(playbook_template))} .venv/bin/ansible-playbook
fi
""",
            )

            inventory = support / "hosts.yml"
            extra_vars = support / "keys.yml"
            inventory.write_text("all: {}\n", encoding="utf-8")
            (support / "group_vars").mkdir()
            (support / "group_vars/all.yml").write_text(
                "source_marker: adjacent-external\n",
                encoding="utf-8",
            )
            extra_vars.write_text("vps_admin_authorized_keys: []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "ANSIBLE_INVENTORY": str(inventory),
                    "ANSIBLE_EXTRA_VARS": str(extra_vars),
                    "ANSIBLE_LIBRARY": "/untrusted/ansible/library",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "protocol.file.allow",
                    "GIT_CONFIG_VALUE_0": "always",
                    "PYTHONPATH": "/untrusted/python",
                }
            )
            invalid_socket = support / "not-a-socket"
            invalid_socket.write_text("not a socket\n", encoding="utf-8")
            invalid_environment = environment.copy()
            invalid_environment["SSH_AUTH_SOCK"] = str(invalid_socket)
            invalid_agent = subprocess.run(
                [converge],
                cwd=root,
                env=invalid_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invalid_agent.returncode, 78)
            self.assertIn("caller-owned Unix socket", invalid_agent.stderr)
            self.assertFalse(log.exists())

            agent_socket_path = support / "agent.sock"
            agent_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(agent_socket.close)
            agent_socket.bind(str(agent_socket_path))
            environment["SSH_AUTH_SOCK"] = str(agent_socket_path)
            result = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            execution = log.read_text(encoding="utf-8")
            self.assertIn("marker=remote-main", execution)
            self.assertNotIn("divergent-worktree", execution)
            self.assertNotIn("adjacent-external", execution)
            self.assertNotIn(str(inventory), execution)
            self.assertIn("inventory_marker=all: {}", execution)
            self.assertIn("inventory_mode=600", execution)
            self.assertIn("group_vars_marker=source_marker: remote-main", execution)
            self.assertRegex(
                execution,
                r"(?m)^inventory_path=/(?:private/)?tmp/vps-c\.[A-Za-z0-9]+/"
                r"checkout/ansible/"
                r"inventories/production/hosts\.yml$",
            )
            self.assertIn(
                f"ssh_auth_sock={agent_socket_path.resolve()}",
                execution,
            )
            self.assertRegex(
                execution,
                r"(?m)^control_path_dir=/tmp/vps-c\.[A-Za-z0-9]+/cp$",
            )
            mise_calls = [
                line for line in execution.splitlines() if line.startswith("mise=")
            ]
            self.assertEqual(len(mise_calls), 3)
            self.assertIn(" trust ", mise_calls[0])
            self.assertTrue(mise_calls[0].endswith("/mise.toml"))
            self.assertIn(" install --locked", mise_calls[1])
            self.assertIn(" exec -- uv sync --locked", mise_calls[2])
            self.assertIn(f"vps_infra_revision={remote_sha}", execution)
            self.assertIn(
                "+refs/heads/main:refs/remotes/origin/main",
                execution,
            )

            log.unlink()
            (root / "retry-mise-install").touch()
            retried_install = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(retried_install.returncode, 0, retried_install.stderr)
            self.assertIn(
                "attempt 1 of 3 failed; retrying with the same isolated cache",
                retried_install.stderr,
            )
            self.assertIn(
                "attempt 2 of 3 failed; retrying with the same isolated cache",
                retried_install.stderr,
            )
            retried_execution = log.read_text(encoding="utf-8")
            retry_records = [
                line
                for line in retried_execution.splitlines()
                if line.startswith("mise_install_attempt=")
            ]
            self.assertEqual(len(retry_records), 3)
            self.assertEqual(
                [record.split(maxsplit=1)[0] for record in retry_records],
                [
                    "mise_install_attempt=1",
                    "mise_install_attempt=2",
                    "mise_install_attempt=3",
                ],
            )
            retry_environments = [
                record.split(maxsplit=1)[1] for record in retry_records
            ]
            self.assertEqual(len(set(retry_environments)), 1)
            retried_mise_calls = [
                line
                for line in retried_execution.splitlines()
                if line.startswith("mise=")
            ]
            self.assertEqual(len(retried_mise_calls), 5)
            self.assertEqual(
                sum(" install --locked" in line for line in retried_mise_calls),
                3,
            )
            self.assertEqual(
                sum(" trust " in line for line in retried_mise_calls),
                1,
            )
            self.assertEqual(
                sum(" exec -- uv sync --locked" in line for line in retried_mise_calls),
                1,
            )
            (root / "retry-mise-install").unlink()

            log.unlink()
            (root / "fail-mise-install").touch()
            refused_install = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(refused_install.returncode, 0)
            self.assertIn(
                "locked mise tool installation failed after 3 attempts",
                refused_install.stderr,
            )
            refused_execution = log.read_text(encoding="utf-8")
            refused_mise_calls = [
                line
                for line in refused_execution.splitlines()
                if line.startswith("mise=")
            ]
            self.assertEqual(len(refused_mise_calls), 4)
            self.assertEqual(
                sum(" install --locked" in line for line in refused_mise_calls),
                3,
            )
            self.assertFalse(any(" exec " in line for line in refused_mise_calls))
            self.assertNotIn("galaxy=", refused_execution)
            self.assertNotIn("playbook_directory=", refused_execution)
            (root / "fail-mise-install").unlink()

            log.unlink()
            check_result = subprocess.run(
                [converge, "--check", "--diff"],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(check_result.returncode, 0, check_result.stderr)
            check_execution = log.read_text(encoding="utf-8")
            self.assertRegex(
                check_execution,
                rf"(?m)^arguments=--check --diff .*vps_infra_revision={remote_sha} .*playbooks/site\.yml$",
            )

            for mode, state in (
                ("--prepare-public-static-edge", "prepare"),
                ("--precutover-public-static-edge", "precutover"),
                ("--activate-public-static-edge", "activate"),
                ("--stop-public-static-edge", "stopped"),
            ):
                log.unlink()
                edge_result = subprocess.run(
                    [converge, mode],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(edge_result.returncode, 0, edge_result.stderr)
                edge_execution = log.read_text(encoding="utf-8")
                self.assertRegex(
                    edge_execution,
                    rf"(?m)^arguments=.*vps_infra_revision={remote_sha} "
                    rf"--extra-vars vps_public_static_edge_state={state} "
                    r"playbooks/public-static-edge\.yml$",
                )

            for mode, state in (
                ("--start-internal-platform", "started"),
                ("--stop-internal-platform", "stopped"),
            ):
                log.unlink()
                internal_result = subprocess.run(
                    [converge, mode],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(internal_result.returncode, 0, internal_result.stderr)
                internal_execution = log.read_text(encoding="utf-8")
                self.assertRegex(
                    internal_execution,
                    rf"(?m)^arguments=.*vps_infra_revision={remote_sha} "
                    rf"--extra-vars vps_internal_platform_state={state} "
                    r"playbooks/internal-platform\.yml$",
                )

            for mode, state in (
                ("--install-postgres-backup", "installed"),
                ("--stop-postgres-backup-schedule", "stopped"),
                ("--backup-postgres-now", "backup-now"),
                ("--rehearse-postgres-restore", "rehearse-latest"),
            ):
                log.unlink()
                backup_result = subprocess.run(
                    [converge, mode],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(backup_result.returncode, 0, backup_result.stderr)
                backup_execution = log.read_text(encoding="utf-8")
                self.assertRegex(
                    backup_execution,
                    rf"(?m)^arguments=.*vps_infra_revision={remote_sha} "
                    rf"--extra-vars vps_postgres_backup_state={state} "
                    r"playbooks/postgres-backup\.yml$",
                )

            for mode, state in (
                ("--prepare-surplasse", "prepare"),
                ("--activate-surplasse", "activate"),
                ("--stop-surplasse", "stopped"),
            ):
                log.unlink()
                surplasse_result = subprocess.run(
                    [converge, mode],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(
                    surplasse_result.returncode, 0, surplasse_result.stderr
                )
                surplasse_execution = log.read_text(encoding="utf-8")
                self.assertRegex(
                    surplasse_execution,
                    rf"(?m)^arguments=.*vps_infra_revision={remote_sha} "
                    rf"--extra-vars vps_surplasse_state={state} "
                    r"playbooks/surplasse\.yml$",
                )

            for mode, state, check_options in (
                ("--plan-parkventory-postgres", "dry-run", "--check --diff "),
                ("--verify-parkventory-postgres", "check", ""),
                ("--prepare-parkventory-postgres", "prepare", ""),
            ):
                log.unlink()
                parkventory_result = subprocess.run(
                    [converge, mode],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(
                    parkventory_result.returncode,
                    0,
                    parkventory_result.stderr,
                )
                parkventory_execution = log.read_text(encoding="utf-8")
                self.assertRegex(
                    parkventory_execution,
                    rf"(?m)^arguments={check_options}.*"
                    rf"vps_infra_revision={remote_sha} "
                    rf"--extra-vars vps_parkventory_postgres_state={state} "
                    r"playbooks/parkventory-postgres\.yml$",
                )

            log.unlink()
            for unsupported_arguments in (
                ["--check"],
                ["--public-static-edge"],
                ["--diff", "--check"],
                ["--check", "--diff", "--limit", "atlas"],
            ):
                refused = subprocess.run(
                    [converge, *unsupported_arguments],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(refused.returncode, 64)
                self.assertIn("converge refused:", refused.stderr)
                self.assertFalse(log.exists())

            result = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log.unlink()
            remote_revision_file.write_text(
                f"{archived_inventory_sha}\n", encoding="utf-8"
            )
            refused_archived_inventory = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(refused_archived_inventory.returncode, 78)
            self.assertIn(
                "origin/main archive unexpectedly contains the private inventory",
                refused_archived_inventory.stderr,
            )
            self.assertNotIn("mise=", log.read_text(encoding="utf-8"))

            log.unlink()
            remote_revision_file.write_text(f"{remote_sha}\n", encoding="utf-8")
            (root / "fail-fetch").touch()
            failed_fetch = subprocess.run(
                [converge],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(failed_fetch.returncode, 0)
            self.assertIn("bounded fetch of origin/main failed", failed_fetch.stderr)
            self.assertNotIn("mise=", log.read_text(encoding="utf-8"))


class ApplicationStateContractTests(unittest.TestCase):
    def test_repository_state_matches_locked_manifest(self) -> None:
        APPLICATION_STATE.validate_repository(ROOT)

    def test_locked_platform_rejects_an_enabled_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            manifest_path = root / "releases/production.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["platform"]["enabled"] = True
            manifest["applications"]["personal"]["enabled"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "cannot be enabled by the locked platform baseline",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_disabled_application_rejects_an_active_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            disabled = root / "platform/caddy/routes/personal.caddy.disabled"
            disabled.rename(disabled.with_suffix(""))
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "missing personal.caddy.disabled.*unexpected personal.caddy",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_unknown_route_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "releases", root / "releases")
            shutil.copytree(ROOT / "platform/caddy", root / "platform/caddy")
            shutil.copytree(
                ROOT / "platform/observability/prometheus",
                root / "platform/observability/prometheus",
            )
            (root / "platform/caddy/routes/unreviewed.caddy").write_text(
                ":443 { respond 200 }\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                APPLICATION_STATE.ApplicationStateError,
                "unexpected unreviewed.caddy",
            ):
                APPLICATION_STATE.validate_repository(root)

    def test_inactive_prometheus_target_has_valid_file_sd_structure(self) -> None:
        target_path = (
            ROOT
            / "platform/observability/prometheus/targets/surplasse.yml.disabled"
        )
        target_groups = yaml.safe_load(target_path.read_text(encoding="utf-8"))
        self.assertIsInstance(target_groups, list)
        self.assertGreater(len(target_groups), 0)
        for group in target_groups:
            self.assertIsInstance(group, dict)
            self.assertEqual(set(group), {"targets", "labels"})
            self.assertIsInstance(group["targets"], list)
            self.assertGreater(len(group["targets"]), 0)
            for target in group["targets"]:
                self.assertRegex(target, r"^[a-z0-9][a-z0-9.-]*:[1-9][0-9]{0,4}$")
            self.assertIsInstance(group["labels"], dict)
            self.assertTrue(
                all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in group["labels"].items()
                )
            )

    def test_parkventory_monitoring_has_one_disabled_end_to_end_path(self) -> None:
        base = yaml.safe_load(
            (
                ROOT / "platform/observability/prometheus/prometheus.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "parkventory-backend",
            {job["job_name"] for job in base["scrape_configs"]},
        )

        candidate = yaml.safe_load(
            (
                ROOT
                / "applications/parkventory/integration/prometheus/"
                "prometheus.yml.disabled"
            ).read_text(encoding="utf-8")
        )
        parkventory_jobs = [
            job
            for job in candidate["scrape_configs"]
            if job["job_name"] == "parkventory-backend"
        ]
        self.assertEqual(
            parkventory_jobs,
            [
                {
                    "job_name": "parkventory-backend",
                    "metrics_path": "/q/metrics",
                    "file_sd_configs": [
                        {
                            "files": [
                                "/etc/prometheus/targets/parkventory.yml"
                            ]
                        }
                    ],
                }
            ],
        )

        override = yaml.safe_load(
            (
                ROOT
                / "applications/parkventory/integration/"
                "internal-platform.override.yaml.disabled"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(override["services"]["prometheus"]["networks"]),
            {"ops", "app_parkventory"},
        )
        self.assertEqual(
            override["networks"],
            {
                "app_parkventory": {
                    "external": True,
                    "name": "app_parkventory",
                }
            },
        )
        sources = {
            volume["source"]: volume["target"]
            for volume in override["services"]["prometheus"]["volumes"]
        }
        self.assertEqual(
            sources,
            {
                "/srv/vps/runtime/parkventory/integration/prometheus/"
                "prometheus.yml": "/etc/prometheus/prometheus.yml",
                "/srv/vps/runtime/parkventory/integration/prometheus/targets":
                    "/etc/prometheus/targets",
                "/srv/vps/runtime/parkventory/integration/prometheus/rules":
                    "/etc/prometheus/rules",
            },
        )
        rule_text = (
            ROOT
            / "platform/observability/prometheus/rules/parkventory.yml.disabled"
        ).read_text(encoding="utf-8")
        self.assertIn('absent(up{application="parkventory"})', rule_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
