#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
USERNAME = "01234567-89ab-cdef-0123-456789abcdef"
SMTP_VALUE = "smtp-password-value-0123456789"
OIDC_VALUE = "oidc-client-secret-value-0123456789abcdef"
INFRA_REVISION = "a" * 40


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GATE = load_script_module(
    "parkventory_provider_ingress_gate",
    SCRIPTS / "materialize-parkventory-providers-live-gate",
)
WORKER = load_script_module(
    "parkventory_provider_ingress_worker",
    SCRIPTS / "materialize-parkventory-providers-worker",
)
AUTH0 = load_script_module(
    "parkventory_auth0_smtp",
    SCRIPTS / "configure-parkventory-auth0-smtp",
)
PROVIDER = load_script_module(
    "parkventory_provider_materializer_ingress_test",
    SCRIPTS / "materialize-parkventory-provider-secrets",
)
SMTP = load_script_module(
    "smtp_materializer_ingress_test",
    SCRIPTS / "materialize-smtp-secrets",
)


def canonical_payload(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "INFRA_REVISION": INFRA_REVISION,
        "PARKVENTORY_OIDC_CLIENT_SECRET": OIDC_VALUE,
        "PARKVENTORY_SMTP_PASSWORD": SMTP_VALUE,
        "PARKVENTORY_SMTP_USERNAME": USERNAME,
    }
    document.update(overrides)
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")


