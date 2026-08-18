#!/usr/bin/env python3
"""Adversarial tests for the bounded Surplasse pilot controller."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/surplasse-pilot-bootstrap"
DIGEST = "sha256:" + "a" * 64
REVISION = "0123456789abcdef0123456789abcdef01234567"


def load_script():
    loader = SourceFileLoader("surplasse_pilot_controller_test_subject", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


PILOT = load_script()


def active_state():
    profile = PILOT.APP.PROFILES["surplasse"]
    return PILOT.APP.ApplicationState(
        application="surplasse",
        source_revision=REVISION,
        release_reference=(
            "ghcr.io/nclsppr/surplasse/application-release@" + DIGEST
        ),
        integration_reference=profile.integration_repository + "@" + DIGEST,
        component_references={
            component: repository + "@" + DIGEST
            for component, repository in profile.component_repositories.items()
        },
        migration_inventory_digest=DIGEST,
        probe_inventory_digest=DIGEST,
    )


class PilotControllerTests(unittest.TestCase):
    def test_corrupt_journal_values_fail_closed_without_raw_exceptions(self) -> None:
        valid = PILOT.state_value(
            active_state(),
            "sha256:" + "b" * 64,
            "verified",
            confirmed_until=None,
        )
        non_ascii = {**valid, "backend_reference": "privé"}
        invalid_phase = {**valid, "phase": []}
        documents = (
            (json.dumps(non_ascii, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            (json.dumps(invalid_phase, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"),
            ("[" * 1100 + "0" + "]" * 1100 + "\n").encode("ascii"),
            b'{"phase":' + b"9" * 5000 + b"}\n",
        )
        for raw in documents:
            with self.subTest(prefix=raw[:20]):
                with self.assertRaises(PILOT.PilotError):
                    PILOT.parse_state(raw)

    def test_cli_accepts_only_one_bounded_operation_and_no_target(self) -> None:
        parser = PILOT.build_parser()
        self.assertEqual(parser.parse_args(["status"]).operation, "status")
        self.assertEqual(parser.parse_args(["apply"]).operation, "apply")
        for arguments in ([], ["verify"], ["apply", DIGEST], ["status", "extra"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args(arguments)
                self.assertEqual(raised.exception.code, 64)

    def test_foreign_dependency_error_is_constant_and_never_echoed(self) -> None:
        for failure in (
            PILOT.APP.ApplicationDeploymentError("PRIVATE@example.invalid"),
            ValueError("PRIVATE@example.invalid"),
        ):
            with self.subTest(exception=type(failure).__name__):
                stderr = io.StringIO()
                with (
                    mock.patch.object(PILOT.os, "geteuid", return_value=0),
                    mock.patch.object(PILOT.os, "getegid", return_value=0),
                    mock.patch.object(PILOT.APP, "require_protected_file"),
                    mock.patch.object(PILOT, "validate_helper"),
                    mock.patch.object(
                        PILOT.APP,
                        "deployment_lock",
                        return_value=contextlib.nullcontext(),
                    ),
                    mock.patch.object(
                        PILOT.APP,
                        "validate_runtime",
                        side_effect=failure,
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    self.assertEqual(PILOT.main(["status"]), 78)
                self.assertEqual(
                    stderr.getvalue(),
                    "Surplasse pilot operation refused: protected dependency failed\n",
                )
                self.assertNotIn("PRIVATE@example.invalid", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_empty_status_writes_short_lived_identity_bound_confirmation(self) -> None:
        active = active_state()
        written: list[dict[str, object]] = []
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=None),
            mock.patch.object(PILOT, "remove_residue") as remove,
            mock.patch.object(PILOT, "invoke", return_value=3) as invoke,
            mock.patch.object(PILOT, "write_pilot_state", side_effect=written.append),
            mock.patch.object(PILOT.time, "time", return_value=1000),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = PILOT.status(
                active,
                PILOT.APP.PROFILES["surplasse"],
                {"HOME": "/release"},
                "sha256:" + "b" * 64,
            )
        self.assertEqual(result, 3)
        self.assertEqual(remove.call_count, 2)
        invoke.assert_called_once_with(
            active,
            PILOT.APP.PROFILES["surplasse"],
            {"HOME": "/release"},
            "status",
        )
        self.assertEqual(written[0]["phase"], "empty-confirmed")
        self.assertEqual(
            written[0]["confirmed_until"],
            1000 + PILOT.EMPTY_CONFIRMATION_SECONDS,
        )
        self.assertEqual(written[0]["release_reference"], active.release_reference)
        self.assertEqual(written[0]["backend_reference"], active.component_references["backend"])
        self.assertEqual(written[0]["manifest_sha256"], "sha256:" + "b" * 64)

    def test_apply_journals_before_mutation_and_requires_separate_status(self) -> None:
        active = active_state()
        manifest_digest = "sha256:" + "b" * 64
        confirmation = PILOT.state_value(
            active,
            manifest_digest,
            "empty-confirmed",
            confirmed_until=2000,
        )
        events: list[str] = []

        def write(value):
            events.append(str(value["phase"]))

        def invoke(*_arguments):
            self.assertEqual(events, ["applying"])
            events.append("container")
            return 0

        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=confirmation),
            mock.patch.object(PILOT, "container_id", return_value=None),
            mock.patch.object(PILOT, "write_pilot_state", side_effect=write),
            mock.patch.object(PILOT, "invoke", side_effect=invoke),
            mock.patch.object(PILOT, "remove_residue") as remove,
            mock.patch.object(PILOT.time, "time", return_value=1500),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = PILOT.apply(
                active,
                PILOT.APP.PROFILES["surplasse"],
                {"HOME": "/release"},
                manifest_digest,
            )
        self.assertEqual(result, 0)
        self.assertEqual(events, ["applying", "container", "applied-unverified"])
        remove.assert_called_once_with(active)

    def test_apply_failure_remains_ambiguous_and_cannot_replay(self) -> None:
        active = active_state()
        manifest_digest = "sha256:" + "b" * 64
        confirmation = PILOT.state_value(
            active,
            manifest_digest,
            "empty-confirmed",
            confirmed_until=2000,
        )
        written: list[dict[str, object]] = []
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=confirmation),
            mock.patch.object(PILOT, "container_id", return_value=None),
            mock.patch.object(PILOT, "write_pilot_state", side_effect=written.append),
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(PILOT, "invoke", return_value=70),
            mock.patch.object(PILOT.time, "time", return_value=1500),
        ):
            with self.assertRaisesRegex(PILOT.PilotError, "status for recovery"):
                PILOT.apply(
                    active,
                    PILOT.APP.PROFILES["surplasse"],
                    {},
                    manifest_digest,
                )
        self.assertEqual([value["phase"] for value in written], ["applying"])
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=written[0]),
            mock.patch.object(PILOT, "container_id", return_value=None),
        ):
            with self.assertRaisesRegex(PILOT.PilotError, "not replayable"):
                PILOT.apply(
                    active,
                    PILOT.APP.PROFILES["surplasse"],
                    {},
                    manifest_digest,
                )

    def test_status_resolves_ambiguous_apply_only_on_exact_readback(self) -> None:
        active = active_state()
        manifest_digest = "sha256:" + "b" * 64
        applying = PILOT.state_value(
            active,
            manifest_digest,
            "applying",
            confirmed_until=None,
        )
        written: list[dict[str, object]] = []
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=applying),
            mock.patch.object(PILOT, "remove_residue"),
            mock.patch.object(PILOT, "invoke", return_value=0),
            mock.patch.object(PILOT, "write_pilot_state", side_effect=written.append),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                PILOT.status(
                    active,
                    PILOT.APP.PROFILES["surplasse"],
                    {},
                    manifest_digest,
                ),
                0,
            )
        self.assertEqual(written[-1]["phase"], "verified")

        written.clear()
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=applying),
            mock.patch.object(PILOT, "remove_residue"),
            mock.patch.object(PILOT, "invoke", return_value=3),
            mock.patch.object(PILOT, "write_pilot_state", side_effect=written.append),
        ):
            with self.assertRaisesRegex(PILOT.PilotError, "ambiguous"):
                PILOT.status(
                    active,
                    PILOT.APP.PROFILES["surplasse"],
                    {},
                    manifest_digest,
                )
        self.assertEqual(written[-1]["phase"], "ambiguous-empty")

    def test_verified_history_then_empty_refuses_implicit_rebootstrap(self) -> None:
        active = active_state()
        manifest_digest = "sha256:" + "b" * 64
        verified = PILOT.state_value(
            active,
            manifest_digest,
            "verified",
            confirmed_until=None,
        )
        written: list[dict[str, object]] = []
        with (
            mock.patch.object(PILOT, "read_pilot_state", return_value=verified),
            mock.patch.object(PILOT, "remove_residue"),
            mock.patch.object(PILOT, "invoke", return_value=3),
            mock.patch.object(PILOT, "write_pilot_state", side_effect=written.append),
        ):
            with self.assertRaisesRegex(PILOT.PilotError, "replay is refused"):
                PILOT.status(
                    active,
                    PILOT.APP.PROFILES["surplasse"],
                    {},
                    manifest_digest,
                )
        self.assertEqual(written[-1]["phase"], "ambiguous-empty")
        self.assertIsNone(written[-1]["confirmed_until"])

    def test_identity_manifest_or_expiration_drift_refuses_apply(self) -> None:
        active = active_state()
        manifest_digest = "sha256:" + "b" * 64
        valid = PILOT.state_value(
            active,
            manifest_digest,
            "empty-confirmed",
            confirmed_until=2000,
        )
        cases = {
            "release": {**valid, "release_reference": "changed"},
            "manifest": {**valid, "manifest_sha256": "sha256:" + "c" * 64},
            "expired": valid,
        }
        for label, journal in cases.items():
            with (
                self.subTest(divergence=label),
                mock.patch.object(PILOT, "read_pilot_state", return_value=journal),
                mock.patch.object(PILOT, "container_id", return_value=None),
                mock.patch.object(PILOT, "invoke") as invoke,
                mock.patch.object(
                    PILOT.time,
                    "time",
                    return_value=2001 if label == "expired" else 1500,
                ),
            ):
                with self.assertRaises(PILOT.PilotError):
                    PILOT.apply(
                        active,
                        PILOT.APP.PROFILES["surplasse"],
                        {},
                        manifest_digest,
                    )
                invoke.assert_not_called()

    def test_compose_invocation_is_exact_and_output_is_discarded(self) -> None:
        active = active_state()
        profile = PILOT.APP.PROFILES["surplasse"]
        with mock.patch.object(PILOT, "run_silent", return_value=0) as run:
            self.assertEqual(PILOT.invoke(active, profile, {"HOME": "/release"}, "apply"), 0)
        command = run.call_args.args[0]
        self.assertEqual(
            command[-9:],
            [
                "run",
                "--rm",
                "--name",
                PILOT.container_name(active),
                "--no-deps",
                "--pull",
                "never",
                "pilot-bootstrap",
                "apply",
            ],
        )
        self.assertNotIn(active.release_reference, command[-1:])
        private = "private@example.invalid acct_Private123"
        completed = subprocess.CompletedProcess(["docker"], 3, private, private)
        with mock.patch.object(PILOT.APP, "_run_bounded_status", return_value=completed):
            self.assertEqual(
                PILOT.run_silent(["docker"], environment={}, timeout=1),
                3,
            )

    def test_container_identity_checks_project_service_image_command_and_name(self) -> None:
        active = active_state()
        identifier = "d" * 64
        exact = (
            f"/{PILOT.container_name(active)}\t{identifier}\t"
            f"{active.component_references['backend']}\tsurplasse\t"
            'pilot-bootstrap\tTrue\t["status"]\n'
        )
        with mock.patch.object(
            PILOT.APP,
            "_run_bounded_status",
            return_value=subprocess.CompletedProcess([], 0, exact, ""),
        ):
            self.assertTrue(PILOT.validate_container(active, identifier))
        for mutation in (
            exact.replace("pilot-bootstrap", "backend", 1),
            exact.replace('"status"', '"shell"'),
            exact.replace(active.component_references["backend"], "other"),
        ):
            with (
                self.subTest(output=mutation[-30:]),
                mock.patch.object(
                    PILOT.APP,
                    "_run_bounded_status",
                    return_value=subprocess.CompletedProcess([], 0, mutation, ""),
                ),
            ):
                with self.assertRaises(PILOT.PilotError):
                    PILOT.validate_container(active, identifier)

    def test_residue_cleanup_requires_stable_absence(self) -> None:
        active = active_state()
        with (
            mock.patch.object(
                PILOT,
                "container_id",
                side_effect=[None, "d" * 64, None, None],
            ) as identify,
            mock.patch.object(PILOT, "validate_container", return_value=True),
            mock.patch.object(
                PILOT.APP,
                "_run_bounded_status",
                return_value=subprocess.CompletedProcess(
                    [], 0, "d" * 64 + "\n", ""
                ),
            ),
            mock.patch.object(PILOT.time, "sleep"),
        ):
            PILOT.remove_residue(active)
        self.assertEqual(identify.call_count, 4)

    def test_active_release_requires_current_link_no_transaction_and_full_policy(self) -> None:
        active = active_state()
        profile = PILOT.APP.PROFILES["surplasse"]
        environment = {"HOME": "/release"}
        with (
            mock.patch.object(PILOT.APP, "read_state", return_value=active),
            mock.patch.object(PILOT.APP, "read_transaction", return_value=None),
            mock.patch.object(
                PILOT.APP,
                "current_target",
                return_value=PILOT.APP.release_target(active),
            ),
            mock.patch.object(
                PILOT.APP,
                "validate_materialized_runtime_policy",
                return_value=(profile, object(), environment),
            ) as policy,
            mock.patch.object(
                PILOT,
                "read_manifest_digest",
                return_value="sha256:" + "b" * 64,
            ),
        ):
            self.assertEqual(
                PILOT.active_release(),
                (active, profile, environment, "sha256:" + "b" * 64),
            )
        policy.assert_called_once_with(active)
        with (
            mock.patch.object(PILOT.APP, "read_state", return_value=active),
            mock.patch.object(PILOT.APP, "read_transaction", return_value=object()),
        ):
            with self.assertRaisesRegex(PILOT.PilotError, "transaction"):
                PILOT.active_release()

    def test_ansible_uses_root_only_helpers_argv_and_no_log(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            defaults["vps_surplasse_pilot_materializer_path"],
            "/usr/local/libexec/vps/materialize-surplasse-pilot-manifest",
        )
        self.assertEqual(
            defaults["vps_surplasse_pilot_controller_path"],
            "/usr/local/libexec/vps/surplasse-pilot-bootstrap",
        )
        self.assertIn(
            "materialize-surplasse-pilot-manifest",
            defaults["vps_deploy_root_helpers"],
        )
        self.assertIn(
            "surplasse-pilot-bootstrap",
            defaults["vps_deploy_root_helpers"],
        )
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/surplasse_pilot/tasks/main.yml").read_text(
                encoding="utf-8"
            )
        )
        by_name = {task["name"]: task for task in tasks}
        materialize = by_name["Materialize the private Surplasse pilot manifest"]
        self.assertIs(materialize["no_log"], True)
        block = {task["name"]: task for task in materialize["block"]}
        install = block["Atomically materialize the validated pilot manifest"]
        self.assertEqual(
            install["ansible.builtin.command"]["argv"][:2],
            ["{{ vps_surplasse_pilot_materializer }}", "--install-from"],
        )
        for name, operation in (
            ("Read the admitted Surplasse pilot status", "status"),
            ("Apply the status-confirmed Surplasse pilot once", "apply"),
        ):
            task = by_name[name]
            self.assertIs(task["no_log"], True)
            self.assertEqual(
                task["ansible.builtin.command"]["argv"],
                ["{{ vps_surplasse_pilot_controller }}", operation],
            )

        safe_status = by_name["Derive the safe Surplasse pilot status result"]
        safe_expression = safe_status["ansible.builtin.set_fact"][
            "vps_surplasse_pilot_safe_status"
        ]
        self.assertIn("vps_surplasse_pilot_status.rc == 0", safe_expression)
        self.assertIn("verified", safe_expression)
        self.assertIn("empty-confirmed", safe_expression)
        self.assertNotIn("stdout", safe_expression)
        self.assertNotIn("stderr", safe_expression)

        playbook = yaml.safe_load(
            (ROOT / "ansible/playbooks/surplasse-pilot.yml").read_text(
                encoding="utf-8"
            )
        )[0]
        post_tasks = {task["name"]: task for task in playbook["post_tasks"]}
        status_message = post_tasks["Report only the safe pilot status result"][
            "ansible.builtin.debug"
        ]["msg"]
        self.assertEqual(
            status_message,
            "Surplasse pilot status: {{ vps_surplasse_pilot_safe_status }}.",
        )
        self.assertNotIn("vps_surplasse_pilot_status", status_message)
        self.assertNotIn("stdout", yaml.safe_dump(post_tasks))
        self.assertNotIn("stderr", yaml.safe_dump(post_tasks))

    def test_converge_rejects_exposed_or_linked_original_manifest(self) -> None:
        converge = ROOT / "scripts/converge"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "hosts.yml"
            variables = root / "vars.yml"
            inventory.write_text("all: {}\n", encoding="utf-8")
            variables.write_text("vps_admin_authorized_keys: []\n", encoding="utf-8")
            manifest = root / "pilot.json"
            manifest.write_text("{}\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "ANSIBLE_INVENTORY": str(inventory),
                    "ANSIBLE_EXTRA_VARS": str(variables),
                    "SURPLASSE_PILOT_MANIFEST": str(manifest),
                }
            )
            manifest.chmod(0o644)
            exposed = subprocess.run(
                [str(converge), "--materialize-surplasse-pilot"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(exposed.returncode, 64)
            self.assertIn("private 16 KiB file policy", exposed.stderr)
            manifest.chmod(0o600)
            linked = root / "pilot-linked.json"
            os.link(manifest, linked)
            hardlinked = subprocess.run(
                [str(converge), "--materialize-surplasse-pilot"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(hardlinked.returncode, 64)
            self.assertIn("private 16 KiB file policy", hardlinked.stderr)


if __name__ == "__main__":
    unittest.main()
