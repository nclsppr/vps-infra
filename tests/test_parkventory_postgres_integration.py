#!/usr/bin/env python3
"""Real PostgreSQL 17.10 proof for the Parkventory application catalog."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/parkventory-postgres"


def load_provisioner():
    path = ROOT / "scripts/provision-parkventory-postgres"
    loader = importlib.machinery.SourceFileLoader(
        "parkventory_postgres_integration_provisioner",
        str(path),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


PROVISIONER = load_provisioner()


class ParkventoryPostgresIntegrationTests(unittest.TestCase):
    maxDiff = None

    def docker(
        self,
        binary: str,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [binary, *arguments],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if check and result.returncode != 0:
            self.fail(
                f"docker {' '.join(arguments[:3])} failed:\n{result.stderr}"
            )
        return result

    def role_psql(
        self,
        docker: str,
        container: str,
        role: str,
        role_auth: str,
        sql: str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.docker(
            docker,
            "exec",
            "--interactive",
            "--env",
            f"PGPASSWORD={role_auth}",
            container,
            "psql",
            "--no-password",
            "--host",
            "127.0.0.1",
            "--username",
            role,
            "--dbname",
            "parkventory",
            "--set",
            "ON_ERROR_STOP=1",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--quiet",
            input_text=sql,
            check=check,
        )

    def test_v1_to_v5_reconciliation_on_contract_image(self) -> None:
        docker = shutil.which("docker")
        self.assertIsNotNone(docker, "the contract test requires Docker")
        assert docker is not None
        image = str(PROVISIONER.EXPECTED_CONTRACT["postgres"]["image"])
        container = f"pv-postgres-contract-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        platform_auth = "PlatformContractPassword123456789"
        migrator_auth = "A" * 64
        runtime_auth = "B" * 64
        original_command = PROVISIONER.command

        try:
            self.docker(
                docker,
                "run",
                "--detach",
                "--name",
                container,
                "--env",
                "POSTGRES_USER=platform_admin",
                "--env",
                f"POSTGRES_PASSWORD={platform_auth}",
                "--env",
                "POSTGRES_DB=postgres",
                image,
            )
            for _ in range(120):
                ready = self.docker(
                    docker,
                    "exec",
                    "--env",
                    f"PGPASSWORD={platform_auth}",
                    container,
                    "psql",
                    "--no-password",
                    "--host",
                    "127.0.0.1",
                    "--username",
                    "platform_admin",
                    "--dbname",
                    "postgres",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT 1",
                    check=False,
                )
                if ready.returncode == 0 and ready.stdout.strip() == "1":
                    break
                time.sleep(0.5)
            else:
                logs = self.docker(
                    docker,
                    "logs",
                    container,
                    check=False,
                )
                self.fail(f"PostgreSQL did not become ready:\n{logs.stderr}")

            def local_command(
                arguments: list[str], *, input_text: str | None = None
            ) -> subprocess.CompletedProcess[str]:
                if arguments and arguments[0] == "/usr/bin/docker":
                    arguments = [docker, *arguments[1:]]
                return original_command(arguments, input_text=input_text)

            PROVISIONER.command = local_command
            PROVISIONER.apply_database(
                container,
                migrator_auth,
                runtime_auth,
            )
            self.assertFalse(PROVISIONER.application_schema_present(container))
            self.assertEqual(
                PROVISIONER.check_database(container),
                PROVISIONER.EXPECTED_PROOF,
            )
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "application extension|row-level security",
            ):
                PROVISIONER.check_database(container, require_rls=True)

            self.role_psql(
                docker,
                container,
                "parkventory_migrator",
                migrator_auth,
                "CREATE VIEW partial_schema_probe AS SELECT 1 AS id;",
            )
            self.assertTrue(PROVISIONER.application_schema_present(container))
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "application extension|row-level security",
            ):
                PROVISIONER.check_database(container)
            self.role_psql(
                docker,
                container,
                "parkventory_migrator",
                migrator_auth,
                "DROP VIEW partial_schema_probe;",
            )
            self.assertFalse(PROVISIONER.application_schema_present(container))

            self.role_psql(
                docker,
                container,
                "parkventory_migrator",
                migrator_auth,
                """