class GateTests(unittest.TestCase):
    def test_gate_accepts_only_the_exact_canonical_secret_payload(self) -> None:
        payload = canonical_payload()
        self.assertEqual(
            GATE.parse_request(payload),
            {
                "INFRA_REVISION": INFRA_REVISION,
                "PARKVENTORY_OIDC_CLIENT_SECRET": OIDC_VALUE,
                "PARKVENTORY_SMTP_PASSWORD": SMTP_VALUE,
                "PARKVENTORY_SMTP_USERNAME": USERNAME,
            },
        )
        invalid = (
            b"",
            payload.rstrip(b"\n"),
            payload + b"\n",
            payload.replace(b'\":\"', b'\": \"', 1),
            canonical_payload(UNEXPECTED="value"),
            canonical_payload(PARKVENTORY_SMTP_USERNAME="not-a-uuid"),
            canonical_payload(INFRA_REVISION="not-a-revision"),
            canonical_payload(PARKVENTORY_SMTP_PASSWORD="short"),
            canonical_payload(PARKVENTORY_OIDC_CLIENT_SECRET="short"),
            b"x" * (GATE.MAX_INPUT_BYTES + 1),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate[:80]):
                with self.assertRaises(GATE.GateError):
                    GATE.parse_request(candidate)

    def test_gate_builds_a_root_worker_with_no_secret_transport_metadata(self) -> None:
        with mock.patch.object(
            GATE.secrets,
            "token_hex",
            return_value="0123456789abcdef01234567",
        ):
            command = GATE.build_worker_command()
        self.assertIn("--pipe", command)
        self.assertIn("RuntimeDirectoryMode=0700", command)
        self.assertIn("MemoryMax=128M", command)
        self.assertIn("MemorySwapMax=0", command)
        self.assertIn("TasksMax=32", command)
        self.assertIn("LimitFSIZE=1M", command)
        self.assertIn("PrivateNetwork=yes", command)
        self.assertIn("ProtectSystem=strict", command)
        self.assertEqual(command[-2:], ["--", GATE.WORKER_PATH])
        rendered = "\n".join(command)
        for value in (USERNAME, SMTP_VALUE, OIDC_VALUE):
            self.assertNotIn(value, rendered)
        self.assertNotIn("StandardOutput=journal", rendered)
        self.assertNotIn("StandardError=journal", rendered)

    def test_gate_forwards_the_payload_only_through_stdin(self) -> None:
        payload = canonical_payload()
        command = ["systemd-run", "--", "worker"]
        completed = subprocess.CompletedProcess(command, 0)
        safe_environment: dict[str, str] = {}
        with (
            mock.patch.object(GATE.sys, "argv", ["gate"]),
            mock.patch.object(GATE.os, "geteuid", return_value=0),
            mock.patch.object(GATE.os, "environ", safe_environment),
            mock.patch.object(GATE, "read_request", return_value=payload),
            mock.patch.object(GATE, "build_worker_command", return_value=command),
            mock.patch.object(GATE.subprocess, "run", return_value=completed) as run,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            GATE.main()
        call = run.call_args
        self.assertEqual(call.args[0], command)
        self.assertEqual(call.kwargs["input"], payload)
        self.assertEqual(call.kwargs["env"], GATE.SAFE_ENVIRONMENT)
        self.assertIs(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(call.kwargs["stderr"], subprocess.DEVNULL)
        rendered = repr(call.args[0]) + repr(call.kwargs["env"])
        for value in (USERNAME, SMTP_VALUE, OIDC_VALUE):
            self.assertNotIn(value, rendered)
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())


class WorkerTests(unittest.TestCase):
    def test_worker_revalidates_payload_and_stages_root_only_sources(self) -> None:
        document = WORKER.parse_payload(canonical_payload())
        provider_env = (ROOT / "applications/parkventory/provider.env").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            with mock.patch.object(WORKER, "require_root_directory"):
                smtp_source, provider_source = WORKER.stage_sources(
                    root, document, provider_env
                )
            expected = {
                smtp_source / "parkventory-smtp-username": USERNAME + "\n",
                smtp_source / "parkventory-smtp-password": SMTP_VALUE + "\n",
                provider_source / "parkventory-oidc-client-secret": OIDC_VALUE + "\n",
                provider_source / "parkventory.env": provider_env.decode("ascii"),
            }
            for path, content in expected.items():
                self.assertEqual(path.read_text(encoding="ascii"), content)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(smtp_source.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(provider_source.stat().st_mode), 0o700)

    def test_worker_binds_the_payload_to_both_markers_head_and_contract(self) -> None:
        provider_env = (ROOT / "applications/parkventory/provider.env").read_bytes()
        with (
            mock.patch.object(
                WORKER,
                "read_controller_revision",
                side_effect=[INFRA_REVISION, INFRA_REVISION],
            ) as read_revision,
            mock.patch.object(
                WORKER,
                "read_repository_contract",
                return_value=(INFRA_REVISION, provider_env),
            ),
        ):
            WORKER.verify_converged_contract(INFRA_REVISION, provider_env)
        self.assertEqual(
            read_revision.call_args_list,
            [mock.call(), mock.call(WORKER.CONVERGED_REVISION_PATH)],
        )

        mismatches = (
            (["b" * 40, INFRA_REVISION], (INFRA_REVISION, provider_env)),
            ([INFRA_REVISION, "b" * 40], (INFRA_REVISION, provider_env)),
            ([INFRA_REVISION, INFRA_REVISION], ("b" * 40, provider_env)),
            ([INFRA_REVISION, INFRA_REVISION], (INFRA_REVISION, b"drift\n")),
        )
        for markers, repository in mismatches:
            with (
                self.subTest(markers=markers, repository=repository[0]),
                mock.patch.object(
                    WORKER, "read_controller_revision", side_effect=markers
                ),
                mock.patch.object(
                    WORKER, "read_repository_contract", return_value=repository
                ),
                self.assertRaises(WORKER.WorkerError),
            ):
                WORKER.verify_converged_contract(INFRA_REVISION, provider_env)

    def test_worker_holds_one_lock_across_the_exact_helper_order(self) -> None:
        smtp_source = Path("/run/private/smtp")
        provider_source = Path("/run/private/provider")
        lock_descriptor = 9
        with (
            mock.patch.object(WORKER, "refuse_transactions") as refuse,
            mock.patch.object(WORKER, "run_helper") as run,
        ):
            WORKER.execute_materialization(
                smtp_source, provider_source, lock_descriptor
            )
        self.assertEqual(refuse.call_count, 6)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                [
                    str(WORKER.PROVIDER_MATERIALIZER),
                    "--validate-source",
                    str(provider_source),
                ],
                [
                    str(WORKER.SMTP_MATERIALIZER),
                    "--product",
                    "parkventory",
                    "--registry-generation",
                    "1",
                    "--install-from",
                    str(smtp_source),
                    "--deployment-lock-held-fd",
                    str(lock_descriptor),
                ],
                [
                    str(WORKER.PROVIDER_MATERIALIZER),
                    "--install-from",
                    str(provider_source),
                    "--deployment-lock-held-fd",
                    str(lock_descriptor),
                ],
                [
                    str(WORKER.SMTP_MATERIALIZER),
                    "--product",
                    "parkventory",
                    "--registry-generation",
                    "1",
                    "--check",
                ],
                [str(WORKER.PROVIDER_MATERIALIZER), "--check"],
            ],
        )
        self.assertTrue(
            all(call.args[2] == lock_descriptor for call in run.call_args_list)
        )

    def test_shared_lock_contention_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "vps-static.lock"
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)
            first = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(WORKER.WorkerError, "busy"):
                    WORKER.acquire_deployment_lock(
                        lock_path,
                        timeout_seconds=0,
                        owner=os.geteuid(),
                        group=os.getegid(),
                    )
            finally:
                os.close(first)

    def test_worker_refuses_active_transaction_and_handoff_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active.json"
            transaction = root / "transactions"
            handoff = root / "handoffs"
            static_transaction = root / "static-transactions"
            base_transaction = root / "base-transaction.json"
            for path in (transaction, handoff, static_transaction):
                path.mkdir()
            with (
                mock.patch.object(WORKER, "PARKVENTORY_ACTIVE_PATHS", (active,)),
                mock.patch.object(
                    WORKER,
                    "TRANSACTION_DIRECTORIES",
                    (transaction, handoff, static_transaction),
                ),
                mock.patch.object(
                    WORKER, "TRANSACTION_FILES", (base_transaction,)
                ),
            ):
                WORKER.refuse_transactions()
                active.touch()
                with self.assertRaisesRegex(WORKER.WorkerError, "active"):
                    WORKER.refuse_transactions()
                active.unlink()
                (handoff / "parkventory.json").touch()
                with self.assertRaisesRegex(WORKER.WorkerError, "transaction"):
                    WORKER.refuse_transactions()
                (handoff / "parkventory.json").unlink()
                base_transaction.touch()
                with self.assertRaisesRegex(WORKER.WorkerError, "transaction"):
                    WORKER.refuse_transactions()

    def test_inherited_lock_is_parkventory_only_and_inode_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "vps-static.lock"
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)
            descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(
                    PROVIDER.use_inherited_deployment_lock(
                        descriptor,
                        lock_path,
                        owner=os.geteuid(),
                        group=os.getegid(),
                    ),
                    descriptor,
                )
                self.assertEqual(
                    SMTP.use_inherited_parkventory_lock(
                        descriptor,
                        lock_path,
                        owner=os.geteuid(),
                        group=os.getegid(),
                    ),
                    descriptor,
                )
                foreign_path = Path(directory) / "foreign.lock"
                foreign_path.touch(mode=0o600)
                foreign_path.chmod(0o600)
                with self.assertRaises(SMTP.MaterializationError):
                    SMTP.use_inherited_parkventory_lock(
                        descriptor,
                        foreign_path,
                        owner=os.geteuid(),
                        group=os.getegid(),
                    )
            finally:
                os.close(descriptor)
        smtp_source = (SCRIPTS / "materialize-smtp-secrets").read_text(
            encoding="utf-8"
        )
        self.assertIn('args.product != "parkventory"', smtp_source)
        self.assertIn("reserved for Parkventory install", smtp_source)


