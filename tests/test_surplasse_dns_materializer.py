#!/usr/bin/env python3
"""Adversarial tests for the Surplasse DNS credential materializer."""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/materialize-surplasse-dns-secrets"
MANIFEST_NAME = "surplasse-dns-credential-manifest.json"
LOCK_NAME = ".surplasse-dns-credentials.lock"
CREDENTIAL_NAMES = {
    "ovh-application-key",
    "ovh-application-secret",
    "ovh-consumer-key",
}
FINAL_INVENTORY = CREDENTIAL_NAMES | {MANIFEST_NAME, LOCK_NAME}


def load_script():
    loader = importlib.machinery.SourceFileLoader(
        "surplasse_dns_materializer", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader("surplasse_dns_materializer", loader)
    if spec is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["surplasse_dns_materializer"] = module
    loader.exec_module(module)
    return module


MATERIALIZER = load_script()


class SurplasseDnsMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")

    @staticmethod
    def valid_values(suffix: bytes = b"") -> dict[str, bytes]:
        return {
            "ovh-application-key": b"A" * 16 + suffix + b"\n",
            "ovh-application-secret": b"B" * 32 + suffix + b"\n",
            "ovh-consumer-key": b"C" * 32 + suffix + b"\n",
        }

    def write_source(
        self, root: Path, values: dict[str, bytes] | None = None
    ) -> dict[str, bytes]:
        root.mkdir(mode=0o700)
        contents = values if values is not None else self.valid_values()
        for name, content in contents.items():
            path = root / name
            path.write_bytes(content)
            path.chmod(0o600)
        (root / "ovh-application-key").chmod(0o400)
        return contents

    def run_helper(
        self,
        target: Path,
        *arguments: str,
        deployment_lock: Path | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_SURPLASSE_DNS_SECRET_TESTING"] = "1"
        command = [str(SCRIPT), *arguments, "--test-root", str(target)]
        if deployment_lock is not None:
            command.extend(("--test-deployment-lock", str(deployment_lock)))
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout,
        )

    @staticmethod
    def snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
        return {
            path.name: (
                path.read_bytes(),
                path.lstat().st_ino,
                stat.S_IMODE(path.lstat().st_mode),
            )
            for path in root.iterdir()
            if path.is_file() and not path.is_symlink()
        }

    def install_valid_bundle(
        self, root: Path, values: dict[str, bytes] | None = None
    ) -> tuple[Path, Path, dict[str, bytes]]:
        target = root / "target"
        source = root / "source"
        target.mkdir(mode=0o700)
        contents = self.write_source(source, values)
        result = self.run_helper(target, "--install-from", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        return target, source, contents

    def test_valid_install_is_exact_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, source, contents = self.install_valid_bundle(root)
            self.assertEqual({path.name for path in target.iterdir()}, FINAL_INVENTORY)
            for name in CREDENTIAL_NAMES | {MANIFEST_NAME}:
                metadata = (target / name).stat()
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o400)
                self.assertEqual(metadata.st_uid, os.geteuid())
                self.assertEqual(metadata.st_gid, os.getegid())
                self.assertEqual(metadata.st_nlink, 1)
            lock_metadata = (target / LOCK_NAME).stat()
            self.assertEqual(stat.S_IMODE(lock_metadata.st_mode), 0o600)
            self.assertEqual(lock_metadata.st_nlink, 1)

            manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="ascii"))
            self.assertEqual(manifest["contract"], "surplasse-ovh-dns-credentials")
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(set(manifest["sha256"]), CREDENTIAL_NAMES)
            for value in contents.values():
                self.assertNotIn(value.decode("ascii").strip(), json.dumps(manifest))

            before = self.snapshot(target)
            second = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(self.snapshot(target), before)
            check = self.run_helper(target, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(self.snapshot(target), before)

    def test_ansible_installs_only_the_root_helper_from_the_proven_mirror(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_surplasse_dns_materializer_path"],
            "/usr/local/libexec/vps/materialize-surplasse-dns-secrets",
        )
        self.assertEqual(
            defaults["vps_deploy_root_helpers"],
            [
                "materialize-monflorian-secret",
                "materialize-surplasse-dns-secrets",
                "materialize-surplasse-pilot-manifest",
                "surplasse-pilot-bootstrap",
            ],
        )
        self.assertNotIn(
            "materialize-surplasse-dns-secrets",
            defaults["vps_deploy_executables"],
        )

        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(encoding="utf-8")
        )
        by_name = {task["name"]: task for task in tasks}
        source = by_name[
            "Inspect root-only controller helper sources in the proven mirror"
        ]["ansible.builtin.stat"]
        self.assertEqual(
            source["path"],
            "{{ vps_deploy_repository_dir }}/scripts/{{ item }}",
        )
        self.assertIs(source["follow"], False)
        install = by_name[
            "Install root-only controller helpers from the proven mirror"
        ]["ansible.builtin.copy"]
        self.assertEqual(
            install,
            {
                "src": "{{ vps_deploy_repository_dir }}/scripts/{{ item }}",
                "dest": "{{ vps_deploy_install_root }}/{{ item }}",
                "remote_src": True,
                "owner": "root",
                "group": "root",
                "mode": "0500",
            },
        )
        proof = by_name["Prove every root-only controller helper is protected"]
        self.assertIn("item.stat.isreg", proof["ansible.builtin.assert"]["that"])
        self.assertIn(
            "item.stat.mode == '0500'", proof["ansible.builtin.assert"]["that"]
        )

    def test_check_is_read_only_and_does_not_create_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir(mode=0o700)
            result = self.run_helper(target, "--check")
            self.assertEqual(result.returncode, 78)
            self.assertEqual(list(target.iterdir()), [])

    def test_test_paths_require_the_explicit_unprivileged_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source)
            environment = os.environ.copy()
            environment.pop("VPS_SURPLASSE_DNS_SECRET_TESTING", None)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--install-from",
                    str(source),
                    "--test-root",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("explicit unprivileged test guard", result.stderr)
            self.assertEqual(list(target.iterdir()), [])
            self.assertFalse((root / ".vps-static.lock").exists())

    def test_complete_valid_bundle_can_rotate_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _, initial = self.install_valid_bundle(root)
            source = root / "replacement"
            replacement = self.write_source(source, self.valid_values(b"R"))
            result = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(initial, replacement)
            self.assertEqual(
                {name: (target / name).read_bytes() for name in CREDENTIAL_NAMES},
                replacement,
            )
            check = self.run_helper(target, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_source_must_be_absolute_private_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = root / "source"
            self.write_source(source)

            relative = self.run_helper(target, "--install-from", "relative-source")
            self.assertEqual(relative.returncode, 78)
            self.assertIn("must be absolute", relative.stderr)
            self.assertEqual(list(target.iterdir()), [])

            source.chmod(0o750)
            public = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(public.returncode, 78)
            self.assertIn("mode 0700", public.stderr)
            self.assertEqual(list(target.iterdir()), [])
            source.chmod(0o700)

            extra = source / "unexpected"
            extra.write_text("not-a-credential\n", encoding="ascii")
            extra.chmod(0o600)
            unexpected = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(unexpected.returncode, 78)
            self.assertIn("unexpected entry", unexpected.stderr)
            self.assertTrue(extra.exists())
            self.assertEqual(list(target.iterdir()), [])

    def test_source_files_must_be_private_regular_and_single_linked(self) -> None:
        cases = ("mode", "symlink", "hardlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                target.mkdir(mode=0o700)
                source = root / "source"
                self.write_source(source)
                credential = source / "ovh-application-key"
                if case == "mode":
                    credential.chmod(0o640)
                elif case == "symlink":
                    credential.unlink()
                    outside = root / "outside"
                    outside.write_bytes(b"A" * 16 + b"\n")
                    outside.chmod(0o600)
                    credential.symlink_to(outside)
                else:
                    os.link(credential, root / "second-link")
                result = self.run_helper(target, "--install-from", str(source))
                self.assertEqual(result.returncode, 78)
                self.assertIn("unsafe", result.stderr)
                self.assertEqual(list(target.iterdir()), [])

    def test_each_credential_format_is_bounded(self) -> None:
        invalid_values = (
            b"short\n",
            b"A" * 129 + b"\n",
            b"A" * 16,
            b"A" * 16 + b"\r\n",
            b"A" * 15 + b"/\n",
            b"A" * 15 + b" \n",
            b"A" * 15 + b"\0\n",
        )
        for invalid_value in invalid_values:
            with (
                self.subTest(value=repr(invalid_value)),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                target = root / "target"
                target.mkdir(mode=0o700)
                values = self.valid_values()
                values["ovh-application-key"] = invalid_value
                source = root / "source"
                self.write_source(source, values)
                result = self.run_helper(target, "--install-from", str(source))
                self.assertEqual(result.returncode, 78)
                self.assertIn("bounded ASCII token", result.stderr)
                self.assertNotIn(invalid_value.decode("ascii", "ignore"), result.stderr)
                self.assertEqual(list(target.iterdir()), [])

    def test_application_secret_and_consumer_key_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            values = self.valid_values()
            values["ovh-consumer-key"] = values["ovh-application-secret"]
            source = root / "source"
            self.write_source(source, values)
            result = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(result.returncode, 78)
            self.assertIn("must be distinct", result.stderr)
            self.assertNotIn(
                values["ovh-consumer-key"].decode("ascii").strip(), result.stderr
            )
            self.assertEqual(list(target.iterdir()), [])

    def test_unexpected_target_entry_is_preserved_and_blocks_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source)
            residue = target / ".ovh-application-key.deadbeef.pending"
            residue.write_text("operator-review-required\n", encoding="ascii")
            residue.chmod(0o400)
            before = self.snapshot(target)
            result = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(result.returncode, 78)
            self.assertIn("unexpected entry", result.stderr)
            self.assertEqual(self.snapshot(target), before)

    def test_incomplete_target_is_not_repaired_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _, _ = self.install_valid_bundle(root)
            missing = target / "ovh-application-key"
            missing.unlink()
            before = self.snapshot(target)
            replacement = root / "replacement"
            self.write_source(replacement, self.valid_values(b"R"))
            result = self.run_helper(target, "--install-from", str(replacement))
            self.assertEqual(result.returncode, 78)
            self.assertIn("incomplete", result.stderr)
            self.assertEqual(self.snapshot(target), before)
            self.assertFalse(missing.exists())

    def test_unsafe_installed_file_is_not_replaced(self) -> None:
        for case in ("mode", "hardlink", "symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, _, _ = self.install_valid_bundle(root)
                credential = target / "ovh-application-key"
                if case == "mode":
                    credential.chmod(0o600)
                elif case == "hardlink":
                    os.link(credential, root / "second-link")
                else:
                    credential.unlink()
                    outside = root / "outside"
                    outside.write_bytes(b"A" * 16 + b"\n")
                    outside.chmod(0o400)
                    credential.symlink_to(outside)
                source = root / "replacement"
                self.write_source(source, self.valid_values(b"R"))
                result = self.run_helper(target, "--install-from", str(source))
                self.assertEqual(result.returncode, 78)
                self.assertIn("unsafe", result.stderr)
                self.assertFalse(
                    any(".pending" in path.name for path in target.iterdir())
                )

    def test_stale_manifest_blocks_check_and_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _, _ = self.install_valid_bundle(root)
            manifest = target / MANIFEST_NAME
            manifest.chmod(0o600)
            manifest.write_text('{"contract":"stale"}\n', encoding="ascii")
            manifest.chmod(0o400)
            before = self.snapshot(target)

            check = self.run_helper(target, "--check")
            self.assertEqual(check.returncode, 78)
            self.assertIn("manifest does not match", check.stderr)
            self.assertEqual(self.snapshot(target), before)

            source = root / "replacement"
            self.write_source(source, self.valid_values(b"R"))
            rotation = self.run_helper(target, "--install-from", str(source))
            self.assertEqual(rotation.returncode, 78)
            self.assertIn("manifest does not match", rotation.stderr)
            self.assertEqual(self.snapshot(target), before)

    def test_manifest_is_the_final_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _, _ = self.install_valid_bundle(root)
            new_contents = self.valid_values(b"R")
            target_fd = os.open(target, MATERIALIZER.DIRECTORY_FLAGS)
            real_replace = os.replace

            def interrupted_replace(source, destination, **keywords):
                if destination == MANIFEST_NAME:
                    raise OSError("simulated interruption before commit marker")
                return real_replace(source, destination, **keywords)

            try:
                with mock.patch.object(
                    MATERIALIZER.os, "replace", side_effect=interrupted_replace
                ):
                    with self.assertRaisesRegex(OSError, "simulated interruption"):
                        MATERIALIZER.install_bundle(
                            target_fd,
                            new_contents,
                            owner=os.geteuid(),
                            group=os.getegid(),
                        )
            finally:
                os.close(target_fd)

            self.assertFalse(any(".pending" in path.name for path in target.iterdir()))
            check = self.run_helper(target, "--check")
            self.assertEqual(check.returncode, 78)
            self.assertIn("manifest does not match", check.stderr)

    def test_mutation_takes_shared_lock_before_bundle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source)
            deployment_lock = root / "vps-static.lock"
            deployment_descriptor = os.open(
                deployment_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(deployment_descriptor, fcntl.LOCK_EX)
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_DNS_SECRET_TESTING"] = "1"
            process = subprocess.Popen(
                [
                    str(SCRIPT),
                    "--install-from",
                    str(source),
                    "--test-root",
                    str(target),
                    "--test-deployment-lock",
                    str(deployment_lock),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse((target / LOCK_NAME).exists())
                fcntl.flock(deployment_descriptor, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertNotIn("A" * 16, stdout + stderr)
            finally:
                os.close(deployment_descriptor)
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_check_does_not_reacquire_the_shared_deployment_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _, _ = self.install_valid_bundle(root)
            deployment_lock = root / ".vps-static.lock"
            descriptor = os.open(
                deployment_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                check = self.run_helper(
                    target,
                    "--check",
                    deployment_lock=deployment_lock,
                    timeout=2,
                )
                self.assertEqual(check.returncode, 0, check.stderr)
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
