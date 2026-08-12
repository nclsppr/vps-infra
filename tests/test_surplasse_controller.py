#!/usr/bin/env python3
"""Tests for the fail-closed Surplasse Atlas preparation controller."""

from __future__ import annotations

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

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


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


class SurplasseControllerTests(unittest.TestCase):
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
            self.assertIn("operator-supplied secret surplasse-jwt-jwks is missing", complete.stderr)

    def test_postgres_provisioning_sql_separates_roles(self) -> None:
        statements: list[tuple[str, str]] = []
        original_psql = PROVISIONER.psql
        original_command = PROVISIONER.command
        try:
            PROVISIONER.psql = lambda container, database, sql: statements.append(
                (database, sql)
            ) or ""
            PROVISIONER.command = lambda arguments, input_text=None: subprocess.CompletedProcess(
                arguments,
                0,
                "false|true|true|surplasse_owner|true|true|false\n",
                "",
            )
            PROVISIONER.provision("postgres-container", "A" * 64, "B" * 64)
        finally:
            PROVISIONER.psql = original_psql
            PROVISIONER.command = original_command

        self.assertEqual([database for database, _ in statements], ["postgres", "surplasse"])
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
            (integration / "internal-platform.override.yaml").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