class Auth0Tests(unittest.TestCase):
    def client_document(self) -> dict[str, object]:
        return {
            "clients": [
                {
                    "client_id": "XtyJ6DUNbXNoGnysWWcqY2XfBlq9GakA",
                    "name": "Default App",
                },
                {
                    "client_id": "BVDpIAxZVZWQhPlziqsQCExYjVeil4YY",
                    "name": "Parkventory",
                },
            ],
            "length": 2,
            "limit": 3,
            "start": 0,
            "total": 2,
        }

    def provider_document(self) -> dict[str, object]:
        return {
            "credentials": {
                "smtp_host": "smtp.tem.scaleway.com",
                "smtp_port": 587,
                "smtp_user": USERNAME,
            },
            "default_from_address": "no-reply@parkventory.com",
            "enabled": True,
            "name": "smtp",
            "settings": {},
        }

    def auth_environment(self) -> dict[str, str]:
        return {
            "AUTH0_MANAGEMENT_TOKEN": "management-token-" + "x" * 64,
            "PARKVENTORY_SMTP_USERNAME": USERNAME,
            "PARKVENTORY_SMTP_PASSWORD": SMTP_VALUE,
        }

    def test_absent_auth0_provider_uses_get_post_get_without_test_email(self) -> None:
        responses = [
            (200, self.client_document()),
            (404, None),
            (201, self.provider_document()),
            (200, self.provider_document()),
        ]
        with (
            mock.patch.dict(os.environ, self.auth_environment(), clear=False),
            mock.patch.object(AUTH0, "request", side_effect=responses) as request,
        ):
            AUTH0.configure()
        self.assertEqual(
            [call.args[2] for call in request.call_args_list],
            ["GET", "GET", "POST", "GET"],
        )
        self.assertTrue(
            all("test" not in call.args[3].lower() for call in request.call_args_list)
        )

    def test_existing_auth0_provider_uses_get_patch_get_and_hides_secrets(self) -> None:
        responses = [
            (200, self.client_document()),
            (200, self.provider_document()),
            (200, self.provider_document()),
            (200, self.provider_document()),
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.auth_environment(), clear=False),
            mock.patch.object(AUTH0, "request", side_effect=responses) as request,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            AUTH0.configure()
        self.assertEqual(
            [call.args[2] for call in request.call_args_list],
            ["GET", "GET", "PATCH", "GET"],
        )
        output = stdout.getvalue() + stderr.getvalue()
        for value in (SMTP_VALUE, self.auth_environment()["AUTH0_MANAGEMENT_TOKEN"]):
            self.assertNotIn(value, output)

    def test_auth0_refuses_a_non_dedicated_tenant_before_provider_access(self) -> None:
        foreign = self.client_document()
        foreign["clients"] = [
            *foreign["clients"],
            {"client_id": "foreign", "name": "Surplasse"},
        ]
        foreign["length"] = 3
        foreign["total"] = 3
        with (
            mock.patch.dict(os.environ, self.auth_environment(), clear=False),
            mock.patch.object(
                AUTH0, "request", return_value=(200, foreign)
            ) as request,
            self.assertRaisesRegex(AUTH0.Auth0ProviderError, "not dedicated"),
        ):
            AUTH0.configure()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[3], AUTH0.GET_CLIENTS_PATH)

    def test_auth0_never_replaces_foreign_or_drifted_providers(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        sendgrid = self.provider_document()
        sendgrid["name"] = "sendgrid"
        cases.append(("sendgrid", sendgrid))
        for field, value in (
            ("smtp_host", "smtp.foreign.invalid"),
            ("smtp_port", 465),
            ("smtp_user", "11111111-2222-3333-4444-555555555555"),
        ):
            document = self.provider_document()
            document["credentials"] = {**document["credentials"], field: value}
            cases.append((field, document))
        for field, value in (
            ("default_from_address", "foreign@example.com"),
            ("settings", {"headers": {"X-Foreign": "true"}}),
        ):
            document = self.provider_document()
            document[field] = value
            cases.append((field, document))

        for label, document in cases:
            responses = [(200, self.client_document()), (200, document)]
            with (
                self.subTest(label=label),
                mock.patch.dict(os.environ, self.auth_environment(), clear=False),
                mock.patch.object(
                    AUTH0, "request", side_effect=responses
                ) as request,
                self.assertRaisesRegex(
                    AUTH0.Auth0ProviderError, "foreign or differs"
                ),
            ):
                AUTH0.configure()
            self.assertEqual(
                [call.args[2] for call in request.call_args_list], ["GET", "GET"]
            )


class RepositoryContractTests(unittest.TestCase):
    def test_workflow_is_manual_main_only_and_uses_the_protected_environment(self) -> None:
        path = ROOT / ".github/workflows/materialize-parkventory-providers.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["concurrency"]["group"], "production-vps")
        job = workflow["jobs"]["materialize"]
        self.assertEqual(job["environment"]["name"], "application-production")
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        self.assertIn("VPS_APPLICATION_DEPLOY_ENABLED", text)
        self.assertIn("test \"${VPS_APPLICATION_DEPLOY_ENABLED}\" = 'false'", text)
        self.assertIn("AUTH0_MANAGEMENT_TOKEN", text)
        self.assertIn("INFRA_REVISION", text)
        self.assertIn("materialize-parkventory-providers-v1", text)
        self.assertIn("VPS_PARKVENTORY_PROVIDER_INGRESS_SSH_PRIVATE_KEY", text)
        self.assertNotIn("VPS_APPLICATION_SSH_PRIVATE_KEY", text)
        step_names = [step["name"] for step in job["steps"]]
        self.assertLess(
            step_names.index(
                "Materialize and check the Parkventory provider bundle on Atlas"
            ),
            step_names.index(
                "Configure the Auth0 SMTP provider after Atlas validation"
            ),
        )
        self.assertNotIn("schedule:", text)
        self.assertNotIn("push:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("--arg", text)
        self.assertNotIn("send-test", text.lower())
        self.assertNotIn("test-email", text.lower())

    def test_dedicated_ssh_key_and_ansible_install_only_the_exact_gate(self) -> None:
        parser = (SCRIPTS / "parse-forced-command").read_text(encoding="utf-8")
        wrapper = (SCRIPTS / "forced-command").read_text(encoding="utf-8")
        ssh_gate_path = SCRIPTS / "materialize-parkventory-providers-ssh-gate"
        ssh_gate = ssh_gate_path.read_text(encoding="utf-8")
        self.assertTrue(os.access(ssh_gate_path, os.X_OK))
        self.assertNotIn("materialize-parkventory-providers-v1", parser)
        self.assertNotIn("materialize-parkventory-providers-v1", wrapper)
        self.assertIn(
            '"${SSH_ORIGINAL_COMMAND-}" != "materialize-parkventory-providers-v1"',
            ssh_gate,
        )
        self.assertIn(
            "/usr/local/libexec/vps/materialize-parkventory-providers-live-gate",
            ssh_gate,
        )
        for original_command in (
            "",
            "deploy " + INFRA_REVISION,
            "materialize-parkventory-providers-v1 extra",
        ):
            refused = subprocess.run(
                [str(ssh_gate_path)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "SSH_ORIGINAL_COMMAND": original_command},
            )
            self.assertEqual(refused.returncode, 64, original_command)
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "materialize-parkventory-providers-live-gate",
            defaults["vps_parkventory_provider_root_helpers"],
        )
        self.assertIn(
            "materialize-parkventory-providers-worker",
            defaults["vps_parkventory_provider_root_helpers"],
        )
        self.assertEqual(
            defaults["vps_parkventory_provider_ssh_executables"],
            ["materialize-parkventory-providers-ssh-gate"],
        )
        self.assertEqual(
            defaults["vps_parkventory_provider_ingress_authorized_key"], ""
        )
        role_tasks = (
            ROOT / "ansible/roles/deploy/tasks/main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "deploy_desired_key_identities + deploy_installed_key_identities",
            role_tasks,
        )
        self.assertIn(
            "vps_parkventory_provider_ingress_authorized_key | length > 0",
            role_tasks,
        )
        self.assertIn('mode: "0755"', role_tasks)
        authorized_keys = (
            ROOT / "ansible/roles/deploy/templates/authorized_keys.j2"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "vps_parkventory_provider_ingress_ssh_gate_path", authorized_keys
        )
        self.assertIn("provider_key | length > 0", authorized_keys)
        site = (ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        self.assertIn(
            "Cryptographically parse the provider ingress key before any role",
            site,
        )
        sudoers = (
            ROOT / "ansible/roles/deploy/templates/deploy.sudoers.j2"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "VPS_PARKVENTORY_PROVIDER_INGRESS = "
            "{{ vps_parkventory_provider_ingress_gate_path }} \"\"",
            sudoers,
        )

    def test_versioned_provider_contract_passes_the_existing_validator(self) -> None:
        provider_env = ROOT / "applications/parkventory/provider.env"
        content = provider_env.read_bytes()
        values = PROVIDER.parse_runtime_configuration(content)
        self.assertEqual(
            values["PARKVENTORY_OIDC_AUTH_SERVER_URL"],
            "https://pieper.eu.auth0.com/",
        )
        self.assertEqual(
            values["PARKVENTORY_OIDC_CLIENT_ID"],
            "BVDpIAxZVZWQhPlziqsQCExYjVeil4YY",
        )


if __name__ == "__main__":
    unittest.main()
