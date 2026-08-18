#!/usr/bin/env python3

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OFFSITE_SCRIPT = ROOT / "scripts/postgres-offsite-backup"
LOCAL_SCRIPT = ROOT / "scripts/postgres-backup"
BACKUP_ID = "20260818T010203123456Z-abcdefabcdef"
IMAGE = (
    "ghcr.io/nclsppr/vps-infra/postgres:"
    "sha-0123456789abcdef0123456789abcdef01234567@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
RECIPIENT = "age1" + "q" * 58


class PostgreSQLOffsiteBackupTests(unittest.TestCase):
    def write_executable(self, path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)
        return path

    def make_local_backup(self, root: Path) -> None:
        root.mkdir(mode=0o700)
        backup = root / BACKUP_ID
        backup.mkdir(mode=0o700)
        globals_raw = b"CREATE ROLE platform_admin;\n"
        database_raw = b"PGDMPdatabase-content\n"
        (backup / "globals.sql").write_bytes(globals_raw)
        (backup / "database-5.dump").write_bytes(database_raw)
        files = [
            {
                "kind": "globals",
                "path": "globals.sql",
                "size": len(globals_raw),
                "sha256": hashlib.sha256(globals_raw).hexdigest(),
            },
            {
                "kind": "database",
                "path": "database-5.dump",
                "size": len(database_raw),
                "sha256": hashlib.sha256(database_raw).hexdigest(),
                "database_oid": 5,
                "database_name_hex": "postgres".encode().hex(),
            },
        ]
        manifest = {
            "contract": "vps-postgres-logical-backup-v1",
            "backup_id": BACKUP_ID,
            "created_at": "2026-08-18T01:02:03Z",
            "source": {
                "compose_project": "vps-platform",
                "compose_service": "postgresql",
                "image": IMAGE,
                "postgres_major": 17,
                "server_version_num": 170010,
                "system_identifier": "1234567890123456789",
            },
            "scope": {
                "method": "pg_dumpall-globals-plus-pg_dump-custom",
                "cross_database_snapshot_atomic": False,
                "encrypted": False,
                "offsite": False,
                "contains_role_password_hashes": True,
            },
            "roles": [
                {"name_hex": "platform_admin".encode().hex()},
                {"name_hex": "postgres_exporter".encode().hex()},
            ],
            "files": files,
        }
        manifest_raw = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        (backup / "manifest.json").write_bytes(manifest_raw)
        (backup / "manifest.sha256").write_text(
            hashlib.sha256(manifest_raw).hexdigest() + "\n",
            encoding="ascii",
        )
        for path in backup.iterdir():
            path.chmod(0o600)

    def make_runtime(self, root: Path, *, valid_gates: bool = True) -> dict[str, Path]:
        local_root = root / "local"
        offsite_root = root / "offsite"
        offsite_root.mkdir(mode=0o700)
        (offsite_root / "transactions").mkdir(mode=0o700)
        (offsite_root / "receipts").mkdir(mode=0o700)
        self.make_local_backup(local_root)

        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "contract": "vps-postgres-offsite-config-v1",
                    "endpoint": "https://s3.example.invalid",
                    "region": "fr-par",
                    "bucket": "backup-bucket",
                    "prefix": "atlas/postgresql",
                    "addressing_style": "path",
                    "age_recipient": RECIPIENT,
                    "gates": {
                        "bucket_object_lock_verified": valid_gates,
                        "bucket_versioning_verified": True,
                        "failure_domain_independence_reviewed": True,
                        "recovery_key_off_host_verified": True,
                        "restore_identity_separate_verified": True,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        config.chmod(0o444)
        aws_config = root / "aws-config"
        aws_config.write_text(
            "[default]\nregion = fr-par\ns3 =\n    addressing_style = path\n",
            encoding="ascii",
        )
        aws_config.chmod(0o444)
        credentials = root / "upload.credentials"
        credentials.write_text(
            "[default]\n"
            "aws_access_key_id = upload-access\n"
            "aws_secret_access_key = upload-secret\n",
            encoding="utf-8",
        )
        credentials.chmod(0o600)
        restore_credentials = root / "restore.credentials"
        restore_credentials.write_text(
            "[default]\n"
            "aws_access_key_id = restore-access\n"
            "aws_secret_access_key = restore-secret\n",
            encoding="utf-8",
        )
        restore_credentials.chmod(0o600)
        identity = root / "identity.age"
        identity.write_text("AGE-" + "SECRET-KEY-1EXAMPLE\n", encoding="ascii")
        identity.chmod(0o600)

        fake_age = self.write_executable(
            root / "age",
            """#!/usr/bin/env python3
import pathlib
import sys

prefix = b"FAKE-AGE-V1\\n"
if "--encrypt" in sys.argv:
    sys.stdout.buffer.write(prefix + sys.stdin.buffer.read())
elif "--decrypt" in sys.argv:
    raw = pathlib.Path(sys.argv[-1]).read_bytes()
    if not raw.startswith(prefix):
        raise SystemExit(9)
    sys.stdout.buffer.write(raw[len(prefix):])
else:
    raise SystemExit(8)
""",
        )
        fake_tar = self.write_executable(
            root / "tar",
            """#!/usr/bin/env python3
import pathlib
import sys
import tarfile

arguments = sys.argv[1:]
source = pathlib.Path(arguments[arguments.index("--directory") + 1])
names = arguments[arguments.index("--") + 1:]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    for name in names:
        path = source / name
        info = archive.gettarinfo(str(path), arcname=name)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.mode &= ~0o077
        if info.isdir():
            archive.addfile(info)
        else:
            with path.open("rb") as content:
                archive.addfile(info, content)
""",
        )
        fake_docker = self.write_executable(root / "docker", "#!/bin/sh\nexit 0\n")
        local_script = root / "postgres-backup"
        local_script.write_bytes(LOCAL_SCRIPT.read_bytes())
        local_script.chmod(0o700)
        fake_aws = self.write_executable(
            root / "aws",
            """#!/usr/bin/env python3
import base64
import hashlib
import json
import pathlib
import shutil
import sys

base = pathlib.Path(__file__).resolve().parent
(base / "aws.log").open("a", encoding="utf-8").write(json.dumps(sys.argv[1:]) + "\\n")
arguments = sys.argv[1:]

def value(name):
    return arguments[arguments.index(name) + 1]

operation = arguments[arguments.index("s3api") + 1]
bucket = value("--bucket")
key = value("--key")
remote = base / "remote" / bucket / key
version = base / "versions" / bucket / f"{key}.version-1"
metadata_file = base / "metadata" / bucket / f"{key}.json"
if operation == "put-object":
    if remote.exists() and value("--if-none-match") == "*":
        raise SystemExit(9)
    remote.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(value("--body"), remote)
    version.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(value("--body"), version)
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    object_metadata = dict(item.split("=", 1) for item in value("--metadata").split(","))
    metadata_file.write_text(json.dumps(object_metadata), encoding="utf-8")
    response = {}
    if not (base / "omit-checksum").exists():
        response["ChecksumSHA256"] = value("--checksum-sha256")
    if not (base / "omit-version").exists():
        response["VersionId"] = "version-1"
    print(json.dumps(response))
elif operation == "get-object":
    destination = pathlib.Path(arguments[-1])
    source = version if "--version-id" in arguments else remote
    if "--version-id" in arguments and value("--version-id") != "version-1":
        raise SystemExit(6)
    shutil.copyfile(source, destination)
    raw = source.read_bytes()
    response = {}
    if not (base / "omit-checksum").exists():
        response["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    if not (base / "omit-version").exists():
        response["VersionId"] = "version-1"
    response["Metadata"] = json.loads(metadata_file.read_text(encoding="utf-8"))
    print(json.dumps(response))
else:
    raise SystemExit(7)
""",
        )
        return {
            "local": local_root,
            "offsite": offsite_root,
            "config": config,
            "aws_config": aws_config,
            "credentials": credentials,
            "restore_credentials": restore_credentials,
            "identity": identity,
            "age": fake_age,
            "aws": fake_aws,
            "tar": fake_tar,
            "docker": fake_docker,
            "local_script": local_script,
        }

    def upload_command(self, paths: dict[str, Path]) -> list[str]:
        return [
            str(OFFSITE_SCRIPT),
            "upload",
            "--latest",
            "--test-root",
            str(paths["offsite"]),
            "--test-local-root",
            str(paths["local"]),
            "--test-config",
            str(paths["config"]),
            "--test-aws-config",
            str(paths["aws_config"]),
            "--test-credentials",
            str(paths["credentials"]),
            "--test-local-script",
            str(paths["local_script"]),
            "--test-age",
            str(paths["age"]),
            "--test-aws",
            str(paths["aws"]),
            "--test-tar",
            str(paths["tar"]),
        ]

    def run_upload(self, paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["VPS_POSTGRES_OFFSITE_TESTING"] = "1"
        return subprocess.run(
            self.upload_command(paths),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

    def recover_command(
        self,
        paths: dict[str, Path],
        destination: Path,
        approved_receipt: Path,
        *,
        rehearse: bool = False,
    ) -> list[str]:
        command = [
            str(OFFSITE_SCRIPT),
            "recover",
            "--config",
            str(paths["config"]),
            "--approved-receipt",
            str(approved_receipt),
            "--credentials-file",
            str(paths["restore_credentials"]),
            "--identity-file",
            str(paths["identity"]),
            "--destination",
            str(destination),
            "--backup-id",
            BACKUP_ID,
            "--age",
            str(paths["age"]),
            "--aws",
            str(paths["aws"]),
            "--postgres-backup",
            str(paths["local_script"]),
            "--docker",
            str(paths["docker"]),
        ]
        if rehearse:
            command.append("--rehearse")
        return command

    def retain_approved_receipt(self, paths: dict[str, Path], root: Path) -> Path:
        approved_root = root / "approved"
        approved_root.mkdir(mode=0o700)
        source = paths["offsite"] / "receipts" / f"{BACKUP_ID}.json"
        destination = approved_root / f"{BACKUP_ID}.json"
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o400)
        return destination

    def test_role_is_disabled_and_fail_closed_by_default(self) -> None:
        defaults = (
            ROOT / "ansible/roles/postgres_offsite_backup/defaults/main.yml"
        ).read_text(encoding="utf-8")
        tasks = (
            ROOT / "ansible/roles/postgres_offsite_backup/tasks/main.yml"
        ).read_text(encoding="utf-8")
        service = (
            ROOT
            / "ansible/roles/postgres_offsite_backup/templates/vps-postgres-offsite-backup.service.j2"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT
            / "ansible/roles/postgres_offsite_backup/templates/vps-postgres-offsite-backup.timer.j2"
        ).read_text(encoding="utf-8")
        playbook = (
            ROOT / "ansible/playbooks/postgres-offsite-backup.yml"
        ).read_text(encoding="utf-8")
        runner = (ROOT / "tests/run").read_text(encoding="utf-8")
        converge = (ROOT / "scripts/converge").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        controller = OFFSITE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("vps_postgres_offsite_state: stopped", defaults)
        self.assertEqual(defaults.count("_verified: false"), 4)
        self.assertIn("failure_domain_independence_reviewed: false", defaults)
        self.assertIn(
            "vps_postgres_offsite_packages:\n"
            "  - age\n"
            "  - awscli\n"
            "  - python3\n"
            "  - tar",
            defaults,
        )
        self.assertIn("vps_postgres_offsite_bucket_object_lock_verified | bool", tasks)
        self.assertIn("vps_postgres_offsite_bucket_versioning_verified | bool", tasks)
        self.assertIn("'ChecksumSHA256', 'IfNoneMatch'", tasks)
        self.assertIn("'VersionId', 'ChecksumMode'", tasks)
        self.assertIn("LoadCredential=upload:", service)
        self.assertNotIn("AGE-" + "SECRET-KEY", service)
        self.assertIn("ReadOnlyPaths=/srv/vps/backups/postgresql", service)
        self.assertIn("ReadWritePaths={{ vps_postgres_offsite_root }} /run/lock", service)
        self.assertIn(
            'credential_directory != "/run/credentials/vps-postgres-offsite-backup.service"',
            controller,
        )
        self.assertNotIn('credential_directory.startswith("/run/credentials/")', controller)
        self.assertIn("OnCalendar=*-*-* 05:17:00 UTC", timer)
        self.assertIn("    - role: postgres_offsite_backup", playbook)
        for mode, state in (
            ("--install-postgres-offsite-backup", "installed"),
            ("--stop-postgres-offsite-backup-schedule", "stopped"),
            ("--upload-postgres-offsite-now", "upload-now"),
        ):
            self.assertIn(f"{mode})", converge)
            self.assertIn(
                f"ansible_extra_options=(--extra-vars vps_postgres_offsite_state={state})",
                converge,
            )
        self.assertIn("--syntax-check playbooks/postgres-offsite-backup.yml", makefile)
        self.assertIn("install-postgres-offsite-backup:", makefile)
        self.assertIn("upload-postgres-offsite-now:", makefile)
        self.assertEqual(
            runner.count('python3 "$TESTS_DIR/test_postgres_offsite_backup.py"'),
            1,
        )

    def test_systemd_sandbox_hides_local_sockets_and_raw_secret_tree(self) -> None:
        service = (
            ROOT
            / "ansible/roles/postgres_offsite_backup/templates/vps-postgres-offsite-backup.service.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6", service)
        self.assertNotIn("AF_UNIX", service)
        inaccessible = next(
            line for line in service.splitlines() if line.startswith("InaccessiblePaths=")
        )
        for path in (
            "/etc/vps/secrets",
            "/run/docker.sock",
            "/var/run/docker.sock",
            "/run/systemd/private",
        ):
            self.assertIn(path, inaccessible)
        self.assertIn("LoadCredential=upload:", service)

    def test_upload_is_encrypted_versioned_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_runtime(Path(temporary))
            first = self.run_upload(paths)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("versioned-object=true", first.stdout)

            remote = (
                Path(temporary)
                / "remote/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age"
            )
            self.assertTrue(remote.read_bytes().startswith(b"FAKE-AGE-V1\n"))
            receipts = list((paths["offsite"] / "receipts").iterdir())
            self.assertEqual([path.name for path in receipts], [f"{BACKUP_ID}.json"])
            self.assertEqual(list((paths["offsite"] / "transactions").iterdir()), [])
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["contract"],
                "vps-postgres-offsite-receipt-v2",
            )
            self.assertRegex(
                receipt["recorded_at"],
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
            )
            self.assertNotIn("uploaded_at", receipt)
            self.assertEqual(receipt["object"]["version_id"], "version-1")
            self.assertEqual(
                receipt["object"]["cipher_sha256"],
                hashlib.sha256(remote.read_bytes()).hexdigest(),
            )

            second = self.run_upload(paths)
            self.assertEqual(second.returncode, 0, second.stderr)
            aws_calls = (Path(temporary) / "aws.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(aws_calls), 1)
            invocation = json.loads(aws_calls[0])
            self.assertIn("--if-none-match", invocation)
            self.assertNotIn("upload-access", " ".join(invocation))
            self.assertNotIn("upload-secret", " ".join(invocation))

    def test_upload_and_off_host_recovery_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            uploaded = self.run_upload(paths)
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            approved_receipt = self.retain_approved_receipt(paths, root)
            destination = root / "recovery"
            destination.mkdir(mode=0o700)
            recovered = subprocess.run(
                self.recover_command(paths, destination, approved_receipt),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn("recovered and verified", recovered.stdout)
            restored = destination / BACKUP_ID
            self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
            self.assertEqual(
                set(path.name for path in restored.iterdir()),
                {"globals.sql", "database-5.dump", "manifest.json", "manifest.sha256"},
            )
            for path in restored.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            calls = [
                json.loads(line)
                for line in (root / "aws.log").read_text(encoding="utf-8").splitlines()
            ]
            get_call = calls[-1]
            self.assertEqual(get_call[get_call.index("--version-id") + 1], "version-1")

    def test_recovery_uses_approved_version_not_replaced_latest_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            uploaded = self.run_upload(paths)
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            approved_receipt = self.retain_approved_receipt(paths, root)
            latest = (
                root
                / "remote/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age"
            )
            latest.write_bytes(b"replacement latest version")
            destination = root / "recovery"
            destination.mkdir(mode=0o700)
            recovered = subprocess.run(
                self.recover_command(paths, destination, approved_receipt),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertTrue((destination / BACKUP_ID / "manifest.json").is_file())

    def test_recovery_rejects_ciphertext_that_differs_from_approved_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            uploaded = self.run_upload(paths)
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            approved_receipt = self.retain_approved_receipt(paths, root)
            receipt = json.loads(approved_receipt.read_text(encoding="utf-8"))
            receipt["object"]["cipher_sha256"] = "0" * 64
            receipt["object"]["checksum_sha256"] = base64.b64encode(
                bytes.fromhex("0" * 64)
            ).decode("ascii")
            approved_receipt.chmod(0o600)
            approved_receipt.write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            approved_receipt.chmod(0o400)
            destination = root / "recovery"
            destination.mkdir(mode=0o700)
            rejected = subprocess.run(
                self.recover_command(paths, destination, approved_receipt),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rejected.returncode, 78)
            self.assertIn("differs from the approved receipt", rejected.stderr)
            self.assertFalse((destination / BACKUP_ID).exists())

    def test_false_provider_gate_prevents_encryption_and_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root, valid_gates=False)
            result = self.run_upload(paths)
            self.assertEqual(result.returncode, 78)
            self.assertIn("every off-site provider and recovery gate", result.stderr)
            self.assertFalse((root / "aws.log").exists())
            self.assertEqual(list((paths["offsite"] / "transactions").iterdir()), [])

    def test_aws_config_rejects_credential_process_and_extra_directives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            paths["aws_config"].chmod(0o644)
            paths["aws_config"].write_text(
                paths["aws_config"].read_text(encoding="utf-8")
                + "credential_process = /bin/false\n",
                encoding="utf-8",
            )
            paths["aws_config"].chmod(0o444)
            rejected = self.run_upload(paths)
            self.assertEqual(rejected.returncode, 78)
            self.assertIn("exact public contract", rejected.stderr)
            self.assertFalse((root / "aws.log").exists())

    def test_credentials_reject_defaults_interpolation_and_extra_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            candidates = {
                "default inheritance": (
                    "[DEFAULT]\n"
                    "aws_access_key_id = inherited-access\n"
                    "[default]\n"
                    "aws_secret_access_key = inherited-secret\n"
                ),
                "basic interpolation": (
                    "[default]\n"
                    "aws_access_key_id = upload-access\n"
                    "aws_secret_access_key = %(aws_access_key_id)s\n"
                ),
                "extended interpolation": (
                    "[default]\n"
                    "aws_access_key_id = upload-access\n"
                    "aws_secret_access_key = ${default:aws_access_key_id}\n"
                ),
                "extra key": (
                    "[default]\n"
                    "aws_access_key_id = upload-access\n"
                    "aws_secret_access_key = upload-secret\n"
                    "aws_session_token = forbidden\n"
                ),
                "extra section": (
                    "[default]\n"
                    "aws_access_key_id = upload-access\n"
                    "aws_secret_access_key = upload-secret\n"
                    "[another]\n"
                ),
            }
            for label, content in candidates.items():
                with self.subTest(label=label):
                    paths["credentials"].write_text(content, encoding="utf-8")
                    rejected = self.run_upload(paths)
                    self.assertEqual(rejected.returncode, 78)
                    self.assertIn("credential file", rejected.stderr)
            self.assertFalse((root / "aws.log").exists())

    def test_existing_local_backup_lock_stops_before_source_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            lock = paths["offsite"].parent / ".postgres-local.lock"
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.run_upload(paths)
            finally:
                os.close(descriptor)
            self.assertEqual(result.returncode, 78)
            self.assertIn("another operation owns local backup lock", result.stderr)
            self.assertFalse((root / "aws.log").exists())

    def test_missing_server_checksum_preserves_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            (root / "omit-checksum").touch()
            result = self.run_upload(paths)
            self.assertEqual(result.returncode, 78)
            self.assertIn("exact SHA-256 upload checksum", result.stderr)
            pending = list((paths["offsite"] / "transactions").iterdir())
            self.assertEqual(len(pending), 1)
            self.assertTrue((pending[0] / "bundle.age").is_file())
            self.assertEqual(list((paths["offsite"] / "receipts").iterdir()), [])

    def test_tampered_pending_ciphertext_is_rejected_without_second_put(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            (root / "omit-checksum").touch()
            first = self.run_upload(paths)
            self.assertEqual(first.returncode, 78)
            pending = next((paths["offsite"] / "transactions").iterdir())
            bundle = pending / "bundle.age"
            bundle.write_bytes(bundle.read_bytes() + b"tampered")
            (root / "omit-checksum").unlink()
            second = self.run_upload(paths)
            self.assertEqual(second.returncode, 78)
            self.assertIn("does not match its transaction", second.stderr)
            calls = (root / "aws.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 1)

    def test_complete_partial_transaction_is_promoted_after_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            (root / "omit-version").touch()
            interrupted = self.run_upload(paths)
            self.assertEqual(interrupted.returncode, 78)
            self.assertIn("versioned object identity", interrupted.stderr)
            pending = next((paths["offsite"] / "transactions").iterdir())
            encrypted = (pending / "bundle.age").read_bytes()
            partial = (
                paths["offsite"]
                / "transactions"
                / f".partial-{BACKUP_ID}-0123456789ab"
            )
            pending.rename(partial)
            remote = (
                root
                / "remote/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age"
            )
            version = (
                root
                / "versions/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age.version-1"
            )
            metadata = (
                root
                / "metadata/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age.json"
            )
            for path in (remote, version, metadata):
                path.unlink()
            (root / "omit-version").unlink()
            resumed = self.run_upload(paths)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(remote.read_bytes(), encrypted)
            self.assertEqual(list((paths["offsite"] / "transactions").iterdir()), [])

    def test_ambiguous_put_is_reconciled_off_host_without_second_put(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            (root / "omit-checksum").touch()
            ambiguous = self.run_upload(paths)
            self.assertEqual(ambiguous.returncode, 78)
            pending = next((paths["offsite"] / "transactions").iterdir())
            transaction_copy = root / "pending-transaction.json"
            transaction_copy.write_bytes((pending / "transaction.json").read_bytes())
            transaction_copy.chmod(0o400)
            (root / "omit-checksum").unlink()
            work_root = root / "reconcile-work"
            approved_root = root / "reconciled"
            work_root.mkdir(mode=0o700)
            approved_root.mkdir(mode=0o700)
            approved_receipt = approved_root / f"{BACKUP_ID}.json"
            reconciled = subprocess.run(
                [
                    str(OFFSITE_SCRIPT),
                    "reconcile",
                    "--config",
                    str(paths["config"]),
                    "--credentials-file",
                    str(paths["restore_credentials"]),
                    "--transaction",
                    str(transaction_copy),
                    "--work-root",
                    str(work_root),
                    "--output-receipt",
                    str(approved_receipt),
                    "--aws",
                    str(paths["aws"]),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            self.assertEqual(stat.S_IMODE(approved_receipt.stat().st_mode), 0o400)
            reconciled_receipt = json.loads(
                approved_receipt.read_text(encoding="utf-8")
            )
            self.assertEqual(
                reconciled_receipt["contract"],
                "vps-postgres-offsite-receipt-v2",
            )
            self.assertIn("recorded_at", reconciled_receipt)
            self.assertNotIn("uploaded_at", reconciled_receipt)
            atlas_receipt = paths["offsite"] / "receipts" / f"{BACKUP_ID}.json"
            approved_raw = approved_receipt.read_bytes()
            mismatched = json.loads(approved_raw)
            mismatched["object"]["cipher_sha256"] = "0" * 64
            mismatched["object"]["checksum_sha256"] = base64.b64encode(
                bytes.fromhex("0" * 64)
            ).decode("ascii")
            atlas_receipt.write_text(
                json.dumps(mismatched, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            atlas_receipt.chmod(0o600)
            rejected_receipt = self.run_upload(paths)
            self.assertEqual(rejected_receipt.returncode, 78)
            self.assertIn("differs from the pending transaction", rejected_receipt.stderr)
            self.assertTrue(pending.is_dir())
            atlas_receipt.write_bytes(approved_raw)
            atlas_receipt.chmod(0o600)
            linked_partial = (
                paths["offsite"]
                / "receipts"
                / f".{BACKUP_ID}.0123456789ab.partial"
            )
            os.link(atlas_receipt, linked_partial)
            resumed = self.run_upload(paths)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(list((paths["offsite"] / "transactions").iterdir()), [])
            self.assertFalse(linked_partial.exists())
            self.assertEqual(atlas_receipt.stat().st_nlink, 1)
            calls = [
                json.loads(line)
                for line in (root / "aws.log").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum("put-object" in call for call in calls),
                1,
            )
            self.assertEqual(
                sum("get-object" in call for call in calls),
                1,
            )

    def test_recovery_rejects_link_member_without_writing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_runtime(root)
            uploaded = self.run_upload(paths)
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            approved_receipt = self.retain_approved_receipt(paths, root)
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
                directory = tarfile.TarInfo(BACKUP_ID)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o700
                archive.addfile(directory)
                link = tarfile.TarInfo(f"{BACKUP_ID}/manifest.json")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                archive.addfile(link)
                checksum = b"0" * 64 + b"\n"
                checksum_info = tarfile.TarInfo(f"{BACKUP_ID}/manifest.sha256")
                checksum_info.size = len(checksum)
                archive.addfile(checksum_info, io.BytesIO(checksum))
            malicious = b"FAKE-AGE-V1\n" + archive_buffer.getvalue()
            version = (
                root
                / "versions/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age.version-1"
            )
            version.write_bytes(malicious)
            digest = hashlib.sha256(malicious).digest()
            receipt = json.loads(approved_receipt.read_text(encoding="utf-8"))
            receipt["object"]["cipher_size"] = len(malicious)
            receipt["object"]["cipher_sha256"] = digest.hex()
            receipt["object"]["checksum_sha256"] = base64.b64encode(digest).decode()
            approved_receipt.chmod(0o600)
            approved_receipt.write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            approved_receipt.chmod(0o400)
            metadata_path = (
                root
                / "metadata/backup-bucket/atlas/postgresql"
                / f"{BACKUP_ID}.tar.age.json"
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["cipher-sha256"] = digest.hex()
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            destination = root / "recovery"
            destination.mkdir(mode=0o700)
            result = subprocess.run(
                self.recover_command(paths, destination, approved_receipt),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("unsafe member", result.stderr)
            self.assertFalse((destination / BACKUP_ID).exists())


if __name__ == "__main__":
    unittest.main()
