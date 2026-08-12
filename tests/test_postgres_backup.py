#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts/postgres-backup"
POSTGRES_IMAGE = (
    "ghcr.io/nclsppr/vps-infra/postgres:"
    "sha-e45dc731d1db291a2b14d7db46eb95d90a06750c@"
    "sha256:f26c37a44c6d2286fe6794a2bc7d18c23907c8bdb9ffc3e0890e07be713d6095"
)


class PostgresBackupTests(unittest.TestCase):
    def test_ansible_and_systemd_contract_is_bounded(self) -> None:
        playbook = (ROOT / "ansible/playbooks/postgres-backup.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("    - role: postgres_backup", playbook)
        defaults = (
            ROOT / "ansible/roles/postgres_backup/defaults/main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "vps_postgres_backup_root: /srv/vps/backups/postgresql", defaults
        )
        self.assertIn("vps_postgres_backup_retention_count: 7", defaults)

        tasks = (ROOT / "ansible/roles/postgres_backup/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Disable the PostgreSQL backup timers without deleting backup data", tasks)
        timer_enablement = tasks.split(
            "- name: Enable the daily backup and monthly restore rehearsal timers\n",
            maxsplit=1,
        )[1].split(
            "- name: Disable the PostgreSQL backup timers without deleting backup data\n",
            maxsplit=1,
        )[0]
        self.assertIn(
            "when: vps_postgres_backup_state == 'installed'", timer_enablement
        )
        self.assertNotIn("!= 'stopped'", timer_enablement)
        self.assertIn("Read PostgreSQL backup timer activity", tasks)
        self.assertIn("item.stdout in ['inactive', 'failed']", tasks)
        self.assertNotIn("state: absent", tasks)
        self.assertNotIn("docker compose down", tasks)
        self.assertNotIn("--volumes", tasks)

        backup_unit = (
            ROOT
            / "ansible/roles/postgres_backup/templates/vps-postgres-backup.service.j2"
        ).read_text(encoding="utf-8")
        restore_unit = (
            ROOT
            / "ansible/roles/postgres_backup/templates/vps-postgres-restore-rehearsal.service.j2"
        ).read_text(encoding="utf-8")
        for unit in (backup_unit, restore_unit):
            self.assertIn("PrivateNetwork=yes", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
            self.assertIn("CapabilityBoundingSet=", unit)
            self.assertNotIn("/var/lib/docker", unit)
            self.assertNotIn("/var/lib/postgresql", unit)
        self.assertIn("ReadOnlyPaths={{ vps_postgres_backup_root }}", restore_unit)

        backup_timer = (
            ROOT / "ansible/roles/postgres_backup/templates/vps-postgres-backup.timer.j2"
        ).read_text(encoding="utf-8")
        restore_timer = (
            ROOT
            / "ansible/roles/postgres_backup/templates/vps-postgres-restore-rehearsal.timer.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:17:00 UTC", backup_timer)
        self.assertIn("OnCalendar=Sun *-*-01..07 04:17:00 UTC", restore_timer)
        self.assertIn("Persistent=true", backup_timer)
        self.assertIn("Persistent=true", restore_timer)

        converge = (ROOT / "scripts/converge").read_text(encoding="utf-8")
        for mode, state in (
            ("--install-postgres-backup", "installed"),
            ("--stop-postgres-backup-schedule", "stopped"),
            ("--backup-postgres-now", "backup-now"),
            ("--rehearse-postgres-restore", "rehearse-latest"),
        ):
            self.assertIn(mode, converge)
            self.assertIn(f"vps_postgres_backup_state={state}", converge)

    def make_fake_docker(self, root: Path) -> tuple[Path, Path, Path, Path]:
        fake = root / "docker"
        log = root / "docker-commands.jsonl"
        failure_marker = root / "fail-restore"
        cleanup_failure_marker = root / "fail-cleanup"
        fake.write_text(
            f"""#!/usr/bin/env python3
import json
import pathlib
import sys

log = pathlib.Path({str(log)!r})
failure_marker = pathlib.Path({str(failure_marker)!r})
cleanup_failure_marker = pathlib.Path({str(cleanup_failure_marker)!r})
arguments = sys.argv[1:]
with log.open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments, separators=(",", ":")) + "\\n")

production_id = "a" * 64
image = {POSTGRES_IMAGE!r}
database_rows = [
    {{"oid": 5, "name_hex": "706f737467726573", "allow_connections": True}},
    {{"oid": 16384, "name_hex": "737572706c61737365", "allow_connections": True}},
]
role_rows = [
    {{"name_hex": "706c6174666f726d5f61646d696e"}},
    {{"name_hex": "706f7374677265735f6578706f72746572"}},
    {{"name_hex": "737572706c617373655f72756e74696d65"}},
]

def json_lines(rows):
    for row in rows:
        print(json.dumps(row, separators=(",", ":")))

if not arguments:
    raise SystemExit(2)
if arguments[0] == "ps":
    print(production_id)
elif arguments[0] == "inspect":
    print(json.dumps([{{
        "Config": {{
            "Image": image,
            "User": "70:70",
            "Labels": {{
                "com.docker.compose.project": "vps-platform",
                "com.docker.compose.service": "postgresql",
            }},
        }},
        "State": {{"Running": True, "Health": {{"Status": "healthy"}}}},
    }}]))
elif arguments[:2] == ["image", "inspect"]:
    pass
elif arguments[:2] == ["volume", "create"]:
    print(arguments[-1])
elif arguments[0] == "run":
    if "POSTGRES_USER=platform_admin" not in arguments:
        raise SystemExit(14)
    print("b" * 64)
elif arguments[0] == "rm":
    if cleanup_failure_marker.exists():
        raise SystemExit(10)
elif arguments[:2] == ["volume", "rm"]:
    if cleanup_failure_marker.exists():
        raise SystemExit(11)
elif arguments[0] == "exec":
    index = 1
    while index < len(arguments) and arguments[index].startswith("--"):
        option = arguments[index]
        index += 1
        if option == "--env":
            index += 1
    container = arguments[index]
    command = arguments[index + 1:]
    executable = command[0]
    command_text = " ".join(command)
    if executable == "pg_isready":
        pass
    elif executable == "pg_dumpall":
        sys.stdout.buffer.write(
            b"-- PostgreSQL globals\\n"
            b"CREATE ROLE platform_admin;\\n"
            b"ALTER ROLE platform_admin WITH SUPERUSER LOGIN PASSWORD 'synthetic';\\n"
            b"CREATE ROLE postgres_exporter;\\n"
            b"ALTER ROLE postgres_exporter SET search_path TO 'pg_catalog';\\n"
            b"GRANT pg_monitor TO postgres_exporter WITH INHERIT TRUE "
            b"GRANTED BY platform_admin;\\n"
        )
    elif executable == "pg_dump":
        database = next(
            value.split("=", 1)[1]
            for position, value in enumerate(arguments)
            if position > 0 and arguments[position - 1] == "--env" and value.startswith("PGDATABASE=")
        )
        sys.stdout.buffer.write(b"PGDMP" + database.encode("utf-8"))
    elif executable == "pg_restore":
        archive = sys.stdin.buffer.read()
        if not archive.startswith(b"PGDMP"):
            raise SystemExit(12)
        if "--list" in command and command != ["pg_restore", "--list"]:
            raise SystemExit(13)
        if failure_marker.exists() and "--create" in command:
            raise SystemExit(9)
    elif executable == "psql":
        if "pg_control_system" in command_text:
            json_lines([{{
                "server_version_num": 170010,
                "system_identifier": "7612345678901234567",
                "custom_tablespaces": 0,
                "platform_admin_oid": 10,
                "platform_admin_superuser": True,
                "exporter_membership_count": 1,
                "database_bytes": 1024,
            }}])
        elif "FROM pg_database" in command_text:
            if container == production_id:
                json_lines(database_rows)
            else:
                json_lines([{{"name_hex": row["name_hex"]}} for row in database_rows])
        elif "FROM pg_roles" in command_text:
            if container != production_id and "current_user" in command_text:
                raise SystemExit(17)
            json_lines(role_rows)
        elif "FROM pg_auth_members" in command_text:
            json_lines([{{"count": 1}}])
        elif not any(value.startswith("--command=") for value in command):
            globals_input = sys.stdin.buffer.read()
            if b"CREATE ROLE platform_admin;\\n" in globals_input:
                raise SystemExit(15)
            for required in (
                b"ALTER ROLE platform_admin WITH SUPERUSER LOGIN PASSWORD 'synthetic';\\n",
                b"ALTER ROLE postgres_exporter SET search_path TO 'pg_catalog';\\n",
                b"GRANT pg_monitor TO postgres_exporter WITH INHERIT TRUE "
                b"GRANTED BY platform_admin;\\n",
            ):
                if required not in globals_input:
                    raise SystemExit(16)
    else:
        raise SystemExit(3)
else:
    raise SystemExit(4)
""",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return fake, log, failure_marker, cleanup_failure_marker

    def run_backup(
        self,
        backup_root: Path,
        fake_docker: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_POSTGRES_BACKUP_TESTING"] = "1"
        return subprocess.run(
            [
                str(BACKUP_SCRIPT),
                "--test-root",
                str(backup_root),
                "--docker",
                str(fake_docker),
                *arguments,
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def test_create_and_verify_bounded_local_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_root = root / "backups"
            backup_root.mkdir(mode=0o700)
            fake, log, _, _ = self.make_fake_docker(root)

            created = self.run_backup(
                backup_root,
                fake,
                "create",
                "--retention-count",
                "2",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertIn("offsite=false; encrypted=false", created.stdout)
            backup_directories = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(len(backup_directories), 1)
            backup = backup_directories[0]
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["contract"], "vps-postgres-logical-backup-v1")
            self.assertEqual(manifest["source"]["image"], POSTGRES_IMAGE)
            self.assertEqual(
                manifest["scope"],
                {
                    "method": "pg_dumpall-globals-plus-pg_dump-custom",
                    "cross_database_snapshot_atomic": False,
                    "encrypted": False,
                    "offsite": False,
                    "contains_role_password_hashes": True,
                },
            )
            for path in backup.iterdir():
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            verified = self.run_backup(backup_root, fake, "verify", "--latest")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(command[:1] == ["ps"] for command in commands))
            archive_verifications = [
                command
                for command in commands
                if "pg_restore" in command and "--list" in command
            ]
            self.assertEqual(
                archive_verifications,
                [
                    ["exec", "--interactive", "a" * 64, "pg_restore", "--list"],
                    ["exec", "--interactive", "a" * 64, "pg_restore", "--list"],
                ],
            )
            self.assertFalse(any(command[:1] in (["rm"], ["volume"]) for command in commands))

    def test_verifier_rejects_modified_archive_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_root = root / "backups"
            backup_root.mkdir(mode=0o700)
            fake, _, _, _ = self.make_fake_docker(root)
            created = self.run_backup(backup_root, fake, "create", "--retention-count", "2")
            self.assertEqual(created.returncode, 0, created.stderr)
            backup = next(path for path in backup_root.iterdir() if path.is_dir())
            archive = backup / "database-5.dump"
            archive.write_bytes(archive.read_bytes() + b"tampered")
            modified = self.run_backup(backup_root, fake, "verify", "--latest")
            self.assertNotEqual(modified.returncode, 0)
            self.assertIn("size does not match", modified.stderr)

            archive.unlink()
            outside = root / "outside"
            outside.write_bytes(b"PGDMPunchanged")
            archive.symlink_to(outside)
            linked = self.run_backup(backup_root, fake, "verify", "--latest")
            self.assertNotEqual(linked.returncode, 0)
            self.assertEqual(outside.read_bytes(), b"PGDMPunchanged")

    def test_restore_rehearsal_is_disposable_and_cleans_up_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_root = root / "backups"
            backup_root.mkdir(mode=0o700)
            fake, log, failure_marker, cleanup_failure_marker = self.make_fake_docker(root)
            created = self.run_backup(backup_root, fake, "create", "--retention-count", "2")
            self.assertEqual(created.returncode, 0, created.stderr)

            rehearsed = self.run_backup(backup_root, fake, "rehearse", "--latest")
            self.assertEqual(rehearsed.returncode, 0, rehearsed.stderr)
            commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            run_command = next(command for command in commands if command[:1] == ["run"])
            for required in (
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "no-new-privileges:true",
            ):
                self.assertIn(required, run_command)
            self.assertEqual(run_command.count("--mount"), 1)
            self.assertTrue(
                any(
                    value.startswith("type=volume,source=vps-postgres-restore-")
                    and value.endswith(",target=/var/lib/postgresql/data")
                    for value in run_command
                )
            )
            serialized_commands = json.dumps(commands)
            self.assertNotIn("vps-platform-postgresql-17-data", serialized_commands)
            self.assertNotIn("/srv/vps/backups/postgresql", serialized_commands)
            self.assertTrue(any(command[:2] == ["rm", "--force"] for command in commands))
            self.assertTrue(any(command[:2] == ["volume", "rm"] for command in commands))

            log.unlink()
            failure_marker.touch()
            failed = self.run_backup(backup_root, fake, "rehearse", "--latest")
            self.assertNotEqual(failed.returncode, 0)
            failure_commands = [
                json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(command[:2] == ["rm", "--force"] for command in failure_commands)
            )
            self.assertTrue(
                any(command[:2] == ["volume", "rm"] for command in failure_commands)
            )

            log.unlink()
            cleanup_failure_marker.touch()
            cleanup_failed = self.run_backup(backup_root, fake, "rehearse", "--latest")
            self.assertNotEqual(cleanup_failed.returncode, 0)
            self.assertIn("disposable restore cleanup also failed", cleanup_failed.stderr)
            self.assertIn("scratch container cleanup", cleanup_failed.stderr)
            self.assertIn("scratch volume cleanup", cleanup_failed.stderr)

    def test_restore_rejects_ambiguous_bootstrap_role_filter_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_root = root / "backups"
            backup_root.mkdir(mode=0o700)
            fake, log, _, _ = self.make_fake_docker(root)
            created = self.run_backup(backup_root, fake, "create", "--retention-count", "2")
            self.assertEqual(created.returncode, 0, created.stderr)
            backup = next(path for path in backup_root.iterdir() if path.is_dir())
            manifest_path = backup / "manifest.json"
            checksum_path = backup / "manifest.sha256"

            for globals_content in (
                b"-- zero exact bootstrap declarations\nCREATE ROLE postgres_exporter;\n",
                b"CREATE ROLE platform_admin;\nCREATE ROLE platform_admin;\n",
                b'CREATE ROLE "platform_admin";\nCREATE ROLE postgres_exporter;\n',
            ):
                globals_path = backup / "globals.sql"
                globals_path.write_bytes(globals_content)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                globals_entry = next(
                    entry for entry in manifest["files"] if entry["kind"] == "globals"
                )
                globals_entry["size"] = len(globals_content)
                globals_entry["sha256"] = hashlib.sha256(globals_content).hexdigest()
                manifest_bytes = (
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                manifest_path.write_bytes(manifest_bytes)
                checksum_path.write_bytes(
                    hashlib.sha256(manifest_bytes).hexdigest().encode("ascii") + b"\n"
                )

                log.unlink(missing_ok=True)
                rejected = self.run_backup(backup_root, fake, "rehearse", "--latest")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(
                    "must contain one exact platform_admin creation statement",
                    rejected.stderr,
                )
                commands = [
                    json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                ]
                self.assertFalse(
                    any(
                        command[:2] == ["exec", "--interactive"] and "psql" in command
                        for command in commands
                    )
                )
                self.assertTrue(any(command[:2] == ["rm", "--force"] for command in commands))
                self.assertTrue(any(command[:2] == ["volume", "rm"] for command in commands))

    @unittest.skipUnless(
        os.environ.get("VPS_POSTGRES_BACKUP_INTEGRATION") == "1",
        "exact PostgreSQL image integration is opt-in",
    )
    def test_exact_postgres17_globals_grantor_restore(self) -> None:
        docker_name = shutil.which("docker")
        self.assertIsNotNone(docker_name, "docker is required for the integration test")
        assert docker_name is not None
        docker = str(Path(docker_name).absolute())
        suffix = uuid.uuid4().hex[:12]
        source_name = f"vps-postgres-source-test-{suffix}"
        source_volume = source_name

        def docker_capture(*arguments: str) -> str:
            result = subprocess.run(
                [docker, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        def rehearsal_resources() -> tuple[set[str], set[str]]:
            containers = set(
                filter(
                    None,
                    docker_capture(
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        "label=com.nclsppr.vps-infra.restore-rehearsal=true",
                    ).splitlines(),
                )
            )
            volumes = set(
                filter(
                    None,
                    docker_capture(
                        "volume",
                        "ls",
                        "--quiet",
                        "--filter",
                        "label=com.nclsppr.vps-infra.restore-rehearsal=true",
                    ).splitlines(),
                )
            )
            return containers, volumes

        baseline_containers, baseline_volumes = rehearsal_resources()
        source_created = False
        volume_created = False
        try:
            docker_capture("image", "inspect", POSTGRES_IMAGE)
            volume_output = docker_capture("volume", "create", source_volume)
            volume_created = True
            self.assertEqual(volume_output, source_volume)
            container_id = docker_capture(
                "run",
                "--detach",
                "--pull=never",
                "--name",
                source_name,
                "--label",
                "com.docker.compose.project=vps-platform",
                "--label",
                "com.docker.compose.service=postgresql",
                "--network",
                "none",
                "--user",
                "70:70",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:size=64m,mode=1777",
                "--tmpfs",
                "/var/run/postgresql:size=16m,mode=2775,uid=70,gid=70",
                "--mount",
                f"type=volume,source={source_volume},target=/var/lib/postgresql/data",
                "--health-cmd",
                "pg_isready --host=/var/run/postgresql --username=platform_admin --dbname=postgres",
                "--health-interval",
                "1s",
                "--health-timeout",
                "2s",
                "--health-retries",
                "60",
                "--env",
                "POSTGRES_DB=postgres",
                "--env",
                "POSTGRES_USER=platform_admin",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-local=trust --auth-host=trust --data-checksums",
                "--env",
                "PGDATA=/var/lib/postgresql/data/pgdata",
                POSTGRES_IMAGE,
                "postgres",
                "-c",
                "listen_addresses=",
                "-c",
                "unix_socket_directories=/var/run/postgresql",
            )
            source_created = True
            self.assertRegex(container_id, r"^[0-9a-f]{64}$")
            for _ in range(90):
                inspected_health = docker_capture(
                    "inspect", "--format", "{{.State.Health.Status}}", source_name
                )
                if inspected_health == "healthy":
                    break
                if inspected_health == "unhealthy":
                    self.fail("synthetic PostgreSQL source became unhealthy")
                time.sleep(1)
            else:
                self.fail("synthetic PostgreSQL source did not become healthy")

            docker_capture(
                "exec",
                source_name,
                "psql",
                "-X",
                "--host=/var/run/postgresql",
                "--username=platform_admin",
                "--dbname=postgres",
                "--set=ON_ERROR_STOP=on",
                "--command=CREATE ROLE postgres_exporter LOGIN PASSWORD 'synthetic-only'; "
                "GRANT pg_monitor TO postgres_exporter; "
                "ALTER ROLE postgres_exporter SET search_path = pg_catalog; "
                "CREATE ROLE surplasse_runtime LOGIN PASSWORD 'synthetic-only';",
            )
            docker_capture(
                "exec",
                source_name,
                "createdb",
                "--host=/var/run/postgresql",
                "--username=platform_admin",
                "--owner=platform_admin",
                "surplasse",
            )

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                backup_root = root / "backups"
                backup_root.mkdir(mode=0o700)
                wrapper = root / "docker-wrapper"
                wrapper.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = ps ]; then\n"
                    f"  printf '%s\\n' {container_id!r}\n"
                    "  exit 0\n"
                    "fi\n"
                    f"exec {docker!r} \"$@\"\n",
                    encoding="utf-8",
                )
                wrapper.chmod(0o700)
                created = self.run_backup(
                    backup_root, wrapper, "create", "--retention-count", "2"
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                rehearsed = self.run_backup(backup_root, wrapper, "rehearse", "--latest")
                self.assertEqual(rehearsed.returncode, 0, rehearsed.stderr)

            final_containers, final_volumes = rehearsal_resources()
            self.assertEqual(final_containers, baseline_containers)
            self.assertEqual(final_volumes, baseline_volumes)
        finally:
            current_containers, current_volumes = rehearsal_resources()
            for identifier in sorted(current_containers - baseline_containers):
                subprocess.run(
                    [docker, "rm", "--force", identifier],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            for name in sorted(current_volumes - baseline_volumes):
                subprocess.run(
                    [docker, "volume", "rm", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if source_created:
                subprocess.run(
                    [docker, "rm", "--force", source_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if volume_created:
                subprocess.run(
                    [docker, "volume", "rm", source_volume],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def test_retention_deletes_only_verified_complete_backup_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_root = root / "backups"
            backup_root.mkdir(mode=0o700)
            unknown = backup_root / "operator-note"
            unknown.write_text("preserve\n", encoding="utf-8")
            fake, _, _, _ = self.make_fake_docker(root)
            for _ in range(3):
                result = self.run_backup(
                    backup_root,
                    fake,
                    "create",
                    "--retention-count",
                    "2",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            backups = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 2)
            self.assertEqual(unknown.read_text(encoding="utf-8"), "preserve\n")


if __name__ == "__main__":
    unittest.main()