CREATE TABLE flyway_schema_history (
  installed_rank INTEGER PRIMARY KEY,
  version VARCHAR(50),
  description VARCHAR(200) NOT NULL,
  type VARCHAR(20) NOT NULL,
  script VARCHAR(1000) NOT NULL,
  checksum INTEGER,
  installed_by VARCHAR(100) NOT NULL,
  installed_on TIMESTAMP NOT NULL DEFAULT now(),
  execution_time INTEGER NOT NULL,
  success BOOLEAN NOT NULL
);
""",
            )
            migrations = sorted(FIXTURES.glob("V*.sql"))
            self.assertEqual(
                [migration.name[:2] for migration in migrations],
                ["V1", "V2", "V3", "V4", "V5"],
            )
            for rank, migration in enumerate(migrations, 1):
                sql = migration.read_text(encoding="utf-8")
                self.role_psql(
                    docker,
                    container,
                    "parkventory_migrator",
                    migrator_auth,
                    (
                        "BEGIN;\n"
                        + sql
                        + "\nINSERT INTO flyway_schema_history ("
                        "installed_rank, version, description, type, script, "
                        "checksum, installed_by, execution_time, success) "
                        f"VALUES ({rank}, '{rank}', '{migration.stem}', "
                        f"'SQL', '{migration.name}', NULL, session_user, 0, true);\n"
                        "COMMIT;\n"
                    ),
                )

            self.assertTrue(PROVISIONER.application_schema_present(container))
            extension_catalog = PROVISIONER.observe_rls(
                container,
                require_runtime_access=False,
            )["extensions"]
            self.assertEqual(
                extension_catalog,
                PROVISIONER.EXPECTED_PUBLIC_EXTENSIONS,
            )
            pre_reconcile_access = json.loads(
                PROVISIONER.psql(
                    container,
                    "parkventory",
                    """
SELECT jsonb_build_object(
  'public_routines', (
    SELECT count(*)
    FROM pg_proc routine
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_proc'::regclass
     AND dependency.objid = routine.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    CROSS JOIN LATERAL aclexplode(
      COALESCE(routine.proacl, acldefault('f', routine.proowner))
    ) acl
    WHERE extension.extname = 'btree_gist'
      AND acl.grantee = 0
      AND acl.privilege_type = 'EXECUTE'
  ),
  'runtime_routines', (
    SELECT count(*)
    FROM pg_proc routine
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_proc'::regclass
     AND dependency.objid = routine.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    WHERE extension.extname = 'btree_gist'
      AND has_function_privilege(
        'parkventory_runtime', routine.oid, 'EXECUTE'
      )
  ),
  'public_types', (
    SELECT count(*)
    FROM pg_type type
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_type'::regclass
     AND dependency.objid = type.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    CROSS JOIN LATERAL aclexplode(
      COALESCE(type.typacl, acldefault('T', type.typowner))
    ) acl
    WHERE extension.extname = 'btree_gist'
      AND type.typisdefined
      AND type.typelem = 0
      AND acl.grantee = 0
      AND acl.privilege_type = 'USAGE'
  ),
  'runtime_types', (
    SELECT count(*)
    FROM pg_type type
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_type'::regclass
     AND dependency.objid = type.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    WHERE extension.extname = 'btree_gist'
      AND type.typisdefined
      AND type.typelem = 0
      AND has_type_privilege('parkventory_runtime', type.oid, 'USAGE')
  )
)::json;
""",
                )
            )
            self.assertEqual(
                pre_reconcile_access,
                {
                    "public_routines": 188,
                    "runtime_routines": 188,
                    "public_types": 6,
                    "runtime_types": 6,
                },
            )
            with self.assertRaises(PROVISIONER.ProvisionError):
                PROVISIONER.check_database(container)

            self.assertTrue(PROVISIONER.reconcile_application_acl(container))
            self.assertEqual(
                PROVISIONER.check_database(container),
                PROVISIONER.EXPECTED_PROOF,
            )
            self.assertFalse(PROVISIONER.reconcile_application_acl(container))

            final_extension_access = json.loads(
                PROVISIONER.psql(
                    container,
                    "parkventory",
                    """
