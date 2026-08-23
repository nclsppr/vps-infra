#!/usr/bin/env python3
"""Tests for the Parkventory provider credential materializer."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/materialize-parkventory-provider-secrets"


def load_helper():
    loader = importlib.machinery.SourceFileLoader(
        "parkventory_provider_materializer", str(HELPER)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_helper()
PROVIDER_VALUES = {
    "parkventory-oidc-client-secret": (
        b"auth0-provider-client-value-0123456789abcdef\n"
    ),
}


def runtime_configuration(**overrides: str) -> bytes:
    values = {
        "PARKVENTORY_DB_MIGRATOR_USER": "parkventory_migrator",
        "PARKVENTORY_DB_RUNTIME_USER": "parkventory_runtime",
        "PARKVENTORY_JDBC_URL": "jdbc:postgresql://postgresql:5432/parkventory",
        "PARKVENTORY_OIDC_AUTH_SERVER_URL": "https://parkventory.eu.auth0.com/",
        "PARKVENTORY_OIDC_CLIENT_ID": "parkventory-client",
        "PARKVENTORY_OIDC_ISSUER": "https://parkventory.eu.auth0.com/",
        "PARKVENTORY_SMTP_FROM": "no-reply@parkventory.com",
        "PARKVENTORY_SMTP_HOST": "smtp.tem.scaleway.com",
        "PARKVENTORY_SMTP_PORT": "587",
        "PARKVENTORY_WEB_BASE_URL": "https://parkventory.com",
    }
    values.update(overrides)
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode(
        "ascii"
    )


class ParkventoryProviderMaterializerTests(unittest.TestCase):
    def make_source(self, parent: Path, **overrides: str) -> Path:
        source = parent / "source"
        source.mkdir(mode=0o700)
        for name, content in PROVIDER_VALUES.items():
            path = source / name
            path.write_bytes(content)
            path.chmod(0o600)
        runtime = source / "parkventory.env"
        runtime.write_bytes(runtime_configuration(**overrides))
        runtime.chmod(0o600)
        return source

    def run_helper(
        self,
        target: Path,
        runtime: Path,
        lock: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_PARKVENTORY_PROVIDER_TESTING"] = "1"
        return subprocess.run(
            [
                str(HELPER),
                *arguments,
                "--test-root",
                str(target),
                "--test-runtime-root",
                str(runtime),
                "--test-deployment-lock",
                str(lock),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    def test_validate_install_check_and_idempotence(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            target = root / "target"
            runtime = root / "applications"
            target.mkdir(mode=0o700)
            unrelated = target / "parkventory-oidc-state-secret"
            unrelated.write_bytes(b"local-generated-value\n")
            unrelated.chmod(0o440)
            unrelated_before = (unrelated.read_bytes(), unrelated.stat().st_ino)
            external_smtp = {
                "parkventory-smtp-generation.json": b"external marker\n",
                "parkventory-smtp-password": b"external-smtp-password\n",
                "parkventory-smtp-username": b"11111111-2222-4333-8444-555555555555\n",
            }
            for name, value in external_smtp.items():
                path = target / name
                path.write_bytes(value)
                path.chmod(0o400 if name.endswith(".json") else 0o440)
            external_before = {
                name: ((target / name).read_bytes(), (target / name).stat().st_ino)
                for name in external_smtp
            }
            lock = root / "deployment.lock"

            validated = self.run_helper(
                target, runtime, lock, "--validate-source", str(source)
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertFalse(runtime.exists())
            installed = self.run_helper(
                target, runtime, lock, "--install-from", str(source)
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(json.loads(installed.stdout)["changed"])

            for name, value in PROVIDER_VALUES.items():
                path = target / name
                self.assertEqual(path.read_bytes(), value)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
                self.assertNotIn(value[:-1], installed.stdout.encode("ascii"))
                self.assertNotIn(
                    hashlib.sha256(value).hexdigest(), installed.stdout
                )
            self.assertEqual(
                (unrelated.read_bytes(), unrelated.stat().st_ino),
                unrelated_before,
            )
            self.assertEqual(
                external_before,
                {
                    name: ((target / name).read_bytes(), (target / name).stat().st_ino)
                    for name in external_smtp
                },
            )
            runtime_path = runtime / "parkventory.env"
            self.assertEqual(runtime_path.read_bytes(), runtime_configuration())
            self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)

            marker_path = target / "parkventory-provider-secret-generation.json"
            self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o400)
            marker = json.loads(marker_path.read_text(encoding="ascii"))
            self.assertEqual(marker["target_generation"], 1)
            self.assertEqual(
                marker["secrets"],
                [
                    {
                        "file": "parkventory-oidc-client-secret",
                        "id": "parkventory.oidc-client-secret",
                    },
                ],
            )
            self.assertNotIn("sha256", marker)

            repeated = self.run_helper(
                target, runtime, lock, "--install-from", str(source)
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["changed"])
            checked = self.run_helper(target, runtime, lock, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["ready"])

    def test_source_inventory_metadata_and_runtime_policy_are_strict(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        invalid_values = {
            "PARKVENTORY_DB_RUNTIME_USER": "postgres",
            "PARKVENTORY_JDBC_URL": "jdbc:postgresql://localhost/parkventory",
            "PARKVENTORY_OIDC_AUTH_SERVER_URL": "http://identity.invalid/",
            "PARKVENTORY_OIDC_CLIENT_ID": "bad id",
            "PARKVENTORY_OIDC_ISSUER": "https://other.eu.auth0.com/",
            "PARKVENTORY_SMTP_FROM": "sender@example.com",
            "PARKVENTORY_SMTP_HOST": "smtp.example.com",
            "PARKVENTORY_SMTP_PORT": "465",
            "PARKVENTORY_WEB_BASE_URL": "https://www.parkventory.com",
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = self.make_source(root, **{key: value})
                target = root / "target"
                runtime = root / "runtime"
                target.mkdir(mode=0o700)
                refused = self.run_helper(
                    target,
                    runtime,
                    root / "lock",
                    "--validate-source",
                    str(source),
                )
                self.assertEqual(refused.returncode, 78)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            target = root / "target"
            target.mkdir(mode=0o700)
            (source / "unexpected").write_text("x", encoding="ascii")
            refused = self.run_helper(
                target,
                root / "runtime",
                root / "lock",
                "--validate-source",
                str(source),
            )
            self.assertEqual(refused.returncode, 78)
            self.assertIn("unexpected entry", refused.stderr)

    def test_marker_blocks_uncommitted_rotation(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            target = root / "target"
            runtime = root / "runtime"
            target.mkdir(mode=0o700)
            lock = root / "lock"
            first = self.run_helper(
                target, runtime, lock, "--install-from", str(source)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            installed = (target / "parkventory-oidc-client-secret").read_bytes()
            replacement = b"auth0-provider-client-value-fedcba9876543210\n"
            (source / "parkventory-oidc-client-secret").write_bytes(replacement)
            (source / "parkventory-oidc-client-secret").chmod(0o600)

            refused = self.run_helper(
                target, runtime, lock, "--install-from", str(source)
            )

            self.assertEqual(refused.returncode, 78)
            self.assertIn("new target generation", refused.stderr)
            self.assertEqual(
                (target / "parkventory-oidc-client-secret").read_bytes(), installed
            )

    def test_killed_install_recovers_only_expected_pending_files(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            target = root / "target"
            runtime = root / "runtime"
            target.mkdir(mode=0o700)
            lock = root / "lock"
            crash_program = """
