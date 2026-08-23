#!/usr/bin/env python3
"""Tests for the fail-closed Parkventory PostgreSQL readiness slice."""

from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROVISIONER = load_script(
    "parkventory_postgres_provisioner",
    SCRIPTS / "provision-parkventory-postgres",
)
MATERIALIZER = load_script(
    "parkventory_secret_materializer",
    SCRIPTS / "materialize-parkventory-secrets",
)

DATABASE_CREDENTIAL_FILES = (
    "parkventory-postgres-migrator-password",
    "parkventory-postgres-runtime-password",
)
LOCAL_APPLICATION_CREDENTIAL_FILES = (
    "parkventory-oidc-state-secret",
    "parkventory-oidc-token-encryption-secret",
)
GENERATED_CREDENTIAL_FILES = (
    DATABASE_CREDENTIAL_FILES + LOCAL_APPLICATION_CREDENTIAL_FILES
)
PROVIDER_INPUT_FILES = (
    "parkventory-oidc-client-secret",
    "parkventory-smtp-username",
    "parkventory-smtp-password",
)


class ParkventoryPostgresTests(unittest.TestCase):
    def run_materializer(
        self, root: Path, mode: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_PARKVENTORY_SECRET_TESTING"] = "1"
        return subprocess.run(
            [
                str(SCRIPTS / "materialize-parkventory-secrets"),
                mode,
                "--test-root",
                str(root),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    def test_materializer_source_exec_accepts_only_real_cli_arguments(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            environment = os.environ.copy()
            environment["VPS_PARKVENTORY_SECRET_TESTING"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (SCRIPTS / "materialize-parkventory-secrets").read_text(
                        encoding="utf-8"
                    ),
                    "--dry-run",
                    "--test-root",
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["changed"])
            self.assertEqual(set(root.iterdir()), set())

    def test_generated_secrets_are_private_distinct_and_idempotent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            plan = self.run_materializer(root, "--dry-run")
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertEqual(set(root.iterdir()), set())
            self.assertTrue(json.loads(plan.stdout)["changed"])

            first = self.run_materializer(root, "--apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            values: dict[str, bytes] = {}
            for name in GENERATED_CREDENTIAL_FILES:
                path = root / name
                values[name] = path.read_bytes()
                self.assertRegex(values[name], rb"^[A-Za-z0-9_-]{64}\n$")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
                self.assertNotIn(values[name][:-1], first.stdout.encode("utf-8"))
                self.assertNotIn(
                    hashlib.sha256(values[name]).hexdigest(), first.stdout
                )
            self.assertNotEqual(
                values[DATABASE_CREDENTIAL_FILES[0]],
                values[DATABASE_CREDENTIAL_FILES[1]],
            )
            self.assertNotEqual(
                values[LOCAL_APPLICATION_CREDENTIAL_FILES[0]],
                values[LOCAL_APPLICATION_CREDENTIAL_FILES[1]],
            )
            for name in PROVIDER_INPUT_FILES:
                self.assertFalse((root / name).exists())

            manifest = root / "parkventory-database-secret-manifest.json"
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o400)
            document = json.loads(manifest.read_text(encoding="ascii"))
            self.assertEqual(
                document["contract"], "vps-infra.parkventory-database-secrets.v1"
            )
            self.assertEqual(
                document["sha256"],
                {
                    name: hashlib.sha256(values[name]).hexdigest()
                    for name in DATABASE_CREDENTIAL_FILES
                },
            )

            marker = root / "parkventory-secret-generation.json"
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o400)
            marker_document = json.loads(marker.read_text(encoding="ascii"))
            self.assertEqual(
                marker_document,
                {
                    "contract": "vps-infra.parkventory-secret-generation.v1",
                    "materializer": "materialize-parkventory-secrets",
                    "secrets": [
                        {
                            "file": "parkventory-oidc-state-secret",
                            "id": "parkventory.oidc-state-secret",
                        },
                        {
                            "file": "parkventory-oidc-token-encryption-secret",
                            "id": "parkventory.oidc-token-encryption-secret",
                        },
                        {
                            "file": "parkventory-postgres-migrator-password",
                            "id": "parkventory.postgres-migrator-password",
                        },
                        {
                            "file": "parkventory-postgres-runtime-password",
                            "id": "parkventory.postgres-runtime-password",
                        },
                    ],
                    "target_generation": 1,
                },
            )
            self.assertNotIn("sha256", marker_document)
            for value in values.values():
                self.assertNotIn(value[:-1], marker.read_bytes())
                self.assertNotIn(
                    hashlib.sha256(value).hexdigest().encode("ascii"),
                    marker.read_bytes(),
                )

            second = self.run_materializer(root, "--apply")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(json.loads(second.stdout)["changed"])
            self.assertEqual(
                values,
                {name: (root / name).read_bytes() for name in values},
            )
            checked = self.run_materializer(root, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["ready"])

    def test_generation_marker_is_published_after_the_exact_generated_set(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            published: list[str] = []
            original_publish = MATERIALIZER.publish_exclusive

            def record_publish(*args, **kwargs):
                created = original_publish(*args, **kwargs)
                if created:
                    published.append(args[1])
                return created

            try:
                MATERIALIZER.publish_exclusive = record_publish
                result = MATERIALIZER.materialize(root, "apply", production=False)
            finally:
                MATERIALIZER.publish_exclusive = original_publish

            self.assertTrue(result["ready"])
            self.assertEqual(
                published[-1], "parkventory-secret-generation.json"
            )
            self.assertEqual(
                set(published[:-1]),
                set(GENERATED_CREDENTIAL_FILES)
                | {"parkventory-database-secret-manifest.json"},
            )

    def test_killed_link_publication_is_recovered_before_retry(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            crash_program = """
import importlib.machinery
import importlib.util
import os
import sys

helper, target = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("crashing_materializer", helper)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
spec.loader.exec_module(module)
original_link = module.os.link

def link_then_die(*args, **kwargs):
    original_link(*args, **kwargs)
    os._exit(92)

module.os.link = link_then_die
sys.argv = [helper, "--apply", "--test-root", target]
module.main()
"""
            environment = os.environ.copy()
            environment["VPS_PARKVENTORY_SECRET_TESTING"] = "1"
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    crash_program,
                    str(SCRIPTS / "materialize-parkventory-secrets"),
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=20,
            )

            self.assertEqual(crashed.returncode, 92, crashed.stderr)
            canonical = root / DATABASE_CREDENTIAL_FILES[0]
            pending = list(
                root.glob(f".{DATABASE_CREDENTIAL_FILES[0]}.*.pending")
            )
            self.assertEqual(len(pending), 1)
            self.assertEqual(canonical.stat().st_ino, pending[0].stat().st_ino)
            self.assertEqual(canonical.stat().st_nlink, 2)

            retried = self.run_materializer(root, "--apply")

            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertTrue(json.loads(retried.stdout)["changed"])
            self.assertEqual(canonical.stat().st_nlink, 1)
            self.assertFalse(list(root.glob(".*.pending")))
            checked = self.run_materializer(root, "--check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["ready"])

    def test_unsafe_generated_staging_residue_is_refused_and_preserved(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        cases = ("malformed-name", "symlink", "wrong-mode", "hardlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                root = parent / "secrets"
                root.mkdir(mode=0o700)
                if case == "malformed-name":
                    residue = (
                        root
                        / ".parkventory-postgres-runtime-password.deadbeef.pending"
                    )
                    residue.write_bytes(b"staging residue\n")
                    residue.chmod(0o440)
                else:
                    residue = (
                        root
                        / ".parkventory-postgres-runtime-password."
                        "0123456789abcdef0123456789abcdef.pending"
                    )
                    external = parent / "external"
                    external.write_bytes(b"staging residue\n")
                    external.chmod(0o440)
                    if case == "symlink":
                        residue.symlink_to(external)
                    elif case == "hardlink":
                        os.link(external, residue)
                    else:
                        residue.write_bytes(b"staging residue\n")
                        residue.chmod(0o644)

                refused = self.run_materializer(root, "--apply")

                self.assertEqual(refused.returncode, 78)
                self.assertIn("staging residue is unsafe", refused.stderr)
                self.assertTrue(os.path.lexists(residue))
                self.assertFalse((root / DATABASE_CREDENTIAL_FILES[0]).exists())

    def test_existing_generation_marker_blocks_partial_repair(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            applied = self.run_materializer(root, "--apply")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            missing = root / "parkventory-oidc-state-secret"
            missing.unlink()

            refused = self.run_materializer(root, "--apply")

            self.assertEqual(refused.returncode, 78)
            self.assertIn("is missing", refused.stderr)
            self.assertFalse(missing.exists())

    def test_provider_secrets_are_not_read_or_modified(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            before: dict[str, tuple[bytes, int, int]] = {}
            for name in PROVIDER_INPUT_FILES:
                path = root / name
                path.write_bytes(b"provider-owned-test-input\n")
                path.chmod(0o440)
                metadata = path.stat()
                before[name] = (
                    path.read_bytes(),
                    metadata.st_ino,
                    metadata.st_mtime_ns,
                )

            applied = self.run_materializer(root, "--apply")

            self.assertEqual(applied.returncode, 0, applied.stderr)
            for name, expected in before.items():
                path = root / name
                metadata = path.stat()
                self.assertEqual(
                    (path.read_bytes(), metadata.st_ino, metadata.st_mtime_ns),
                    expected,
                )

    def test_source_plan_uses_only_the_embedded_contract(self) -> None:
        original_parse_args = PROVISIONER.parse_args
        original_geteuid = PROVISIONER.os.geteuid
        original_load_contract = PROVISIONER.load_contract
        original_resolve_postgres = PROVISIONER.resolve_postgres
        calls: list[tuple[object, bool]] = []
        try:
            PROVISIONER.parse_args = lambda: SimpleNamespace(
                mode="dry-run",
                validate_contract=False,
                require_rls=False,
                embedded_contract=True,
                contract=PROVISIONER.CONTRACT_PATH,
            )
            PROVISIONER.os.geteuid = lambda: 0
            PROVISIONER.load_contract = lambda path: self.fail(
                "source planning must not read the installed contract"
            )

            def resolve(contract, *, require_network):
                calls.append((contract, require_network))
                return "postgres-container", "postgres-image"

            PROVISIONER.resolve_postgres = resolve
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(PROVISIONER.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")
        finally:
            PROVISIONER.parse_args = original_parse_args
            PROVISIONER.os.geteuid = original_geteuid
            PROVISIONER.load_contract = original_load_contract
            PROVISIONER.resolve_postgres = original_resolve_postgres
        self.assertEqual(calls, [(PROVISIONER.EXPECTED_CONTRACT, False)])

    def test_secret_check_rejects_unsafe_metadata(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the helper intentionally rejects root test mode")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            root.mkdir(mode=0o700)
            applied = self.run_materializer(root, "--apply")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            target = root / "parkventory-postgres-runtime-password"
            target.chmod(0o600)
            checked = self.run_materializer(root, "--check")
            self.assertEqual(checked.returncode, 78)
            self.assertIn("unsafe metadata", checked.stderr)

    def test_contract_fixes_shared_postgres_17_10_and_private_transport(self) -> None:
        contract_path = ROOT / "applications/parkventory/postgres.json"
        contract, digest = PROVISIONER.load_contract(contract_path)
        self.assertEqual(contract["postgres"]["version"], "17.10")
        self.assertEqual(contract["postgres"]["major"], 17)
        expected_images = json.loads(
            (ROOT / "platform/expected-images.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["postgres"]["image"], expected_images["postgresql"])
        self.assertTrue(contract["network"]["internal"])
        self.assertFalse(contract["network"]["published_postgres_port"])
        self.assertFalse(contract["network"]["tls"])
        rls = contract["row_level_security"]
        self.assertEqual(
            rls["extensions"],
            [
                {
                    "name": "btree_gist",
                    "version": "1.7",
                    "schema": "public",
                    "owner": "parkventory_owner",
                    "member_owner": "platform_admin",
                    "all_members": {
                        "count": 264,
                        "identity_sha256": (
                            "sha256:52b11efe87483725ab571138cf124124deca29c45d114472055e358b2382aa17"
                        ),
                    },
                    "routine_members": {
                        "count": 188,
                        "identity_sha256": (
                            "sha256:40dbd8d5fc3f5340d65f078738049ccdc9249a8d63fc2cbb06a470982602142e"
                        ),
                    },
                    "type_members": {
                        "count": 6,
                        "identity_sha256": (
                            "sha256:8866390c21998f6e60995a60fd3edf5fc8ebc480b76b5701628ed4dbf5e86828"
                        ),
                    },
                },
                {
                    "name": "plpgsql",
                    "version": "1.0",
                    "schema": "pg_catalog",
                    "owner": "platform_admin",
                    "member_owner": "platform_admin",
                    "all_members": {
                        "count": 4,
                        "identity_sha256": (
                            "sha256:9daaf0961466e69fdc380fd1c0066b4fa6b37cc60b25fae14ac54b1f26e4a9f8"
                        ),
                    },
                    "routine_members": {
                        "count": 3,
                        "identity_sha256": (
                            "sha256:2872282c187c13d718ea00e72335763abd665dc0f377e0efaa66bebc1362f885"
                        ),
                    },
                    "type_members": {
                        "count": 0,
                        "identity_sha256": (
                            "sha256:37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
                        ),
                    },
                },
            ],
        )
        self.assertEqual(len(rls["tables"]), 18)
        self.assertEqual(len(rls["policies"]), 24)
        self.assertEqual(len(rls["helpers"]), 7)
        self.assertEqual(len(rls["runtime_access"]["tables"]), 18)
        self.assertEqual(len(rls["runtime_access"]["functions"]), 7)
        self.assertEqual(rls["runtime_access"]["sequences"], [])
        self.assertEqual(rls["runtime_access"]["types"], [])
        self.assertEqual(
            [
                table
                for table in rls["tables"]
                if not table["enabled"] or not table["forced"]
            ],
            [
                {
                    "table": "outbox_dispatch",
                    "enabled": False,
                    "forced": False,
                }
            ],
        )
        self.assertTrue(
            all(policy["permissive"] for policy in rls["policies"])
        )
        self.assertTrue(
            all(policy["roles"] == ["PUBLIC"] for policy in rls["policies"])
        )
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_apply_sql_separates_owner_migrator_and_runtime(self) -> None:
        statements: list[tuple[str, str]] = []
        original = PROVISIONER.psql
        try:
            PROVISIONER.psql = (
                lambda container, database, sql: statements.append((database, sql))
                or ""
            )
            PROVISIONER.apply_database("postgres-container", "A" * 64, "B" * 64)
        finally:
            PROVISIONER.psql = original
        self.assertEqual(
            [database for database, _ in statements], ["postgres", "parkventory"]
        )
        sql = "\n".join(statement for _, statement in statements)
        self.assertIn("CREATE ROLE parkventory_owner NOLOGIN", sql)
        self.assertIn("parkventory_owner NOLOGIN NOINHERIT", sql)
        self.assertIn("parkventory_migrator LOGIN NOINHERIT", sql)
        self.assertIn("parkventory_runtime LOGIN NOINHERIT", sql)
        self.assertIn("parkventory_owner", sql)
        self.assertIn("PASSWORD NULL", sql)
        self.assertGreaterEqual(sql.count("NOBYPASSRLS"), 3)
        self.assertIn("GRANT parkventory_owner TO parkventory_migrator", sql)
        self.assertIn("WITH ADMIN FALSE, INHERIT FALSE, SET TRUE", sql)
        self.assertIn("REVOKE %I FROM %I", sql)
        self.assertIn("ALTER DATABASE parkventory OWNER TO parkventory_owner", sql)
        self.assertIn("ALTER ROLE parkventory_owner RESET ALL", sql)
        self.assertIn("ALTER ROLE parkventory_migrator RESET ALL", sql)
        self.assertIn("ALTER ROLE parkventory_runtime RESET ALL", sql)
        self.assertIn("ALTER DATABASE parkventory RESET ALL", sql)
        self.assertIn(
            "ALTER ROLE parkventory_owner IN DATABASE parkventory RESET ALL",
            sql,
        )
        self.assertIn(
            "ALTER ROLE parkventory_migrator IN DATABASE parkventory RESET ALL",
            sql,
        )
        self.assertIn(
            "ALTER ROLE parkventory_runtime IN DATABASE parkventory RESET ALL",
            sql,
        )
        self.assertIn("REVOKE ALL PRIVILEGES ON DATABASE parkventory FROM PUBLIC", sql)
        self.assertIn("unexpected_role", sql)
        self.assertIn("GRANT USAGE ON SCHEMA public TO parkventory_runtime", sql)
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public",
            sql,
        )
        self.assertIn("REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I", sql)
        self.assertIn(
            "ALTER DEFAULT PRIVILEGES FOR ROLE parkventory_owner REVOKE ALL ON FUNCTIONS FROM PUBLIC",
            sql,
        )
        self.assertIn("ALTER DEFAULT PRIVILEGES FOR ROLE parkventory_owner", sql)
        self.assertNotIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES",
            sql,
        )
        self.assertNotIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES",
            sql,
        )
        self.assertNotIn("GRANT EXECUTE ON ALL FUNCTIONS", sql)
        self.assertNotIn("GRANT USAGE ON TYPES", sql)
        self.assertNotIn("GRANT CREATE ON SCHEMA public TO parkventory_runtime", sql)

    def test_credential_proof_uses_network_password_auth_without_argv_secret(
        self,
    ) -> None:
        passwords = {
            "parkventory_migrator": "A" * 64,
            "parkventory_runtime": "B" * 64,
        }
        identities = {
            "parkventory_migrator": (
                "parkventory_migrator|parkventory_owner|parkventory\n"
            ),
            "parkventory_runtime": (
                "parkventory_runtime|parkventory_runtime|parkventory\n"
            ),
        }
        calls: list[tuple[list[str], dict[str, str] | None]] = []
        original_command = PROVISIONER.command
        original_docker_json = PROVISIONER.docker_json
        try:

            def authenticate(
                arguments: list[str],
                *,
                input_text: str | None = None,
                environment: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                del input_text
                calls.append((arguments, environment))
                role = arguments[arguments.index("--username") + 1]
                assert environment is not None
                accepted = environment.get("PGPASSWORD") == passwords[role]
                return subprocess.CompletedProcess(
                    arguments,
                    0 if accepted else 2,
                    identities[role] if accepted else "",
                    "" if accepted else "password authentication failed\n",
                )

            PROVISIONER.command = authenticate
            PROVISIONER.docker_json = lambda arguments, label: [
                {
                    "NetworkSettings": {
                        "Networks": {
                            "db_parkventory": {"IPAddress": "172.30.21.2"}
                        }
                    }
                }
            ]
            PROVISIONER.verify_database_credentials(
                "postgres-container",
                passwords["parkventory_migrator"],
                passwords["parkventory_runtime"],
            )
        finally:
            PROVISIONER.command = original_command
            PROVISIONER.docker_json = original_docker_json
        self.assertEqual(len(calls), 4)
        for arguments, environment in calls:
            self.assertIn("172.30.21.2", arguments)
            self.assertNotIn("postgresql", arguments)
            self.assertIn("PGPASSWORD", arguments)
            assert environment is not None
            self.assertNotIn(environment["PGPASSWORD"], "\0".join(arguments))

        original_probe = PROVISIONER.credential_probe
        try:
            PROVISIONER.credential_probe = (
                lambda container, role, password: subprocess.CompletedProcess(
                    [],
                    0,
                    identities[role],
                    "",
                )
            )
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "network authentication does not enforce",
            ):
                PROVISIONER.verify_database_credentials(
                    "postgres-container",
                    passwords["parkventory_migrator"],
                    passwords["parkventory_runtime"],
                )
        finally:
            PROVISIONER.credential_probe = original_probe

    def test_credential_address_is_bounded_to_db_parkventory(self) -> None:
        original_docker_json = PROVISIONER.docker_json
        try:
            for address in (
                None,
                2887652610,
                "172.30.31.2",
                "172.30.21.0",
                "172.30.21.255",
            ):
                with self.subTest(address=address):
                    PROVISIONER.docker_json = lambda arguments, label, value=address: [
                        {
                            "NetworkSettings": {
                                "Networks": {
                                    "db_parkventory": {"IPAddress": value}
                                }
                            }
                        }
                    ]
                    with self.assertRaisesRegex(
                        PROVISIONER.ProvisionError,
                        "db_parkventory|outside",
                    ):
                        PROVISIONER.parkventory_database_address(
                            "postgres-container"
                        )
            for invalid in ([None], [{"NetworkSettings": None}]):
                with self.subTest(invalid=invalid):
                    PROVISIONER.docker_json = (
                        lambda arguments, label, value=invalid: value
                    )
                    with self.assertRaisesRegex(
                        PROVISIONER.ProvisionError,
                        "incomplete|db_parkventory",
                    ):
                        PROVISIONER.parkventory_database_address(
                            "postgres-container"
                        )
        finally:
            PROVISIONER.docker_json = original_docker_json

    def test_observation_is_exact_and_evidence_is_canonical(self) -> None:
        original = PROVISIONER.psql
        try:
            PROVISIONER.psql = lambda container, database, sql: json.dumps(
                PROVISIONER.EXPECTED_PROOF
            )
            proof = PROVISIONER.observe("postgres-container")
        finally:
            PROVISIONER.psql = original
        image = (
            "ghcr.io/nclsppr/vps-infra/postgres:sha-"
            + "a" * 40
            + "@sha256:"
            + "b" * 64
        )
        evidence = PROVISIONER.evidence_document(
            "sha256:" + "c" * 64,
            image,
            proof,
            {"migrator": "sha256:" + "d" * 64, "runtime": "sha256:" + "e" * 64},
        )
        self.assertEqual(evidence, PROVISIONER.canonical_json(json.loads(evidence)))
        document = json.loads(evidence)
        self.assertEqual(
            document["contract"],
            "vps-infra.parkventory-postgres-readiness.v1",
        )
        self.assertEqual(document["proof"]["server_version_num"], 170010)
        self.assertFalse(document["proof"]["owner_bypasses_rls"])
        self.assertFalse(document["proof"]["migrator_bypasses_rls"])
        self.assertFalse(document["proof"]["runtime_bypasses_rls"])
        self.assertEqual(document["proof"]["owner_memberships"], [])
        self.assertEqual(
            document["proof"]["migrator_memberships"],
            [
                {
                    "role": "parkventory_owner",
                    "admin_option": False,
                    "inherit_option": False,
                    "set_option": True,
                }
            ],
        )
        self.assertEqual(document["proof"]["runtime_memberships"], [])
        self.assertEqual(
            document["proof"]["owner_members"],
            [
                {
                    "member": "parkventory_migrator",
                    "admin_option": False,
                    "inherit_option": False,
                    "set_option": True,
                }
            ],
        )
        self.assertEqual(document["proof"]["migrator_members"], [])
        self.assertEqual(document["proof"]["runtime_members"], [])
        for key in (
            "unexpected_database_grantee",
            "unexpected_schema_grantee",
            "unexpected_application_schema",
            "unexpected_role_settings",
            "unexpected_default_grantee",
            "global_default_public_grantee",
            "global_default_runtime_grantee",
            "unexpected_global_default_grantee",
            "unexpected_default_scope",
            "unexpected_relation_grantee",
            "column_acl_present",
            "unexpected_routine_grantee",
            "unexpected_type_grantee",
        ):
            self.assertFalse(document["proof"][key])
        for key in (
            "all_relations_owned",
            "runtime_relation_privileges_bounded",
            "runtime_sequence_privileges_bounded",
            "routine_ownership_bounded",
            "runtime_routine_privileges_bounded",
            "type_ownership_bounded",
            "runtime_type_privileges_bounded",
        ):
            self.assertTrue(document["proof"][key])
        self.assertFalse(document["transport"]["tls"])
        self.assertTrue(document["transport"]["postgres_attached"])
        self.assertEqual(
            document["validity"],
            {
                "requires_live_check": True,
                "requires_postgres_network_attachment": True,
            },
        )
        self.assertNotIn("A" * 64, evidence.decode("utf-8"))

        tampered = dict(PROVISIONER.EXPECTED_PROOF)
        tampered["unexpected_database_grantee"] = True
        original = PROVISIONER.psql
        try:
            PROVISIONER.psql = lambda container, database, sql: json.dumps(tampered)
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "effective database roles or default privileges differ",
            ):
                PROVISIONER.observe("postgres-container")
        finally:
            PROVISIONER.psql = original

    def test_postmigration_rls_proof_is_exact_and_catalog_backed(self) -> None:
        base_sql = PROVISIONER.proof_sql()
        for fragment in (
            "extension_dependency.classid='pg_proc'::regclass",
            "extension_dependency.classid='pg_type'::regclass",
            "extension.extname<>'btree_gist'",
            "extension.extversion<>'1.7'",
            "a.grantee=p.proowner",
            "a.grantee<>t.typowner",
            "ARRAY['role=parkventory_owner','search_path=public']::text[]",
            "ARRAY['search_path=public']::text[]",
            "left(n.nspname,3)<>'pg_'",
            "has_table_privilege('parkventory_runtime',c.oid,'MAINTAIN')",
            "s.setrole=0 AND s.setdatabase IN",
            "s.setdatabase IN (0,",
        ):
            self.assertIn(fragment, base_sql)
        sql = PROVISIONER.rls_proof_sql()
        self.assertIn("('MAINTAIN'), ('MAINTAIN WITH GRANT OPTION')", sql)
        for catalog_field in (
            "relrowsecurity",
            "relforcerowsecurity",
            "pg_policy",
            "polpermissive",
            "polroles",
            "pg_get_expr",
            "pg_get_functiondef",
            "has_table_privilege",
            "pg_depend",
            "pg_extension",
            "pg_identify_object",
        ):
            self.assertIn(catalog_field, sql)

        responses = iter(
            (
                json.dumps(PROVISIONER.EXPECTED_PROOF),
                json.dumps(PROVISIONER.EXPECTED_RLS),
            )
        )
        original_psql = PROVISIONER.psql
        original_fingerprint = PROVISIONER.fingerprint_rls_helpers
        original_extension_fingerprint = (
            PROVISIONER.fingerprint_extension_members
        )
        try:
            PROVISIONER.psql = (
                lambda container, database, statement: next(responses)
            )
            PROVISIONER.fingerprint_rls_helpers = lambda proof: proof
            PROVISIONER.fingerprint_extension_members = lambda proof: proof
            proof = PROVISIONER.observe(
                "postgres-container",
                require_rls=True,
            )
        finally:
            PROVISIONER.psql = original_psql
            PROVISIONER.fingerprint_rls_helpers = original_fingerprint
            PROVISIONER.fingerprint_extension_members = (
                original_extension_fingerprint
            )
        self.assertEqual(proof, PROVISIONER.EXPECTED_PROOF)

    def test_postmigration_rls_proof_rejects_hostile_catalog_changes(self) -> None:
        def rejects(rls_proof: dict[str, object]) -> None:
            responses = iter(
                (
                    json.dumps(PROVISIONER.EXPECTED_PROOF),
                    json.dumps(rls_proof),
                )
            )
            original_psql = PROVISIONER.psql
            original_fingerprint = PROVISIONER.fingerprint_rls_helpers
            original_extension_fingerprint = (
                PROVISIONER.fingerprint_extension_members
            )
            try:
                PROVISIONER.psql = (
                    lambda container, database, statement: next(responses)
                )
                PROVISIONER.fingerprint_rls_helpers = lambda proof: proof
                PROVISIONER.fingerprint_extension_members = lambda proof: proof
                with self.assertRaisesRegex(
                    PROVISIONER.ProvisionError,
                    "row-level security|runtime database access",
                ):
                    PROVISIONER.observe(
                        "postgres-container",
                        require_rls=True,
                    )
            finally:
                PROVISIONER.psql = original_psql
                PROVISIONER.fingerprint_rls_helpers = original_fingerprint
                PROVISIONER.fingerprint_extension_members = (
                    original_extension_fingerprint
                )

        disabled = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        disabled["tables"][0]["enabled"] = False
        no_force = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        no_force["tables"][0]["forced"] = False
        weakened = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        weakened["policies"][0]["using"] = "true"
        weakened["policies"][0]["with_check"] = "true"
        added = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        added["policies"].append(
            {
                "table": "admin_claim",
                "name": "admin_claim_permissive_bypass",
                "command": "ALL",
                "permissive": True,
                "roles": ["PUBLIC"],
                "using": "true",
                "with_check": "true",
            }
        )
        unexpected_runtime_table = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        unexpected_runtime_table["runtime_access"]["tables"].append(
            {
                "table": "flyway_schema_history",
                "privileges": ["DELETE", "INSERT", "SELECT", "UPDATE"],
            }
        )
        unexpected_extension_version = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        unexpected_extension_version["extensions"][0]["version"] = "1.8"

        for label, proof in (
            ("disabled RLS", disabled),
            ("removed FORCE RLS", no_force),
            ("permissive true policy", weakened),
            ("additional permissive policy", added),
            ("undeclared runtime table", unexpected_runtime_table),
            ("unexpected extension version", unexpected_extension_version),
        ):
            with self.subTest(label):
                rejects(proof)

    def test_rls_helper_fingerprint_rejects_permissive_redefinition(self) -> None:
        helper = copy.deepcopy(PROVISIONER.EXPECTED_RLS_HELPERS[0])
        expected_digest = helper.pop("definition_sha256")
        definition = (
            "CREATE OR REPLACE FUNCTION public.app_current_identity_user_id()\n"
            " RETURNS uuid\n"
            " LANGUAGE sql\n"
            " STABLE PARALLEL SAFE\n"
            " SET search_path TO 'pg_catalog'\n"
            "AS $function$\n"
            "    SELECT NULLIF(current_setting('app.identity_user_id', true), '')::UUID\n"
            "$function$\n"
        )
        helper["definition"] = definition
        canonical = PROVISIONER.fingerprint_rls_helpers({"helpers": [helper]})
        self.assertEqual(
            canonical["helpers"][0]["definition_sha256"],
            expected_digest,
        )

        helper = copy.deepcopy(PROVISIONER.EXPECTED_RLS_HELPERS[0])
        helper.pop("definition_sha256")
        helper["definition"] = definition.replace(
            "SELECT NULLIF(current_setting('app.identity_user_id', true), '')::UUID",
            "SELECT '00000000-0000-0000-0000-000000000000'::UUID",
        )
        permissive = PROVISIONER.fingerprint_rls_helpers({"helpers": [helper]})
        self.assertNotEqual(
            permissive["helpers"][0]["definition_sha256"],
            expected_digest,
        )

    def test_application_acl_reconciliation_is_default_deny_and_allowlisted(
        self,
    ) -> None:
        before = copy.deepcopy(PROVISIONER.EXPECTED_RLS)
        before["runtime_access"]["tables"].append(
            {
                "table": "flyway_schema_history",
                "privileges": ["DELETE", "INSERT", "SELECT", "UPDATE"],
            }
        )
        statements: list[str] = []
        original_observe_rls = PROVISIONER.observe_rls
        original_read_database_proof = PROVISIONER.read_database_proof
        original_observe = PROVISIONER.observe
        original_psql = PROVISIONER.psql
        try:
            PROVISIONER.observe_rls = (
                lambda container, *, require_runtime_access: before
            )
            PROVISIONER.read_database_proof = (
                lambda container: copy.deepcopy(PROVISIONER.EXPECTED_PROOF)
            )
            PROVISIONER.observe = (
                lambda container, *, require_rls: copy.deepcopy(
                    PROVISIONER.EXPECTED_PROOF
                )
            )
            PROVISIONER.psql = (
                lambda container, database, sql: statements.append(sql) or ""
            )
            self.assertTrue(
                PROVISIONER.reconcile_application_acl("postgres-container")
            )
        finally:
            PROVISIONER.observe_rls = original_observe_rls
            PROVISIONER.read_database_proof = original_read_database_proof
            PROVISIONER.observe = original_observe
            PROVISIONER.psql = original_psql
        self.assertEqual(len(statements), 1)
        sql = statements[0]
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public",
            sql,
        )
        self.assertIn("FROM PUBLIC, parkventory_runtime", sql)
        self.assertIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.admin_claim",
            sql,
        )
        self.assertIn("public.user_email", sql)
        self.assertNotIn("flyway_schema_history", sql)
        for helper in PROVISIONER.EXPECTED_RLS_HELPERS:
            self.assertIn(f"public.{helper['name']}()", sql)
        self.assertNotIn("app_fill_outbox_dispatch_aggregate()", sql)
        self.assertNotIn("GRANT EXECUTE ON ALL FUNCTIONS", sql)
        self.assertNotIn("GRANT USAGE, SELECT, UPDATE", sql)

        original_observe_rls = PROVISIONER.observe_rls
        original_read_database_proof = PROVISIONER.read_database_proof
        original_psql = PROVISIONER.psql
        try:
            PROVISIONER.observe_rls = (
                lambda container, *, require_runtime_access: copy.deepcopy(
                    PROVISIONER.EXPECTED_RLS
                )
            )
            PROVISIONER.read_database_proof = (
                lambda container: copy.deepcopy(PROVISIONER.EXPECTED_PROOF)
            )
            PROVISIONER.psql = lambda *args, **kwargs: self.fail(
                "idempotent reconciliation must not execute SQL"
            )
            self.assertFalse(
                PROVISIONER.reconcile_application_acl("postgres-container")
            )
        finally:
            PROVISIONER.observe_rls = original_observe_rls
            PROVISIONER.read_database_proof = original_read_database_proof
            PROVISIONER.psql = original_psql

    def test_application_acl_reconciliation_repairs_base_acl_only_drift(
        self,
    ) -> None:
        drifted = copy.deepcopy(PROVISIONER.EXPECTED_PROOF)
        drifted["column_acl_present"] = True
        statements: list[str] = []
        original_observe_rls = PROVISIONER.observe_rls
        original_read_database_proof = PROVISIONER.read_database_proof
        original_observe = PROVISIONER.observe
        original_psql = PROVISIONER.psql
        try:
            PROVISIONER.observe_rls = (
                lambda container, *, require_runtime_access: copy.deepcopy(
                    PROVISIONER.EXPECTED_RLS
                )
            )
            PROVISIONER.read_database_proof = lambda container: drifted
            PROVISIONER.observe = (
                lambda container, *, require_rls: copy.deepcopy(
                    PROVISIONER.EXPECTED_PROOF
                )
            )
            PROVISIONER.psql = (
                lambda container, database, sql: statements.append(sql) or ""
            )
            self.assertTrue(
                PROVISIONER.reconcile_application_acl("postgres-container")
            )
        finally:
            PROVISIONER.observe_rls = original_observe_rls
            PROVISIONER.read_database_proof = original_read_database_proof
            PROVISIONER.observe = original_observe
            PROVISIONER.psql = original_psql
        self.assertEqual(len(statements), 1)
        self.assertIn("DO $column_acl$", statements[0])

    def test_check_auto_requires_rls_for_partial_schema(self) -> None:
        calls: list[bool] = []
        original_schema_present = PROVISIONER.application_schema_present
        original_observe = PROVISIONER.observe
        try:
            PROVISIONER.observe = (
                lambda container, *, require_rls: calls.append(require_rls)
                or copy.deepcopy(PROVISIONER.EXPECTED_PROOF)
            )
            for schema_present, explicit in (
                (False, False),
                (False, True),
                (True, False),
                (True, True),
            ):
                PROVISIONER.application_schema_present = (
                    lambda container, present=schema_present: present
                )
                PROVISIONER.check_database(
                    "postgres-container",
                    require_rls=explicit,
                )
        finally:
            PROVISIONER.application_schema_present = original_schema_present
            PROVISIONER.observe = original_observe
        self.assertEqual(calls, [False, True, True, True])

    def test_partial_schema_detection_covers_all_public_object_kinds(self) -> None:
        statements: list[str] = []
        original_psql = PROVISIONER.psql
        try:
            PROVISIONER.psql = (
                lambda container, database, sql: statements.append(sql) or "false"
            )
            self.assertFalse(
                PROVISIONER.application_schema_present("postgres-container")
            )
        finally:
            PROVISIONER.psql = original_psql
        self.assertEqual(len(statements), 1)
        sql = statements[0]
        self.assertIn("relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')", sql)
        self.assertIn("relation.relname <> 'flyway_schema_history'", sql)
        self.assertIn("FROM pg_policy policy", sql)
        self.assertIn("FROM pg_proc routine", sql)
        self.assertIn("FROM pg_type type", sql)
        self.assertIn("FROM pg_extension extension", sql)

    def test_check_requires_the_effective_postgres_network_attachment(self) -> None:
        calls: list[bool] = []
        original_parse_args = PROVISIONER.parse_args
        original_geteuid = PROVISIONER.os.geteuid
        original_load_contract = PROVISIONER.load_contract
        original_resolve_postgres = PROVISIONER.resolve_postgres
        try:
            PROVISIONER.parse_args = lambda: SimpleNamespace(
                mode="check",
                validate_contract=False,
                require_rls=False,
                contract=PROVISIONER.CONTRACT_PATH,
            )
            PROVISIONER.os.geteuid = lambda: 0
            PROVISIONER.load_contract = lambda path: (
                PROVISIONER.EXPECTED_CONTRACT,
                "sha256:" + "a" * 64,
            )

            def reject_absent_network(contract, *, require_network):
                calls.append(require_network)
                raise PROVISIONER.ProvisionError(
                    "shared PostgreSQL is detached from db_parkventory"
                )

            PROVISIONER.resolve_postgres = reject_absent_network
            self.assertEqual(PROVISIONER.main(), 78)
        finally:
            PROVISIONER.parse_args = original_parse_args
            PROVISIONER.os.geteuid = original_geteuid
            PROVISIONER.load_contract = original_load_contract
            PROVISIONER.resolve_postgres = original_resolve_postgres
        self.assertEqual(calls, [True])

    def test_role_has_no_activation_or_public_edge_mutation(self) -> None:
        role = (
            ROOT / "ansible/roles/parkventory_postgres/tasks/main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("vps_parkventory_postgres_state == 'prepare'", role)
        self.assertIn("'encrypted_offsite_backup'", role)
        self.assertIn("].sha256 == none", role)
        self.assertNotIn("activate-parkventory", role)
        self.assertNotIn("docker compose", role)
        self.assertNotIn("Caddyfile", role)
        self.assertNotIn("OVH", role)
        self.assertNotIn("dig\n", role)

    def test_role_check_proves_installed_artifacts_without_rewriting_them(
        self,
    ) -> None:
        tasks = yaml.safe_load(
            (
                ROOT / "ansible/roles/parkventory_postgres/tasks/main.yml"
            ).read_text(encoding="utf-8")
        )
        by_name = {task["name"]: task for task in tasks}
        platform_state = by_name[
            "Read the effective internal platform activation state"
        ]
        self.assertFalse(platform_state["check_mode"])
        self.assertIs(platform_state["changed_when"], False)
        self.assertEqual(
            platform_state["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/systemctl",
                "is-active",
                "vps-internal-platform.service",
            ],
        )
        platform_proof = by_name[
            "Require the healthy internal platform before database inspection"
        ]["ansible.builtin.assert"]["that"]
        self.assertIn(
            "vps_parkventory_internal_platform_active.stdout == 'active'",
            platform_proof,
        )
        self.assertNotIn(
            "ansible_facts.services['vps-internal-platform.service'].state == 'running'",
            platform_proof,
        )
        copy_tasks = [task for task in tasks if "ansible.builtin.copy" in task]
        self.assertEqual(len(copy_tasks), 2)
        for task in copy_tasks:
            self.assertIn(
                "vps_parkventory_postgres_state == 'prepare' or",
                task["when"],
            )
            self.assertIn("ansible_check_mode", task["when"])

        for name in (
            "Run the reviewed-source Parkventory secret plan in check mode",
            "Run the reviewed-source Parkventory PostgreSQL plan in check mode",
        ):
            task = by_name[name]
            self.assertFalse(task["check_mode"])
            self.assertIn("ansible_check_mode", task["when"])
        self.assertEqual(
            by_name[
                "Run the reviewed-source Parkventory secret plan in check mode"
            ]["ansible.builtin.command"]["argv"][3:],
            ["--dry-run"],
        )
        self.assertEqual(
            by_name[
                "Run the reviewed-source Parkventory PostgreSQL plan in check mode"
            ]["ansible.builtin.command"]["argv"][3:],
            ["--dry-run", "--embedded-contract"],
        )

        contract_inspection = by_name[
            "Inspect the installed Parkventory PostgreSQL contract without changing it"
        ]
        self.assertEqual(
            contract_inspection["ansible.builtin.stat"]["checksum_algorithm"],
            "sha256",
        )
        helper_inspection = by_name[
            "Inspect installed Parkventory database helpers without changing them"
        ]
        self.assertEqual(
            helper_inspection["ansible.builtin.stat"]["checksum_algorithm"],
            "sha256",
        )
        for name in (
            "Prove the installed Parkventory PostgreSQL contract without changing it",
            "Prove installed Parkventory database helpers without changing them",
        ):
            task = by_name[name]
            proof = "\n".join(task["ansible.builtin.assert"]["that"])
            self.assertIn(".stat.checksum ==", proof)
            self.assertIn(".stat.nlink == 1", proof)
            self.assertIn(".stat.pw_name == 'root'", proof)
            self.assertIn(".stat.gr_name == 'root'", proof)
            self.assertIn("not ansible_check_mode", task["when"])


if __name__ == "__main__":
    unittest.main()