SELECT jsonb_build_object(
  'unexpected_routine_acl', EXISTS (
    SELECT 1
    FROM pg_proc routine
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_proc'::regclass
     AND dependency.objid = routine.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    CROSS JOIN LATERAL aclexplode(
      COALESCE(routine.proacl, acldefault('f', routine.proowner))
    ) acl
    WHERE extension.extname = 'btree_gist'
      AND acl.grantee <> routine.proowner
  ),
  'runtime_routine', EXISTS (
    SELECT 1
    FROM pg_proc routine
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_proc'::regclass
     AND dependency.objid = routine.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    WHERE extension.extname = 'btree_gist'
      AND has_function_privilege(
        'parkventory_runtime', routine.oid, 'EXECUTE'
      )
  ),
  'unexpected_type_acl', EXISTS (
    SELECT 1
    FROM pg_type type
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_type'::regclass
     AND dependency.objid = type.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    CROSS JOIN LATERAL aclexplode(
      COALESCE(type.typacl, acldefault('T', type.typowner))
    ) acl
    WHERE extension.extname = 'btree_gist'
      AND type.typisdefined
      AND type.typelem = 0
      AND acl.grantee <> type.typowner
  ),
  'runtime_type', EXISTS (
    SELECT 1
    FROM pg_type type
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_type'::regclass
     AND dependency.objid = type.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    WHERE extension.extname = 'btree_gist'
      AND type.typisdefined
      AND type.typelem = 0
      AND has_type_privilege('parkventory_runtime', type.oid, 'USAGE')
  ),
  'runtime_array_type', EXISTS (
    SELECT 1
    FROM pg_type array_type
    JOIN pg_type element_type ON element_type.oid = array_type.typelem
    JOIN pg_depend dependency
      ON dependency.classid = 'pg_type'::regclass
     AND dependency.objid = element_type.oid
     AND dependency.objsubid = 0
     AND dependency.refclassid = 'pg_extension'::regclass
     AND dependency.deptype = 'e'
    JOIN pg_extension extension ON extension.oid = dependency.refobjid
    WHERE extension.extname = 'btree_gist'
      AND has_type_privilege(
        'parkventory_runtime', array_type.oid, 'USAGE'
      )
  )
)::json;
""",
                )
            )
            self.assertEqual(
                final_extension_access,
                {
                    "unexpected_routine_acl": False,
                    "runtime_routine": False,
                    "unexpected_type_acl": False,
                    "runtime_type": False,
                    "runtime_array_type": False,
                },
            )

            flyway_access = self.role_psql(
                docker,
                container,
                "parkventory_runtime",
                runtime_auth,
                """
SELECT concat_ws('|',
  has_table_privilege(
    'parkventory_runtime', 'public.flyway_schema_history', 'SELECT'
  ),
  has_table_privilege(
    'parkventory_runtime', 'public.flyway_schema_history', 'INSERT'
  ),
  has_table_privilege(
    'parkventory_runtime', 'public.flyway_schema_history', 'UPDATE'
  ),
  has_table_privilege(
    'parkventory_runtime', 'public.flyway_schema_history', 'DELETE'
  )
);
""",
            )
            self.assertEqual(flyway_access.stdout.strip(), "f|f|f|f")

            self.role_psql(
                docker,
                container,
                "parkventory_migrator",
                migrator_auth,
                """
GRANT SELECT (name) ON organization TO parkventory_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE parkventory_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO parkventory_runtime;
""",
            )
            PROVISIONER.observe_rls(container, require_runtime_access=True)
            self.assertTrue(PROVISIONER.reconcile_application_acl(container))
            self.assertFalse(PROVISIONER.reconcile_application_acl(container))

            dml = self.role_psql(
                docker,
                container,
                "parkventory_runtime",
                runtime_auth,
                """
BEGIN;
SET LOCAL app.organization_id = '00000000-0000-0000-0000-000000000001';
SET LOCAL app.identity_user_id = '00000000-0000-0000-0000-000000000002';
INSERT INTO organization (id, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'Contract tenant');
INSERT INTO user_account (id, display_name)
VALUES ('00000000-0000-0000-0000-000000000002', 'Contract user');
INSERT INTO membership (id, organization_id, user_account_id)
VALUES (
  '00000000-0000-0000-0000-000000000003',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002'
);
INSERT INTO parking_site (id, organization_id, name)
VALUES (
  '00000000-0000-0000-0000-000000000004',
  '00000000-0000-0000-0000-000000000001',
  'Contract site'
);
INSERT INTO parking_spot (id, organization_id, parking_site_id, label)
VALUES (
  '00000000-0000-0000-0000-000000000005',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000004',
  'A-1'
);
INSERT INTO spot_assignment (
  id, organization_id, parking_spot_id, membership_id, starts_at
) VALUES (
  '00000000-0000-0000-0000-000000000006',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000005',
  '00000000-0000-0000-0000-000000000003',
  '2026-08-23T08:00:00Z'
);
INSERT INTO availability_offer (
  id, organization_id, parking_spot_id, spot_assignment_id,
  offered_by_membership_id, starts_at, ends_at
) VALUES (
  '00000000-0000-0000-0000-000000000007',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000005',
  '00000000-0000-0000-0000-000000000006',
  '00000000-0000-0000-0000-000000000003',
  '2026-08-23T09:00:00Z',
  '2026-08-23T10:00:00Z'
);
INSERT INTO reservation (
  id, organization_id, availability_offer_id, parking_spot_id,
  reserved_by_membership_id, starts_at, ends_at, idempotency_key
) VALUES (
  '00000000-0000-0000-0000-000000000008',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000007',
  '00000000-0000-0000-0000-000000000005',
  '00000000-0000-0000-0000-000000000003',
  '2026-08-23T09:00:00Z',
  '2026-08-23T10:00:00Z',
  'contract-reservation'
);
SELECT concat_ws('|',
  (SELECT count(*) FROM spot_assignment),
  (SELECT count(*) FROM reservation)
);
ROLLBACK;
""",
            )
            self.assertEqual(dml.stdout.strip(), "1|1")

            explicit_operator = self.role_psql(
                docker,
                container,
                "parkventory_runtime",
                runtime_auth,
                "SELECT 1::integer <-> 2::integer;",
                check=False,
            )
            self.assertNotEqual(explicit_operator.returncode, 0)
            self.assertIn("permission denied", explicit_operator.stderr)

            self.role_psql(
                docker,
                container,
                "parkventory_migrator",
                migrator_auth,
                """
