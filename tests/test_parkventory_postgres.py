#!/usr/bin/env python3
"""Tests for the fail-closed Parkventory PostgreSQL readiness slice."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


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

    def test_database_passwords_are_private_distinct_and_idempotent(self) -> None:
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
            for name in (
                "parkventory-postgres-migrator-password",
                "parkventory-postgres-runtime-password",
            ):
                path = root / name
                values[name] = path.read_bytes()
                self.assertRegex(values[name], rb"^[A-Za-z0-9_-]{64}\n$")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
                self.assertNotIn(values[name][:-1], first.stdout.encode("utf-8"))
            self.assertNotEqual(*values.values())
            manifest = root / "parkventory-database-secret-manifest.json"
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o400)
            document = json.loads(manifest.read_text(encoding="ascii"))
            self.assertEqual(
                document["sha256"],
                {
                    name: hashlib.sha256(value).hexdigest()
                    for name, value in values.items()
                },
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
        self.assertTrue(contract["network"]["internal"])
        self.assertFalse(contract["network"]["published_postgres_port"])
        self.assertFalse(contract["network"]["tls"])
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
        self.assertIn("GRANT parkventory_owner TO parkventory_migrator", sql)
        self.assertIn("ALTER DATABASE parkventory OWNER TO parkventory_owner", sql)
        self.assertIn("REVOKE ALL PRIVILEGES ON DATABASE parkventory FROM PUBLIC", sql)
        self.assertIn("GRANT USAGE ON SCHEMA public TO parkventory_runtime", sql)
        self.assertIn("ALTER DEFAULT PRIVILEGES FOR ROLE parkventory_owner", sql)
        self.assertIn("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES", sql)
        self.assertNotIn("GRANT CREATE ON SCHEMA public TO parkventory_runtime", sql)

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
        self.assertIn("] == none", role)
        self.assertNotIn("activate-parkventory", role)
        self.assertNotIn("docker compose", role)
        self.assertNotIn("Caddyfile", role)
        self.assertNotIn("OVH", role)
        self.assertNotIn("dig\n", role)


if __name__ == "__main__":
    unittest.main()
