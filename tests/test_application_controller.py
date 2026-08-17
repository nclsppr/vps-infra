#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REVISION = "0123456789abcdef0123456789abcdef01234567"
PREVIOUS_REVISION = "89abcdef0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
PREVIOUS_DIGEST = "sha256:" + "b" * 64


def load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTROLLER = load_module(
    "application_controller_test_subject",
    SCRIPTS / "deploy-application",
)
GATE = load_module(
    "application_live_gate_test_subject",
    SCRIPTS / "deploy-application-live-gate",
)


def state(
    application: str = "surplasse",
    *,
    revision: str = REVISION,
    digest: str = DIGEST,
):
    profile = CONTROLLER.PROFILES[application]
    components = {
        component: f"{repository}@{digest}"
        for component, repository in profile.component_repositories.items()
    }
    value = CONTROLLER.ApplicationState(
        application=application,
        source_revision=revision,
        release_reference=(
            f"ghcr.io/nclsppr/{application}/application-release@{digest}"
        ),
        integration_reference=f"{profile.integration_repository}@{digest}",
        component_references=components,
        migration_inventory_digest=digest,
        probe_inventory_digest=digest,
    )
    CONTROLLER.validate_state(value)
    return value


class ApplicationControllerTests(unittest.TestCase):
    def test_state_and_transaction_journals_are_strict_and_canonical(self):
        candidate = state()
        previous = state(revision=PREVIOUS_REVISION, digest=PREVIOUS_DIGEST)
        encoded_state = CONTROLLER.canonical_state(candidate)
        self.assertTrue(encoded_state.endswith(b"\n"))
        self.assertEqual(
            CONTROLLER.state_from_value(
                json.loads(encoded_state),
                "candidate state",
            ),
            candidate,
        )

        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=previous,
            previous_target=CONTROLLER.release_target(previous),
            expected_target=CONTROLLER.release_target(candidate),
            phase="migration-running",
        )
        encoded_transaction = CONTROLLER.canonical_transaction(transaction)
        self.assertEqual(
            CONTROLLER.transaction_from_value(
                json.loads(encoded_transaction),
                "deployment transaction",
            ),
            transaction,
        )
        tampered = json.loads(encoded_transaction)
        tampered["unknown"] = True
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "invalid shape",
        ):
            CONTROLLER.transaction_from_value(tampered, "deployment transaction")

    def test_disabled_application_stops_before_runtime_and_network(self):
        disabled = types.SimpleNamespace(name="surplasse", enabled=False)
        contract = types.SimpleNamespace(applications=(disabled,))
        release_reference = (
            "ghcr.io/nclsppr/surplasse/application-release@" + DIGEST
        )
        with (
            mock.patch.object(CONTROLLER, "require_protected_file"),
            mock.patch.object(
                CONTROLLER,
                "load_production_contract",
                return_value=contract,
            ),
            mock.patch.object(CONTROLLER, "validate_runtime") as runtime,
            mock.patch.object(
                CONTROLLER,
                "fetch_and_validate_candidate",
            ) as fetch,
            mock.patch.object(
                CONTROLLER,
                "assert_exact_source_head",
            ) as source_head,
            mock.patch.object(
                CONTROLLER.STATIC,
                "fetch_github_trusted_root_isolated",
            ) as trusted_root,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "disabled",
            ):
                CONTROLLER.deploy(
                    "surplasse",
                    REVISION,
                    release_reference,
                    activate_live=True,
                )
        runtime.assert_not_called()
        fetch.assert_not_called()
        source_head.assert_not_called()
        trusted_root.assert_not_called()

    def test_static_and_application_deployments_share_one_lock(self):
        self.assertEqual(
            CONTROLLER.LOCK_PATH,
            Path("/run/lock/vps-static.lock"),
        )

    def test_application_runtime_reestablishes_shared_worker_preconditions(self):
        with (
            mock.patch.object(CONTROLLER.STATIC, "validate_runtime") as shared,
            mock.patch.object(CONTROLLER.os, "geteuid", return_value=0),
            mock.patch.object(CONTROLLER.os, "getegid", return_value=0),
            mock.patch.object(CONTROLLER, "require_protected_directory"),
            mock.patch.object(CONTROLLER, "require_protected_file"),
        ):
            CONTROLLER.validate_runtime()
        shared.assert_called_once_with()

    def test_candidate_routes_exact_attestation_subject_media_types(self):
        profile = CONTROLLER.PROFILES["parkventory"]
        release_manifest = b"release manifest"
        release_digest = CONTROLLER.content_digest(release_manifest)
        release_reference = f"ghcr.io/nclsppr/parkventory/application-release@{release_digest}"
        candidate = CONTROLLER.ApplicationState(
            application="parkventory",
            source_revision=REVISION,
            release_reference=release_reference,
            integration_reference=f"{profile.integration_repository}@{DIGEST}",
            component_references={
                component: f"{repository}@{DIGEST}"
                for component, repository in profile.component_repositories.items()
            },
            migration_inventory_digest=DIGEST,
            probe_inventory_digest=DIGEST,
        )
        descriptor = types.SimpleNamespace(digest=DIGEST, size=1)
        index = types.SimpleNamespace(runtime_manifest=descriptor)
        integration = types.SimpleNamespace(
            archive=descriptor,
            inventory=descriptor,
            created="2026-08-17T00:00:00Z",
        )
        policy = types.SimpleNamespace(
            name="parkventory",
            release_repository="ghcr.io/nclsppr/parkventory/application-release",
        )
        bundle = types.SimpleNamespace()
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            with (
                mock.patch.object(
                    CONTROLLER,
                    "_fetch_reference",
                    side_effect=(
                        release_manifest,
                        b"backend index",
                        b"backend runtime",
                        b"frontend index",
                        b"frontend runtime",
                        b"integration manifest",
                    ),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "_fetch_blob",
                    side_effect=(
                        b"release descriptor",
                        b"backend config",
                        b"frontend config",
                        b"integration archive",
                        b"integration inventory",
                    ),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "validate_release_manifest",
                    return_value=descriptor,
                ),
                mock.patch.object(CONTROLLER, "validate_release_descriptor"),
                mock.patch.object(
                    CONTROLLER,
                    "_state_from_descriptor",
                    return_value=candidate,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "validate_component_index",
                    return_value=index,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "validate_runtime_manifest",
                    return_value=descriptor,
                ),
                mock.patch.object(CONTROLLER, "validate_image_config"),
                mock.patch.object(
                    CONTROLLER,
                    "validate_integration_manifest",
                    return_value=integration,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "validate_bundle",
                    return_value=bundle,
                ),
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "fetch_github_trusted_root_isolated",
                    return_value=work / "trusted-root.json",
                ),
                mock.patch.object(CONTROLLER.STATIC, "unlink_trusted_file"),
                mock.patch.object(CONTROLLER, "_verify_provenance") as provenance,
            ):
                resolved, actual_bundle, _evidence = (
                    CONTROLLER.fetch_and_validate_candidate(
                        policy,
                        profile,
                        REVISION,
                        release_reference,
                        work,
                    )
                )
        self.assertEqual(resolved, candidate)
        self.assertIs(actual_bundle, bundle)
        self.assertEqual(
            [call.args[-2:] for call in provenance.call_args_list],
            [
                (CONTROLLER.STATIC.OCI_MANIFEST_MEDIA_TYPE, CONTROLLER.MAX_RELEASE_BYTES),
                (
                    CONTROLLER.STATIC.OCI_INDEX_MEDIA_TYPE,
                    CONTROLLER.MAX_COMPONENT_MANIFEST_BYTES,
                ),
                (
                    CONTROLLER.STATIC.OCI_INDEX_MEDIA_TYPE,
                    CONTROLLER.MAX_COMPONENT_MANIFEST_BYTES,
                ),
                (CONTROLLER.STATIC.OCI_MANIFEST_MEDIA_TYPE, CONTROLLER.MAX_MANIFEST_BYTES),
            ],
        )

    def test_shared_registry_worker_admits_only_exact_application_bounds(self):
        contracts = []
        for application, profile in CONTROLLER.PROFILES.items():
            release_repository = (
                f"ghcr.io/nclsppr/{application}/application-release"
            )
            contracts.extend(
                (
                    CONTROLLER.STATIC.RegistryFetchContract(
                        release_repository,
                        "manifest",
                        DIGEST,
                        CONTROLLER.MAX_RELEASE_BYTES,
                        None,
                    ),
                    CONTROLLER.STATIC.RegistryFetchContract(
                        release_repository,
                        "blob",
                        DIGEST,
                        CONTROLLER.MAX_RELEASE_BYTES,
                        CONTROLLER.MAX_RELEASE_BYTES,
                    ),
                    CONTROLLER.STATIC.RegistryFetchContract(
                        profile.integration_repository,
                        "manifest",
                        DIGEST,
                        CONTROLLER.MAX_MANIFEST_BYTES,
                        None,
                    ),
                    CONTROLLER.STATIC.RegistryFetchContract(
                        profile.integration_repository,
                        "blob",
                        DIGEST,
                        CONTROLLER.MAX_ARCHIVE_BYTES,
                        CONTROLLER.MAX_ARCHIVE_BYTES,
                    ),
                    CONTROLLER.STATIC.RegistryFetchContract(
                        profile.integration_repository,
                        "blob",
                        DIGEST,
                        CONTROLLER.MAX_INVENTORY_BYTES,
                        CONTROLLER.MAX_INVENTORY_BYTES,
                    ),
                )
            )
            for repository in profile.component_repositories.values():
                contracts.extend(
                    (
                        CONTROLLER.STATIC.RegistryFetchContract(
                            repository,
                            "manifest",
                            DIGEST,
                            CONTROLLER.MAX_COMPONENT_MANIFEST_BYTES,
                            None,
                        ),
                        CONTROLLER.STATIC.RegistryFetchContract(
                            repository,
                            "blob",
                            DIGEST,
                            CONTROLLER.MAX_IMAGE_CONFIG_BYTES,
                            CONTROLLER.MAX_IMAGE_CONFIG_BYTES,
                        ),
                    )
                )
        for contract in contracts:
            with self.subTest(contract=contract):
                CONTROLLER.STATIC.validate_registry_fetch_contract(contract)

        invalid = (
            CONTROLLER.STATIC.RegistryFetchContract(
                "ghcr.io/nclsppr/unknown/application-release",
                "manifest",
                DIGEST,
                CONTROLLER.MAX_RELEASE_BYTES,
                None,
            ),
            CONTROLLER.STATIC.RegistryFetchContract(
                CONTROLLER.PROFILES["surplasse"].component_repositories["backend"],
                "manifest",
                DIGEST,
                CONTROLLER.MAX_RELEASE_BYTES,
                None,
            ),
            CONTROLLER.STATIC.RegistryFetchContract(
                CONTROLLER.PROFILES["parkventory"].integration_repository,
                "blob",
                DIGEST,
                2,
                1,
            ),
            CONTROLLER.STATIC.RegistryFetchContract(
                CONTROLLER.PROFILES["parkventory"].component_repositories[
                    "backend"
                ],
                "manifest",
                DIGEST,
                CONTROLLER.MAX_COMPONENT_MANIFEST_BYTES + 1,
                None,
            ),
            CONTROLLER.STATIC.RegistryFetchContract(
                CONTROLLER.PROFILES["parkventory"].component_repositories[
                    "backend"
                ],
                "blob",
                DIGEST,
                CONTROLLER.MAX_IMAGE_CONFIG_BYTES + 1,
                CONTROLLER.MAX_IMAGE_CONFIG_BYTES + 1,
            ),
        )
        for contract in invalid:
            with self.subTest(contract=contract):
                with self.assertRaises(CONTROLLER.STATIC.StaticDeploymentError):
                    CONTROLLER.STATIC.validate_registry_fetch_contract(contract)

    def test_blob_worker_uses_the_attested_exact_size_not_a_loose_ceiling(self):
        descriptor = types.SimpleNamespace(digest=DIGEST, size=17)
        destination = Path("/unused/blob")
        with (
            mock.patch.object(CONTROLLER, "_registry_fetch") as fetch,
            mock.patch.object(
                CONTROLLER.STATIC,
                "read_bounded_file",
                return_value=b"x" * 17,
            ) as read,
        ):
            raw = CONTROLLER._fetch_blob(
                CONTROLLER.PROFILES["parkventory"].integration_repository,
                descriptor,
                destination,
                maximum_size=CONTROLLER.MAX_ARCHIVE_BYTES,
            )
        self.assertEqual(raw, b"x" * 17)
        self.assertEqual(fetch.call_args.kwargs["maximum_size"], 17)
        self.assertEqual(fetch.call_args.kwargs["expected_size"], 17)
        read.assert_called_once_with(
            destination,
            CONTROLLER.MAX_ARCHIVE_BYTES,
            "registry blob",
        )

    def test_command_output_is_killed_while_streaming_past_its_bound(self):
        command = [
            sys.executable,
            "-c",
            (
                "import os,time; "
                "[(os.write(1,b'x'*4096),time.sleep(0.001)) "
                "for _ in range(1024)]; time.sleep(30)"
            ),
        ]
        started = time.monotonic()
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "output exceeds the limit",
        ):
            CONTROLLER._run_bounded_status(
                command,
                environment={},
                timeout=10,
                maximum_stdout=32 * 1024,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_release_root_mode_ignores_a_restrictive_outer_umask(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory) / "release"
            previous_umask = os.umask(0o077)
            try:
                release.mkdir(mode=0o755)
                self.assertEqual(release.stat().st_mode & 0o777, 0o700)
                CONTROLLER.expose_release_directory(release)
            finally:
                os.umask(previous_umask)
            self.assertEqual(release.stat().st_mode & 0o777, 0o755)

    def test_recovery_removes_only_exact_protected_filesystem_residue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_root = root / "surplasse"
            releases = app_root / "releases"
            releases.mkdir(parents=True)
            app_root.chmod(0o755)
            releases.chmod(0o755)
            staging = releases / f".sha256-{'a' * 64}-{'b' * 16}"
            staging.mkdir(mode=0o700)
            (staging / "partial").write_bytes(b"partial")
            link = app_root / f".current-{'c' * 16}"
            link.symlink_to(f"releases/sha256-{'d' * 64}")
            with mock.patch.object(CONTROLLER, "APPLICATION_ROOT", root):
                CONTROLLER.cleanup_application_filesystem_residue(
                    "surplasse",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )
            self.assertFalse(staging.exists())
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())

    def test_recovery_refuses_a_symlink_disguised_as_staging_residue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_root = root / "surplasse"
            releases = app_root / "releases"
            releases.mkdir(parents=True)
            app_root.chmod(0o755)
            releases.chmod(0o755)
            outside = root / "outside"
            outside.mkdir()
            staging = releases / f".sha256-{'a' * 64}-{'b' * 16}"
            staging.symlink_to(outside, target_is_directory=True)
            with mock.patch.object(CONTROLLER, "APPLICATION_ROOT", root):
                with self.assertRaisesRegex(
                    CONTROLLER.ApplicationDeploymentError,
                    "staging residue is not one protected directory",
                ):
                    CONTROLLER.cleanup_application_filesystem_residue(
                        "surplasse",
                        expected_uid=os.geteuid(),
                        expected_gid=os.getegid(),
                    )
            self.assertTrue(staging.is_symlink())
            self.assertTrue(outside.is_dir())

    def test_existing_release_repairs_only_a_missing_external_inventory(self):
        candidate = state()
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            raw = b'{"files":[],"schema":1}\n'
            (release / "materialization.json").write_bytes(raw)
            with (
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "read_protected_state_bytes",
                    return_value=None,
                ),
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "write_protected_state_bytes",
                ) as write,
            ):
                CONTROLLER.ensure_external_inventory(release, candidate)
            self.assertEqual(write.call_args.args[2], raw)
            with (
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "read_protected_state_bytes",
                    return_value=b"different\n",
                ),
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "write_protected_state_bytes",
                ) as rewrite,
            ):
                with self.assertRaisesRegex(
                    CONTROLLER.ApplicationDeploymentError,
                    "external application inventory differs",
                ):
                    CONTROLLER.ensure_external_inventory(release, candidate)
            rewrite.assert_not_called()

    def test_empty_boot_recovery_does_not_require_attestation_runtime(self):
        with (
            mock.patch.object(CONTROLLER, "validate_recovery_state_runtime"),
            mock.patch.object(
                CONTROLLER,
                "deployment_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                CONTROLLER,
                "cleanup_application_filesystem_residue",
            ) as cleanup,
            mock.patch.object(CONTROLLER, "read_transaction", return_value=None),
            mock.patch.object(
                CONTROLLER,
                "validate_runtime",
                side_effect=AssertionError("unused attestation runtime"),
            ) as runtime,
            mock.patch.object(CONTROLLER, "recover_application") as recover,
        ):
            CONTROLLER.recover_live(None)
        runtime.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(
            cleanup.call_args_list,
            [mock.call("parkventory"), mock.call("surplasse")],
        )

    def test_boot_recovery_with_a_journal_requires_the_full_runtime(self):
        transaction = types.SimpleNamespace()
        with (
            mock.patch.object(CONTROLLER, "validate_recovery_state_runtime"),
            mock.patch.object(
                CONTROLLER,
                "deployment_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(CONTROLLER, "cleanup_application_filesystem_residue"),
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                side_effect=(transaction, None),
            ),
            mock.patch.object(CONTROLLER, "validate_runtime") as runtime,
            mock.patch.object(CONTROLLER, "recover_application") as recover,
        ):
            CONTROLLER.recover_live(None)
        runtime.assert_called_once_with()
        self.assertEqual(
            recover.call_args_list,
            [mock.call("parkventory"), mock.call("surplasse")],
        )

    def test_parkventory_static_owner_refuses_compose_before_state_changes(self):
        candidate = state("parkventory")
        with (
            mock.patch.object(CONTROLLER, "recover_application"),
            mock.patch.object(
                CONTROLLER,
                "static_parkventory_owner_present",
                return_value=True,
            ),
            mock.patch.object(CONTROLLER, "read_state") as read_state,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "static state still owns",
            ):
                CONTROLLER.prepare_transaction(candidate)
        read_state.assert_not_called()

    def test_parkventory_static_owner_inspection_fails_closed(self):
        with mock.patch.object(
            CONTROLLER.Path,
            "lstat",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "cannot inspect Parkventory static ownership",
            ):
                CONTROLLER.static_parkventory_owner_present()

    def test_parkventory_static_transaction_alone_blocks_compose(self):
        candidate = state("parkventory")
        with (
            mock.patch.object(CONTROLLER, "recover_application"),
            mock.patch.object(
                CONTROLLER.Path,
                "lstat",
                side_effect=(FileNotFoundError(), object()),
            ) as lstat,
            mock.patch.object(CONTROLLER, "read_state") as read_state,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "static state still owns",
            ):
                CONTROLLER.prepare_transaction(candidate)
        self.assertEqual(lstat.call_count, 2)
        read_state.assert_not_called()

    def test_parkventory_static_owner_race_fails_without_touching_a_transaction(self):
        candidate = state("parkventory")
        with (
            mock.patch.object(CONTROLLER, "recover_application"),
            mock.patch.object(
                CONTROLLER,
                "static_parkventory_owner_present",
                side_effect=(False, True),
            ),
            mock.patch.object(CONTROLLER, "read_state", return_value=None),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "static state still owns",
            ):
                CONTROLLER.prepare_transaction(candidate)

    def test_candidate_must_descend_from_active_before_activation(self):
        candidate = state()
        previous = state(revision=PREVIOUS_REVISION, digest=PREVIOUS_DIGEST)
        reads = iter((None, previous))
        with (
            mock.patch.object(CONTROLLER, "recover_application"),
            mock.patch.object(
                CONTROLLER,
                "static_parkventory_owner_present",
                return_value=False,
            ),
            mock.patch.object(
                CONTROLLER,
                "read_state",
                side_effect=lambda *_args, **_kwargs: next(reads),
            ),
            mock.patch.object(
                CONTROLLER,
                "current_target",
                return_value=CONTROLLER.release_target(previous),
            ),
            mock.patch.object(CONTROLLER, "validate_materialized_release"),
            mock.patch.object(
                CONTROLLER,
                "assert_source_revision_descends_from_active",
            ) as ancestry,
        ):
            proceed, active = CONTROLLER.prepare_transaction(candidate)
        self.assertTrue(proceed)
        self.assertEqual(active, previous)
        ancestry.assert_called_once_with(
            CONTROLLER.PROFILES["surplasse"],
            PREVIOUS_REVISION,
            REVISION,
        )

    def test_source_ancestry_uses_the_bounded_shared_worker(self):
        worker_state = object()
        with (
            mock.patch.object(
                CONTROLLER.STATIC,
                "run_isolated_worker",
                return_value=worker_state,
            ) as run_worker,
            mock.patch.object(
                CONTROLLER.STATIC,
                "cleanup_isolated_worker_state",
            ) as cleanup,
        ):
            CONTROLLER.assert_source_revision_descends_from_active(
                CONTROLLER.PROFILES["surplasse"],
                PREVIOUS_REVISION,
                REVISION,
            )
        arguments = run_worker.call_args
        self.assertEqual(arguments.args[0], "appancestry")
        self.assertIn("--source-ancestry-worker", arguments.args[1])
        self.assertTrue(arguments.kwargs["network"])
        cleanup.assert_called_once_with(worker_state)

    def test_public_edge_cutover_must_match_bundle_and_network(self):
        candidate = state("parkventory")
        route = b"parkventory.com { respond ok }\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            edge = Path(temporary_directory)
            route_root = edge / "routes"
            route_root.mkdir()
            (route_root / "parkventory.caddy").write_bytes(route)
            completed = subprocess.CompletedProcess(
                [],
                0,
                "running\thealthy\tvps-public-static-edge\tattached\n",
                "",
            )
            with (
                mock.patch.object(
                    CONTROLLER,
                    "PUBLIC_EDGE_RUNTIME_ROOT",
                    edge,
                ),
                mock.patch.object(CONTROLLER, "require_protected_file"),
                mock.patch.object(CONTROLLER, "state_release", return_value=edge),
                mock.patch.object(
                    CONTROLLER,
                    "bundle_from_release",
                    return_value=types.SimpleNamespace(
                        files={"caddy/parkventory.caddy": route},
                        probes={
                            "public": [
                                {
                                    "body_contains": "parkventory-compose-v1",
                                    "path": "/.well-known/parkventory-release",
                                    "status": 200,
                                }
                            ]
                        },
                    ),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "_run_bounded",
                    return_value=completed,
                ) as inspect,
                mock.patch.object(CONTROLLER, "_probe_http") as probe,
            ):
                CONTROLLER.validate_public_edge_cutover(candidate, edge)
        command = inspect.call_args.args[0]
        self.assertIn("app_parkventory", command[-2])
        self.assertEqual(command[-1], CONTROLLER.PUBLIC_EDGE_CONTAINER)
        probe.assert_called_once_with(
            "https://parkventory.com/.well-known/parkventory-release",
            expected_status=200,
            expected_body="parkventory-compose-v1",
            work=edge,
            resolve_host="parkventory.com",
        )

    def test_http_probe_bounds_the_download_before_reading_it(self):
        completed = subprocess.CompletedProcess([], 0, "200", "")
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            with (
                mock.patch.object(
                    CONTROLLER,
                    "_run_bounded",
                    return_value=completed,
                ) as bounded,
                mock.patch.object(
                    CONTROLLER.STATIC,
                    "read_bounded_file",
                    return_value=b"ready",
                ),
            ):
                CONTROLLER._probe_http(
                    "http://backend/health",
                    expected_status=200,
                    expected_body="ready",
                    work=work,
                )
        command = bounded.call_args.args[0]
        self.assertEqual(
            command[command.index("--max-filesize") + 1],
            str(CONTROLLER.MAX_PROBE_BODY_BYTES),
        )

    def test_public_runtime_probes_are_pinned_to_the_local_edge(self):
        candidate = state("parkventory")
        bundle = types.SimpleNamespace(
            probes={
                "internal": [],
                "public": [
                    {
                        "host": "parkventory.com",
                        "path": "/app",
                        "status": 200,
                        "body_contains": "Parkventory",
                    }
                ],
            }
        )
        with (
            mock.patch.object(CONTROLLER, "state_release", return_value=Path("/release")),
            mock.patch.object(CONTROLLER, "bundle_from_release", return_value=bundle),
            mock.patch.object(CONTROLLER, "_probe_http") as probe,
        ):
            CONTROLLER.probe_runtime(candidate, Path("/work"))
        probe.assert_called_once_with(
            "https://parkventory.com/app",
            expected_status=200,
            expected_body="Parkventory",
            work=Path("/work"),
            resolve_host="parkventory.com",
        )

    def test_compose_migrator_and_runtime_auto_migration_are_exact(self):
        profile = CONTROLLER.PROFILES["parkventory"]
        service_credentials = {
            service: [
                {
                    "source": source,
                    "target": f"/run/secrets/{source}",
                }
                for source in sources
            ]
            for service, sources in profile.service_credentials.items()
        }
        rendered = {
            "services": {
                "backend": {
                    "environment": {
                        "PARKVENTORY_MIGRATION_ONLY": "false",
                        "QUARKUS_FLYWAY_MIGRATE_AT_START": "false",
                    },
                    "secrets": service_credentials["backend"],
                    "user": "10001:10001",
                },
                "frontend": {"secrets": service_credentials["frontend"]},
                "migrator": {
                    "entrypoint": [profile.migration_runner],
                    "secrets": service_credentials["migrator"],
                    "user": "10001:10001",
                },
            }
        }
        CONTROLLER.validate_application_compose_semantics(profile, rendered)
        rendered["services"]["migrator"]["entrypoint"] = ["/bin/true"]
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "entrypoint differs",
        ):
            CONTROLLER.validate_application_compose_semantics(profile, rendered)
        rendered["services"]["migrator"]["entrypoint"] = [
            profile.migration_runner
        ]
        rendered["services"]["backend"]["environment"][
            "QUARKUS_FLYWAY_MIGRATE_AT_START"
        ] = "true"
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "automatic migrations",
        ):
            CONTROLLER.validate_application_compose_semantics(profile, rendered)

    def test_service_secret_allocation_is_exact(self):
        profile = CONTROLLER.PROFILES["parkventory"]
        rendered = {
            "services": {
                service: {
                    "secrets": [
                        {
                            "source": source,
                            "target": f"/run/secrets/{source}",
                        }
                        for source in sources
                    ]
                }
                for service, sources in profile.service_credentials.items()
            }
        }
        rendered["services"]["backend"]["environment"] = {
            "PARKVENTORY_MIGRATION_ONLY": "false",
            "QUARKUS_FLYWAY_MIGRATE_AT_START": "false",
        }
        rendered["services"]["migrator"]["entrypoint"] = [
            profile.migration_runner
        ]
        rendered["services"]["backend"]["user"] = "10001:10001"
        rendered["services"]["migrator"]["user"] = "10001:10001"
        CONTROLLER.validate_application_compose_semantics(profile, rendered)
        rendered["services"]["frontend"]["secrets"] = [
            {
                "source": "parkventory_postgres_migrator_password",
                "target": "/run/secrets/parkventory_postgres_migrator_password",
            }
        ]
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "least-privilege profile",
        ):
            CONTROLLER.validate_application_compose_semantics(profile, rendered)

    def test_file_secrets_are_host_private_and_container_readable(self):
        profile = CONTROLLER.PROFILES["parkventory"]
        rendered = {
            "secrets": {
                name: {"file": path}
                for name, path in profile.credential_files.items()
            }
        }
        with (
            mock.patch.object(
                CONTROLLER,
                "require_protected_directory",
            ) as directory,
            mock.patch.object(CONTROLLER, "require_protected_file") as secret_file,
        ):
            CONTROLLER.validate_secret_metadata(profile, rendered)
        self.assertEqual(
            directory.call_args_list,
            [
                mock.call(CONTROLLER.CREDENTIAL_ROOT, "application secret root"),
                mock.call(
                    CONTROLLER.CREDENTIAL_ROOT / "parkventory",
                    "parkventory secret directory",
                ),
            ],
        )
        self.assertEqual(secret_file.call_count, len(profile.credential_files))
        for call in secret_file.call_args_list:
            self.assertEqual(call.kwargs["allowed_modes"], frozenset({0o440}))
            self.assertEqual(call.kwargs["expected_gid"], 10001)

    def test_precommit_override_changes_only_runtime_restart_policy(self):
        profile = CONTROLLER.PROFILES["parkventory"]
        rendered = {
            "name": "parkventory",
            "services": {
                "backend": {"image": "backend", "restart": "unless-stopped"},
                "frontend": {"image": "frontend", "restart": "unless-stopped"},
                "migrator": {"image": "backend", "restart": "no"},
            },
        }
        candidate = copy.deepcopy(rendered)
        candidate["services"]["backend"]["restart"] = "no"
        candidate["services"]["frontend"]["restart"] = "no"
        CONTROLLER.validate_candidate_restart_config(
            profile,
            rendered,
            candidate,
        )
        candidate["services"]["backend"]["image"] = "mutable:latest"
        with self.assertRaisesRegex(
            CONTROLLER.ApplicationDeploymentError,
            "more than runtime restart policy",
        ):
            CONTROLLER.validate_candidate_restart_config(
                profile,
                rendered,
                candidate,
            )

    def test_every_runtime_compose_command_replays_current_policy(self):
        candidate = state()
        profile = CONTROLLER.PROFILES["surplasse"]
        bundle = types.SimpleNamespace()
        environment = {"PATH": "/usr/bin"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                CONTROLLER,
                "validate_materialized_runtime_policy",
                return_value=(profile, bundle, environment),
            ) as validate,
            mock.patch.object(CONTROLLER, "state_release", return_value=Path("/release")),
            mock.patch.object(
                CONTROLLER,
                "_run_bounded",
                return_value=completed,
            ) as bounded,
        ):
            result = CONTROLLER._compose_for_state(
                candidate,
                ["ps", "--quiet", "backend"],
                timeout=30,
            )
        self.assertIs(result, completed)
        validate.assert_called_once_with(candidate)
        self.assertEqual(bounded.call_args.kwargs["environment"], environment)

    def test_materialized_bundle_replays_current_inventory_policy(self):
        candidate = state("parkventory")
        archive = b"archive"
        inventory = b"inventory"
        manifest = b"manifest"
        descriptor_archive = types.SimpleNamespace(
            digest=CONTROLLER.content_digest(archive),
            size=len(archive),
        )
        descriptor_inventory = types.SimpleNamespace(
            digest=CONTROLLER.content_digest(inventory),
            size=len(inventory),
        )
        integration = types.SimpleNamespace(
            archive=descriptor_archive,
            inventory=descriptor_inventory,
            created="2026-08-17T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            evidence = release / "evidence"
            evidence.mkdir()
            (evidence / "integration-manifest.json").write_bytes(manifest)
            (evidence / "integration.tar.gz").write_bytes(archive)
            (evidence / "inventory.json").write_bytes(inventory)
            with (
                mock.patch.object(CONTROLLER, "require_protected_file"),
                mock.patch.object(
                    CONTROLLER,
                    "validate_integration_manifest",
                    return_value=integration,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "validate_bundle",
                    side_effect=CONTROLLER.ApplicationBundleError(
                        "probe inventory violates current profile"
                    ),
                ) as validate,
            ):
                with self.assertRaisesRegex(
                    CONTROLLER.ApplicationBundleError,
                    "violates current profile",
                ):
                    CONTROLLER.bundle_from_release(release, candidate)
        self.assertEqual(validate.call_args.kwargs["revision"], REVISION)
        self.assertEqual(
            validate.call_args.kwargs["probe_inventory_digest"],
            candidate.probe_inventory_digest,
        )

    def test_compose_environment_uses_the_release_configuration_snapshot(self):
        candidate = state("parkventory")
        profile = CONTROLLER.PROFILES["parkventory"]
        configuration = {
            key: f"value-{index}"
            for index, key in enumerate(profile.runtime_configuration_keys)
        }
        bundle = types.SimpleNamespace(contract={})
        with (
            mock.patch.object(
                CONTROLLER,
                "runtime_configuration",
                side_effect=AssertionError("host configuration must not be reread"),
            ) as host_configuration,
            mock.patch.object(
                CONTROLLER,
                "image_environment",
                return_value={"PARKVENTORY_BACKEND_IMAGE": "image@sha256"},
            ),
        ):
            environment = CONTROLLER.compose_environment(
                profile,
                bundle,
                candidate,
                Path("/release"),
                configuration,
            )
        host_configuration.assert_not_called()
        for key, value in configuration.items():
            self.assertEqual(environment[key], value)
        prefix = CONTROLLER.compose_prefix(Path("/release"), profile)
        self.assertEqual(
            prefix[prefix.index("--env-file") + 1],
            "/release/runtime.env",
        )
        self.assertNotIn(
            str(CONTROLLER.RUNTIME_CONFIG_ROOT / "parkventory.env"),
            prefix,
        )

    def test_migration_has_one_deterministic_container_identity(self):
        candidate = state("parkventory")
        expected_name = (
            "vps-application-parkventory-migrator-" + "a" * 64
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "require_migration_container_absent",
            ) as absent,
            mock.patch.object(CONTROLLER, "_compose_for_state") as compose,
        ):
            CONTROLLER.run_migration(candidate)
        self.assertEqual(CONTROLLER.migration_container_name(candidate), expected_name)
        self.assertEqual(absent.call_count, 2)
        compose.assert_called_once_with(
            candidate,
            [
                "run",
                "--rm",
                "--name",
                expected_name,
                "--no-deps",
                "--pull",
                "never",
                "migrator",
            ],
            timeout=900,
        )

    def test_recovery_stops_only_the_exact_journaled_migrator(self):
        candidate = state()
        container_id = "c" * 64
        name = CONTROLLER.migration_container_name(candidate)
        image = candidate.component_references["backend"]
        inspected = subprocess.CompletedProcess(
            [],
            0,
            (
                f"/{name}\t{container_id}\t{image}\tsurplasse\t"
                "migrator\tTrue\n"
            ),
            "",
        )
        removed = subprocess.CompletedProcess([], 0, f"{container_id}\n", "")
        with (
            mock.patch.object(
                CONTROLLER,
                "migration_container_id",
                side_effect=(container_id, None, None),
            ),
            mock.patch.object(
                CONTROLLER,
                "_run_bounded_status",
                side_effect=(inspected, removed),
            ) as bounded,
            mock.patch.object(CONTROLLER.time, "sleep") as sleep,
        ):
            CONTROLLER.remove_migration_container(candidate)
        self.assertEqual(bounded.call_args_list[0].args[0][1:3], ["container", "inspect"])
        self.assertEqual(
            bounded.call_args_list[1].args[0][1:5],
            ["container", "rm", "--force", "--volumes"],
        )
        sleep.assert_called_once_with(0.5)

    def test_recovery_refuses_an_unrelated_container_with_the_same_name(self):
        candidate = state()
        container_id = "c" * 64
        inspected = subprocess.CompletedProcess(
            [],
            0,
            f"/wrong\t{container_id}\tbusybox:latest\tother\tworker\tFalse\n",
            "",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "migration_container_id",
                return_value=container_id,
            ),
            mock.patch.object(
                CONTROLLER,
                "_run_bounded_status",
                return_value=inspected,
            ) as bounded,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "identity differs",
            ):
                CONTROLLER.remove_migration_container(candidate)
        self.assertEqual(bounded.call_count, 1)

    def test_recovery_accepts_auto_removal_between_list_and_inspect(self):
        candidate = state()
        container_id = "c" * 64
        disappeared = subprocess.CompletedProcess([], 1, "", "No such container")
        with (
            mock.patch.object(
                CONTROLLER,
                "migration_container_id",
                side_effect=(container_id, None, None),
            ),
            mock.patch.object(
                CONTROLLER,
                "_run_bounded_status",
                return_value=disappeared,
            ),
            mock.patch.object(CONTROLLER.time, "sleep"),
        ):
            CONTROLLER.remove_migration_container(candidate)

    def test_recovery_accepts_auto_removal_between_inspect_and_remove(self):
        candidate = state()
        container_id = "c" * 64
        name = CONTROLLER.migration_container_name(candidate)
        image = candidate.component_references["backend"]
        inspected = subprocess.CompletedProcess(
            [],
            0,
            (
                f"/{name}\t{container_id}\t{image}\tsurplasse\t"
                "migrator\tTrue\n"
            ),
            "",
        )
        disappeared = subprocess.CompletedProcess([], 1, "", "No such container")
        with (
            mock.patch.object(
                CONTROLLER,
                "migration_container_id",
                side_effect=(container_id, None, None),
            ),
            mock.patch.object(
                CONTROLLER,
                "_run_bounded_status",
                side_effect=(inspected, disappeared),
            ),
            mock.patch.object(CONTROLLER.time, "sleep"),
        ):
            CONTROLLER.remove_migration_container(candidate)

    def test_public_edge_mismatch_refuses_before_docker_or_migration(self):
        candidate = state("parkventory")
        with tempfile.TemporaryDirectory() as temporary_directory:
            edge = Path(temporary_directory)
            route_root = edge / "routes"
            route_root.mkdir()
            (route_root / "parkventory.caddy").write_bytes(b"old route\n")
            with (
                mock.patch.object(
                    CONTROLLER,
                    "PUBLIC_EDGE_RUNTIME_ROOT",
                    edge,
                ),
                mock.patch.object(CONTROLLER, "require_protected_file"),
                mock.patch.object(CONTROLLER, "state_release", return_value=edge),
                mock.patch.object(
                    CONTROLLER,
                    "bundle_from_release",
                    return_value=types.SimpleNamespace(
                        files={
                            "caddy/parkventory.caddy": b"parkventory.com {}\n"
                        },
                        probes={"public": []},
                    ),
                ),
                mock.patch.object(CONTROLLER, "_run_bounded") as inspect,
            ):
                with self.assertRaisesRegex(
                    CONTROLLER.ApplicationDeploymentError,
                    "not the attested application route",
                ):
                    CONTROLLER.validate_public_edge_cutover(candidate, edge)
        inspect.assert_not_called()

    def test_edge_preflight_failure_happens_before_dedicated_migration(self):
        candidate = state()
        with (
            mock.patch.object(CONTROLLER, "validate_materialized_runtime_policy"),
            mock.patch.object(
                CONTROLLER,
                "prepare_transaction",
                return_value=(True, None),
            ),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
            mock.patch.object(CONTROLLER, "write_transaction"),
            mock.patch.object(CONTROLLER, "pull_and_verify_images"),
            mock.patch.object(
                CONTROLLER,
                "validate_public_edge_cutover",
                side_effect=CONTROLLER.ApplicationDeploymentError("edge not ready"),
            ),
            mock.patch.object(CONTROLLER, "run_migration") as migrate,
            mock.patch.object(CONTROLLER, "read_transaction", return_value=None),
            mock.patch.object(CONTROLLER, "recover_application"),
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "edge not ready",
            ):
                CONTROLLER.activate_candidate(candidate, Path("/unused"))
        migrate.assert_not_called()

    def test_candidate_runtime_is_non_restarting_until_probed_commit(self):
        candidate = state()
        with (
            mock.patch.object(CONTROLLER, "validate_materialized_runtime_policy"),
            mock.patch.object(
                CONTROLLER,
                "prepare_transaction",
                return_value=(True, None),
            ),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
            mock.patch.object(CONTROLLER, "write_transaction"),
            mock.patch.object(CONTROLLER, "pull_and_verify_images"),
            mock.patch.object(CONTROLLER, "validate_public_edge_cutover"),
            mock.patch.object(CONTROLLER, "run_migration"),
            mock.patch.object(CONTROLLER, "start_runtime") as start,
            mock.patch.object(CONTROLLER, "probe_runtime"),
            mock.patch.object(CONTROLLER, "assert_exact_source_head"),
            mock.patch.object(CONTROLLER, "commit_probed_candidate") as commit,
        ):
            CONTROLLER.activate_candidate(candidate, Path("/unused"))
        start.assert_called_once_with(candidate, precommit=True)
        committed_transaction = commit.call_args.args[0]
        self.assertEqual(committed_transaction.phase, "probed")

    def test_restart_promotion_preserves_exact_container_identities(self):
        candidate = state("parkventory")
        backend_id = "b" * 64
        frontend_id = "f" * 64

        def docker_command(command, **_kwargs):
            container_id = command[-1]
            if command[2:4] == ["update", "--restart"]:
                return subprocess.CompletedProcess(command, 0, f"{container_id}\n", "")
            if command[2:4] == ["inspect", "--format"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"{container_id}\tunless-stopped\n",
                    "",
                )
            self.fail(f"unexpected Docker command: {command}")

        with (
            mock.patch.object(
                CONTROLLER,
                "runtime_container_id",
                side_effect=(backend_id, backend_id, frontend_id, frontend_id),
            ),
            mock.patch.object(
                CONTROLLER,
                "validate_runtime_container_identity",
            ) as identity,
            mock.patch.object(
                CONTROLLER,
                "_run_bounded",
                side_effect=docker_command,
            ) as bounded,
        ):
            CONTROLLER.promote_runtime_restart_policy(candidate)
        self.assertEqual(
            identity.call_args_list,
            [
                mock.call(candidate, "backend", backend_id),
                mock.call(candidate, "backend", backend_id),
                mock.call(candidate, "frontend", frontend_id),
                mock.call(candidate, "frontend", frontend_id),
            ],
        )
        commands = [call.args[0] for call in bounded.call_args_list]
        self.assertEqual(sum("update" in command for command in commands), 2)
        self.assertFalse(any("compose" in command for command in commands))

    def test_ambiguous_commit_error_is_success_only_after_exact_recovery(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(CONTROLLER, "write_transaction"),
            mock.patch.object(CONTROLLER, "switch_current"),
            mock.patch.object(CONTROLLER, "write_state"),
            mock.patch.object(CONTROLLER, "start_runtime"),
            mock.patch.object(CONTROLLER, "promote_runtime_restart_policy"),
            mock.patch.object(
                CONTROLLER,
                "remove_state",
                side_effect=OSError("journal fsync failed"),
            ),
            mock.patch.object(CONTROLLER, "recover_application") as recover,
            mock.patch.object(
                CONTROLLER,
                "application_tuple_is_active",
                return_value=True,
            ) as active,
        ):
            CONTROLLER.commit_probed_candidate(transaction)
        recover.assert_called_once_with("surplasse")
        active.assert_called_once_with(transaction)

    def test_ambiguous_commit_error_remains_failure_without_exact_tuple(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "write_transaction",
                side_effect=OSError("journal fsync failed"),
            ),
            mock.patch.object(CONTROLLER, "start_runtime"),
            mock.patch.object(CONTROLLER, "promote_runtime_restart_policy"),
            mock.patch.object(CONTROLLER, "recover_application"),
            mock.patch.object(
                CONTROLLER,
                "application_tuple_is_active",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(OSError, "journal fsync failed"):
                CONTROLLER.commit_probed_candidate(transaction)

    def test_recovery_commits_only_a_probed_candidate(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        events = []
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "read_state", return_value=None),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
            mock.patch.object(CONTROLLER, "validate_materialized_release"),
            mock.patch.object(CONTROLLER, "remove_migration_container") as migrator,
            mock.patch.object(
                CONTROLLER,
                "start_runtime",
                side_effect=lambda *_args, **_kwargs: events.append("start"),
            ) as start,
            mock.patch.object(
                CONTROLLER,
                "switch_current",
                side_effect=lambda *_args, **_kwargs: events.append("switch"),
            ) as switch,
            mock.patch.object(
                CONTROLLER,
                "write_state",
                side_effect=lambda *_args, **_kwargs: events.append("state"),
            ) as write_state,
            mock.patch.object(
                CONTROLLER,
                "promote_runtime_restart_policy",
                side_effect=lambda *_args, **_kwargs: events.append("promote"),
            ) as promote,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
            mock.patch.object(CONTROLLER, "restore_previous") as restore,
        ):
            CONTROLLER.recover_application("surplasse")
        switch.assert_called_once()
        write_state.assert_called_once()
        remove.assert_called_once()
        restore.assert_not_called()
        migrator.assert_called_once_with(candidate)
        start.assert_called_once_with(candidate, precommit=True)
        promote.assert_called_once_with(candidate)
        self.assertEqual(events, ["start", "switch", "state", "promote"])

    def test_recovery_keeps_a_probed_journal_when_runtime_cannot_start(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "read_state", return_value=None),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
            mock.patch.object(CONTROLLER, "validate_materialized_release"),
            mock.patch.object(CONTROLLER, "remove_migration_container"),
            mock.patch.object(
                CONTROLLER,
                "start_runtime",
                side_effect=CONTROLLER.ApplicationDeploymentError("not healthy"),
            ),
            mock.patch.object(CONTROLLER, "switch_current") as switch,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "not healthy",
            ):
                CONTROLLER.recover_application("surplasse")
        switch.assert_not_called()
        remove.assert_not_called()

    def test_probed_recovery_refuses_an_unreachable_active_tuple(self):
        candidate = state()
        previous = state(revision=PREVIOUS_REVISION, digest=PREVIOUS_DIGEST)
        unexpected = state(
            revision="c" * 40,
            digest="sha256:" + "c" * 64,
        )
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=previous,
            previous_target=CONTROLLER.release_target(previous),
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "remove_migration_container"),
            mock.patch.object(CONTROLLER, "read_state", return_value=unexpected),
            mock.patch.object(
                CONTROLLER,
                "current_target",
                return_value=CONTROLLER.release_target(unexpected),
            ),
            mock.patch.object(CONTROLLER, "start_runtime") as start,
            mock.patch.object(CONTROLLER, "switch_current") as switch,
            mock.patch.object(CONTROLLER, "write_state") as write,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "unexpected active tuple",
            ):
                CONTROLLER.recover_application("surplasse")
        start.assert_not_called()
        switch.assert_not_called()
        write.assert_not_called()
        remove.assert_not_called()

    def test_recovery_reconciles_an_already_committed_candidate(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "read_state", return_value=candidate),
            mock.patch.object(
                CONTROLLER,
                "current_target",
                return_value=CONTROLLER.release_target(candidate),
            ),
            mock.patch.object(CONTROLLER, "validate_materialized_release"),
            mock.patch.object(CONTROLLER, "remove_migration_container"),
            mock.patch.object(CONTROLLER, "start_runtime") as start,
            mock.patch.object(
                CONTROLLER,
                "promote_runtime_restart_policy",
            ) as promote,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
            mock.patch.object(CONTROLLER, "switch_current") as switch,
        ):
            CONTROLLER.recover_application("surplasse")
        start.assert_called_once_with(candidate, precommit=True)
        promote.assert_called_once_with(candidate)
        remove.assert_called_once()
        switch.assert_not_called()

    def test_recovery_refuses_an_active_tuple_from_an_unprobed_phase(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="started",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "remove_migration_container"),
            mock.patch.object(CONTROLLER, "read_state", return_value=candidate),
            mock.patch.object(
                CONTROLLER,
                "current_target",
                return_value=CONTROLLER.release_target(candidate),
            ),
            mock.patch.object(CONTROLLER, "start_runtime") as start,
            mock.patch.object(
                CONTROLLER,
                "promote_runtime_restart_policy",
            ) as promote,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "unprobed application transaction",
            ):
                CONTROLLER.recover_application("surplasse")
        start.assert_not_called()
        promote.assert_not_called()
        remove.assert_not_called()

    def test_recovery_rolls_back_and_quarantines_started_candidate(self):
        candidate = state()
        previous = state(revision=PREVIOUS_REVISION, digest=PREVIOUS_DIGEST)
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=previous,
            previous_target=CONTROLLER.release_target(previous),
            expected_target=CONTROLLER.release_target(candidate),
            phase="started",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "read_state", return_value=previous),
            mock.patch.object(
                CONTROLLER,
                "current_target",
                return_value=CONTROLLER.release_target(previous),
            ),
            mock.patch.object(CONTROLLER, "restore_previous") as restore,
            mock.patch.object(CONTROLLER, "remove_migration_container") as migrator,
            mock.patch.object(CONTROLLER, "write_state") as quarantine,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
        ):
            CONTROLLER.recover_application("surplasse")
        restore.assert_called_once_with(transaction)
        migrator.assert_called_once_with(candidate)
        self.assertEqual(quarantine.call_args.args[0], CONTROLLER.QUARANTINE_ROOT)
        self.assertEqual(quarantine.call_args.args[2], candidate)
        remove.assert_called_once()

    def test_prepared_recovery_refuses_a_migrator_residue(self):
        candidate = state()
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="prepared",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(
                CONTROLLER,
                "require_migration_container_absent",
                side_effect=CONTROLLER.ApplicationDeploymentError(
                    "migration container still exists"
                ),
            ),
            mock.patch.object(CONTROLLER, "read_state") as read_state,
            mock.patch.object(CONTROLLER, "switch_current") as switch,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
        ):
            with self.assertRaisesRegex(
                CONTROLLER.ApplicationDeploymentError,
                "migration container still exists",
            ):
                CONTROLLER.recover_application("surplasse")
        read_state.assert_not_called()
        switch.assert_not_called()
        remove.assert_not_called()

    def test_parkventory_recovery_never_forward_commits_over_static_owner(self):
        candidate = state("parkventory")
        transaction = CONTROLLER.ApplicationTransaction(
            candidate=candidate,
            previous_state=None,
            previous_target=None,
            expected_target=CONTROLLER.release_target(candidate),
            phase="probed",
        )
        with (
            mock.patch.object(
                CONTROLLER,
                "read_transaction",
                return_value=transaction,
            ),
            mock.patch.object(CONTROLLER, "remove_migration_container"),
            mock.patch.object(CONTROLLER, "read_state", return_value=None),
            mock.patch.object(CONTROLLER, "current_target", return_value=None),
            mock.patch.object(
                CONTROLLER,
                "static_parkventory_owner_present",
                return_value=True,
            ),
            mock.patch.object(CONTROLLER, "restore_previous") as restore,
            mock.patch.object(CONTROLLER, "write_state") as quarantine,
            mock.patch.object(CONTROLLER, "remove_state") as remove,
            mock.patch.object(CONTROLLER, "start_runtime") as start,
            mock.patch.object(CONTROLLER, "switch_current") as switch,
        ):
            CONTROLLER.recover_application("parkventory")
        restore.assert_called_once_with(transaction)
        self.assertEqual(quarantine.call_args.args[0], CONTROLLER.QUARANTINE_ROOT)
        remove.assert_called_once()
        start.assert_not_called()
        switch.assert_not_called()

    def test_live_runtime_stays_inside_the_outer_systemd_directory(self):
        runtime = Path(
            "/run/vps-application-live-surplasse-0123456789abcdef01234567"
        )
        with (
            mock.patch.dict(
                os.environ,
                {CONTROLLER.RUNTIME_DIRECTORY_ENV: str(runtime)},
                clear=False,
            ),
            mock.patch.object(CONTROLLER, "require_protected_directory"),
        ):
            self.assertEqual(
                CONTROLLER.deployment_temporary_root("surplasse", True),
                runtime,
            )