CREATE TABLE unexpected_runtime_object (id INTEGER PRIMARY KEY);
GRANT SELECT ON unexpected_runtime_object TO parkventory_runtime;
""",
            )
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "application extension|row-level security",
            ):
                PROVISIONER.reconcile_application_acl(container)
            PROVISIONER.psql(
                container,
                "parkventory",
                "DROP TABLE public.unexpected_runtime_object;",
            )

            self.role_psql(
                docker,
                container,
                "platform_admin",
                platform_auth,
                """
CREATE TYPE public.unexpected_base_type;
CREATE FUNCTION public.unexpected_base_type_in(cstring)
RETURNS public.unexpected_base_type
AS 'int4in'
LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION public.unexpected_base_type_out(public.unexpected_base_type)
RETURNS cstring
AS 'int4out'
LANGUAGE internal IMMUTABLE STRICT;
CREATE TYPE public.unexpected_base_type (
  INPUT = public.unexpected_base_type_in,
  OUTPUT = public.unexpected_base_type_out,
  INTERNALLENGTH = 4,
  PASSEDBYVALUE,
  ALIGNMENT = int4,
  STORAGE = plain
);
ALTER FUNCTION public.unexpected_base_type_in(cstring)
  OWNER TO parkventory_owner;
ALTER FUNCTION public.unexpected_base_type_out(public.unexpected_base_type)
  OWNER TO parkventory_owner;
""",
            )
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "effective database roles or default privileges",
            ):
                PROVISIONER.reconcile_application_acl(container)
            PROVISIONER.psql(
                container,
                "parkventory",
                "DROP TYPE public.unexpected_base_type CASCADE;",
            )

            PROVISIONER.psql(
                container,
                "parkventory",
                """
CREATE ROLE extension_hijacker NOLOGIN;
DO $owner_drift$
DECLARE
  target record;
BEGIN
  SELECT
    namespace.nspname,
    routine.proname,
    pg_get_function_identity_arguments(routine.oid) AS arguments
    INTO STRICT target
  FROM pg_proc routine
  JOIN pg_namespace namespace ON namespace.oid = routine.pronamespace
  JOIN pg_depend dependency
    ON dependency.classid = 'pg_proc'::regclass
   AND dependency.objid = routine.oid
   AND dependency.objsubid = 0
   AND dependency.refclassid = 'pg_extension'::regclass
   AND dependency.deptype = 'e'
  JOIN pg_extension extension ON extension.oid = dependency.refobjid
  WHERE extension.extname = 'btree_gist'
  ORDER BY routine.oid
  LIMIT 1;
  EXECUTE format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO extension_hijacker',
    target.nspname,
    target.proname,
    target.arguments
  );
END
$owner_drift$;
""",
            )
            with self.assertRaisesRegex(
                PROVISIONER.ProvisionError,
                "application extension|row-level security",
            ):
                PROVISIONER.reconcile_application_acl(container)
            PROVISIONER.psql(
                container,
                "parkventory",
                """
DO $owner_restore$
DECLARE
  target record;
BEGIN
  SELECT
    namespace.nspname,
    routine.proname,
    pg_get_function_identity_arguments(routine.oid) AS arguments
    INTO STRICT target
  FROM pg_proc routine
  JOIN pg_namespace namespace ON namespace.oid = routine.pronamespace
  JOIN pg_depend dependency
    ON dependency.classid = 'pg_proc'::regclass
   AND dependency.objid = routine.oid
   AND dependency.objsubid = 0
   AND dependency.refclassid = 'pg_extension'::regclass
   AND dependency.deptype = 'e'
  JOIN pg_extension extension ON extension.oid = dependency.refobjid
  WHERE extension.extname = 'btree_gist'
    AND routine.proowner = 'extension_hijacker'::regrole
  LIMIT 1;
  EXECUTE format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO platform_admin',
    target.nspname,
    target.proname,
    target.arguments
  );
END
$owner_restore$;
DROP ROLE extension_hijacker;
""",
            )
            PROVISIONER.observe(container, require_rls=True)
        finally:
            PROVISIONER.command = original_command
            self.docker(
                docker or "docker",
                "rm",
                "--force",
                container,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