import importlib.machinery
import importlib.util
import os
import sys

helper, source, target, runtime, lock = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("crashing_materializer", helper)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
spec.loader.exec_module(module)
original_replace = module.os.replace

def replace_then_die(*args, **kwargs):
    original_replace(*args, **kwargs)
    os._exit(91)

module.os.replace = replace_then_die
sys.argv = [
    helper,
    "--install-from",
    source,
    "--test-root",
    target,
    "--test-runtime-root",
    runtime,
    "--test-deployment-lock",
    lock,
]
module.main()
"""
            environment = os.environ.copy()
            environment["VPS_PARKVENTORY_PROVIDER_TESTING"] = "1"
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    crash_program,
                    str(HELPER),
                    str(source),
                    str(target),
                    str(runtime),
                    str(lock),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=20,
            )
            self.assertEqual(crashed.returncode, 91, crashed.stderr)
            self.assertTrue(list(target.glob(".*.pending")))
            self.assertTrue(list(runtime.glob(".*.pending")))

            unrelated = target / ".unrelated.0123456789abcdef01234567.pending"
            unrelated.write_bytes(b"must remain untouched\n")
            unrelated.chmod(0o600)
            unrelated_before = (unrelated.read_bytes(), unrelated.stat().st_ino)

            retried = self.run_helper(
                target, runtime, lock, "--install-from", str(source)
            )

            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertTrue(json.loads(retried.stdout)["changed"])
            self.assertEqual(
                (unrelated.read_bytes(), unrelated.stat().st_ino),
                unrelated_before,
            )
            expected_prefixes = tuple(
                f".{name}."
                for name in (
                    *PROVIDER_VALUES,
                    "parkventory-provider-secret-generation.json",
                )
            )
            self.assertFalse(
                any(
                    path.name.startswith(expected_prefixes)
                    and path.name.endswith(".pending")
                    for path in target.iterdir()
                )
            )
            self.assertFalse(list(runtime.glob(".parkventory.env.*.pending")))
            checked = self.run_helper(target, runtime, lock, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_unsafe_pending_residue_is_refused_and_preserved(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        cases = ("malformed-name", "symlink", "wrong-mode")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = self.make_source(root)
                target = root / "target"
                runtime = root / "runtime"
                target.mkdir(mode=0o700)
                if case == "malformed-name":
                    residue = (
                        target
                        / ".parkventory-oidc-client-secret.deadbeef.pending"
                    )
                    residue.write_bytes(b"staging residue\n")
                    residue.chmod(0o600)
                else:
                    residue = (
                        target
                        / ".parkventory-oidc-client-secret.0123456789abcdef01234567.pending"
                    )
                    if case == "symlink":
                        residue.symlink_to(source / "parkventory-oidc-client-secret")
                    else:
                        residue.write_bytes(b"staging residue\n")
                        residue.chmod(0o644)

                refused = self.run_helper(
                    target,
                    runtime,
                    root / "lock",
                    "--install-from",
                    str(source),
                )

                self.assertEqual(refused.returncode, 78)
                self.assertIn("unsafe staging file", refused.stderr)
                self.assertTrue(os.path.lexists(residue))
                self.assertFalse(
                    (target / "parkventory-provider-secret-generation.json").exists()
                )

    def test_generation_marker_is_replaced_last(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            runtime = root / "runtime"
            target.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
            runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
            replaced: list[str] = []
            original_replace = MATERIALIZER.os.replace

            def record_replace(source, destination, **kwargs):
                original_replace(source, destination, **kwargs)
                replaced.append(destination)

            try:
                MATERIALIZER.os.replace = record_replace
                changed = MATERIALIZER.install_bundle(
                    target_fd,
                    runtime_fd,
                    dict(PROVIDER_VALUES),
                    runtime_configuration(),
                    owner=os.geteuid(),
                    application_group=os.getegid(),
                    root_group=os.getegid(),
                )
            finally:
                MATERIALIZER.os.replace = original_replace
                os.close(runtime_fd)
                os.close(target_fd)

            self.assertTrue(changed)
            self.assertEqual(
                replaced[-1], "parkventory-provider-secret-generation.json"
            )


if __name__ == "__main__":
    unittest.main()