class ApplicationLiveGateTests(unittest.TestCase):
    def test_gate_accepts_only_one_canonical_digest_request(self):
        command = (
            "deploy-application-live surplasse "
            f"{REVISION} "
            "ghcr.io/nclsppr/surplasse/application-release@"
            f"{DIGEST}\n"
        ).encode("ascii")
        self.assertEqual(
            GATE.parse_request(command),
            [
                "surplasse",
                REVISION,
                "ghcr.io/nclsppr/surplasse/application-release@" + DIGEST,
            ],
        )
        for invalid in (
            command.replace(b"@sha256:", b":latest@sha256:"),
            command.replace(b"surplasse", b"unknown"),
            command.rstrip(b"\n"),
            command.replace(b" ", b"  ", 1),
            command + b"id\n",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(GATE.GateError):
                    GATE.parse_request(invalid)

    def test_gate_builds_bounded_unit_with_automatic_recovery(self):
        arguments = [
            "parkventory",
            REVISION,
            "ghcr.io/nclsppr/parkventory/application-release@" + DIGEST,
        ]
        with mock.patch.object(
            GATE.secrets,
            "token_hex",
            return_value="0123456789abcdef01234567",
        ):
            command = GATE.build_activation_command(arguments)
        self.assertIn(
            "RuntimeDirectory=vps-application-live-parkventory-"
            "0123456789abcdef01234567",
            command,
        )
        self.assertIn(
            "ExecStopPost=/usr/local/libexec/vps/deploy-application "
            "--recover-live parkventory",
            command,
        )
        for resource_bound in (
            "MemoryMax=1G",
            "MemorySwapMax=0",
            "TasksMax=512",
            "LimitFSIZE=64M",
        ):
            self.assertIn(resource_bound, command)
        self.assertEqual(
            command[-6:],
            [
                "--",
                "/usr/local/libexec/vps/deploy-application",
                "--activate-live",
                *arguments,
            ],
        )


if __name__ == "__main__":
    unittest.main()
