#!/usr/bin/env python3
"""Adversarial tests for the private Surplasse pilot manifest materializer."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/materialize-surplasse-pilot-manifest"


def load_script():
    loader = SourceFileLoader("surplasse_pilot_manifest_test_subject", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_script()


def valid_manifest() -> dict[str, object]:
    return {
        "contract": "surplasse.pilot-bootstrap",
        "schema": 1,
        "mode": "testers",
        "restaurateur": {
            "id": "11111111-1111-4111-8111-111111111111",
            "email": "tester@restaurant.invalid",
            "full_name": "Testeur Pilote",
            "phone": None,
        },
        "establishment": {
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "Restaurant Pilote",
            "slug": "restaurant-pilote",
            "address": "1 rue du Test, 75001 Paris",
            "stripe_account_id": "acct_TestPilot1234",
        },
        "menu": {
            "id": "33333333-3333-4333-8333-333333333333",
            "name": "Carte pilote",
        },
        "category": {
            "id": "44444444-4444-4444-8444-444444444444",
            "name": "Plats",
        },
        "product": {
            "id": "55555555-5555-4555-8555-555555555555",
            "name": "Produit pilote",
            "description": None,
            "price_cents": 1200,
            "currency": "eur",
        },
        "table": {
            "id": "66666666-6666-4666-8666-666666666666",
            "label": "Table pilote",
        },
    }


class PilotManifestMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")

    def write_source(self, root: Path, value: object | None = None) -> Path:
        root.mkdir(mode=0o700)
        source = root / "pilot.json"
        source.write_text(
            json.dumps(valid_manifest() if value is None else value, indent=2) + "\n",
            encoding="utf-8",
        )
        source.chmod(0o600)
        return source

    def run_helper(
        self,
        target: Path,
        lock: Path,
        *arguments: str,
        interpreter: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_SURPLASSE_PILOT_TESTING"] = "1"
        command = (
            [sys.executable, "-I", str(SCRIPT)]
            if interpreter
            else [str(SCRIPT)]
        )
        return subprocess.run(
            [
                *command,
                *arguments,
                "--test-root",
                str(target),
                "--test-deployment-lock",
                str(lock),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def test_install_is_canonical_atomic_idempotent_and_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = self.write_source(root / "source")
            lock = root / "deployment.lock"
            first = self.run_helper(target, lock, "--install-from", str(source))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, "")
            self.assertEqual(first.stderr, "")
            installed = target / MATERIALIZER.TARGET.name
            metadata = installed.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o440)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(metadata.st_uid, os.geteuid())
            self.assertEqual(metadata.st_gid, os.getegid())
            raw = installed.read_bytes()
            self.assertEqual(raw, MATERIALIZER.canonical_manifest(source.read_bytes()))
            inode = metadata.st_ino
            second = self.run_helper(target, lock, "--install-from", str(source))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(installed.lstat().st_ino, inode)
            check = self.run_helper(target, lock, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(check.stdout, "")
            self.assertEqual(check.stderr, "")
            for private_value in (
                valid_manifest()["restaurateur"]["email"],
                valid_manifest()["establishment"]["stripe_account_id"],
            ):
                self.assertNotIn(str(private_value), first.stdout + first.stderr)

    def test_policy_rejects_nested_divergence_without_echoing_values(self) -> None:
        mutations = {
            "extra": lambda value: value.__setitem__("extra", True),
            "duplicate-id": lambda value: value["table"].__setitem__(
                "id", value["product"]["id"]
            ),
            "uppercase-email": lambda value: value["restaurateur"].__setitem__(
                "email", "PRIVATE@EXAMPLE.INVALID"
            ),
            "unicode-case-drift": lambda value: value["restaurateur"].__setitem__(
                "email", "x@\U00010d50.invalid"
            ),
            "reserved-slug": lambda value: value["establishment"].__setitem__(
                "slug", "api"
            ),
            "live-contract": lambda value: value.__setitem__("mode", "public"),
            "boolean-price": lambda value: value["product"].__setitem__(
                "price_cents", True
            ),
            "c1-control": lambda value: value["product"].__setitem__(
                "name", "A\u0085B"
            ),
            "utf16-overflow": lambda value: value["product"].__setitem__(
                "name", "😀" * 160
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(divergence=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                target.mkdir(mode=0o700)
                value = valid_manifest()
                mutate(value)
                source = self.write_source(root / "source", value)
                result = self.run_helper(
                    target,
                    root / "deployment.lock",
                    "--install-from",
                    str(source),
                )
                self.assertEqual(result.returncode, 78)
                self.assertEqual(list(target.iterdir()), [])
                self.assertNotIn("PRIVATE@EXAMPLE.INVALID", result.stderr)
                self.assertNotIn("acct_TestPilot1234", result.stderr)

    def test_duplicate_json_key_and_invalid_framing_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source_root = root / "source"
            source = self.write_source(source_root)
            source.write_bytes(
                source.read_bytes().replace(b'"schema": 1,', b'"schema": 1,\n  "schema": 1,', 1)
            )
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("duplicate key", result.stderr)
            source.write_bytes(b"{}\r\n")
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("framing", result.stderr)

            source.write_bytes(
                json.dumps(valid_manifest()).replace(
                    '"Produit pilote"', '"\\ud800"', 1
                ).encode("ascii")
                + b"\n"
            )
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 78)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("invalid", result.stderr)

            source.write_bytes(("[" * 1100 + "0" + "]" * 1100 + "\n").encode())
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 78)
            self.assertNotIn("Traceback", result.stderr)
            self.assertRegex(result.stderr, r"(?:invalid shape|strict UTF-8 JSON)")

            source.write_bytes(b'{"schema":' + b"9" * 5000 + b"}\n")
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
                interpreter=True,
            )
            self.assertEqual(result.returncode, 78)
            self.assertNotIn("Traceback", result.stderr)
            self.assertRegex(result.stderr, r"(?:invalid shape|strict UTF-8 JSON)")

    def test_text_limits_match_java_utf16_and_iso_control_semantics(self) -> None:
        self.assertEqual(MATERIALIZER.text("😀" * 80, 1, 160, "name"), "😀" * 80)
        for value in ("A\u0085B", "😀" * 81):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(MATERIALIZER.ManifestError):
                    MATERIALIZER.text(value, 1, 160, "name")

    def test_source_refuses_symlink_hardlink_mode_and_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = self.write_source(root / "source")
            lock = root / "deployment.lock"
            source.chmod(0o644)
            mode = self.run_helper(target, lock, "--install-from", str(source))
            self.assertEqual(mode.returncode, 78)
            source.chmod(0o600)
            hardlink = source.with_name("hardlink.json")
            os.link(source, hardlink)
            linked = self.run_helper(target, lock, "--install-from", str(source))
            self.assertEqual(linked.returncode, 78)
            hardlink.unlink()
            real = source.with_name("real.json")
            source.rename(real)
            source.symlink_to(real.name)
            symbolic = self.run_helper(target, lock, "--install-from", str(source))
            self.assertEqual(symbolic.returncode, 78)

            source.unlink()
            real.rename(source)
            original_stat = MATERIALIZER.os.stat

            def raced_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                values = list(result)
                values[1] += 1
                return os.stat_result(values)

            with mock.patch.object(MATERIALIZER.os, "stat", side_effect=raced_stat):
                with self.assertRaisesRegex(MATERIALIZER.ManifestError, "changed"):
                    MATERIALIZER.read_source(
                        source,
                        owner=os.geteuid(),
                        group=os.getegid(),
                    )

    def test_installed_symlink_or_hardlink_is_never_replaced(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                target.mkdir(mode=0o700)
                source = self.write_source(root / "source")
                installed = target / MATERIALIZER.TARGET.name
                if kind == "symlink":
                    installed.symlink_to(source)
                else:
                    installed.write_bytes(MATERIALIZER.canonical_manifest(source.read_bytes()))
                    installed.chmod(0o440)
                    os.link(installed, target / "second-link")
                result = self.run_helper(
                    target,
                    root / "deployment.lock",
                    "--install-from",
                    str(source),
                )
                self.assertEqual(result.returncode, 78)
                if kind == "symlink":
                    self.assertTrue(installed.is_symlink())
                else:
                    self.assertEqual(installed.lstat().st_nlink, 2)

    def test_crash_residue_cleanup_is_bounded_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = self.write_source(root / "source")
            residue = target / f"{MATERIALIZER.PENDING_PREFIX}dead{MATERIALIZER.PENDING_SUFFIX}"
            residue.write_bytes(b"")
            residue.chmod(0o600)
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(residue.exists())

            installed = target / MATERIALIZER.TARGET.name
            installed.unlink()
            residue.symlink_to(source)
            result = self.run_helper(
                target,
                root / "deployment.lock",
                "--install-from",
                str(source),
            )
            self.assertEqual(result.returncode, 78)
            self.assertTrue(residue.is_symlink())

    def test_cleanup_recovers_each_stage_around_chown_and_chmod(self) -> None:
        states = (
            (os.getegid(), 0o600, "before-fchown"),
            (10001, 0o600, "after-fchown"),
            (10001, 0o440, "after-fchmod"),
        )
        for group, mode, label in states:
            with self.subTest(crash=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                residue = root / (
                    f"{MATERIALIZER.PENDING_PREFIX}dead{MATERIALIZER.PENDING_SUFFIX}"
                )
                residue.write_bytes(b"")
                residue.chmod(mode)
                directory_fd = os.open(
                    root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                original_stat = MATERIALIZER.os.stat

                def staged_stat(path, *args, **kwargs):
                    metadata = original_stat(path, *args, **kwargs)
                    values = list(metadata)
                    values[5] = group
                    values[0] = stat.S_IFREG | mode
                    return os.stat_result(values)

                try:
                    with mock.patch.object(
                        MATERIALIZER.os,
                        "stat",
                        side_effect=staged_stat,
                    ):
                        MATERIALIZER.cleanup_residue(
                            directory_fd,
                            owner=os.geteuid(),
                            group=10001,
                        )
                finally:
                    os.close(directory_fd)
                self.assertFalse(residue.exists())

    def test_test_paths_require_explicit_unprivileged_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = self.write_source(root / "source")
            environment = os.environ.copy()
            environment.pop("VPS_SURPLASSE_PILOT_TESTING", None)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--install-from",
                    str(source),
                    "--test-root",
                    str(target),
                    "--test-deployment-lock",
                    str(root / "lock"),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(result.returncode, 78)
            self.assertEqual(list(target.iterdir()), [])

    def test_filesystem_error_is_generic_and_never_echoes_private_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            source = self.write_source(root / "source")
            lock = root / "deployment.lock"
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"VPS_SURPLASSE_PILOT_TESTING": "1"},
                ),
                mock.patch.object(
                    MATERIALIZER,
                    "install",
                    side_effect=OSError("PRIVATE@EXAMPLE.INVALID"),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                result = MATERIALIZER.main(
                    [
                        "--install-from",
                        str(source),
                        "--test-root",
                        str(target),
                        "--test-deployment-lock",
                        str(lock),
                    ]
                )
            self.assertEqual(result, 78)
            self.assertNotIn("PRIVATE@EXAMPLE.INVALID", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
