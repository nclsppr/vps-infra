#!/usr/bin/env python3
"""Adversarial tests for the Mon Florian singleton secret materializer."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/materialize-monflorian-secret"
REGISTRY = ROOT / "secrets/registry.json"
TEST_GUARD = "VPS_MONFLORIAN_SECRET_TESTING"
FAILPOINT = "VPS_MONFLORIAN_SECRET_FAILPOINT"
SPECS = {
    "monflorian.openai-api-key": (
        "monflorian-openai-api-key",
        0o440,
        b"sk-proj-" + b"A" * 80 + b"\n",
    ),
    "monflorian.private-access": (
        "monflorian-private-access.caddy",
        0o400,
        b"basic_auth {\n\tflorian $2b$14$" + b"A" * 53 + b"\n}\n",
    ),
}


class MonFlorianSecretMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")

    @staticmethod
    def write_registry(
        destination: Path,
        *,
        identifier: str | None = None,
        generation: int = 0,
        target_generation: int = 1,
    ) -> None:
        document = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for entry in document["secrets"]:
            if entry["id"] in SPECS and (
                identifier is None or entry["id"] == identifier
            ):
                entry["generation"] = generation
                entry["target_generation"] = target_generation
                entry["generation_binding"] = (
                    "materializer-marker" if generation > 0 else "unlinked"
                )
                entry["host_state"] = "materialized" if generation > 0 else "absent"
        destination.write_text(
            json.dumps(document, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        destination.chmod(0o644)

    @staticmethod
    def write_source(path: Path, content: bytes) -> None:
        path.write_bytes(content)
        path.chmod(0o600)

    def fixture(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory) / "monflorian"
        root.mkdir(mode=0o700)
        registry = Path(directory) / "registry.json"
        self.write_registry(registry)
        return root, registry

    def run_helper(
        self,
        root: Path,
        registry: Path,
        *arguments: str,
        failpoint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment[TEST_GUARD] = "1"
        if failpoint is not None:
            environment[FAILPOINT] = failpoint
        else:
            environment.pop(FAILPOINT, None)
        return subprocess.run(
            [
                str(SCRIPT),
                *arguments,
                "--test-root",
                str(root),
                "--test-registry",
                str(registry),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    @staticmethod
    def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
        result: dict[str, tuple[Any, ...]] = {}
        for path in sorted((root, *root.rglob("*"))):
            relative = "." if path == root else str(path.relative_to(root))
            metadata = path.lstat()
            common = (
                metadata.st_ino,
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
            )
            if path.is_symlink():
                result[relative] = ("symlink", *common, os.readlink(path))
            elif path.is_dir():
                result[relative] = ("directory", *common)
            else:
                result[relative] = ("file", *common, path.read_bytes())
        return result

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        return json.loads(result.stdout)

    def install(
        self,
        root: Path,
        registry: Path,
        identifier: str,
        source: Path,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_helper(
            root,
            registry,
            "--install-from",
            str(source),
            identifier,
        )

    def adopt(
        self,
        root: Path,
        registry: Path,
        identifier: str,
        *,
        failpoint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_helper(
            root,
            registry,
            "--adopt-existing",
            identifier,
            failpoint=failpoint,
        )

    def check_adopt(
        self,
        root: Path,
        registry: Path,
        identifier: str,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_helper(
            root,
            registry,
            "--check-adopt-existing",
            identifier,
        )

    @staticmethod
    def write_installed(root: Path, identifier: str, content: bytes) -> Path:
        filename, mode, _value = SPECS[identifier]
        destination = root / filename
        destination.write_bytes(content)
        destination.chmod(mode)
        return destination

    def test_check_is_zero_mutation_for_an_absent_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            before = self.snapshot(root)
            result = self.run_helper(root, registry, "--check")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(root), before)
            document = self.output(result)
            self.assertFalse(document["changed"])
            self.assertEqual(document["mode"], "check")
            self.assertEqual(
                document["entries"],
                {
                    identifier: {
                        "generation": 0,
                        "generation_binding": "unlinked",
                        "host_state": "absent",
                    }
                    for identifier in SPECS
                },
            )
            self.assertFalse((root / ".generations").exists())

    def test_each_source_materializes_independently_and_is_idempotent(self) -> None:
        for identifier, (filename, mode, value) in SPECS.items():
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                source = Path(directory) / "source"
                self.write_source(source, value)

                first = self.install(root, registry, identifier, source)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertTrue(self.output(first)["changed"])
                destination = root / filename
                self.assertEqual(destination.read_bytes(), value)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), mode)
                self.assertEqual(destination.stat().st_nlink, 1)
                marker = root / ".generations" / f"{identifier}.json"
                self.assertEqual(
                    marker.read_bytes(),
                    (
                        json.dumps(
                            {
                                "materializer": "materialize-monflorian-secret",
                                "schema": 1,
                                "secret_ids": [identifier],
                                "target_generation": 1,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("ascii"),
                )
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o400)
                self.assertEqual(marker.stat().st_nlink, 1)
                other = next(item for item in SPECS if item != identifier)
                self.assertEqual(self.output(first)["entries"][other]["host_state"], "absent")

                before = self.snapshot(root)
                second = self.install(root, registry, identifier, source)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertFalse(self.output(second)["changed"])
                self.assertEqual(self.snapshot(root), before)

    def test_two_sources_commit_as_two_singleton_file_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            for index, (identifier, (_filename, _mode, value)) in enumerate(SPECS.items()):
                source = Path(directory) / f"source-{index}"
                self.write_source(source, value)
                result = self.install(root, registry, identifier, source)
                self.assertEqual(result.returncode, 0, result.stderr)
            check = self.run_helper(root, registry, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(
                {
                    entry["generation"]
                    for entry in self.output(check)["entries"].values()
                },
                {1},
            )
            self.assertEqual(
                {path.name for path in (root / ".generations").iterdir()},
                {f"{identifier}.json" for identifier in SPECS},
            )

    def test_same_generation_content_change_is_refused_before_mutation(self) -> None:
        for identifier, (_filename, _mode, value) in SPECS.items():
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                source = Path(directory) / "source"
                self.write_source(source, value)
                first = self.install(root, registry, identifier, source)
                self.assertEqual(first.returncode, 0, first.stderr)
                before = self.snapshot(root)
                replacement = Path(directory) / "replacement"
                replacement_value = value.replace(b"A", b"B")
                self.write_source(replacement, replacement_value)
                refused = self.install(root, registry, identifier, replacement)
                self.assertEqual(refused.returncode, 78)
                self.assertIn("same-generation content change", refused.stderr)
                self.assertEqual(self.snapshot(root), before)

    def test_unlinked_existing_content_cannot_be_replaced(self) -> None:
        for identifier, (_filename, _mode, value) in SPECS.items():
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                installed_value = value.replace(b"A", b"B")
                destination = self.write_installed(root, identifier, installed_value)
                inode = destination.stat().st_ino
                source = Path(directory) / "different-source"
                self.write_source(source, value)
                before = self.snapshot(root)

                refused = self.install(root, registry, identifier, source)
                self.assertEqual(refused.returncode, 78)
                self.assertIn("unlinked existing content", refused.stderr)
                self.assertEqual(self.snapshot(root), before)
                self.assertEqual(destination.stat().st_ino, inode)
                self.assertEqual(destination.read_bytes(), installed_value)

                matching_source = Path(directory) / "matching-source"
                self.write_source(matching_source, installed_value)
                resumed = self.install(root, registry, identifier, matching_source)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertTrue(self.output(resumed)["changed"])
                self.assertEqual(destination.stat().st_ino, inode)

    def test_rotation_rejects_identical_content_before_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.openai-api-key"
            source = Path(directory) / "source"
            self.write_source(source, SPECS[identifier][2])
            installed = self.install(root, registry, identifier, source)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.write_registry(
                registry,
                identifier=identifier,
                generation=1,
                target_generation=2,
            )
            before = self.snapshot(root)

            refused = self.install(root, registry, identifier, source)
            self.assertEqual(refused.returncode, 78)
            self.assertIn("requires new content", refused.stderr)
            self.assertEqual(self.snapshot(root), before)

    def test_check_preserves_every_byte_and_inode_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            for index, (identifier, (_filename, _mode, value)) in enumerate(SPECS.items()):
                source = Path(directory) / f"source-{index}"
                self.write_source(source, value)
                result = self.install(root, registry, identifier, source)
                self.assertEqual(result.returncode, 0, result.stderr)
            before = self.snapshot(root)
            checked = self.run_helper(root, registry, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(self.snapshot(root), before)

    def test_source_symlink_and_hardlink_are_refused_without_mutation(self) -> None:
        for case in ("symlink", "hardlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                outside = Path(directory) / "outside"
                self.write_source(outside, SPECS["monflorian.openai-api-key"][2])
                source = Path(directory) / "source"
                if case == "symlink":
                    source.symlink_to(outside)
                else:
                    os.link(outside, source)
                before = self.snapshot(root)
                result = self.install(
                    root,
                    registry,
                    "monflorian.openai-api-key",
                    source,
                )
                self.assertEqual(result.returncode, 78)
                self.assertIn("source file", result.stderr)
                self.assertEqual(self.snapshot(root), before)

    def test_installed_secret_symlink_and_hardlink_block_check_and_install(self) -> None:
        for case in ("symlink", "hardlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                identifier = "monflorian.openai-api-key"
                filename, mode, value = SPECS[identifier]
                target = root / filename
                target.write_bytes(value)
                target.chmod(mode)
                if case == "symlink":
                    target.unlink()
                    outside = Path(directory) / "outside"
                    outside.write_bytes(value)
                    outside.chmod(mode)
                    target.symlink_to(outside)
                else:
                    os.link(target, Path(directory) / "second-link")
                source = Path(directory) / "source"
                self.write_source(source, value)
                before = self.snapshot(root)
                for arguments in (("--check",), ("--install-from", str(source), identifier)):
                    result = self.run_helper(root, registry, *arguments)
                    self.assertEqual(result.returncode, 78)
                    self.assertIn("installed file", result.stderr)
                    self.assertEqual(self.snapshot(root), before)

    def test_bad_marker_content_metadata_symlink_and_hardlink_are_read_only_failures(self) -> None:
        for case in ("content", "mode", "symlink", "hardlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                identifier = "monflorian.private-access"
                source = Path(directory) / "source"
                self.write_source(source, SPECS[identifier][2])
                installed = self.install(root, registry, identifier, source)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                marker = root / ".generations" / f"{identifier}.json"
                if case == "content":
                    marker.chmod(0o600)
                    marker.write_text('{"schema":1}\n', encoding="ascii")
                    marker.chmod(0o400)
                elif case == "mode":
                    marker.chmod(0o600)
                elif case == "symlink":
                    marker.unlink()
                    outside = Path(directory) / "outside-marker"
                    outside.write_text('{"schema":1}\n', encoding="ascii")
                    outside.chmod(0o400)
                    marker.symlink_to(outside)
                else:
                    os.link(marker, Path(directory) / "second-marker-link")
                before = self.snapshot(root)
                checked = self.run_helper(root, registry, "--check")
                self.assertEqual(checked.returncode, 78)
                self.assertIn("generation marker", checked.stderr)
                self.assertEqual(self.snapshot(root), before)

    def test_pending_recovery_is_bounded_and_check_is_read_only(self) -> None:
        for location in ("root", "marker"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                identifier = "monflorian.openai-api-key"
                filename, mode, value = SPECS[identifier]
                if location == "root":
                    pending = root / f".{filename}.{'a' * 32}.pending"
                    pending.write_bytes(value)
                    pending.chmod(mode)
                else:
                    generation_directory = root / ".generations"
                    generation_directory.mkdir(mode=0o700)
                    pending = generation_directory / (
                        f".{identifier}.json.{'a' * 32}.pending"
                    )
                    pending.write_bytes(
                        (
                            json.dumps(
                                {
                                    "materializer": "materialize-monflorian-secret",
                                    "schema": 1,
                                    "secret_ids": [identifier],
                                    "target_generation": 1,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("ascii")
                    )
                    pending.chmod(0o400)
                before = self.snapshot(root)
                checked = self.run_helper(root, registry, "--check")
                self.assertEqual(checked.returncode, 78)
                self.assertIn("mutating recovery", checked.stderr)
                self.assertEqual(self.snapshot(root), before)

                source = Path(directory) / "source"
                self.write_source(source, value)
                recovered = self.install(root, registry, identifier, source)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertFalse(pending.exists())
                self.assertFalse(
                    any(
                        path.name.endswith(".pending")
                        for path in root.rglob("*")
                    )
                )

    def test_pending_recovery_validates_all_files_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.openai-api-key"
            filename, mode, value = SPECS[identifier]
            valid = root / f".{filename}.{'a' * 32}.pending"
            valid.write_bytes(value)
            valid.chmod(mode)
            generation_directory = root / ".generations"
            generation_directory.mkdir(mode=0o700)
            invalid = generation_directory / (
                f".{identifier}.json.{'b' * 32}.pending"
            )
            invalid.write_text('{"schema":1}\n', encoding="ascii")
            invalid.chmod(0o400)
            source = Path(directory) / "source"
            self.write_source(source, value)
            before = self.snapshot(root)

            refused = self.install(root, registry, identifier, source)
            self.assertEqual(refused.returncode, 78)
            self.assertIn("pending generation marker content", refused.stderr)
            self.assertEqual(self.snapshot(root), before)
            self.assertTrue(valid.exists())
            self.assertTrue(invalid.exists())

    def test_pending_recovery_rejects_unsafe_files_without_deletion(self) -> None:
        identifier = "monflorian.openai-api-key"
        filename, mode, value = SPECS[identifier]
        for case in ("name", "mode", "hardlink", "symlink", "size"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                pending = root / f".{filename}.{'a' * 32}.pending"
                if case == "name":
                    pending = root / f".unknown.{'a' * 32}.pending"
                if case == "symlink":
                    outside = Path(directory) / "outside"
                    outside.write_bytes(value)
                    outside.chmod(mode)
                    pending.symlink_to(outside)
                else:
                    pending.write_bytes(b"" if case == "size" else value)
                    pending.chmod(0o600 if case == "mode" else mode)
                    if case == "hardlink":
                        os.link(pending, Path(directory) / "second-link")
                source = Path(directory) / "source"
                self.write_source(source, value)
                before = self.snapshot(root)
                refused = self.install(root, registry, identifier, source)
                self.assertEqual(refused.returncode, 78)
                self.assertIn("pending", refused.stderr)
                self.assertEqual(self.snapshot(root), before)
                self.assertTrue(os.path.lexists(pending))

    def test_initial_interruption_after_replacement_leaves_an_unlinked_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.openai-api-key"
            source = Path(directory) / "source"
            self.write_source(source, SPECS[identifier][2])
            interrupted = self.run_helper(
                root,
                registry,
                "--install-from",
                str(source),
                identifier,
                failpoint="after-secret-replacement",
            )
            self.assertEqual(interrupted.returncode, 78)
            self.assertTrue((root / SPECS[identifier][0]).is_file())
            self.assertFalse((root / ".generations" / f"{identifier}.json").exists())
            checked = self.run_helper(root, registry, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(
                self.output(checked)["entries"][identifier],
                {
                    "generation": 0,
                    "generation_binding": "unlinked",
                    "host_state": "materialized",
                },
            )
            resumed = self.install(root, registry, identifier, source)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(self.output(resumed)["entries"][identifier]["generation"], 1)

    def test_marker_stage_interruption_removes_the_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.private-access"
            source = Path(directory) / "source"
            self.write_source(source, SPECS[identifier][2])

            interrupted = self.run_helper(
                root,
                registry,
                "--install-from",
                str(source),
                identifier,
                failpoint="after-marker-stage",
            )
            self.assertEqual(interrupted.returncode, 78)
            generation_directory = root / ".generations"
            self.assertTrue(generation_directory.is_dir())
            self.assertEqual(list(generation_directory.iterdir()), [])
            self.assertTrue((root / SPECS[identifier][0]).is_file())

            resumed = self.install(root, registry, identifier, source)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertTrue(self.output(resumed)["changed"])

    def test_existing_file_adoption_is_initial_bounded_and_idempotent(self) -> None:
        for identifier, (_filename, _mode, value) in SPECS.items():
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                destination = self.write_installed(root, identifier, value)
                inode = destination.stat().st_ino

                before_preflight = self.snapshot(root)
                preflight = self.check_adopt(root, registry, identifier)
                self.assertEqual(preflight.returncode, 0, preflight.stderr)
                self.assertFalse(self.output(preflight)["changed"])
                self.assertEqual(self.output(preflight)["mode"], "check-adopt")
                self.assertTrue(self.output(preflight)["adoption_required"])
                self.assertEqual(self.output(preflight)["secret_id"], identifier)
                self.assertEqual(self.snapshot(root), before_preflight)
                self.assertFalse((root / ".generations").exists())

                first = self.adopt(root, registry, identifier)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertTrue(self.output(first)["changed"])
                self.assertEqual(self.output(first)["mode"], "adopt")
                self.assertEqual(destination.read_bytes(), value)
                self.assertEqual(destination.stat().st_ino, inode)
                self.assertEqual(
                    self.output(first)["entries"][identifier],
                    {
                        "generation": 1,
                        "generation_binding": "materializer-marker",
                        "host_state": "materialized",
                    },
                )

                before = self.snapshot(root)
                completed_preflight = self.check_adopt(root, registry, identifier)
                self.assertEqual(
                    completed_preflight.returncode,
                    0,
                    completed_preflight.stderr,
                )
                self.assertFalse(
                    self.output(completed_preflight)["adoption_required"]
                )
                self.assertEqual(self.snapshot(root), before)
                second = self.adopt(root, registry, identifier)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertFalse(self.output(second)["changed"])
                self.assertEqual(self.snapshot(root), before)

                self.write_registry(
                    registry,
                    identifier=identifier,
                    generation=1,
                    target_generation=1,
                )
                observed = self.snapshot(root)
                refused = self.adopt(root, registry, identifier)
                self.assertEqual(refused.returncode, 78)
                self.assertIn("initial-only", refused.stderr)
                self.assertEqual(self.snapshot(root), observed)
                refused_preflight = self.check_adopt(root, registry, identifier)
                self.assertEqual(refused_preflight.returncode, 78)
                self.assertIn("initial-only", refused_preflight.stderr)
                self.assertEqual(self.snapshot(root), observed)

    def test_existing_file_adoption_interruption_does_not_publish_a_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.openai-api-key"
            destination = self.write_installed(root, identifier, SPECS[identifier][2])
            inode = destination.stat().st_ino
            before = self.snapshot(root)

            interrupted = self.adopt(
                root,
                registry,
                identifier,
                failpoint="after-existing-secret-sync",
            )
            self.assertEqual(interrupted.returncode, 78)
            self.assertIn("after-existing-secret-sync", interrupted.stderr)
            self.assertEqual(self.snapshot(root), before)
            self.assertEqual(destination.stat().st_ino, inode)
            self.assertFalse((root / ".generations").exists())

            resumed = self.adopt(root, registry, identifier)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertTrue(self.output(resumed)["changed"])

    def test_existing_file_adoption_refuses_missing_invalid_and_unsafe_files(self) -> None:
        identifier = "monflorian.openai-api-key"
        valid = SPECS[identifier][2]
        for case in ("missing", "content", "mode", "symlink", "hardlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                if case != "missing":
                    destination = self.write_installed(root, identifier, valid)
                    if case == "content":
                        destination.chmod(0o640)
                        destination.write_bytes(b"not-an-api-key\n")
                        destination.chmod(0o440)
                    elif case == "mode":
                        destination.chmod(0o640)
                    elif case == "symlink":
                        destination.unlink()
                        outside = Path(directory) / "outside"
                        outside.write_bytes(valid)
                        outside.chmod(0o440)
                        destination.symlink_to(outside)
                    elif case == "hardlink":
                        os.link(destination, Path(directory) / "second-link")
                before = self.snapshot(root)
                for operation in (self.check_adopt, self.adopt):
                    refused = operation(root, registry, identifier)
                    self.assertEqual(refused.returncode, 78)
                    expected = "content" if case == "content" else "installed file"
                    self.assertIn(expected, refused.stderr)
                    self.assertEqual(self.snapshot(root), before)
                    self.assertFalse((root / ".generations").exists())

    def test_interrupted_rotation_requires_separate_recovery(self) -> None:
        for failpoint, secret_changed in (
            ("after-marker-invalidation", False),
            ("after-secret-replacement", True),
        ):
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as directory:
                root, registry = self.fixture(directory)
                identifier = "monflorian.openai-api-key"
                filename, _mode, value = SPECS[identifier]
                source = Path(directory) / "source"
                self.write_source(source, value)
                first = self.install(root, registry, identifier, source)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.write_registry(
                    registry,
                    identifier=identifier,
                    generation=1,
                    target_generation=2,
                )
                replacement = Path(directory) / "replacement"
                replacement_value = value.replace(b"A", b"B")
                self.write_source(replacement, replacement_value)

                interrupted = self.run_helper(
                    root,
                    registry,
                    "--install-from",
                    str(replacement),
                    identifier,
                    failpoint=failpoint,
                )
                self.assertEqual(interrupted.returncode, 78)
                self.assertFalse(
                    (root / ".generations" / f"{identifier}.json").exists()
                )
                self.assertEqual(
                    (root / filename).read_bytes(),
                    replacement_value if secret_changed else value,
                )
                audit = self.run_helper(root, registry, "--check")
                self.assertEqual(audit.returncode, 78)
                self.assertIn("has no marker", audit.stderr)

                interrupted_state = self.snapshot(root)
                old_source = Path(directory) / "old-source"
                self.write_source(old_source, value)
                for candidate in (old_source, replacement):
                    resumed = self.install(root, registry, identifier, candidate)
                    self.assertEqual(resumed.returncode, 78)
                    self.assertIn("has no recovery marker", resumed.stderr)
                    self.assertEqual(self.snapshot(root), interrupted_state)

    def test_closed_identifier_invalid_source_and_registry_contract_fail_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            source = Path(directory) / "source"
            self.write_source(source, SPECS["monflorian.openai-api-key"][2])
            before = self.snapshot(root)
            unknown = self.run_helper(
                root,
                registry,
                "--install-from",
                str(source),
                "monflorian.unknown",
            )
            self.assertEqual(unknown.returncode, 78)
            self.assertIn("closed contract", unknown.stderr)
            self.assertEqual(self.snapshot(root), before)

            relative = self.run_helper(
                root,
                registry,
                "--install-from",
                "relative-source",
                "monflorian.openai-api-key",
            )
            self.assertEqual(relative.returncode, 78)
            self.assertIn("must be absolute", relative.stderr)
            self.assertEqual(self.snapshot(root), before)

            document = json.loads(registry.read_text(encoding="utf-8"))
            next(
                entry
                for entry in document["secrets"]
                if entry["id"] == "monflorian.openai-api-key"
            )["target_generation"] = 3
            registry.write_text(json.dumps(document) + "\n", encoding="utf-8")
            invalid_registry = self.run_helper(root, registry, "--check")
            self.assertEqual(invalid_registry.returncode, 78)
            self.assertIn("registry generation", invalid_registry.stderr)
            self.assertEqual(self.snapshot(root), before)

    def test_output_never_contains_source_paths_or_value_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            identifier = "monflorian.openai-api-key"
            source = Path(directory) / "do-not-log-this-source-name"
            sample_value = b"sk-proj-output-redaction-check-1234567890\n"
            self.write_source(source, sample_value)

            installed = self.install(root, registry, identifier, source)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            replacement = Path(directory) / "do-not-log-replacement-name"
            replacement_value = b"sk-proj-output-redaction-check-0987654321\n"
            self.write_source(replacement, replacement_value)
            refused = self.install(root, registry, identifier, replacement)
            self.assertEqual(refused.returncode, 78)

            for result in (installed, refused):
                output = result.stdout + result.stderr
                self.assertNotIn(str(source), output)
                self.assertNotIn(str(replacement), output)
                self.assertNotIn("output-redaction-check", output)
                self.assertNotIn(sample_value.decode("ascii").strip(), output)
                self.assertNotIn(replacement_value.decode("ascii").strip(), output)

    def test_test_mode_needs_both_hidden_paths_and_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, registry = self.fixture(directory)
            environment = os.environ.copy()
            environment.pop(TEST_GUARD, None)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--check",
                    "--test-root",
                    str(root),
                    "--test-registry",
                    str(registry),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("explicit unprivileged guard", result.stderr)


if __name__ == "__main__":
    unittest.main()
