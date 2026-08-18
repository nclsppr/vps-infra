#!/usr/bin/env python3
"""Adversarial tests for local Surplasse pilot manifest staging."""

from __future__ import annotations

import importlib.util
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
SCRIPT = ROOT / "scripts/stage-surplasse-pilot-manifest"


def load_script():
    loader = SourceFileLoader("surplasse_pilot_local_stager_test_subject", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


STAGER = load_script()
PRIVATE_BYTES = b'{"email":"PRIVATE@example.invalid","account":"acct_Private123"}\n'


class PilotManifestLocalStagingTests(unittest.TestCase):
    def paths(self, root: Path) -> tuple[Path, Path]:
        source_directory = root / "source"
        destination_directory = root / "isolated-home"
        source_directory.mkdir(mode=0o700)
        destination_directory.mkdir(mode=0o700)
        source = source_directory / "pilot.json"
        source.write_bytes(PRIVATE_BYTES)
        source.chmod(0o600)
        return source, destination_directory / "pilot-manifest.json"

    def run_stager(
        self,
        source: Path,
        destination: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(source), str(destination)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_success_is_exact_protected_and_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self.paths(Path(directory))
            result = self.run_stager(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(destination.read_bytes(), PRIVATE_BYTES)
            metadata = destination.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_uid, os.geteuid())
            self.assertEqual(metadata.st_gid, os.getegid())
            self.assertEqual(metadata.st_nlink, 1)
            self.assertTrue(STAGER.SOURCE_FLAGS & os.O_NOFOLLOW)
            self.assertTrue(
                STAGER.SOURCE_FLAGS & getattr(os, "O_CLOEXEC", 0)
                or getattr(os, "O_CLOEXEC", 0) == 0
            )

    def test_mode_symlink_hardlink_and_oversize_are_refused(self) -> None:
        for divergence in ("mode", "symlink", "hardlink", "oversize"):
            with self.subTest(divergence=divergence), tempfile.TemporaryDirectory() as directory:
                source, destination = self.paths(Path(directory))
                if divergence == "mode":
                    source.chmod(0o644)
                elif divergence == "symlink":
                    real = source.with_name("real.json")
                    source.rename(real)
                    source.symlink_to(real.name)
                elif divergence == "hardlink":
                    os.link(source, source.with_name("second-link.json"))
                else:
                    source.write_bytes(b"x" * (STAGER.MAXIMUM_BYTES + 1))
                result = self.run_stager(source, destination)
                self.assertEqual(result.returncode, 78)
                self.assertFalse(destination.exists())
                self.assertEqual(
                    result.stderr,
                    "pilot manifest local staging refused: protected copy failed\n",
                )
                self.assertNotIn("PRIVATE@example.invalid", result.stderr)
                self.assertNotIn("acct_Private123", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_path_replacement_after_open_is_refused_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination = self.paths(Path(directory))
            original_copy = STAGER.copy_exact

            def replace_after_copy(source_fd: int, destination_fd: int, size: int) -> None:
                original_copy(source_fd, destination_fd, size)
                previous = source.with_name("opened.json")
                source.rename(previous)
                source.write_bytes(b"replacement\n")
                source.chmod(0o600)

            with mock.patch.object(
                STAGER,
                "copy_exact",
                side_effect=replace_after_copy,
            ):
                with self.assertRaisesRegex(STAGER.StagingError, "changed"):
                    STAGER.stage(source, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), b"replacement\n")
            self.assertEqual(source.with_name("opened.json").read_bytes(), PRIVATE_BYTES)


if __name__ == "__main__":
    unittest.main()
