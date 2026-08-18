#!/usr/bin/env python3
"""Tests for the fail-closed Surplasse Atlas preparation controller."""

from __future__ import annotations

import base64
import fcntl
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
RUNTIME_CONFIG_NAME = "surplasse.env"
APPLICATION_CREDENTIAL_NAMES = {
    "surplasse-jwt-jwks",
    "surplasse-jwt-private-key",
    "surplasse-postgres-migrator-password",
    "surplasse-postgres-runtime-password",
    "surplasse-smtp-password",
    "surplasse-smtp-username",
    "surplasse-stripe-account-webhook-secret",
    "surplasse-stripe-payment-webhook-secret",
    "surplasse-stripe-secret-key",
}


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


def stripe_restricted_test_key(label: str) -> bytes:
    payload = hashlib.sha256(label.encode("ascii")).hexdigest().encode("ascii")
    return b"rk_test_" + payload + b"\n"


def stripe_webhook_secret(label: str) -> bytes:
    payload = hashlib.sha256(label.encode("ascii")).hexdigest().encode("ascii")
    return b"whsec_" + payload + b"\n"


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
            "surplasse-smtp-password": b"smtp-password-for-test-only\n",
            "surplasse-smtp-username": b"surplasse-test\n",
            "surplasse-stripe-account-webhook-secret": stripe_webhook_secret(
                "account-webhook"
            ),
            "surplasse-stripe-payment-webhook-secret": stripe_webhook_secret(
                "payment-webhook"
            ),
            "surplasse-stripe-secret-key": stripe_restricted_test_key(
                "default-operator-bundle"
            ),
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
            timeout=20,
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
                        "surplasse-jwt-key-id",
                        "surplasse-smtp-host",
                    }
                    else 0o440
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
                self.assertEqual(path.stat().st_nlink, 1)
            runtime_path = root / "applications" / RUNTIME_CONFIG_NAME
            self.assertEqual(
                runtime_path.read_bytes(),
                b"SURPLASSE_AUTH_JWT_KEY_ID=atlas-2026-08\n"
                b"SURPLASSE_SMTP_HOST=smtp.example.invalid\n",
            )
            self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)
            self.assertEqual(runtime_path.stat().st_nlink, 1)
            manifest_path = protected_root / OPERATOR_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            self.assertEqual(manifest["contract"], "surplasse-operator-bundle")
            self.assertEqual(manifest["payment_mode"], "test")
            self.assertEqual(manifest["version"], 3)
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
            runtime_inode = runtime_path.stat().st_ino

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
            self.assertEqual(runtime_inode, runtime_path.stat().st_ino)
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

            database = self.run_secret_helper(protected_root, "--database-only")
            self.assertEqual(database.returncode, 0, database.stderr)
            application_secrets = {
                path.name
                for path in protected_root.iterdir()
                if stat.S_IMODE(path.stat().st_mode) == 0o440
            }
            self.assertEqual(application_secrets, APPLICATION_CREDENTIAL_NAMES)
            for name in application_secrets:
                metadata = (protected_root / name).stat()
                self.assertEqual(metadata.st_gid, os.getegid())
                self.assertEqual(metadata.st_nlink, 1)

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
            sentinel = protected_root / "surplasse-smtp-host"
            sentinel.write_bytes(b"existing.example.invalid\n")
            sentinel.chmod(0o400)
            before = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in protected_root.iterdir()
            }
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            for invalid_value, expected_message in (
                (b"rk_live_" + b"X7" * 16 + b"\n", "stripe-secret-key format"),
                (b"sk_test_" + b"Y8" * 16 + b"\n", "stripe-secret-key format"),
                (b"rk_test_replace_me_with_real_key\n", "placeholder-like"),
                (b"rk_test_" + b"Z" * 32 + b"\n", "placeholder-like"),
            ):
                with self.subTest(prefix=invalid_value[:8]):
                    invalid.write_bytes(invalid_value)
                    invalid.chmod(0o600)
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
                    self.assertIn(expected_message, result.stderr)
                    self.assertEqual(
                        before,
                        {
                            path.name: (path.read_bytes(), path.stat().st_ino)
                            for path in protected_root.iterdir()
                            if path.name != OPERATOR_LOCK
                        },
                    )

    def test_legacy_dns_and_port_files_fail_closed_without_migration(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source_a = root / "source-a"
            source_b = root / "source-b"
            self.write_operator_bundle(source_a)
            self.write_operator_bundle(source_b)
            rotated_smtp_input = source_b / "surplasse-smtp-password"
            rotated_smtp_input.write_bytes(b"rotation-must-not-be-applied\n")
            rotated_smtp_input.chmod(0o600)
            install = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_a)
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            legacy = {
                "ovh-application-key": b"D" * 16 + b"\n",
                "ovh-application-secret": b"E" * 32 + b"\n",
                "ovh-consumer-key": b"F" * 32 + b"\n",
                "surplasse-smtp-port": b"587\n",
            }
            for name, value in legacy.items():
                path = protected_root / name
                path.write_bytes(value)
                path.chmod(0o400)
            before = {
                path.name: (path.read_bytes(), path.stat().st_ino)
                for path in protected_root.iterdir()
            }

            rotation = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_b)
            )

            self.assertEqual(rotation.returncode, 78)
            self.assertIn("unexpected entry", rotation.stderr)
            self.assertEqual(
                before,
                {
                    path.name: (path.read_bytes(), path.stat().st_ino)
                    for path in protected_root.iterdir()
                },
            )
            for name, value in legacy.items():
                self.assertEqual((protected_root / name).read_bytes(), value)

    def test_operator_bundle_rejects_equal_or_placeholder_webhook_secrets(
        self,
    ) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        cases = (
            ("equal", stripe_webhook_secret("payment-webhook"), "must be distinct"),
            ("placeholder", b"whsec_" + b"A" * 32 + b"\n", "placeholder-like"),
            ("documented-marker", b"whsec_replace_with_real_secret\n", "placeholder-like"),
        )
        for label, invalid_value, expected_message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                protected_root = root / "target"
                protected_root.mkdir(mode=0o700)
                source = root / "source"
                self.write_operator_bundle(source)
                webhook = source / "surplasse-stripe-account-webhook-secret"
                webhook.write_bytes(invalid_value)
                webhook.chmod(0o600)

                result = self.run_secret_helper(
                    protected_root, "--install-operator-from", str(source)
                )

                self.assertEqual(result.returncode, 78)
                self.assertIn(expected_message, result.stderr)
                self.assertEqual(
                    {path.name for path in protected_root.iterdir()},
                    {OPERATOR_LOCK},
                )

    def test_uppercase_smtp_host_is_rejected_before_runtime_publication(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            self.write_operator_bundle(source)
            smtp_host = source / "surplasse-smtp-host"
            smtp_host.write_bytes(b"SMTP.example.invalid\n")
            smtp_host.chmod(0o600)

            result = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source)
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("lowercase DNS name", result.stderr)
            self.assertFalse((root / "applications" / RUNTIME_CONFIG_NAME).exists())
            self.assertEqual(
                {path.name for path in protected_root.iterdir()},
                {OPERATOR_LOCK},
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
                "surplasse-stripe-secret-key": stripe_restricted_test_key(
                    "rotated-operator-bundle"
                ),
                "surplasse-smtp-host": b"relay.example.invalid\n",
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
            runtime_path = root / "applications" / RUNTIME_CONFIG_NAME
            runtime_pending = (
                root
                / "applications"
                / ".surplasse.env.000000000000000000000003.pending"
            )
            runtime_pending.write_bytes(
                b"SURPLASSE_AUTH_JWT_KEY_ID=atlas-2026-08\n"
                b"SURPLASSE_SMTP_HOST=relay.example.invalid\n"
            )
            runtime_pending.chmod(0o600)
            os.replace(runtime_pending, runtime_path)
            interrupted = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(interrupted.returncode, 78)
            self.assertIn("manifest does not match", interrupted.stderr)

            orphan = (
                protected_root
                / ".surplasse-stripe-secret-key.ffffffffffffffffffffffff.pending"
            )
            orphan.write_bytes(stripe_restricted_test_key("orphaned-copy"))
            orphan.chmod(0o440)
            runtime_orphan = (
                root
                / "applications"
                / ".surplasse.env.ffffffffffffffffffffffff.pending"
            )
            runtime_orphan.write_bytes(runtime_path.read_bytes())
            runtime_orphan.chmod(0o600)

            recovery = self.run_secret_helper(
                protected_root, "--install-operator-from", str(source_b)
            )
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            self.assertFalse(orphan.exists())
            self.assertFalse(runtime_orphan.exists())
            validation = self.run_secret_helper(protected_root, "--operator-only")
            self.assertEqual(validation.returncode, 0, validation.stderr)
            self.assertEqual(
                values_b,
                {name: (protected_root / name).read_bytes() for name in values_b},
            )
            self.assertEqual(
                runtime_path.read_bytes(),
                b"SURPLASSE_AUTH_JWT_KEY_ID=atlas-2026-08\n"
                b"SURPLASSE_SMTP_HOST=relay.example.invalid\n",
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
        for unsafe_kind in (
            "directory",
            "fifo",
            "symlink",
            "hardlink",
            "wrong-mode",
        ):
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
                elif unsafe_kind == "fifo":
                    manifest.unlink()
                    os.mkfifo(manifest, mode=0o400)
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

    def test_database_preparation_rejects_an_unsafe_existing_manifest(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            manifest = protected_root / OPERATOR_MANIFEST
            manifest.symlink_to(root / "outside")

            result = self.run_secret_helper(protected_root, "--database-only")

            self.assertEqual(result.returncode, 78)
            self.assertIn("manifest is unsafe", result.stderr)
            self.assertFalse(
                (protected_root / "surplasse-postgres-migrator-password").exists()
            )
            self.assertFalse(
                (protected_root / "surplasse-postgres-runtime-password").exists()
            )

    def test_unsafe_runtime_target_blocks_rotation_before_secret_changes(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        for unsafe_kind in (
            "directory",
            "fifo",
            "symlink",
            "hardlink",
            "wrong-mode",
        ):
            with self.subTest(unsafe_kind=unsafe_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                protected_root = root / "target"
                protected_root.mkdir(mode=0o700)
                source_a = root / "source-a"
                source_b = root / "source-b"
                values_a = self.write_operator_bundle(source_a)
                self.write_operator_bundle(source_b)
                rotated_smtp_input = source_b / "surplasse-smtp-password"
                rotated_smtp_input.write_bytes(b"blocked-runtime-rotation\n")
                rotated_smtp_input.chmod(0o600)
                install = self.run_secret_helper(
                    protected_root, "--install-operator-from", str(source_a)
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                runtime_path = root / "applications" / RUNTIME_CONFIG_NAME
                if unsafe_kind == "directory":
                    runtime_path.unlink()
                    runtime_path.mkdir(mode=0o700)
                elif unsafe_kind == "fifo":
                    runtime_path.unlink()
                    os.mkfifo(runtime_path, mode=0o600)
                elif unsafe_kind == "symlink":
                    runtime_path.unlink()
                    runtime_path.symlink_to(protected_root / "surplasse-smtp-host")
                elif unsafe_kind == "hardlink":
                    os.link(runtime_path, root / "external-runtime-link")
                else:
                    runtime_path.chmod(0o640)
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
                self.assertIn("runtime configuration", rotation.stderr)
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
                "surplasse-stripe-account-webhook-secret": stripe_webhook_secret(
                    "concurrent-account-webhook"
                ),
                "surplasse-stripe-secret-key": stripe_restricted_test_key(
                    "concurrent-operator-bundle"
                ),
                "surplasse-smtp-host": b"smtp-b.example.invalid\n",
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
            self.assertEqual(
                (root / "applications" / RUNTIME_CONFIG_NAME).read_bytes(),
                b"SURPLASSE_AUTH_JWT_KEY_ID="
                + installed["surplasse-jwt-key-id"][:-1]
                + b"\nSURPLASSE_SMTP_HOST="
                + installed["surplasse-smtp-host"][:-1]
                + b"\n",
            )
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

    def test_mutation_takes_deployment_lock_before_bundle_lock(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally forbids root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected_root = root / "target"
            protected_root.mkdir(mode=0o700)
            source = root / "source"
            self.write_operator_bundle(source)
            deployment_lock = root / "deployment.lock"
            descriptor = os.open(
                deployment_lock,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            environment = os.environ.copy()
            environment["VPS_SURPLASSE_SECRET_TESTING"] = "1"
            process = subprocess.Popen(
                [
                    str(SCRIPTS / "materialize-surplasse-secrets"),
                    "--install-operator-from",
                    str(source),
                    "--test-root",
                    str(protected_root),
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
                self.assertFalse((protected_root / OPERATOR_LOCK).exists())
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)

            descriptor = os.open(deployment_lock, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                validation = self.run_secret_helper(
                    protected_root,
                    "--operator-only",
                    "--test-deployment-lock",
                    str(deployment_lock),
                )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(validation.returncode, 0, validation.stderr)

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
        self.assertEqual(set(internal["services"]), {"prometheus"})
        self.assertEqual(
            set(internal["services"]["prometheus"]["networks"]),
            {"ops", "app_surplasse"},
        )
        self.assertNotIn("db_surplasse", internal["networks"])
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
            set(defaults["vps_surplasse_application_operator_inputs"]),
            {
                "surplasse-jwt-jwks",
                "surplasse-jwt-key-id",
                "surplasse-jwt-private-key",
                "surplasse-smtp-host",
                "surplasse-smtp-password",
                "surplasse-smtp-username",
                "surplasse-stripe-account-webhook-secret",
                "surplasse-stripe-payment-webhook-secret",
                "surplasse-stripe-secret-key",
            },
        )
        self.assertEqual(
            set(defaults["vps_surplasse_application_secrets"]),
            APPLICATION_CREDENTIAL_NAMES,
        )
        self.assertEqual(
            set(defaults["vps_surplasse_dns_operator_inputs"]),
            {
                "ovh-application-key",
                "ovh-application-secret",
                "ovh-consumer-key",
            },
        )
        self.assertEqual(
            defaults["vps_surplasse_dns_secrets"],
            defaults["vps_surplasse_dns_operator_inputs"],
        )
        self.assertEqual(
            defaults["vps_surplasse_runtime_config_file"],
            "/etc/vps/applications/surplasse.env",
        )


if __name__ == "__main__":
    unittest.main()
