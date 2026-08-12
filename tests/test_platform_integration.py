#!/usr/bin/env python3

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml


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


INTEGRATION = load_script_module(
    "platform_integration_contract",
    SCRIPTS / "lib/platform_integration.py",
)


class PlatformIntegrationPackageTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repository"
        self.repository.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Platform Test"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "platform-test@example.invalid"],
            cwd=self.repository,
            check=True,
        )
        for relative in INTEGRATION.RUNTIME_PATHS:
            source = ROOT / relative
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.revision = self.commit("initial platform integration")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit(self, message: str) -> str:
        subprocess.run(["git", "add", "--all"], cwd=self.repository, check=True)
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-08-12T05:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-12T05:00:00Z",
        }
        subprocess.run(
            ["git", "commit", "--quiet", "--message", message],
            cwd=self.repository,
            env=environment,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def package(self):
        return INTEGRATION.build_package(self.repository, self.revision)

    def test_build_is_deterministic_and_verifies_exact_content(self) -> None:
        first = self.package()
        second = self.package()
        self.assertEqual(first, second)
        self.assertEqual(first.created, "2026-08-12T05:00:00Z")
        archive_digest, inventory_digest = INTEGRATION.verify_package(
            first.archive,
            first.inventory,
            expected_revision=self.revision,
            expected_created=first.created,
        )
        self.assertEqual(archive_digest, INTEGRATION.sha256(first.archive))
        self.assertEqual(inventory_digest, INTEGRATION.sha256(first.inventory))
        inventory = json.loads(first.inventory)
        self.assertEqual(
            [entry["path"] for entry in inventory["files"]],
            list(INTEGRATION.RUNTIME_PATHS),
        )
        self.assertEqual(
            first.inventory,
            INTEGRATION.canonical_json(inventory),
        )

    def test_builder_rejects_unexpected_runtime_file(self) -> None:
        unexpected = self.repository / "platform/caddy/routes/unexpected.caddy"
        unexpected.write_text("example.invalid { respond 200 }\n", encoding="utf-8")
        revision = self.commit("add an unexpected route")
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "unexpected"):
            INTEGRATION.build_package(self.repository, revision)

    def test_builder_rejects_missing_runtime_file(self) -> None:
        (self.repository / INTEGRATION.RUNTIME_PATHS[-1]).unlink()
        revision = self.commit("remove a required platform file")
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "missing"):
            INTEGRATION.build_package(self.repository, revision)

    def test_builder_rejects_tracked_symlink(self) -> None:
        target = self.repository / INTEGRATION.RUNTIME_PATHS[1]
        target.unlink()
        target.symlink_to("../compose.yaml")
        revision = self.commit("replace runtime file with a link")
        with self.assertRaisesRegex(
            INTEGRATION.IntegrationError,
            "unsafe tracked file type",
        ):
            INTEGRATION.build_package(self.repository, revision)

    def test_builder_cli_rejects_symlink_output_directory(self) -> None:
        output_target = Path(self.temporary.name) / "real-output"
        output_target.mkdir()
        output_link = Path(self.temporary.name) / "linked-output"
        output_link.symlink_to(output_target, target_is_directory=True)
        rejected = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build-platform-integration"),
                "--repository",
                str(self.repository),
                "--revision",
                self.revision,
                "--output-directory",
                str(output_link),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("must not be a symbolic link", rejected.stderr)
        self.assertEqual(list(output_target.iterdir()), [])

    def test_verifier_rejects_duplicate_inventory_key(self) -> None:
        package = self.package()
        duplicate = b'{"schema":1,' + package.inventory[1:]
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "duplicate key"):
            INTEGRATION.verify_package(package.archive, duplicate)

    def test_verifier_rejects_special_archive_member(self) -> None:
        package = self.package()
        inventory = json.loads(package.inventory)
        epoch = INTEGRATION.epoch_from_created(inventory["created"])
        tar_buffer = io.BytesIO()
        with tarfile.open(
            fileobj=tar_buffer,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            for index, expected in enumerate(inventory["files"]):
                entry = tarfile.TarInfo(expected["path"])
                entry.mode = 0o644
                entry.uid = 0
                entry.gid = 0
                entry.mtime = epoch
                if index == 0:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../../outside"
                    entry.size = 0
                    archive.addfile(entry)
                else:
                    content = subprocess.run(
                        [
                            "git",
                            "show",
                            f"{self.revision}:{expected['path']}",
                        ],
                        cwd=self.repository,
                        check=True,
                        capture_output=True,
                    ).stdout
                    entry.type = tarfile.REGTYPE
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
        compressed = io.BytesIO()
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=compressed,
            mtime=epoch,
        ) as output:
            output.write(tar_buffer.getvalue())
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "special file"):
            INTEGRATION.verify_package(compressed.getvalue(), package.inventory)

    def test_verifier_rejects_noncanonical_gzip(self) -> None:
        package = self.package()
        expanded = gzip.decompress(package.archive)
        changed = gzip.compress(expanded, compresslevel=1, mtime=0)
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "not canonical"):
            INTEGRATION.verify_package(changed, package.inventory)

    def test_verifier_bounds_gzip_expansion_before_parsing_tar(self) -> None:
        package = self.package()
        expanded = b"0" * (INTEGRATION.MAX_TAR_SIZE + 1)
        compressed = gzip.compress(expanded, compresslevel=9, mtime=0)
        self.assertLess(len(compressed), INTEGRATION.MAX_ARCHIVE_SIZE)
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "expanded.*limit"):
            INTEGRATION.verify_package(compressed, package.inventory)

    def test_verifier_reapplies_text_content_invariants(self) -> None:
        package = self.package()
        inventory = json.loads(package.inventory)
        epoch = INTEGRATION.epoch_from_created(inventory["created"])

        for invalid_content, expected_error in (
            (b"invalid\0text", "NUL byte"),
            (b"invalid-utf8-\xff", "non-UTF-8"),
        ):
            with self.subTest(expected_error=expected_error):
                runtime_files = []
                for index, expected in enumerate(inventory["files"]):
                    content = subprocess.run(
                        [
                            "git",
                            "show",
                            f"{self.revision}:{expected['path']}",
                        ],
                        cwd=self.repository,
                        check=True,
                        capture_output=True,
                    ).stdout
                    if index == 0:
                        content = invalid_content
                    expected["size"] = len(content)
                    expected["sha256"] = INTEGRATION.sha256(content)
                    runtime_files.append(
                        INTEGRATION.RuntimeFile(
                            path=expected["path"], mode=0o644, content=content
                        )
                    )
                malicious_archive = INTEGRATION.archive_for(runtime_files, epoch=epoch)
                malicious_inventory = INTEGRATION.canonical_json(inventory)
                with self.assertRaisesRegex(
                    INTEGRATION.IntegrationError, expected_error
                ):
                    INTEGRATION.verify_package(
                        malicious_archive,
                        malicious_inventory,
                        expected_revision=self.revision,
                        expected_created=package.created,
                    )

    def test_manifest_requires_exact_layers_and_annotations(self) -> None:
        package = self.package()
        manifest = {
            "schemaVersion": 2,
            "mediaType": INTEGRATION.OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": INTEGRATION.ARTIFACT_TYPE,
            "config": {
                "mediaType": INTEGRATION.OCI_EMPTY_CONFIG_MEDIA_TYPE,
                "digest": INTEGRATION.OCI_EMPTY_CONFIG_DIGEST,
                "size": 2,
                "data": "e30=",
            },
            "layers": [
                {
                    "mediaType": INTEGRATION.ARCHIVE_MEDIA_TYPE,
                    "digest": INTEGRATION.sha256(package.archive),
                    "size": len(package.archive),
                    "annotations": {
                        "org.opencontainers.image.title": INTEGRATION.ARCHIVE_NAME
                    },
                },
                {
                    "mediaType": INTEGRATION.INVENTORY_MEDIA_TYPE,
                    "digest": INTEGRATION.sha256(package.inventory),
                    "size": len(package.inventory),
                    "annotations": {
                        "org.opencontainers.image.title": INTEGRATION.INVENTORY_NAME
                    },
                },
            ],
            "annotations": {
                "org.opencontainers.image.created": package.created,
                "org.opencontainers.image.revision": self.revision,
                "org.opencontainers.image.source": INTEGRATION.SOURCE_URL,
            },
        }
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        digest = INTEGRATION.sha256(manifest_bytes)
        INTEGRATION.validate_manifest(
            manifest_bytes,
            expected_digest=digest,
            archive_bytes=package.archive,
            inventory_bytes=package.inventory,
            expected_revision=self.revision,
            expected_created=package.created,
        )

        malicious = json.loads(manifest_bytes)
        malicious["layers"].append(malicious["layers"][0])
        malicious_bytes = json.dumps(malicious, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "layer count"):
            INTEGRATION.validate_manifest(
                malicious_bytes,
                expected_digest=INTEGRATION.sha256(malicious_bytes),
                archive_bytes=package.archive,
                inventory_bytes=package.inventory,
                expected_revision=self.revision,
                expected_created=package.created,
            )

        malicious = json.loads(manifest_bytes)
        malicious["annotations"]["unreviewed"] = "true"
        malicious_bytes = json.dumps(malicious, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(INTEGRATION.IntegrationError, "annotations"):
            INTEGRATION.validate_manifest(
                malicious_bytes,
                expected_digest=INTEGRATION.sha256(malicious_bytes),
                archive_bytes=package.archive,
                inventory_bytes=package.inventory,
                expected_revision=self.revision,
                expected_created=package.created,
            )

    def test_raw_evidence_is_canonical_and_digest_bound(self) -> None:
        package = self.package()
        artifact_digest = "sha256:" + "a" * 64
        evidence = INTEGRATION.evidence_bytes(
            artifact_reference=(
                "ghcr.io/nclsppr/vps-infra/platform-integration@"
                f"{artifact_digest}"
            ),
            archive_bytes=package.archive,
            inventory_bytes=package.inventory,
            revision=self.revision,
            created=package.created,
            run_id=123,
            run_attempt=1,
        )
        parsed = json.loads(evidence)
        self.assertEqual(evidence, INTEGRATION.canonical_json(parsed))
        self.assertEqual(parsed["artifact"].rsplit("@", maxsplit=1)[1], artifact_digest)
        self.assertEqual(parsed["archive"]["sha256"], INTEGRATION.sha256(package.archive))
        self.assertEqual(
            parsed["inventory"]["sha256"],
            INTEGRATION.sha256(package.inventory),
        )


class PlatformIntegrationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = ROOT / ".github/workflows/platform-integration.yml"
        self.text = self.path.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.text, Loader=yaml.BaseLoader)
        self.steps = self.workflow["jobs"]["publish"]["steps"]

    def test_workflow_has_bounded_main_publication_triggers(self) -> None:
        self.assertEqual(set(self.workflow["on"]), {"push", "workflow_dispatch"})
        self.assertEqual(self.workflow["on"]["push"]["branches"], ["main"])
        self.assertEqual(
            self.workflow["on"]["push"]["paths"],
            [
                ".github/workflows/platform-integration.yml",
                "platform/.env.example",
                "platform/compose.yaml",
                "platform/caddy/Caddyfile",
                "platform/caddy/routes/**",
                "platform/observability/**",
                "platform/postgres/**",
                "mise.lock",
                "mise.toml",
                "scripts/build-platform-integration",
                "scripts/lib/platform_integration.py",
                "scripts/verify-platform-integration",
                "scripts/verify-platform-integration-manifest",
                "scripts/write-platform-integration-evidence",
                "tests/test_platform_integration.py",
            ],
        )
        job = self.workflow["jobs"]["publish"]
        self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            job["permissions"],
            {
                "attestations": "write",
                "contents": "read",
                "id-token": "write",
                "packages": "write",
            },
        )

    def test_every_action_is_pinned_to_a_full_commit(self) -> None:
        actions = [step["uses"] for step in self.steps if "uses" in step]
        self.assertEqual(len(actions), 5)
        for action in actions:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
        self.assertIn(
            "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8",
            actions,
        )
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            actions,
        )
        self.assertIn(
            "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
            actions,
        )

    def test_oras_and_attestation_share_standard_registry_credentials(self) -> None:
        login = next(step for step in self.steps if step["name"] == "Authenticate to GHCR")
        self.assertEqual(login["with"]["registry"], "ghcr.io")
        self.assertEqual(login["with"]["username"], "${{ github.actor }}")
        self.assertEqual(login["with"]["password"], "${{ secrets.GITHUB_TOKEN }}")

        oras_commands = "\n".join(
            step["run"]
            for step in self.steps
            if "run" in step and "oras " in step["run"]
        )
        self.assertNotIn("--registry-config", oras_commands)
        self.assertNotIn("DOCKER_CONFIG", self.text)

    def test_attestation_follows_registry_content_validation(self) -> None:
        names = [step["name"] for step in self.steps]
        ordered = [
            "Build the deterministic package",
            "Verify the local package",
            "Push the source revision tag",
            "Validate the published manifest, layers, annotations, and content",
            "Attest the verified OCI artifact",
            "Verify exact GitHub provenance",
            "Write the raw verification record",
            "Upload the raw verification record",
            "Verify evidence metadata and publish the result",
        ]
        positions = [names.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))

        push = next(
            step for step in self.steps if step["name"] == "Push the source revision tag"
        )["run"]
        self.assertIn('"${PACKAGE_REPOSITORY}:sha-${GITHUB_SHA}"', push)
        self.assertIn('--artifact-type "${ARTIFACT_TYPE}"', push)
        self.assertIn("org.opencontainers.image.source=${SOURCE_URL}", push)
        self.assertIn("org.opencontainers.image.revision=${GITHUB_SHA}", push)
        self.assertIn("org.opencontainers.image.created=${CREATED}", push)
        self.assertIn(
            '"platform-integration.tar.gz:${ARCHIVE_MEDIA_TYPE}"', push
        )
        self.assertIn(
            '"platform-integration.inventory.json:${INVENTORY_MEDIA_TYPE}"',
            push,
        )

        remote_validation = next(
            step
            for step in self.steps
            if step["name"]
            == "Validate the published manifest, layers, annotations, and content"
        )["run"]
        self.assertIn("verify-platform-integration-manifest", remote_validation)
        self.assertEqual(remote_validation.count("oras blob fetch"), 2)
        self.assertEqual(remote_validation.count("cmp --"), 2)
        self.assertIn("verify-platform-integration", remote_validation)

    def test_provenance_verification_is_exact_and_rejects_self_hosted(self) -> None:
        verify = next(
            step
            for step in self.steps
            if step["name"] == "Verify exact GitHub provenance"
        )["run"]
        self.assertIn('gh attestation verify "oci://${REFERENCE}"', verify)
        self.assertIn('--source-digest "${GITHUB_SHA}"', verify)
        self.assertIn("--source-ref refs/heads/main", verify)
        self.assertIn(
            '"${GITHUB_REPOSITORY}/.github/workflows/platform-integration.yml"',
            verify,
        )
        self.assertIn("--deny-self-hosted-runners", verify)
        self.assertIn("--format json", verify)

    def test_raw_evidence_is_unarchived_and_content_addressed(self) -> None:
        upload = next(
            step
            for step in self.steps
            if step["name"] == "Upload the raw verification record"
        )
        self.assertEqual(upload["with"]["archive"], "false")
        self.assertEqual(upload["with"]["retention-days"], "90")
        final = next(
            step
            for step in self.steps
            if step["name"] == "Verify evidence metadata and publish the result"
        )["run"]
        self.assertIn('test "${normalized_digest}" = "${EVIDENCE_DIGEST}"', final)


