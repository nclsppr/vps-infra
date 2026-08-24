#!/usr/bin/env python3
"""Adversarial tests for the Surplasse public edge transition controller."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy-surplasse-public-edge"
REVISION = "0123456789abcdef0123456789abcdef01234567"
IMAGE = (
    "ghcr.io/nclsppr/vps-infra/caddy:sha-0123456789ab@sha256:"
    "89abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
)


def load_controller():
    loader = SourceFileLoader("surplasse_public_edge_controller", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


CONTROLLER = load_controller()


def protected_file(path: Path, content: bytes, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def changed_metadata(metadata, **changes):
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    values = {field: getattr(metadata, field) for field in fields}
    values.update(changes)
    return SimpleNamespace(**values)


class ControllerFixture:
    def __init__(self, root: Path):
        self.root = root
        self.paths = CONTROLLER.RuntimePaths(
            runtime_link=root / "srv/vps/runtime/public-static-edge",
            base_release_root=root / "srv/vps/releases/public-static-edge",
            extension_release_root=root
            / "srv/vps/releases/public-static-edge-surplasse",
            state_root=root / "var/lib/vps-public-edge-surplasse",
            repository_root=root / "srv/vps/repository",
            controller_revision=root / "usr/local/share/vps-infra/controller-revision",
            dns_materializer=root
            / "usr/local/libexec/vps/materialize-surplasse-dns-secrets",
            dns_bundle_root=root / "etc/vps/secrets/dns/surplasse",
            deployment_lock=root / "run/lock/vps-static.lock",
            base_transaction=root
            / "var/lib/vps-public-edge-parkventory/base-transaction.json",
            docker=root / "usr/bin/docker",
            systemctl=root / "usr/bin/systemctl",
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )
        for directory, mode in (
            (self.paths.runtime_link.parent, 0o755),
            (self.paths.base_release_root, 0o755),
            (self.paths.extension_release_root, 0o755),
            (self.paths.state_root, 0o700),
            (self.paths.base_transaction.parent, 0o700),
            (self.paths.repository_root, 0o755),
            (self.paths.deployment_lock.parent, 0o755),
            (self.paths.controller_revision.parent, 0o755),
            (self.paths.dns_materializer.parent, 0o755),
            (self.paths.dns_bundle_root, 0o700),
            (self.paths.docker.parent, 0o755),
            (self.paths.systemctl.parent, 0o755),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(mode)
        protected_file(self.paths.controller_revision, f"{REVISION}\n".encode())
        protected_file(self.paths.dns_materializer, b"#!/bin/sh\nexit 0\n", 0o755)
        protected_file(self.paths.docker, b"docker\n", 0o755)
        protected_file(self.paths.systemctl, b"systemctl\n", 0o755)
        self.route = (
            ROOT / "platform/caddy/routes/surplasse.caddy.disabled"
        ).read_bytes()
        self.tls = (
            ROOT / "platform/public-static-edge/surplasse-tls.caddy"
        ).read_bytes()
        self.overlay = (
            ROOT / "applications/surplasse/integration/public-edge.override.yaml"
        ).read_bytes()
        protected_file(self.paths.approved_route, self.route)
        protected_file(self.paths.tls_snippet, self.tls)
        protected_file(self.paths.overlay, self.overlay)
        protected_file(self.paths.candidate_validator, b"#!/bin/sh\nexit 0\n", 0o755)
        protected_file(self.paths.caddy_verifier, b"#!/bin/sh\nexit 0\n", 0o755)
        self.base = self.create_base()
        self.paths.runtime_link.symlink_to(self.base)
        self.attested = (
            root
            / "srv/applications/surplasse/releases"
            / ("sha256-" + "a" * 64)
            / "integration/caddy/surplasse.caddy"
        )
        protected_file(self.attested, self.route)

    def create_base(self, phase: str = "prepare") -> Path:
        base = self.paths.base_release_root / f"{REVISION}-{phase}"
        routes = base / "routes"
        routes.mkdir(parents=True)
        for name in CONTROLLER.ROUTE_NAMES:
            protected_file(routes / name, f"{name}\n".encode())
        for name, content, mode in (
            ("Caddyfile", b"import /etc/caddy/routes/*.caddy\n", 0o444),
            ("compose.yaml", b"name: vps-public-static-edge\n", 0o444),
            ("expected-images.json", b"{}\n", 0o444),
            ("phase", f"{phase}\n".encode(), 0o444),
            ("source-revision", f"{REVISION}\n".encode(), 0o444),
            ("validate-compose", b"#!/bin/sh\nexit 0\n", 0o555),
        ):
            protected_file(base / name, content, mode)
        routes.chmod(0o555)
        base.chmod(0o555)
        return base

    def stage(self):
        compose = CONTROLLER.canonical_json(
            {"name": CONTROLLER.PROJECT, "services": {"caddy": {"image": IMAGE}}}
        )

        def render(_base, staging, _paths):
            protected_file(staging / "compose.yaml", compose)
            return compose, {
                "name": CONTROLLER.PROJECT,
                "services": {"caddy": {"image": IMAGE}},
            }

        with (
            mock.patch.object(CONTROLLER, "require_dns_secrets"),
            mock.patch.object(
                CONTROLLER, "render_candidate_compose", side_effect=render
            ),
            mock.patch.object(CONTROLLER, "validate_local_image"),
            mock.patch.object(CONTROLLER, "validate_caddy_candidate"),
        ):
            return CONTROLLER.stage_candidate(self.attested, self.paths)


class SurplassePublicEdgeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ControllerFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_docker_executable_larger_than_16_mib_needs_no_content_read(
        self,
    ) -> None:
        incident_size = 45_570_321
        with self.fixture.paths.docker.open("r+b") as stream:
            stream.truncate(incident_size)
        self.assertGreater(incident_size, 16 * 1024 * 1024)
        self.assertEqual(
            CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES, 128 * 1024 * 1024
        )
        self.assertEqual(
            CONTROLLER.MAX_CONTROL_EXECUTABLE_BYTES, 16 * 1024 * 1024
        )
        with mock.patch.object(
            CONTROLLER.os,
            "read",
            side_effect=AssertionError("executable content was read"),
        ) as read:
            CONTROLLER.validate_protected_executable(
                self.fixture.paths.docker,
                "Docker executable",
                self.fixture.paths,
                modes=frozenset({0o755}),
                maximum_size=CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES,
            )
        read.assert_not_called()

    def test_executable_validation_refuses_oversize_symlink_mode_and_nlink(
        self,
    ) -> None:
        docker = self.fixture.paths.docker
        cases = ("oversize", "symlink", "mode", "nlink")
        for case in cases:
            with self.subTest(case=case):
                candidate = docker
                linked = docker.with_name(f"docker-{case}")
                try:
                    if case == "oversize":
                        with docker.open("r+b") as stream:
                            stream.truncate(
                                CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES + 1
                            )
                    elif case == "symlink":
                        linked.symlink_to(docker)
                        candidate = linked
                    elif case == "mode":
                        docker.chmod(0o775)
                    else:
                        os.link(docker, linked)
                    with self.assertRaises(CONTROLLER.EdgeDeploymentError):
                        CONTROLLER.validate_protected_executable(
                            candidate,
                            "Docker executable",
                            self.fixture.paths,
                            modes=frozenset({0o755}),
                            maximum_size=CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES,
                        )
                finally:
                    if linked.exists() or linked.is_symlink():
                        linked.unlink()
                    protected_file(docker, b"docker\n", 0o755)

    def test_executable_validation_refuses_an_untrusted_uid(self) -> None:
        metadata = self.fixture.paths.docker.lstat()
        untrusted = changed_metadata(
            metadata, st_uid=self.fixture.paths.expected_uid + 1
        )
        with mock.patch.object(CONTROLLER.os, "fstat", return_value=untrusted):
            with self.assertRaisesRegex(
                CONTROLLER.EdgeDeploymentError, "not one protected executable"
            ):
                CONTROLLER.validate_protected_executable(
                    self.fixture.paths.docker,
                    "Docker executable",
                    self.fixture.paths,
                    modes=frozenset({0o755}),
                    maximum_size=CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES,
                )

    def test_executable_validation_refuses_descriptor_metadata_change(
        self,
    ) -> None:
        metadata = self.fixture.paths.docker.lstat()
        changed = changed_metadata(
            metadata, st_mtime_ns=metadata.st_mtime_ns + 1
        )
        with mock.patch.object(
            CONTROLLER.os, "fstat", side_effect=(metadata, changed)
        ):
            with self.assertRaisesRegex(
                CONTROLLER.EdgeDeploymentError, "changed while it was validated"
            ):
                CONTROLLER.validate_protected_executable(
                    self.fixture.paths.docker,
                    "Docker executable",
                    self.fixture.paths,
                    modes=frozenset({0o755}),
                    maximum_size=CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES,
                )

    def test_executable_validation_refuses_path_replacement_during_check(
        self,
    ) -> None:
        docker = self.fixture.paths.docker
        metadata = docker.lstat()
        replacement = changed_metadata(metadata, st_ino=metadata.st_ino + 1)
        original_lstat = CONTROLLER.Path.lstat
        target_calls = 0

        def changing_lstat(candidate):
            nonlocal target_calls
            if candidate == docker:
                target_calls += 1
                return metadata if target_calls == 1 else replacement
            return original_lstat(candidate)

        with mock.patch.object(
            CONTROLLER.Path,
            "lstat",
            autospec=True,
            side_effect=changing_lstat,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.EdgeDeploymentError, "changed while it was validated"
            ):
                CONTROLLER.validate_protected_executable(
                    docker,
                    "Docker executable",
                    self.fixture.paths,
                    modes=frozenset({0o755}),
                    maximum_size=CONTROLLER.MAX_DOCKER_EXECUTABLE_BYTES,
                )

    def test_pre_cutover_is_a_valid_public_edge_base_phase(self) -> None:
        release = self.fixture.create_base("precutover")
        revision, phase = CONTROLLER.validate_base_release(
            release, self.fixture.paths
        )
        self.assertEqual(revision, REVISION)
        self.assertEqual(phase, "precutover")

    def test_stage_retains_every_static_route_and_publishes_exact_inputs(self) -> None:
        state = self.fixture.stage()
        release = Path(state.release)
        self.assertEqual(
            sorted(path.name for path in (release / "routes").iterdir()),
            [
                "parkventory.caddy",
                "personal.caddy",
                "surplasse.caddy",
            ],
        )
        for name in CONTROLLER.ROUTE_NAMES:
            self.assertEqual(
                (release / "routes" / name).read_bytes(),
                (self.fixture.base / "routes" / name).read_bytes(),
            )
        self.assertEqual(
            (release / "routes/surplasse.caddy").read_bytes(), self.fixture.route
        )
        self.assertEqual(
            (release / "surplasse-tls.caddy").read_bytes(), self.fixture.tls
        )
        self.assertEqual(state.route_sha256, CONTROLLER.ROUTE_SHA256)
        CONTROLLER.validate_candidate_release(release, state, self.fixture.paths)

    def test_approved_route_reserves_the_complete_technical_name_set(self) -> None:
        route = self.fixture.route.decode("utf-8")
        reserved_line = next(
            line for line in route.splitlines() if line.startswith("\t@reserved host ")
        )
        names = {
            item.removesuffix(".{$SURPLASSE_DOMAIN:surplasse.com}")
            for item in reserved_line.removeprefix("\t@reserved host ").split()
        }
        self.assertEqual(
            names,
            {
                "admin",
                "app",
                "autoconfig",
                "autodiscover",
                "grafana",
                "imap",
                "local",
                "mail",
                "mta-sts",
                "pop",
                "pop3",
                "reports",
                "smtp",
                "status",
                "webmail",
            },
        )

    def test_stage_rejects_a_route_that_is_not_byte_identical(self) -> None:
        self.fixture.attested.chmod(0o644)
        self.fixture.attested.write_bytes(self.fixture.route + b"# changed\n")
        self.fixture.attested.chmod(0o444)
        with self.assertRaisesRegex(
            CONTROLLER.EdgeDeploymentError,
            "differs from the approved route",
        ):
            CONTROLLER.validate_attested_route(
                self.fixture.attested, self.fixture.paths
            )

    def test_stage_cli_checks_dns_only_while_the_shared_lock_is_held(self) -> None:
        lock_held = False
        state = mock.Mock(
            release=str(self.fixture.paths.extension_release_root / ("b" * 64))
        )

        @contextlib.contextmanager
        def locked(_paths):
            nonlocal lock_held
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

        def stage(_route, _paths):
            self.assertTrue(lock_held)
            return state

        with (
            mock.patch.object(CONTROLLER, "require_runtime"),
            mock.patch.object(CONTROLLER, "deployment_lock", side_effect=locked),
            mock.patch.object(CONTROLLER, "recover_locked"),
            mock.patch.object(CONTROLLER, "stage_candidate", side_effect=stage),
        ):
            self.assertEqual(
                CONTROLLER.main(
                    ["--stage", str(self.fixture.attested)], paths=self.fixture.paths
                ),
                0,
            )

    def test_shared_lock_refuses_a_second_hard_link(self) -> None:
        self.fixture.paths.deployment_lock.touch(mode=0o600)
        os.link(
            self.fixture.paths.deployment_lock,
            self.fixture.paths.deployment_lock.with_name("vps-static-linked.lock"),
        )
        with self.assertRaisesRegex(
            CONTROLLER.EdgeDeploymentError, "shared deployment lock is not protected"
        ):
            with CONTROLLER.deployment_lock(self.fixture.paths):
                self.fail("unsafe lock was acquired")

    def test_fingerprint_binds_base_route_tls_overlay_and_revision(self) -> None:
        baseline = CONTROLLER.candidate_fingerprint(
            base_release=self.fixture.base,
            revision=REVISION,
            attested_route=self.fixture.attested,
            route=self.fixture.route,
            tls=self.fixture.tls,
            overlay=self.fixture.overlay,
        )
        variants = (
            {"route": self.fixture.route + b"x"},
            {"tls": self.fixture.tls + b"x"},
            {"overlay": self.fixture.overlay + b"x"},
            {"revision": "f" * 40},
            {"attested_route": self.fixture.attested.with_name("other.caddy")},
            {"base_release": self.fixture.base.with_name(f"{'f' * 40}-prepare")},
        )
        defaults = {
            "base_release": self.fixture.base,
            "revision": REVISION,
            "attested_route": self.fixture.attested,
            "route": self.fixture.route,
            "tls": self.fixture.tls,
            "overlay": self.fixture.overlay,
        }
        for changed in variants:
            with self.subTest(changed=next(iter(changed))):
                self.assertNotEqual(
                    baseline,
                    CONTROLLER.candidate_fingerprint(**(defaults | changed)),
                )

    def transaction(self, phase: str):
        state = self.fixture.stage()
        transaction = CONTROLLER.EdgeTransaction(
            candidate=state,
            previous=None,
            previous_release=str(self.fixture.base),
            phase=phase,
        )
        CONTROLLER._write_transaction(transaction, self.fixture.paths)
        return state, transaction

    def test_prepared_recovery_rolls_back_even_after_the_link_switched(self) -> None:
        state, _transaction = self.transaction("prepared")
        CONTROLLER.switch_release(self.fixture.paths, Path(state.release))
        with (
            mock.patch.object(CONTROLLER, "restart_edge") as restart,
            mock.patch.object(CONTROLLER, "verify_live_runtime") as verify,
        ):
            CONTROLLER.recover_locked(self.fixture.paths)
        self.assertEqual(
            CONTROLLER.current_release(self.fixture.paths), self.fixture.base
        )
        restart.assert_called_once_with(self.fixture.paths)
        verify.assert_called_once_with(self.fixture.paths, None)
        self.assertFalse(self.fixture.paths.transaction_path.exists())

    def test_reconciled_recovery_commits_forward_only_after_live_proof(self) -> None:
        state, _transaction = self.transaction("reconciled")
        CONTROLLER.switch_release(self.fixture.paths, Path(state.release))
        with mock.patch.object(CONTROLLER, "verify_live_runtime") as verify:
            CONTROLLER.recover_locked(self.fixture.paths)
        verify.assert_called_once_with(self.fixture.paths, state)
        self.assertEqual(CONTROLLER.read_active(self.fixture.paths), state)
        self.assertFalse(self.fixture.paths.transaction_path.exists())

    def test_failed_forward_recovery_restores_the_static_base(self) -> None:
        state, _transaction = self.transaction("reconciled")
        CONTROLLER.switch_release(self.fixture.paths, Path(state.release))
        with (
            mock.patch.object(CONTROLLER, "restart_edge") as restart,
            mock.patch.object(
                CONTROLLER,
                "verify_live_runtime",
                side_effect=[
                    CONTROLLER.EdgeDeploymentError("candidate unhealthy"),
                    None,
                ],
            ),
        ):
            CONTROLLER.recover_locked(self.fixture.paths)
        self.assertEqual(
            CONTROLLER.current_release(self.fixture.paths), self.fixture.base
        )
        restart.assert_called_once_with(self.fixture.paths)
        self.assertFalse(self.fixture.paths.transaction_path.exists())

    def test_forward_recovery_restores_base_when_repository_is_unavailable(
        self,
    ) -> None:
        state, _transaction = self.transaction("reconciled")
        CONTROLLER.switch_release(self.fixture.paths, Path(state.release))
        unavailable = self.fixture.paths.repository_root.with_name("repository-away")
        self.fixture.paths.repository_root.rename(unavailable)
        with (
            mock.patch.object(CONTROLLER, "restart_edge") as restart,
            mock.patch.object(CONTROLLER, "verify_live_runtime") as verify,
        ):
            CONTROLLER.recover_locked(self.fixture.paths)
        self.assertEqual(
            CONTROLLER.current_release(self.fixture.paths), self.fixture.base
        )
        restart.assert_called_once_with(self.fixture.paths)
        verify.assert_called_once_with(self.fixture.paths, None)
        self.assertFalse(self.fixture.paths.transaction_path.exists())

    def test_recovery_does_not_reconcile_an_unprotected_previous_base(self) -> None:
        state, _transaction = self.transaction("switched")
        CONTROLLER.switch_release(self.fixture.paths, Path(state.release))
        (self.fixture.base / "compose.yaml").chmod(0o644)
        with mock.patch.object(CONTROLLER, "restart_edge") as restart:
            with self.assertRaisesRegex(
                CONTROLLER.EdgeDeploymentError,
                "public edge base compose.yaml is not one protected regular file",
            ):
                CONTROLLER.recover_locked(self.fixture.paths)
        self.assertEqual(
            CONTROLLER.current_release(self.fixture.paths), Path(state.release)
        )
        restart.assert_not_called()

    def test_recovery_refuses_an_unexpected_runtime_target(self) -> None:
        _state, _transaction = self.transaction("switched")
        unexpected = self.fixture.paths.base_release_root / f"{'f' * 40}-prepare"
        unexpected.mkdir()
        unexpected.chmod(0o555)
        self.fixture.paths.runtime_link.unlink()
        self.fixture.paths.runtime_link.symlink_to(unexpected)
        with self.assertRaisesRegex(
            CONTROLLER.EdgeDeploymentError, "unexpected runtime release"
        ):
            CONTROLLER.recover_locked(self.fixture.paths)

    def test_transaction_binds_a_candidate_to_its_previous_base(self) -> None:
        state = self.fixture.stage()
        detached = CONTROLLER.dataclasses.replace(
            state,
            base_release=str(
                self.fixture.paths.base_release_root / f"{'f' * 40}-prepare"
            ),
        )
        transaction = CONTROLLER.EdgeTransaction(
            candidate=detached,
            previous=None,
            previous_release=str(self.fixture.base),
            phase="prepared",
        )
        with self.assertRaisesRegex(
            CONTROLLER.EdgeDeploymentError,
            "does not retain its previous base release",
        ):
            CONTROLLER.validate_transaction(
                transaction.as_dict(), self.fixture.paths, "test transaction"
            )

    def test_activation_failure_immediately_restores_previous_release(self) -> None:
        state = self.fixture.stage()
        with (
            mock.patch.object(CONTROLLER, "verify_unit_active"),
            mock.patch.object(CONTROLLER, "require_dns_secrets"),
            mock.patch.object(CONTROLLER, "validate_local_image"),
            mock.patch.object(CONTROLLER, "validate_caddy_candidate"),
            mock.patch.object(
                CONTROLLER,
                "restart_edge",
                side_effect=[CONTROLLER.EdgeDeploymentError("restart failed"), None],
            ),
            mock.patch.object(CONTROLLER, "verify_live_runtime"),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.EdgeDeploymentError, "restart failed"
            ):
                CONTROLLER.activate_candidate(Path(state.release), self.fixture.paths)
        self.assertEqual(
            CONTROLLER.current_release(self.fixture.paths), self.fixture.base
        )
        self.assertFalse(self.fixture.paths.transaction_path.exists())

    def test_actual_secret_preflight_uses_only_file_variables(self) -> None:
        commands: list[list[str]] = []

        def capture(arguments, **_kwargs):
            command = [str(item) for item in arguments]
            commands.append(command)
            return mock.Mock(stdout=b"", stderr=b"", returncode=0)

        with mock.patch.object(CONTROLLER, "run", side_effect=capture):
            CONTROLLER.validate_caddy_candidate(
                self.fixture.base, IMAGE, self.fixture.paths
            )
        docker = commands[1]
        environment = [
            docker[index + 1] for index, item in enumerate(docker) if item == "--env"
        ]
        self.assertEqual(
            environment,
            [
                "OVH_APPLICATION_KEY_FILE=/run/secrets/surplasse_ovh_application_key",
                "OVH_APPLICATION_SECRET_FILE=/run/secrets/surplasse_ovh_application_secret",
                "OVH_CONSUMER_KEY_FILE=/run/secrets/surplasse_ovh_consumer_key",
            ],
        )
        self.assertNotIn("validation-placeholder", " ".join(docker))

    def test_dns_materializer_contract_is_read_only_and_exact(self) -> None:
        commands: list[list[str]] = []

        def capture(arguments, **_kwargs):
            commands.append([str(item) for item in arguments])
            return mock.Mock(
                stdout=b"Surplasse DNS credential contract valid\n",
                stderr=b"",
                returncode=0,
            )

        with (
            mock.patch.object(
                CONTROLLER, "read_protected_file", return_value=b"helper"
            ),
            mock.patch.object(CONTROLLER, "run", side_effect=capture),
        ):
            CONTROLLER.require_dns_secrets(self.fixture.paths)
        self.assertEqual(
            commands,
            [[str(self.fixture.paths.dns_materializer), "--check"]],
        )

    def test_reconcile_forces_secret_bind_mount_recreation(self) -> None:
        commands: list[list[str]] = []

        def capture(arguments, **_kwargs):
            commands.append([str(item) for item in arguments])
            return mock.Mock(stdout=b"", stderr=b"", returncode=0)

        with (
            mock.patch.object(
                CONTROLLER, "current_release", return_value=self.fixture.base
            ),
            mock.patch.object(CONTROLLER, "run", side_effect=capture),
        ):
            CONTROLLER.restart_edge(self.fixture.paths)
        self.assertIn("--force-recreate", commands[0])
        self.assertEqual(commands[0][-1], "caddy")

    def test_surplasse_writer_refuses_a_public_edge_base_transaction(self) -> None:
        self.fixture.paths.base_transaction.write_text("{}\n", encoding="utf-8")
        self.fixture.paths.base_transaction.chmod(0o600)
        with self.assertRaisesRegex(
            CONTROLLER.EdgeDeploymentError,
            "unfinished public edge base transaction",
        ):
            CONTROLLER.refuse_public_edge_base_transaction(self.fixture.paths)

    def test_ansible_installs_recovery_before_the_public_edge(self) -> None:
        defaults = (
            ROOT / "ansible/roles/public_static_edge/defaults/main.yml"
        ).read_text()
        self.assertIn("vps_public_static_edge_surplasse_controller: >-", defaults)
        self.assertIn("/usr/local/libexec/vps/deploy-surplasse-public-edge", defaults)
        self.assertIn("vps_public_static_edge_application_controller: >-", defaults)
        unit = (
            ROOT
            / "ansible/roles/public_static_edge/templates/vps-public-static-edge.service.j2"
        ).read_text()
        recovery = (
            ROOT / "ansible/roles/public_static_edge/templates/"
            "vps-public-edge-surplasse-recover.service.j2"
        ).read_text()
        tasks = (ROOT / "ansible/roles/public_static_edge/tasks/main.yml").read_text()
        self.assertIn("vps_public_static_edge_surplasse_recovery_unit", unit)
        self.assertIn("--recover", recovery)
        self.assertNotIn("--assert-base-switch-safe", tasks)
        self.assertIn("--prepare-public-edge-base", tasks)
        self.assertIn("--commit-public-edge-base", tasks)
        self.assertIn("--rollback-public-edge-base", tasks)
        top_level_tasks = yaml.safe_load(tasks)
        convergence = next(
            task
            for task in top_level_tasks
            if task.get("name")
            == "Stage, switch, and verify the isolated public static edge"
        )
        convergence_tasks = convergence["block"]
        transaction = next(
            task
            for task in convergence_tasks
            if task.get("name") == "Switch and reconcile the public static edge"
        )
        nested_names = [task.get("name") for task in transaction["block"]]
        self.assertEqual(
            nested_names,
            [
                "Prepare the locked public edge base transaction",
                "Verify the switched public static edge from host and operator networks",
                "Commit the probed public edge base transaction",
            ],
        )

    def test_canonical_admission_is_enabled_but_legacy_adapter_remains_locked(self) -> None:
        application = json.loads(
            (ROOT / "releases/application-production.json").read_text()
        )
        adapter = json.loads((ROOT / "applications/surplasse/adapter.json").read_text())
        self.assertTrue(application["applications"]["surplasse"]["enabled"])
        self.assertEqual(adapter["activation_policy"], "locked")


if __name__ == "__main__":
    unittest.main()
