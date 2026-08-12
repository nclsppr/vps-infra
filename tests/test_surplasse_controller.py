#!/usr/bin/env python3
"""Tests for the fail-closed Surplasse Atlas preparation controller."""

from __future__ import annotations

import base64
import hashlib
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

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
OPERATOR_MANIFEST = "surplasse-operator-bundle-manifest.json"
OPERATOR_LOCK = ".surplasse-operator-bundle.lock"


def load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


CONTROLLER = load_script(
    "surplasse_controller_validator", SCRIPTS / "validate-surplasse-controller"
)
PROVISIONER = load_script(
    "surplasse_postgres_provisioner", SCRIPTS / "provision-surplasse-postgres"
)
MATERIALIZER = load_script(
    "surplasse_secret_materializer", SCRIPTS / "materialize-surplasse-secrets"
)


class SurplasseControllerTests(unittest.TestCase):
    def write_operator_bundle(self, root: Path) -> dict[str, bytes]:
        root.mkdir(mode=0o700)
        signing_material = subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        modulus_output = (
            subprocess.run(
                ["openssl", "rsa", "-noout", "-modulus"],
                input=signing_material,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            .stdout.decode("ascii")
            .strip()
        )
        self.assertTrue(modulus_output.startswith("Modulus="))
        modulus = bytes.fromhex(modulus_output.removeprefix("Modulus="))

        def base64url(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

        key_id = "atlas-2026-08"
        jwks = (
            json.dumps(
                {
                    "keys": [
                        {
                            "alg": "RS256",
                            "e": base64url((65537).to_bytes(3, "big")),
                            "kid": key_id,
                            "kty": "RSA",
                            "n": base64url(modulus),
                            "use": "sig",
                        }
                    ]
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        values = {
            "surplasse-jwt-jwks": jwks,
            "surplasse-jwt-private-key": signing_material,
            "surplasse-jwt-key-id": key_id.encode("ascii") + b"\n",
            "surplasse-smtp-host": b"smtp.example.invalid\n",
            "surplasse-smtp-port": b"587\n",
            "surplasse-smtp-password": b"smtp-password-for-test-only\n",
            "surplasse-smtp-username": b"surplasse-test\n",
            "surplasse-stripe-account-webhook-secret": b"whsec_" + b"A" * 32 + b"\n",
            "surplasse-stripe-payment-webhook-secret": b"whsec_" + b"B" * 32 + b"\n",
            "surplasse-stripe-secret-key": b"sk_" + b"live_" + b"C" * 32 + b"\n",
            "ovh-application-key": b"D" * 16 + b"\n",
            "ovh-application-secret": b"E" * 32 + b"\n",
            "ovh-consumer-key": b"F" * 32 + b"\n",
        }
        for name, value in values.items():
            path = root / name
            path.write_bytes(value)
            path.chmod(0o600)
        return values

    def run_secret_helper(
        self, protected_root: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
        return subprocess.run(
            [
                str(SCRIPTS / "materialize-surplasse-secrets"),
                *arguments,
                "--test-root",
                str(protected_root),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def rendered_compose(self, root: Path) -> Path:
        output = root / "compose.json"
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / "applications/surplasse/.env.example"),
                "--file",
                str(ROOT / "applications/surplasse/compose.yaml"),
                "--profile",
                "migration",
                "config",
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output.write_text(result.stdout, encoding="utf-8")
        return output

    def test_current_adapter_prepares_and_refuses_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rendered = self.rendered_compose(Path(directory))
            CONTROLLER.validate(ROOT, "prepare", rendered)
            with self.assertRaisesRegex(
                CONTROLLER.ControllerError,
                "activation requires adapter.activation_policy=ready",
            ):
                CONTROLLER.validate(ROOT, "activate", rendered)

    def test_database_secret_preparation_is_private_and_idempotent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            protected_root = Path(directory) / "secrets"
            protected_root.mkdir(mode=0o700)
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            command = [
                str(SCRIPTS / "materialize-surplasse-secrets"),
                "--database-only",
                "--test-root",
                str(protected_root),
            ]
            first = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            values = {}
            for name in (
                "surplasse-postgres-migrator-password",
                "surplasse-postgres-runtime-password",
            ):
                path = protected_root / name
                metadata = path.stat()
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o440)
                self.assertEqual(metadata.st_uid, os.geteuid())
                self.assertEqual(metadata.st_gid, os.getegid())
                values[name] = path.read_bytes()
                self.assertRegex(values[name], rb"^[A-Za-z0-9_-]{64}\n$")
            self.assertNotEqual(*values.values())

            second = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                values,
                {name: (protected_root / name).read_bytes() for name in values},
            )

            complete = subprocess.run(
                [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--test-root",
                    str(protected_root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertNotEqual(complete.returncode, 0)
            self.assertIn(
                "operator-supplied secret surplasse-jwt-jwks is missing",
                complete.stderr,
            )

    def test_operator_bundle_is_validated_materialized_and_idempotent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            values = self.write_operator_bundle(root / "source")
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            command = [
                str(SCRIPTS / "materialize-surplasse-secrets"),
                "--install-operator-from",
                str(root / "source"),
                "--test-root",
                str(protected_root),
            ]
            first = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            combined_output = (first.stdout + first.stderr).encode("utf-8")
            for value in values.values():
                self.assertNotIn(value.strip(), combined_output)
            for name, value in values.items():
                path = protected_root / name
                self.assertEqual(path.read_bytes(), value)
                expected_mode = (
                    0o400
                    if name
                    in {
                        "ovh-application-key",
                        "ovh-application-secret",
                        "ovh-consumer-key",
                        "surplasse-jwt-key-id",
                        "surplasse-smtp-host",
                        "surplasse-smtp-port",
                    }
                    else 0o440
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
            manifest_path = protected_root / OPERATOR_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            self.assertEqual(manifest["contract"], "surplasse-operator-bundle")
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(
                manifest["sha256"],
                {
                    name: hashlib.sha256(value).hexdigest()
                    for name, value in values.items()
                },
            )
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o400)
            inode_contract = {
                name: (protected_root / name).stat().st_ino for name in values
            }
            manifest_inode = manifest_path.stat().st_ino

            second = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                values,
                {name: (protected_root / name).read_bytes() for name in values},
            )
            self.assertEqual(
                inode_contract,
                {name: (protected_root / name).stat().st_ino for name in values},
            )
            self.assertEqual(manifest_inode, manifest_path.stat().st_ino)
            validation = subprocess.run(
                [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--operator-only",
                    "--test-root",
                    str(protected_root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_invalid_operator_bundle_is_rejected_without_materialization(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            self.write_operator_bundle(source)
            invalid = source / "surplasse-stripe-secret-key"
            invalid.write_bytes(b"sk_" + b"test_" + b"X" * 32 + b"\n")
            invalid.chmod(0o600)
            sentinel = protected_root / "surplasse-smtp-host"
            sentinel.write_bytes(b"existing.example.invalid\n")
            sentinel.chmod(0o400)
            before = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in protected_root.iterdir()
            }
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            result = subprocess.run(
                [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--install-operator-from",
                    str(source),
                    "--test-root",
                    str(protected_root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("stripe-secret-key format", result.stderr)
            self.assertEqual(
                before,
                {
                    path.name: (path.read_bytes(), path.stat().st_ino)
                    for path in protected_root.iterdir()
                    if path.name != OPERATOR_LOCK
                },
            )

    def test_operator_bundle_rejects_a_jwt_kid_mismatch(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            self.write_operator_bundle(source)
            key_id = source / "surplasse-jwt-key-id"
            key_id.write_bytes(b"different-active-key\n")
            key_id.chmod(0o600)
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            result = subprocess.run(
                [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--install-operator-from",
                    str(source),
                    "--test-root",
                    str(protected_root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("active kid exactly once", result.stderr)
            self.assertEqual(
                {path.name for path in protected_root.iterdir()}, {OPERATOR_LOCK}
            )

    def test_operator_bundle_rejects_concatenated_private_keys(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            values = self.write_operator_bundle(source)
            pem_path = source / "surplasse-jwt-private-key"
            pem_path.write_bytes(
                values["surplasse-jwt-private-key"]
                + values["surplasse-jwt-private-key"]
            )
            pem_path.chmod(0o600)

            result = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source)
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("not a bounded PEM private key", result.stderr)
            self.assertEqual(
                {path.name for path in protected_root.iterdir()}, {OPERATOR_LOCK}
            )

    def test_interrupted_rotation_fails_closed_and_recovers(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source_a = root / "source-a"
            source_b = root / "source-b"
            self.write_operator_bundle(source_a)
            values_b = self.write_operator_bundle(source_b)
            changes = {
                "surplasse-smtp-password": b"rotated-smtp-password\n",
                "surplasse-stripe-secret-key": b"sk_live_" + b"G" * 32 + b"\n",
                "ovh-consumer-key": b"H" * 32 + b"\n",
            }
            values_b.update(changes)
            for name, value in changes.items():
                path = source_b / name
                path.write_bytes(value)
                path.chmod(0o600)

            first = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_a)
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            for index, (name, value) in enumerate(changes.items()):
                destination = protected_root / name
                pending = protected_root / f".{name}.{index:024x}.pending"
                pending.write_bytes(value)
                pending.chmod(stat.S_IMODE(destination.stat().st_mode))
                os.replace(pending, destination)
            orphan = (
                protected_root
                / ".surplasse-stripe-secret-key.ffffffffffffffffffffffff.pending"
            )
            orphan.write_bytes(b"sk_live_" + b"Z" * 32 + b"\n")
            orphan.chmod(0o440)

            interrupted = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(interrupted.returncode, 78)
            self.assertIn("manifest does not match", interrupted.stderr)
            self.assertFalse(orphan.exists())

            recovery = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_b)
            )
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            validation = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(validation.returncode, 0, validation.stderr)
            self.assertEqual(
                values_b,
                {name: (protected_root / name).read_bytes() for name in values_b},
            )

    def test_missing_and_malformed_operator_manifest_are_rejected(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            self.write_operator_bundle(source)
            install = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source)
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            manifest = protected_root / OPERATOR_MANIFEST
            manifest.unlink()
            missing = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(missing.returncode, 78)
            self.assertIn("manifest is missing", missing.stderr)

            reinstall = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source)
            )
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            manifest.chmod(0o600)
            manifest.write_bytes(b"{}\n")
            manifest.chmod(0o400)
            malformed = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(malformed.returncode, 78)
            self.assertIn("manifest does not match", malformed.stderr)

    def test_unsafe_manifest_target_blocks_rotation_before_secret_changes(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        for unsafe_kind in ("directory", "symlink", "hardlink", "wrong-mode"):
            with self.subTest(unsafe_kind=unsafe_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                protected_root = root / "target"
                protected_root.mkdir(mode=0o700)
                source_a = root / "source-a"
                source_b = root / "source-b"
                values_a = self.write_operator_bundle(source_a)
                self.write_operator_bundle(source_b)
                install = self.run_secret_helper(
                    protected_root, "--install-operator-from", str(source_a)
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                manifest = protected_root / OPERATOR_MANIFEST
                if unsafe_kind == "directory":
                    manifest.unlink()
                    manifest.mkdir(mode=0o700)
                elif unsafe_kind == "symlink":
                    manifest.unlink()
                    manifest.symlink_to(protected_root / "surplasse-smtp-host")
                elif unsafe_kind == "hardlink":
                    os.link(manifest, root / "external-manifest-link")
                else:
                    manifest.chmod(0o600)
                before = {
                    name: (
                        (protected_root / name).read_bytes(),
                        (protected_root / name).stat().st_ino,
                    )
                    for name in values_a
                }

                rotation = self.run_secret_helper(
                    protected_root, "--install-operator-from", str(source_b)
                )

                self.assertEqual(rotation.returncode, 78)
                self.assertIn("manifest", rotation.stderr)
                self.assertIn("unsafe", rotation.stderr)
                self.assertEqual(
                    before,
                    {
                        name: (
                            (protected_root / name).read_bytes(),
                            (protected_root / name).stat().st_ino,
                        )
                        for name in values_a
                    },
                )

    def test_manifest_metadata_rejects_unexpected_owner_and_group(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            protected_root = Path(directory) / "target"
            protected_root.mkdir(mode=0o700)
            manifest = protected_root / OPERATOR_MANIFEST
            manifest.write_bytes(b"{}\n")
            manifest.chmod(0o400)
            descriptor = os.open(protected_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(
                    MATERIALIZER.SecretError, "unsafe metadata"
                ):
                    MATERIALIZER.validate_manifest_target_metadata(
                        descriptor, os.geteuid() + 1, os.getegid()
                    )
                with self.assertRaisesRegex(
                    MATERIALIZER.SecretError, "unsafe metadata"
                ):
                    MATERIALIZER.validate_manifest_target_metadata(
                        descriptor, os.geteuid(), os.getegid() + 1
                    )
            finally:
                os.close(descriptor)

    def test_unrecognized_pending_secret_copy_is_rejected(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            values = self.write_operator_bundle(source)
            install = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source)
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            pending = (
                protected_root
                / ".surplasse-stripe-secret-key.deadbeef.pending"
            )
            pending.write_bytes(values["surplasse-stripe-secret-key"])
            pending.chmod(0o440)

            source_b = root / "source-b"
            self.write_operator_bundle(source_b)
            rotated_value = b"blocked-rotation-password\n"
            rotated_path = source_b / "surplasse-smtp-password"
            rotated_path.write_bytes(rotated_value)
            rotated_path.chmod(0o600)
            before = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in protected_root.iterdir()
            }

            validation = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_b)
            )

            self.assertEqual(validation.returncode, 78)
            self.assertIn("unexpected entry", validation.stderr)
            self.assertEqual(
                before,
                {
                    path.name: (path.read_bytes(), path.stat().st_ino)
                    for path in protected_root.iterdir()
                },
            )
            self.assertNotEqual(
                (protected_root / "surplasse-smtp-password").read_bytes(),
                rotated_value,
            )

    def test_concurrent_operator_installers_publish_one_complete_bundle(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source_a = root / "source-a"
            source_b = root / "source-b"
            values_a = self.write_operator_bundle(source_a)
            values_b = self.write_operator_bundle(source_b)
            changes = {
                "surplasse-smtp-password": b"concurrent-smtp-password\n",
                "surplasse-stripe-account-webhook-secret": b"whsec_"
                + b"J" * 32
                + b"\n",
                "surplasse-stripe-secret-key": b"sk_live_" + b"K" * 32 + b"\n",
                "ovh-application-key": b"L" * 16 + b"\n",
                "ovh-application-secret": b"M" * 32 + b"\n",
                "ovh-consumer-key": b"N" * 32 + b"\n",
            }
            values_b.update(changes)
            for name, value in changes.items():
                path = source_b / name
                path.write_bytes(value)
                path.chmod(0o600)
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"

            def command(source: Path) -> list[str]:
                return [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--install-operator-from",
                    str(source),
                    "--test-root",
                    str(protected_root),
                ]

            processes = [
                subprocess.Popen(
                    command(source),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for source in (source_a, source_b)
            ]
            outputs = [process.communicate(timeout=20) for process in processes]
            for process, (_, stderr) in zip(processes, outputs, strict=True):
                self.assertEqual(process.returncode, 0, stderr)

            installed = {
                name: (protected_root / name).read_bytes() for name in values_a
            }
            self.assertIn(installed, (values_a, values_b))
            validation = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_operator_bundle_lock_timeout_is_bounded(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            protected_root = Path(directory) / "target"
            protected_root.mkdir(mode=0o700)
            directory_fd = os.open(protected_root, os.O_RDONLY | os.O_DIRECTORY)
            first_lock = MATERIALIZER.acquire_bundle_lock(
                directory_fd, os.geteuid(), os.getegid()
            )
            previous_timeout = MATERIALIZER.LOCK_TIMEOUT_SECONDS
            MATERIALIZER.LOCK_TIMEOUT_SECONDS = 0.05
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(
                    MATERIALIZER.SecretError, "operator bundle lock is busy"
                ):
                    MATERIALIZER.acquire_bundle_lock(
                        directory_fd, os.geteuid(), os.getegid()
                    )
                self.assertLess(time.monotonic() - started, 1)
            finally:
                MATERIALIZER.LOCK_TIMEOUT_SECONDS = previous_timeout
                os.close(first_lock)
                os.close(directory_fd)

    def test_postgres_provisioning_sql_separates_roles(self) -> None:
        statements: list[tuple[str, str]] = []
        original_psql = PROVISIONER.psql
        original_command = PROVISIONER.command
        try:
            PROVISIONER.psql = (
                lambda container, database, sql: statements.append((database, sql))
                or ""
            )
            PROVISIONER.command = (
                lambda arguments, input_text=None: subprocess.CompletedProcess(
                    arguments,
                    0,
                    "false|true|true|surplasse_owner|true|true|false\n",
                    "",
                )
            )
            PROVISIONER.provision("postgres-container", "A" * 64, "B" * 64)
        finally:
            PROVISIONER.psql = original_psql
            PROVISIONER.command = original_command

        self.assertEqual(
            [database for database, _ in statements], ["postgres", "surplasse"]
        )
        sql = "\n".join(statement for _, statement in statements)
        self.assertIn("CREATE ROLE surplasse_owner NOLOGIN", sql)
        self.assertIn("GRANT surplasse_owner TO surplasse_migrator", sql)
        self.assertIn("REVOKE CONNECT ON DATABASE surplasse FROM PUBLIC", sql)
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM PUBLIC", sql)
        self.assertIn("GRANT USAGE ON SCHEMA public TO surplasse_runtime", sql)
        self.assertNotIn("GRANT CREATE ON SCHEMA public TO surplasse_runtime", sql)

    def test_controller_preparation_has_no_public_or_application_mutation(self) -> None:
        role = (ROOT / "ansible/roles/surplasse/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        activation_guard = role.index(
            "Refuse application activation while the adapter remains locked"
        )
        preparation = role.index("Create only the missing database passwords")
        self.assertLess(activation_guard, preparation)
        self.assertIn("network\n              - connect", role)
        self.assertIn("network\n              - disconnect", role)
        self.assertIn("no_log: true", role)
        self.assertNotIn("docker compose down", role)
        self.assertNotIn("--volumes", role)
        self.assertNotIn("OVH_", role)
        self.assertNotIn("dig\n", role)

    def test_platform_attachment_candidates_are_minimal(self) -> None:
        integration = ROOT / "applications/surplasse/integration"
        internal = yaml.safe_load(
            (integration / "internal-platform.override.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(internal["services"]), {"postgresql", "prometheus"})
        self.assertEqual(
            set(internal["services"]["postgresql"]["networks"]),
            {"db_monitoring", "db_surplasse"},
        )
        self.assertEqual(
            set(internal["services"]["prometheus"]["networks"]),
            {"ops", "app_surplasse"},
        )
        edge = yaml.safe_load(
            (integration / "public-edge.override.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(edge["services"]), {"caddy"})
        self.assertEqual(
            edge["services"]["caddy"]["networks"]["app_surplasse"]["ipv4_address"],
            "172.30.10.254",
        )
        self.assertEqual(
            set(edge["services"]["caddy"]["networks"]),
            {"edge", "app_surplasse"},
        )

    def test_operator_input_inventory_is_exact(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/surplasse/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(defaults["vps_surplasse_operator_inputs"]),
            {
                "ovh-application-key",
                "ovh-application-secret",
                "ovh-consumer-key",
                "surplasse-jwt-jwks",
                "surplasse-jwt-key-id",
                "surplasse-jwt-private-key",
                "surplasse-smtp-host",
                "surplasse-smtp-password",
                "surplasse-smtp-port",
                "surplasse-smtp-username",
                "surplasse-stripe-account-webhook-secret",
                "surplasse-stripe-payment-webhook-secret",
                "surplasse-stripe-secret-key",
            },
        )


if __name__ == "__main__":
    unittest.main()