class PlatformIntegrationRepositoryContractTests(unittest.TestCase):
    def test_runtime_allowlist_is_complete_and_excludes_build_sources(self) -> None:
        self.assertEqual(tuple(sorted(INTEGRATION.RUNTIME_PATHS)), INTEGRATION.RUNTIME_PATHS)
        selected = set(INTEGRATION.RUNTIME_PATHS)
        expected = {
            "platform/.env.example",
            "platform/compose.yaml",
            "platform/caddy/Caddyfile",
            *(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / "platform/caddy/routes").rglob("*")
                if path.is_file()
            ),
            *(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / "platform/observability").rglob("*")
                if path.is_file()
            ),
            *(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / "platform/postgres").rglob("*")
                if path.is_file()
            ),
        }
        self.assertEqual(selected, expected)
        for forbidden in (
            "platform/README.md",
            "platform/caddy/Dockerfile",
            "platform/caddy/build.env",
            "platform/caddy/build/go.mod",
            "platform/caddy/entrypoint.sh",
        ):
            self.assertNotIn(forbidden, selected)

    def test_publication_tools_are_locked(self) -> None:
        mise = (ROOT / "mise.toml").read_text(encoding="utf-8")
        self.assertIn('gh = "2.97.0"', mise)
        self.assertIn('oras = "1.3.3"', mise)
        lock = (ROOT / "mise.lock").read_text(encoding="utf-8")
        self.assertIn("[[tools.gh]]\nversion = \"2.97.0\"", lock)
        self.assertIn("[[tools.oras]]\nversion = \"1.3.3\"", lock)

    def test_commands_are_executable(self) -> None:
        for name in (
            "build-platform-integration",
            "verify-platform-integration",
            "verify-platform-integration-manifest",
            "write-platform-integration-evidence",
        ):
            self.assertTrue(os.access(SCRIPTS / name, os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
