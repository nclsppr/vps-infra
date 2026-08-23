#!/usr/bin/env python3
"""Tests for the bounded SMTP credential materializer."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/materialize-smtp-secrets"
PROJECT_ID = "11111111-2222-4333-8444-555555555555\n"


class SmtpMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper forbids root test mode")

    def write_source(self, root: Path, product: str, opaque: str = "A" * 32) -> None:
        root.mkdir(mode=0o700)
        values = {
            f"{product}-smtp-password": f"{opaque}\n",
            f"{product}-smtp-username": PROJECT_ID,
        }
        for name, value in values.items():
            path = root / name
            path.write_text(value, encoding="ascii")
            path.chmod(0o600)

    def run_helper(
        self, target: Path, product: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_SMTP_SECRET_TESTING"] = "1"
        return subprocess.run(
            [
                str(SCRIPT),
                "--product",
                product,
                "--registry-generation",
                "1",
                *arguments,
                "--test-root",
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=5,
        )

    def crash_install(
        self,
        target: Path,
        source: Path,
        product: str,
        event_number: int,
    ) -> subprocess.CompletedProcess[str]:
        program = r"""
import importlib.machinery
import importlib.util
import os
import sys

helper, target, source, product, event_number = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("crashing_smtp_materializer", helper)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
spec.loader.exec_module(module)
original_fsync = module.os.fsync
original_replace = module.os.replace
seen = 0

def stop_after_call(result):
    global seen
    seen += 1
    if seen == int(event_number):
        os._exit(91)
    return result

def fsync_then_stop(*args, **kwargs):
    return stop_after_call(original_fsync(*args, **kwargs))

def replace_then_stop(*args, **kwargs):
    return stop_after_call(original_replace(*args, **kwargs))

