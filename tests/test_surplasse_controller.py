#!/usr/bin/env python3
"""Tests for the fail-closed Surplasse Atlas preparation controller."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

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
ACTIVATOR = load_script(
    "surplasse_runtime_activator", SCRIPTS / "activate-surplasse-runtime"
)


class FakeActivationBackend:
    """Record the activation transaction without touching the host."""

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        rollback_fails: bool = False,
        preflight_hook=None,
    ) -> None:
        self.fail_at = fail_at
        self.rollback_fails = rollback_fails
        self.preflight_hook = preflight_hook
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)
        if self.fail_at == event:
            raise RuntimeError(f"boom:{event}")

    def preflight(self) -> None:
        self.record("preflight")
        if self.preflight_hook is not None:
            self.preflight_hook()

    @contextlib.contextmanager
    def activation_lock(self):
        self.record("activation-lock-enter")
        try:
            yield
        finally:
            self.events.append("activation-lock-exit")

    def recover_interrupted_activation(self) -> None:
        self.record("recover_interrupted_activation")

    @contextlib.contextmanager
    def bundle_lock(self):
        self.record("lock-enter")
        try:
            yield
        finally:
            self.events.append("lock-exit")

    def revalidate_bundle(self) -> None:
        self.record("revalidate_bundle")

    def prove_restore(self) -> None:
        self.record("prove_restore")

    def quiesce_supervision(self) -> None:
        self.record("quiesce_supervision")

    def write_runtime_environment(self) -> None:
        self.record("write_runtime_environment")

    def switch_runtime_link(self) -> None:
        self.record("switch_runtime_link")

    def attach_postgresql(self) -> None:
        self.record("attach_postgresql")

    def migrate(self) -> None:
        self.record("migrate")

    def start_runtime_services(self) -> None:
        self.record("start_runtime_services")

    def attach_observability_and_edge(self) -> None:
        self.record("attach_observability_and_edge")

    def arm_supervision(self) -> None:
        self.record("arm_supervision")

    def verify_activation(self) -> None:
        self.record("verify_activation")

    def adopt_supervision(self) -> None:
        self.record("adopt_supervision")

    def rollback(self) -> None:
        self.events.append("rollback")
        if self.rollback_fails:
            raise RuntimeError("boom:rollback")


class FakeSupervisorBackend:
    """Exercise monitor invariants without Docker or an infinite loop."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_after = fail_after

    def require_supervised_container(
        self,
        project: str,
        service: str,
        *,
        expected_id: str | None = None,
    ) -> str:
        self.calls.append((project, service))
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise ACTIVATOR.ActivationError("container health changed")
        return expected_id or f"{service}-container"

    def require_network(
        self,
        project: str,
        service: str,
        network: str,
        *,
        present: bool,
        address: str | None = None,
    ) -> None:
        self.calls.append((project, service))
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise ACTIVATOR.ActivationError("container network changed")

    def notify_systemd_watchdog(self) -> None:
        return None

    def require_supervision_authorization(self) -> None:
        return None

    def verify_current_bundle_if_available(self) -> None:
        return None


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

    def test_activation_order_arms_before_supervised_starts(self) -> None:
        backend = FakeActivationBackend()

        ACTIVATOR.activate(backend)

        self.assertEqual(
            backend.events,
            [
                "activation-lock-enter",
                "recover_interrupted_activation",
                "preflight",
                "lock-enter",
                "revalidate_bundle",
                "prove_restore",
                "quiesce_supervision",
                "write_runtime_environment",
                "switch_runtime_link",
                "attach_postgresql",
                "migrate",
                "start_runtime_services",
                "attach_observability_and_edge",
                "arm_supervision",
                "lock-exit",
                "adopt_supervision",
                "activation-lock-exit",
            ],
        )
        lock_enter = backend.events.index("lock-enter")
        lock_exit = backend.events.index("lock-exit")
        for operation in (
            "revalidate_bundle",
            "start_runtime_services",
            "attach_observability_and_edge",
            "arm_supervision",
        ):
            self.assertLess(lock_enter, backend.events.index(operation))
            self.assertLess(backend.events.index(operation), lock_exit)

    def test_current_locked_metadata_refuses_before_lock_or_mutation(self) -> None:
        backend = FakeActivationBackend(
            preflight_hook=lambda: ACTIVATOR.validate_activation_metadata(ROOT)
        )

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError, "reviewed Surplasse adapter is not ready"
        ):
            ACTIVATOR.activate(backend)

        self.assertEqual(
            backend.events,
            [
                "activation-lock-enter",
                "recover_interrupted_activation",
                "preflight",
                "activation-lock-exit",
            ],
        )

    def test_runtime_cli_is_implementation_locked_before_backend_access(self) -> None:
        for arguments in (("a" * 40,), ("--start-application",), ("--start-edge",)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(SCRIPTS / "activate-surplasse-runtime"), *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, ACTIVATOR.EX_CONFIG)
                self.assertIn(
                    "activation implementation remains locked before host access",
                    result.stderr,
                )
                for blocker in ACTIVATOR.ACTIVATION_IMPLEMENTATION_BLOCKERS:
                    self.assertIn(blocker, result.stderr)

    def test_release_and_adapter_encode_every_implementation_blocker(self) -> None:
        adapter = json.loads(
            (ROOT / "applications/surplasse/adapter.json").read_text(
                encoding="utf-8"
            )
        )
        release = json.loads(
            (ROOT / "releases/production.yaml").read_text(encoding="utf-8")
        )
        blockers = set(ACTIVATOR.ACTIVATION_IMPLEMENTATION_BLOCKERS)
        self.assertTrue(blockers.issubset(adapter["blocked_by"]))
        self.assertTrue(
            blockers.issubset(
                release["applications"]["surplasse"]["blocked_by"]
            )
        )

        role = (ROOT / "ansible/roles/surplasse/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        refusal = role.index(
            "Refuse the dormant activation implementation before Surplasse host operations"
        )
        first_host_read = role.index("Inspect the installed operator bundle helper")
        self.assertLess(refusal, first_host_read)
        self.assertIn("vps_surplasse_state != 'activate'", role[refusal:first_host_read])

    def test_each_post_mutation_failure_rolls_back_inside_the_bundle_lock(self) -> None:
        post_mutation_operations = (
            "quiesce_supervision",
            "write_runtime_environment",
            "switch_runtime_link",
            "attach_postgresql",
            "migrate",
            "start_runtime_services",
            "attach_observability_and_edge",
            "arm_supervision",
        )
        for operation in post_mutation_operations:
            with self.subTest(operation=operation):
                backend = FakeActivationBackend(fail_at=operation)

                with self.assertRaisesRegex(
                    ACTIVATOR.ActivationError,
                    rf"activation failed and reversible state was restored: boom:{operation}",
                ):
                    ACTIVATOR.activate(backend)

                self.assertIn("rollback", backend.events)
                self.assertLess(
                    backend.events.index(operation), backend.events.index("rollback")
                )
                self.assertLess(
                    backend.events.index("rollback"), backend.events.index("lock-exit")
                )

    def test_restore_proof_failure_does_not_attempt_rollback(self) -> None:
        backend = FakeActivationBackend(fail_at="prove_restore")

        with self.assertRaisesRegex(RuntimeError, "boom:prove_restore"):
            ACTIVATOR.activate(backend)

        self.assertNotIn("rollback", backend.events)
        self.assertEqual(backend.events[-2:], ["lock-exit", "activation-lock-exit"])

    def test_production_restore_proof_creates_then_rehearses_latest_backup(self) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(self, arguments, label, *, capture=False, check=True):
                self.commands.append(list(arguments))
                return subprocess.CompletedProcess(arguments, 0, "", "")

        runner = RecordingRunner()
        backend = object.__new__(ACTIVATOR.ProductionBackend)
        backend.runner = runner

        ACTIVATOR.ProductionBackend.prove_restore(backend)

        self.assertEqual(
            runner.commands,
            [
                [
                    "/usr/local/libexec/vps/postgres-backup",
                    "create",
                    "--retention-count",
                    "7",
                ],
                [
                    "/usr/local/libexec/vps/postgres-backup",
                    "rehearse",
                    "--latest",
                ],
            ],
        )

    def test_application_network_contract_accepts_only_reviewed_networks(self) -> None:
        def network(name: str, subnet: str, internal: bool) -> dict[str, object]:
            return {
                "Name": name,
                "Driver": "bridge",
                "Internal": internal,
                "IPAM": {
                    "Driver": "default",
                    "Config": [{"Subnet": subnet, "Gateway": subnet[:-4] + "1"}],
                },
                "Labels": {
                    "com.docker.compose.network": name,
                    "com.nclsppr.vps-infra.managed": "true",
                },
            }

        class NetworkRunner:
            def __init__(self, documents: dict[str, dict[str, object]]) -> None:
                self.documents = documents

            def run(self, arguments, label, *, capture=False, check=True):
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps([self.documents[arguments[-1]]]), ""
                )

        reviewed = {
            "app_surplasse": network(
                "app_surplasse", "172.30.10.0/24", False
            ),
            "db_surplasse": network("db_surplasse", "172.30.11.0/24", True),
        }
        backend = object.__new__(ACTIVATOR.ProductionBackend)
        backend.runner = NetworkRunner(reviewed)
        ACTIVATOR.ProductionBackend.require_application_network_contract(backend)

        for name, mutate in (
            ("managed label", lambda value: value["Labels"].pop(
                "com.nclsppr.vps-infra.managed"
            )),
            ("subnet", lambda value: value["IPAM"].update(
                {"Config": [{"Subnet": "172.31.10.0/24"}]}
            )),
            ("internal flag", lambda value: value.update({"Internal": True})),
        ):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(reviewed))
                mutate(changed["app_surplasse"])
                backend.runner = NetworkRunner(changed)
                with self.assertRaisesRegex(
                    ACTIVATOR.ActivationError, "reviewed network contract"
                ):
                    ACTIVATOR.ProductionBackend.require_application_network_contract(
                        backend
                    )

    def test_prometheus_scrape_proof_is_exact_and_total(self) -> None:
        proven = json.dumps(
            {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"job": "surplasse-backend"},
                            "value": [1720000000, "1"],
                        }
                    ]
                },
            }
        )
        self.assertTrue(ACTIVATOR.ProductionBackend.prometheus_scrape_proven(proven))
        for payload in (
            "not-json",
            "{}",
            '{"status":"success","data":{"result":[]}}',
            '{"status":"success","data":{"result":[{"metric":[],"value":[]}]}}',
            proven.replace('"1"', '"0"'),
        ):
            with self.subTest(payload=payload):
                self.assertFalse(
                    ACTIVATOR.ProductionBackend.prometheus_scrape_proven(payload)
                )

    def test_activation_reports_operation_and_rollback_failures(self) -> None:
        backend = FakeActivationBackend(
            fail_at="start_runtime_services", rollback_fails=True
        )

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError,
            "activation failed: boom:start_runtime_services; "
            "rollback also failed: boom:rollback",
        ) as raised:
            ACTIVATOR.activate(backend)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(str(raised.exception.__cause__), "boom:start_runtime_services")
        self.assertLess(
            backend.events.index("rollback"), backend.events.index("lock-exit")
        )

    def test_supervision_adoption_keeps_the_global_transaction_lock(self) -> None:
        backend = FakeActivationBackend()

        ACTIVATOR.activate(backend)

        self.assertLess(
            backend.events.index("lock-exit"),
            backend.events.index("adopt_supervision"),
        )
        self.assertLess(
            backend.events.index("adopt_supervision"),
            backend.events.index("activation-lock-exit"),
        )

    def test_supervision_adoption_failure_rolls_back_under_the_global_lock(self) -> None:
        backend = FakeActivationBackend(fail_at="adopt_supervision")

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError,
            "supervision adoption failed and reversible state was restored",
        ):
            ACTIVATOR.activate(backend)

        self.assertEqual(backend.events.count("lock-enter"), 1)
        self.assertEqual(backend.events.count("lock-exit"), 1)
        self.assertLess(
            backend.events.index("adopt_supervision"),
            backend.events.index("rollback"),
        )
        self.assertLess(
            backend.events.index("rollback"),
            backend.events.index("activation-lock-exit"),
        )

    def test_application_monitor_fails_on_a_container_health_change(self) -> None:
        backend = FakeSupervisorBackend(fail_after=3)
        original_sleep = ACTIVATOR.time.sleep
        try:
            ACTIVATOR.time.sleep = lambda _: None
            with self.assertRaisesRegex(
                ACTIVATOR.ActivationError, "container health changed"
            ):
                ACTIVATOR.ProductionBackend.monitor_application(
                    backend,
                    {
                        service: f"{service}-container"
                        for service in ACTIVATOR.RUNTIME_SERVICES
                    },
                )
        finally:
            ACTIVATOR.time.sleep = original_sleep

        self.assertEqual(
            backend.calls,
            [("surplasse", service) for service in ACTIVATOR.RUNTIME_SERVICES[:3]],
        )

    def test_edge_monitor_fails_on_a_container_network_change(self) -> None:
        backend = FakeSupervisorBackend(fail_after=1)

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError, "container network changed"
        ):
            ACTIVATOR.ProductionBackend.monitor_edge(backend, "caddy-container")

        self.assertEqual(backend.calls, [("vps-public-static-edge", "caddy")])

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
        for unsafe_kind in (
            "directory",
            "fifo",
            "symlink",
            "hardlink",
            "wrong-mode",
        ):
            with (
                self.subTest(unsafe_kind=unsafe_kind),
                tempfile.TemporaryDirectory() as directory,
            ):
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
            pending = protected_root / ".surplasse-stripe-secret-key.deadbeef.pending"
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
            "Refuse incomplete activation gates before any host mutation"
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

    def test_guarded_supervisors_force_recreate_runtime_and_caddy(self) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        production_backend = source[source.index("class ProductionBackend:") :]
        runtime_method = production_backend[
            production_backend.index(
                "    def guarded_start_application("
            ) : production_backend.index("    def guarded_start_edge(")
        ]
        edge_method = production_backend[
            production_backend.index(
                "    def guarded_start_edge("
            ) : production_backend.index("    def notify_systemd(")
        ]

        self.assertIn('"--force-recreate"', runtime_method)
        self.assertIn("*RUNTIME_SERVICES", runtime_method)
        self.assertIn('"--force-recreate"', edge_method)
        self.assertIn('"caddy"', edge_method)

        transaction_runtime = production_backend[
            production_backend.index(
                "    def start_runtime_services("
            ) : production_backend.index("    def remove_runtime_containers(")
        ]
        transaction_edge = production_backend[
            production_backend.index(
                "    def attach_observability_and_edge("
            ) : production_backend.index("    def arm_supervision(")
        ]
        self.assertNotIn('"up"', transaction_runtime)
        self.assertIn('"prometheus"', transaction_edge)
        self.assertNotIn('"caddy"', transaction_edge)

    def test_first_activation_renders_the_prepared_release_before_link_switch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "releases" / ("a" * 40)
            runtime_link = root / "runtime" / "surplasse"
            runtime_environment = root / "etc" / "surplasse.env"
            paths = ACTIVATOR.RuntimePaths(
                revision="a" * 40,
                release_root=release_root,
                runtime_link=runtime_link,
                secret_root_path=root / "secrets",
                systemd_root=root / "systemd",
                runtime_environment=runtime_environment,
                internal_platform=root / "internal-platform",
                public_edge=root / "public-edge",
                controller_state_root=root / "controller-state",
            )
            backend = ACTIVATOR.ProductionBackend(paths)

            self.assertFalse(runtime_link.exists())
            command = backend.app_compose(include_runtime_environment=True)

            project_directory = command[command.index("--project-directory") + 1]
            env_files = [
                command[index + 1]
                for index, member in enumerate(command)
                if member == "--env-file"
            ]
            self.assertEqual(project_directory, str(paths.application))
            self.assertEqual(
                env_files,
                [
                    str(paths.application / ".env.example"),
                    str(runtime_environment),
                ],
            )
            self.assertNotIn(str(runtime_link), command)

        source = (SCRIPTS / "activate-surplasse-runtime").read_text(
            encoding="utf-8"
        )
        write_environment = source[
            source.index("    def write_runtime_environment(") : source.index(
                "    def verify_runtime_environment("
            )
        ]
        self.assertIn(
            "self.render_and_validate(include_runtime_environment=True)",
            write_environment,
        )
        self.assertNotIn("self.render_and_validate(runtime=True)", write_environment)

    def test_systemd_starts_are_guarded_by_the_activation_controller(self) -> None:
        systemd = ROOT / "applications/surplasse/systemd"
        application_unit = (systemd / "vps-surplasse.service").read_text(
            encoding="utf-8"
        )
        edge_dropin = (systemd / "vps-public-static-edge-surplasse.conf").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "ExecStart=/srv/vps/runtime/surplasse/scripts/activate-surplasse-runtime "
            "--start-application",
            application_unit,
        )
        self.assertIn(
            "ExecStart=/srv/vps/runtime/surplasse/scripts/activate-surplasse-runtime "
            "--start-edge",
            edge_dropin,
        )
        self.assertNotIn("ExecStart=/usr/bin/docker compose", application_unit)
        self.assertNotIn("ExecStart=/usr/bin/docker compose", edge_dropin)
        for content in (application_unit, edge_dropin):
            with self.subTest(unit=content.splitlines()[0]):
                self.assertIn("Type=notify", content)
                self.assertIn("NotifyAccess=main", content)
                self.assertIn("KillMode=control-group", content)
                self.assertIn("Restart=on-failure", content)
                self.assertIn("RestartSec=10", content)
                self.assertIn("WatchdogSec=30", content)
                self.assertIn("StartLimitIntervalSec=300", content)
                self.assertIn("StartLimitBurst=3", content)
                self.assertIn("PartOf=docker.service", content)
                self.assertIn("BindsTo=docker.service", content)
                self.assertIn("WantedBy=", content)
                self.assertIn("docker.service", content.split("WantedBy=", 1)[1])
                self.assertIn("ExecStopPost=/usr/local/libexec/vps/", content)
                self.assertNotIn("RemainAfterExit=yes", content)

    def test_guarded_start_revalidates_before_recreation_and_supervision(self) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        for method, next_method in (
            ("guarded_start_application", "guarded_start_edge"),
            ("guarded_start_edge", "notify_systemd"),
        ):
            body = source[
                source.index(f"    def {method}(") : source.index(
                    f"    def {next_method}("
                )
            ]
            with self.subTest(method=method):
                self.assertLess(
                    body.index("self.revalidate_bundle()"),
                    body.index('"--force-recreate"'),
                )
                self.assertLess(
                    body.index("self.verify_runtime_environment()"),
                    body.index('"--force-recreate"'),
                )

        main = source[source.index("def main()") :]
        self.assertLess(
            main.index("backend.guarded_start_application()"),
            main.index("backend.notify_systemd_ready(subject)"),
        )
        self.assertLess(
            main.index("backend.notify_systemd_ready(subject)"),
            main.index("backend.monitor_application(containers)"),
        )

    def test_docker_restart_requeues_both_guarded_supervisors(self) -> None:
        systemd = ROOT / "applications/surplasse/systemd"
        application_unit = (systemd / "vps-surplasse.service").read_text(
            encoding="utf-8"
        )
        edge_dropin = (systemd / "vps-public-static-edge-surplasse.conf").read_text(
            encoding="utf-8"
        )
        for content in (application_unit, edge_dropin):
            with self.subTest(unit=content.splitlines()[0]):
                self.assertIn("BindsTo=docker.service", content)
                self.assertIn("PartOf=docker.service", content)
                install = content.split("[Install]", 1)[1]
                self.assertRegex(install, r"WantedBy=.*\bdocker\.service\b")

        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        runtime_start = source[
            source.index("    def start_runtime_services(") : source.index(
                "    def remove_runtime_containers("
            )
        ]
        edge_attachment = source[
            source.index("    def attach_observability_and_edge(") : source.index(
                "    def public_status("
            )
        ]
        self.assertIn('"enable", "vps-surplasse.service"', runtime_start)
        self.assertIn('"reenable",', edge_attachment)
        self.assertIn('"vps-public-static-edge.service"', edge_attachment)

    def test_guarded_stop_is_secret_independent_and_allowlisted(self) -> None:
        class StopRunner:
            def __init__(self, *services: str) -> None:
                self.services = iter(services)
                self.commands: list[list[str]] = []

            def run(
                self,
                arguments,
                label,
                *,
                capture=False,
                check=True,
            ):
                self.commands.append(list(arguments))
                if arguments[1] == "ps":
                    stdout = "".join(
                        f"{chr(ord('a') + index) * 64}\n"
                        for index, _ in enumerate(self.services_snapshot)
                    )
                elif arguments[1] == "inspect":
                    stdout = next(self.services) + "\n"
                else:
                    stdout = ""
                return subprocess.CompletedProcess(arguments, 0, stdout, "")

            @property
            def services_snapshot(self):
                first, self.services = itertools.tee(self.services)
                return list(first)

        runner = StopRunner("backend")
        ACTIVATOR.stop_bounded_project(
            runner, "surplasse", frozenset(ACTIVATOR.RUNTIME_SERVICES), 120
        )
        self.assertEqual(runner.commands[-1][1:4], ["stop", "--time", "120"])

        unexpected = StopRunner("backend", "migrator")
        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError, "unexpected services: migrator"
        ):
            ACTIVATOR.stop_bounded_project(
                unexpected,
                "surplasse",
                frozenset(ACTIVATOR.RUNTIME_SERVICES),
                120,
            )
        stop = next(command for command in unexpected.commands if command[1] == "stop")
        self.assertEqual(stop[-1], "a" * 64)

        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        stop_body = source[
            source.index("def stop_bounded_project(") : source.index("def main()")
        ]
        self.assertNotIn("OPERATOR_MANIFEST", stop_body)
        self.assertNotIn("runtime_environment", stop_body)
        self.assertNotIn("secret_root", stop_body)

    def test_rollback_removes_allowlisted_containers_before_reporting_unknowns(
        self,
    ) -> None:
        class RollbackRunner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []
                self.services = iter(("backend", "mystery"))

            def run(self, arguments, label, *, capture=False, check=True):
                self.commands.append(list(arguments))
                if arguments[1] == "ps":
                    stdout = f"{'a' * 64}\n{'b' * 64}\n"
                elif arguments[1] == "inspect":
                    stdout = next(self.services) + "\n"
                else:
                    stdout = ""
                return subprocess.CompletedProcess(arguments, 0, stdout, "")

        runner = RollbackRunner()
        backend = object.__new__(ACTIVATOR.ProductionBackend)
        backend.runner = runner
        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError,
            "unexpected Surplasse project services: mystery",
        ):
            ACTIVATOR.ProductionBackend.remove_runtime_containers(backend)

        removal = next(command for command in runner.commands if command[1] == "rm")
        self.assertEqual(removal, ["/usr/bin/docker", "rm", "--force", "a" * 64])

    def test_runtime_environment_binds_the_complete_secret_generation(self) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        generation = source[
            source.index("    def bundle_generation(") : source.index(
                "    def expected_runtime_environment("
            )
        ]
        environment_start = source.index("    def expected_runtime_environment(self)")
        environment = source[
            environment_start : source.index(
                "    def write_runtime_environment(self)", environment_start
            )
        ]

        self.assertIn("manifest_path.read_bytes()", generation)
        self.assertIn("for name in GENERATED_FILES", generation)
        self.assertIn(
            '"SURPLASSE_BUNDLE_GENERATION": self.bundle_generation()', environment
        )

    def test_bundle_revalidation_enforces_exact_secret_metadata(self) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        production = source[source.index("class ProductionBackend:") :]
        revalidation = production[
            production.index("    def revalidate_bundle(") : production.index(
                "    def prove_restore("
            )
        ]
        self.assertIn("expected_group = 10001 if application_readable else 0", revalidation)
        self.assertIn("expected_mode = 0o440 if application_readable else 0o400", revalidation)
        self.assertIn("metadata.st_nlink != 1", revalidation)

    def test_starting_authorization_requires_matching_live_transaction_lease(
        self,
    ) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        authorization = source[
            source.index("    def require_supervision_authorization(") : source.index(
                "    def verify_current_bundle_if_available("
            )
        ]
        self.assertIn("ACTIVATION_AUTHORIZATION_ENV", authorization)
        self.assertIn("transaction_token_sha256", authorization)
        self.assertIn("ACTIVATION_LEASE_ROOT / authorization", authorization)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", authorization)
        self.assertIn("activation transaction lease expired", authorization)

    def test_starting_authorization_accepts_the_matching_atomic_commit_race(
        self,
    ) -> None:
        authorization_value = "1" * 64
        digest = hashlib.sha256(authorization_value.encode("ascii")).hexdigest()
        starting = {
            "bundle_generation": "2" * 64,
            "contract": ACTIVATOR.ACTIVATION_STATE_CONTRACT,
            "phase": "starting",
            "revision": "a" * 40,
            "transaction_token_sha256": digest,
            "version": 1,
        }
        active = {**starting, "phase": "active"}

        for lease_disappeared in (True, False):
            with self.subTest(lease_disappeared=lease_disappeared):
                backend = object.__new__(ACTIVATOR.ProductionBackend)
                backend.paths = types.SimpleNamespace(revision="a" * 40)
                states = iter((starting, active))
                backend.read_activation_state = (
                    lambda *, required=True: next(states)
                )
                if lease_disappeared:
                    backend.require_safe_path = lambda path: (_ for _ in ()).throw(
                        ACTIVATOR.ActivationError("lease disappeared")
                    )
                    patches = contextlib.ExitStack()
                else:
                    backend.require_safe_path = lambda path: None
                    patches = contextlib.ExitStack()
                    patches.enter_context(mock.patch.object(ACTIVATOR.os, "open", return_value=7))
                    patches.enter_context(
                        mock.patch.object(
                            ACTIVATOR.os,
                            "fstat",
                            return_value=types.SimpleNamespace(
                                st_mode=stat.S_IFREG | 0o600,
                                st_nlink=1,
                                st_uid=0,
                                st_gid=0,
                            ),
                        )
                    )
                    patches.enter_context(mock.patch.object(ACTIVATOR.os, "close"))
                    patches.enter_context(mock.patch.object(ACTIVATOR.fcntl, "flock"))
                with patches, mock.patch.dict(
                    ACTIVATOR.os.environ,
                    {ACTIVATOR.ACTIVATION_AUTHORIZATION_ENV: authorization_value},
                    clear=False,
                ):
                    ACTIVATOR.ProductionBackend.require_supervision_authorization(
                        backend
                    )

    def test_starting_authorization_rejects_a_different_committed_generation(
        self,
    ) -> None:
        starting = {
            "bundle_generation": "2" * 64,
            "contract": ACTIVATOR.ACTIVATION_STATE_CONTRACT,
            "phase": "starting",
            "revision": "a" * 40,
            "transaction_token_sha256": "3" * 64,
            "version": 1,
        }
        backend = object.__new__(ACTIVATOR.ProductionBackend)
        backend.read_activation_state = lambda *, required=True: {
            **starting,
            "phase": "active",
            "bundle_generation": "4" * 64,
        }
        self.assertFalse(
            ACTIVATOR.ProductionBackend.starting_state_was_committed(
                backend, starting
            )
        )

    def test_interrupted_transaction_cleanup_includes_publish_temporaries(self) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")
        production = source[source.index("class ProductionBackend:") :]
        cleanup = production[
            production.index("    def remove_transaction_temporaries(") : production.index(
                "    def preflight("
            )
        ]
        self.assertIn('f".surplasse-{self.paths.revision}"', cleanup)
        self.assertIn('f".{destination.name}.*.pending"', cleanup)

    def test_affected_services_cannot_restart_outside_the_bundle_guard(self) -> None:
        application = yaml.safe_load(
            (ROOT / "applications/surplasse/compose.yaml").read_text(encoding="utf-8")
        )
        for service in ACTIVATOR.RUNTIME_SERVICES:
            with self.subTest(service=service):
                self.assertEqual(application["services"][service]["restart"], "no")

        edge = yaml.safe_load(
            (
                ROOT / "applications/surplasse/integration/public-edge.override.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(edge["services"]["caddy"]["restart"], "no")

    def test_activation_uses_direct_probes_and_has_no_traffic_cutover_client(
        self,
    ) -> None:
        source = (SCRIPTS / "activate-surplasse-runtime").read_text(encoding="utf-8")

        self.assertIn('"--resolve"', source)
        self.assertIn('"--connect-timeout"', source)
        self.assertIn('"--max-time"', source)
        self.assertIn("deadline = time.monotonic() + 180", source)
        self.assertIn('f"{host}:443:127.0.0.1"', source)
        for forbidden in (
            '"/usr/bin/dig"',
            '"dig"',
            "api.ovh.com",
            "/domain/zone",
            "ovhcloud",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

        deployment = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
        self.assertIn("_acme-challenge", deployment)
        self.assertIn("A, AAAA, or CNAME", deployment)


if __name__ == "__main__":
    unittest.main()