module.os.fsync = fsync_then_stop
module.os.replace = replace_then_stop
sys.argv = [
    helper,
    "--product",
    product,
    "--registry-generation",
    "1",
    "--install-from",
    source,
    "--test-root",
    target,
]
raise SystemExit(module.main())
"""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(SCRIPT),
                str(target),
                str(source),
                product,
                str(event_number),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "VPS_SMTP_SECRET_TESTING": "1"},
            timeout=5,
        )

    def test_each_exact_profile_installs_and_checks_generation_one(self) -> None:
        expected = {
            "monflorian": (False, True),
            "parkventory": (False, False),
            "surplasse": (True, False),
        }
        for product, (include_host, storage_only) in expected.items():
            with self.subTest(product=product), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                source = root / "source"
                target.mkdir(mode=0o700)
                self.write_source(source, product)

                install = self.run_helper(
                    target, product, "--install-from", str(source)
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                names = {
                    f"{product}-smtp-password",
                    f"{product}-smtp-username",
                    f"{product}-smtp-generation.json",
                }
                if include_host:
                    names.add(f"{product}-smtp-host")
                self.assertEqual({path.name for path in target.iterdir()}, names)

                for name in names - {f"{product}-smtp-generation.json"}:
                    metadata = (target / name).stat()
                    expected_mode = 0o400 if name.endswith("-host") else 0o440
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), expected_mode)
                    self.assertEqual(metadata.st_nlink, 1)
                marker_path = target / f"{product}-smtp-generation.json"
                self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o400)
                marker = json.loads(marker_path.read_text(encoding="ascii"))
                self.assertEqual(
                    set(marker),
                    {
                        "contract",
                        "installed_files",
                        "product",
                        "registry_generation",
                        "secret_ids",
                        "storage_only",
                        "version",
                    },
                )
                self.assertEqual(marker["contract"], "atlas-smtp-credentials")
                self.assertEqual(marker["product"], product)
                self.assertEqual(marker["registry_generation"], 1)
                self.assertEqual(marker["storage_only"], storage_only)
                self.assertEqual(set(marker["installed_files"]), names - {marker_path.name})
                self.assertEqual(
                    set(marker["secret_ids"]),
                    {
                        name.replace(f"{product}-", f"{product}.")
                        for name in names
                        if name != marker_path.name
                    },
                )
                marker_text = marker_path.read_text(encoding="ascii")
                self.assertNotIn("A" * 32, marker_text)
                self.assertNotIn(PROJECT_ID.strip(), marker_text)

                before = {
                    path.name: (path.stat().st_ino, path.read_bytes())
                    for path in target.iterdir()
                }
                check = self.run_helper(target, product, "--check")
                self.assertEqual(check.returncode, 0, check.stderr)
                second = self.run_helper(
                    target, product, "--install-from", str(source)
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(
                    {
                        path.name: (path.stat().st_ino, path.read_bytes())
                        for path in target.iterdir()
                    },
                    before,
                )

    def test_source_is_private_exact_and_never_printed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source, "parkventory")

            extra = source / "unexpected"
            extra.write_text("secret-looking-value\n", encoding="ascii")
            extra.chmod(0o600)
            result = self.run_helper(
                target, "parkventory", "--install-from", str(source)
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("unexpected entry", result.stderr)
            self.assertNotIn("secret-looking-value", result.stdout + result.stderr)
            self.assertEqual(list(target.iterdir()), [])

            extra.unlink()
            source.chmod(0o750)
            result = self.run_helper(
                target, "parkventory", "--install-from", str(source)
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("mode 0700", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_generation_mismatch_rotation_and_incomplete_sets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source, "surplasse")
            install = self.run_helper(
                target, "surplasse", "--install-from", str(source)
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            (source / "surplasse-smtp-password").write_text(
                "B" * 32 + "\n", encoding="ascii"
            )
            (source / "surplasse-smtp-password").chmod(0o600)
            rotation = self.run_helper(
                target, "surplasse", "--install-from", str(source)
            )
            self.assertEqual(rotation.returncode, 78)
            self.assertIn("new registry generation", rotation.stderr)
            self.assertNotIn("B" * 32, rotation.stdout + rotation.stderr)

            marker = target / "surplasse-smtp-generation.json"
            marker.unlink()
            incomplete = self.run_helper(target, "surplasse", "--check")
            self.assertEqual(incomplete.returncode, 78)
            self.assertIn("incomplete", incomplete.stderr)

            wrong_generation = subprocess.run(
                [
                    str(SCRIPT),
                    "--product",
                    "surplasse",
                    "--registry-generation",
                    "2",
                    "--check",
                    "--test-root",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "VPS_SMTP_SECRET_TESTING": "1"},
                timeout=5,
            )
            self.assertEqual(wrong_generation.returncode, 78)
            self.assertIn("requires registry generation 1", wrong_generation.stderr)

    def test_every_write_boundary_recovers_with_the_identical_source(self) -> None:
        profiles = {"monflorian": 2, "parkventory": 2, "surplasse": 3}
        for product, installed_count in profiles.items():
            event_count = 2 * installed_count + 4
            for event_number in range(1, event_count + 1):
                with (
                    self.subTest(product=product, event=event_number),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    target = root / "target"
                    source = root / "source"
                    target.mkdir(mode=0o700)
                    self.write_source(source, product)

                    crashed = self.crash_install(
                        target, source, product, event_number
                    )
                    self.assertEqual(crashed.returncode, 91, crashed.stderr)

                    recovered = self.run_helper(
                        target, product, "--install-from", str(source)
                    )
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    checked = self.run_helper(target, product, "--check")
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    before = {
                        path.name: (path.stat().st_ino, path.read_bytes())
                        for path in target.iterdir()
                    }
                    repeated = self.run_helper(
                        target, product, "--install-from", str(source)
                    )
                    self.assertEqual(repeated.returncode, 0, repeated.stderr)
                    self.assertEqual(
                        {
                            path.name: (path.stat().st_ino, path.read_bytes())
                            for path in target.iterdir()
                        },
                        before,
                    )
                    self.assertFalse(list(target.glob(".*.pending")))

    def test_retry_refuses_a_different_source_and_foreign_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source, "parkventory")
            crashed = self.crash_install(target, source, "parkventory", 4)
            self.assertEqual(crashed.returncode, 91, crashed.stderr)
            password_path = source / "parkventory-smtp-password"
            password_path.write_text("B" * 32 + "\n", encoding="ascii")
            password_path.chmod(0o600)

            refused = self.run_helper(
                target, "parkventory", "--install-from", str(source)
            )
            self.assertEqual(refused.returncode, 78)
            self.assertIn("differs from the supplied source", refused.stderr)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            source = root / "source"
            target.mkdir(mode=0o700)
            self.write_source(source, "parkventory")
            residue = (
                target
                / ".parkventory-smtp-password.0123456789abcdef01234567.pending"
            )
            residue.write_text("foreign-value-that-is-not-the-source\n", encoding="ascii")
            residue.chmod(0o600)

            refused = self.run_helper(
                target, "parkventory", "--install-from", str(source)
            )
            self.assertEqual(refused.returncode, 78)
            self.assertIn("foreign staging file", refused.stderr)
            self.assertTrue(residue.exists())

    def test_check_validates_password_username_and_exact_host_content(self) -> None:
        cases = (
            ("parkventory", "parkventory-smtp-password", b"invalid\n", 0o440),
            ("parkventory", "parkventory-smtp-username", b"not-a-uuid\n", 0o440),
            ("surplasse", "surplasse-smtp-host", b"smtp.example.com\n", 0o400),
        )
        for product, name, invalid, mode in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                source = root / "source"
                target.mkdir(mode=0o700)
                self.write_source(source, product)
                installed = self.run_helper(
                    target, product, "--install-from", str(source)
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)
                path = target / name
                path.chmod(0o600)
                path.write_bytes(invalid)
                path.chmod(mode)

                checked = self.run_helper(target, product, "--check")
                self.assertEqual(checked.returncode, 78)
                self.assertIn("invalid framing", checked.stderr)

    def test_ansible_installs_the_helper_as_root_only(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_smtp_materializer_path"],
            "/usr/local/libexec/vps/materialize-smtp-secrets",
        )
        self.assertIn("materialize-smtp-secrets", defaults["vps_deploy_root_helpers"])
        self.assertNotIn("materialize-smtp-secrets", defaults["vps_deploy_executables"])


if __name__ == "__main__":
    unittest.main()
