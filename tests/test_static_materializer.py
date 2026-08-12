#!/usr/bin/env python3

from __future__ import annotations

import copy
import dataclasses
import gzip
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-static"
REVISION = "0123456789abcdef0123456789abcdef01234567"
CREATED = "2026-08-12T09:00:00+02:00"


def load_script_module():
    loader = SourceFileLoader("static_materializer", str(SCRIPT))
    spec = importlib.util.spec_from_loader("static_materializer", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["static_materializer"] = module
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_script_module()


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def producer_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def write_gzip(source: Path, destination: Path) -> None:
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=output_stream,
            compresslevel=9,
            mtime=0,
        ) as compressed:
            shutil.copyfileobj(input_stream, compressed)


def create_personal_archive(root: Path, files: dict[str, bytes]) -> tuple[Path, str]:
    repository = root / "personal-source"
    repository.mkdir()
    for relative, content in files.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    subprocess.run(["git", "init", "--quiet", repository], check=True)
    subprocess.run(["git", "-C", repository, "config", "user.name", "Static Test"], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "static-test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2026-08-12T07:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-12T07:00:00Z",
        }
    )
    subprocess.run(
        ["git", "-C", repository, "commit", "--quiet", "-m", "fixture"],
        check=True,
        env=environment,
    )
    revision = subprocess.run(
        ["git", "-C", repository, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tar_path = root / "personal.tar"
    with tar_path.open("wb") as output_stream:
        subprocess.run(
            ["git", "-C", repository, "archive", "--format=tar", "--prefix=site/", revision],
            check=True,
            stdout=output_stream,
        )
    archive = root / "personal.tar.gz"
    write_gzip(tar_path, archive)
    return archive, revision


def add_tar_entry(
    archive: tarfile.TarFile,
    name: str,
    *,
    data: bytes = b"",
    type_flag: bytes = tarfile.REGTYPE,
    mode: int = 0o644,
    linkname: str = "",
) -> None:
    member = tarfile.TarInfo(name)
    member.type = type_flag
    member.mode = mode
    member.uid = 0
    member.gid = 0
    member.mtime = 0
    member.linkname = linkname
    if type_flag in {tarfile.REGTYPE, tarfile.AREGTYPE}:
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    else:
        member.size = 0
        archive.addfile(member)


def create_papers_archive(
    root: Path,
    files: dict[str, bytes],
    *,
    extra_entries: list[dict[str, object]] | None = None,
    name: str = "papers",
) -> Path:
    tar_path = root / f"{name}.tar"
    directories = {"site"}
    for relative in files:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add(f"site/{'/'.join(parts[:index])}")
    with tarfile.open(tar_path, "w", format=tarfile.GNU_FORMAT) as archive:
        for directory in sorted(directories):
            add_tar_entry(
                archive,
                directory,
                type_flag=tarfile.DIRTYPE,
                mode=0o755,
            )
        for relative, content in sorted(files.items()):
            add_tar_entry(archive, f"site/{relative}", data=content)
        for entry in extra_entries or []:
            add_tar_entry(archive, **entry)
    destination = root / f"{name}.tar.gz"
    write_gzip(tar_path, destination)
    return destination


def inventory_value(
    profile,
    archive: Path,
    revision: str,
    files: dict[str, bytes],
) -> dict[str, object]:
    archive_bytes = archive.read_bytes()
    routes = []
    for relative, content in files.items():
        routes.append(
            {
                "bytes": len(content),
                "file": relative,
                "path": MATERIALIZER.route_for_file(relative),
                "sha256": hashlib.sha256(content).hexdigest(),
                "status": 200,
            }
        )
    return {
        "contract": "vps-infra.route-inventory.v1",
        "schema": 1,
        "site": {
            "archive_bytes": len(archive_bytes),
            "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "file_count": len(files),
            "uncompressed_bytes": sum(len(content) for content in files.values()),
        },
        "source": {
            "repository": profile.source_repository,
            "revision": revision,
        },
        "routes": routes,
    }


def write_inventory(root: Path, value: dict[str, object], name: str = "routes.json") -> Path:
    destination = root / name
    destination.write_bytes(canonical_json(value))
    return destination


def layer_for(archive: Path) -> object:
    content = archive.read_bytes()
    return MATERIALIZER.LayerContract(
        manifest_digest=f"sha256:{'1' * 64}",
        layer_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        layer_size=len(content),
        created=CREATED,
    )


def populate_worker_tree(
    root: Path,
    files: dict[str, bytes],
    *,
    file_mode: int,
    directory_mode: int,
) -> None:
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(file_mode)
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        directory.chmod(directory_mode)


def manifest_value(
    profile,
    revision: str,
    layer,
    *,
    kind: str,
) -> dict[str, object]:
    if kind == "site":
        artifact_type = MATERIALIZER.SITE_ARTIFACT_TYPE
        media_type = MATERIALIZER.SITE_LAYER_MEDIA_TYPE
        title = "site.tar.gz"
    else:
        artifact_type = MATERIALIZER.ROUTES_ARTIFACT_TYPE
        media_type = MATERIALIZER.ROUTES_LAYER_MEDIA_TYPE
        title = "routes.json"
    return {
        "schemaVersion": 2,
        "mediaType": MATERIALIZER.OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": artifact_type,
        "config": copy.deepcopy(MATERIALIZER.OCI_EMPTY_CONFIG),
        "layers": [
            {
                "mediaType": media_type,
                "digest": layer.layer_digest,
                "size": layer.layer_size,
                "annotations": {MATERIALIZER.TITLE_ANNOTATION: title},
            }
        ],
        "annotations": {
            MATERIALIZER.CREATED_ANNOTATION: CREATED,
            MATERIALIZER.REVISION_ANNOTATION: revision,
            MATERIALIZER.SOURCE_ANNOTATION: profile.source_url,
        },
    }


class StaticBundleContractTests(unittest.TestCase):
    def validate_fixture(
        self,
        root: Path,
        profile,
        archive: Path,
        revision: str,
        files: dict[str, bytes],
    ):
        value = inventory_value(profile, archive, revision, files)
        inventory_path = write_inventory(root, value)
        layer = layer_for(archive)
        return MATERIALIZER.validate_bundle(
            archive,
            inventory_path,
            profile,
            revision,
            layer,
        )

    def test_accepts_both_real_producer_archive_profiles(self) -> None:
        files = {
            "index.html": b"home\n",
            "assets/site.css": b"body{}\n",
            "docs/index.html": b"docs\n",
            "docs/accessibility.md": b"source\n",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal_archive, personal_revision = create_personal_archive(root, files)
            personal_root = root / "personal-contract"
            personal_root.mkdir()
            personal = self.validate_fixture(
                personal_root,
                MATERIALIZER.PROFILES["personal"],
                personal_archive,
                personal_revision,
                files,
            )
            self.assertEqual(personal.file_count, len(files))

            papers_archive = create_papers_archive(root, files)
            papers_root = root / "papers-contract"
            papers_root.mkdir()
            papers = self.validate_fixture(
                papers_root,
                MATERIALIZER.PROFILES["papersempire"],
                papers_archive,
                REVISION,
                files,
            )
            self.assertEqual(papers.uncompressed_bytes, sum(map(len, files.values())))

    def test_manifest_envelope_is_exact_and_digest_bound(self) -> None:
        files = {"index.html": b"home\n"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            archive = create_papers_archive(root, files)
            site_layer = layer_for(archive)
            manifest = manifest_value(profile, REVISION, site_layer, kind="site")
            raw = producer_json(manifest)
            path = root / "manifest.json"
            path.write_bytes(raw)
            reference = f"{profile.site_repository}@sha256:{hashlib.sha256(raw).hexdigest()}"
            parsed = MATERIALIZER.validate_manifest(
                path,
                reference,
                profile,
                REVISION,
                kind="site",
            )
            self.assertEqual(parsed.layer_digest, site_layer.layer_digest)
            self.assertEqual(parsed.created, CREATED)
            MATERIALIZER.bind_static_manifest_contract(
                path,
                reference,
                profile,
                REVISION,
                parsed,
                kind="site",
            )

            malleable_raw = canonical_json(manifest)
            path.write_bytes(malleable_raw)
            malleable_reference = (
                f"{profile.site_repository}@sha256:{hashlib.sha256(malleable_raw).hexdigest()}"
            )
            malleable_contract = MATERIALIZER.validate_manifest(
                path,
                malleable_reference,
                profile,
                REVISION,
                kind="site",
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "reconstruct",
            ):
                MATERIALIZER.bind_static_manifest_contract(
                    path,
                    malleable_reference,
                    profile,
                    REVISION,
                    malleable_contract,
                    kind="site",
                )

            invalid = copy.deepcopy(manifest)
            invalid["annotations"][MATERIALIZER.SOURCE_ANNOTATION] = "https://github.com/example/wrong"
            invalid_raw = producer_json(invalid)
            path.write_bytes(invalid_raw)
            invalid_reference = (
                f"{profile.site_repository}@sha256:{hashlib.sha256(invalid_raw).hexdigest()}"
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "source annotation",
            ):
                MATERIALIZER.validate_manifest(
                    path,
                    invalid_reference,
                    profile,
                    REVISION,
                    kind="site",
                )

    def test_root_reconstruction_rejects_forged_route_and_integration_contracts(
        self,
    ) -> None:
        files = {
            "index.html": b"home\n",
            "assets/site.css": b"body{}\n",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            archive = create_papers_archive(root, files)
            inventory_path = write_inventory(
                root,
                inventory_value(profile, archive, REVISION, files),
            )
            site_layer = layer_for(archive)
            inventory = MATERIALIZER.validate_inventory(
                inventory_path,
                profile,
                REVISION,
                site_layer,
            )
            inventory_raw = inventory_path.read_bytes()
            routes_layer = MATERIALIZER.LayerContract(
                manifest_digest=f"sha256:{'1' * 64}",
                layer_digest=f"sha256:{hashlib.sha256(inventory_raw).hexdigest()}",
                layer_size=len(inventory_raw),
                created=CREATED,
            )
            MATERIALIZER.bind_route_inventory_contract(
                inventory_path,
                inventory,
                profile,
                REVISION,
                routes_layer,
            )
            forged_route = MATERIALIZER.RouteFile(
                file=inventory.files[0].file,
                route=inventory.files[0].route,
                size=inventory.files[0].size,
                sha256="f" * 64,
            )
            forged_inventory = MATERIALIZER.InventoryContract(
                archive_bytes=inventory.archive_bytes,
                archive_sha256=inventory.archive_sha256,
                file_count=inventory.file_count,
                uncompressed_bytes=inventory.uncompressed_bytes,
                files=(forged_route, *inventory.files[1:]),
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "root reconstruction",
            ):
                MATERIALIZER.bind_route_inventory_contract(
                    inventory_path,
                    forged_inventory,
                    profile,
                    REVISION,
                    routes_layer,
                )

            integration_files = tuple(
                MATERIALIZER.IntegrationFileContract(
                    path=path,
                    size=index + 1,
                    sha256=f"sha256:{index:064x}",
                )
                for index, path in enumerate(MATERIALIZER.INTEGRATION_RUNTIME_PATHS)
            )
            integration_raw = MATERIALIZER.canonical_integration_inventory_bytes(
                integration_files,
                REVISION,
                CREATED,
            )
            integration_path = root / "platform-integration.inventory.json"
            integration_path.write_bytes(integration_raw)
            integration_contract = MATERIALIZER.IntegrationContract(
                manifest_digest=f"sha256:{'2' * 64}",
                archive_digest=f"sha256:{'3' * 64}",
                archive_size=123,
                inventory_digest=(
                    f"sha256:{hashlib.sha256(integration_raw).hexdigest()}"
                ),
                inventory_size=len(integration_raw),
                created=CREATED,
            )
            MATERIALIZER.bind_integration_inventory_contract(
                integration_path,
                integration_files,
                REVISION,
                integration_contract,
            )
            forged_integration_files = (
                MATERIALIZER.IntegrationFileContract(
                    path=integration_files[0].path,
                    size=integration_files[0].size,
                    sha256=f"sha256:{'f' * 64}",
                ),
                *integration_files[1:],
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "root reconstruction",
            ):
                MATERIALIZER.bind_integration_inventory_contract(
                    integration_path,
                    forged_integration_files,
                    REVISION,
                    integration_contract,
                )

    def test_root_reconstructs_route_and_platform_oras_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            routes_blob = root / "routes.json"
            routes_blob.write_bytes(b"{}\n")
            unsigned_routes = layer_for(routes_blob)
            routes_value = manifest_value(
                profile,
                REVISION,
                unsigned_routes,
                kind="routes",
            )
            routes_raw = producer_json(routes_value)
            routes_manifest = root / "routes-manifest.json"
            routes_manifest.write_bytes(routes_raw)
            routes_reference = (
                f"{profile.routes_repository}@sha256:{hashlib.sha256(routes_raw).hexdigest()}"
            )
            routes_contract = MATERIALIZER.validate_manifest(
                routes_manifest,
                routes_reference,
                profile,
                REVISION,
                kind="routes",
            )
            MATERIALIZER.bind_static_manifest_contract(
                routes_manifest,
                routes_reference,
                profile,
                REVISION,
                routes_contract,
                kind="routes",
            )

            unsigned_integration = MATERIALIZER.IntegrationContract(
                manifest_digest=f"sha256:{'0' * 64}",
                archive_digest=f"sha256:{'1' * 64}",
                archive_size=123,
                inventory_digest=f"sha256:{'2' * 64}",
                inventory_size=456,
                created=CREATED,
            )
            integration_value = {
                "schemaVersion": 2,
                "mediaType": MATERIALIZER.OCI_MANIFEST_MEDIA_TYPE,
                "artifactType": MATERIALIZER.INTEGRATION_ARTIFACT_TYPE,
                "config": copy.deepcopy(MATERIALIZER.OCI_EMPTY_CONFIG),
                "layers": [
                    {
                        "mediaType": MATERIALIZER.INTEGRATION_ARCHIVE_MEDIA_TYPE,
                        "digest": unsigned_integration.archive_digest,
                        "size": unsigned_integration.archive_size,
                        "annotations": {
                            MATERIALIZER.TITLE_ANNOTATION: MATERIALIZER.INTEGRATION_ARCHIVE_NAME
                        },
                    },
                    {
                        "mediaType": MATERIALIZER.INTEGRATION_INVENTORY_MEDIA_TYPE,
                        "digest": unsigned_integration.inventory_digest,
                        "size": unsigned_integration.inventory_size,
                        "annotations": {
                            MATERIALIZER.TITLE_ANNOTATION: MATERIALIZER.INTEGRATION_INVENTORY_NAME
                        },
                    },
                ],
                "annotations": {
                    MATERIALIZER.CREATED_ANNOTATION: CREATED,
                    MATERIALIZER.REVISION_ANNOTATION: REVISION,
                    MATERIALIZER.SOURCE_ANNOTATION: MATERIALIZER.INTEGRATION_SOURCE_URL,
                },
            }
            integration_raw = producer_json(integration_value)
            self.assertEqual(
                integration_raw,
                MATERIALIZER.producer_oci_manifest_bytes(
                    artifact_type=MATERIALIZER.INTEGRATION_ARTIFACT_TYPE,
                    layers=(
                        (
                            MATERIALIZER.INTEGRATION_ARCHIVE_MEDIA_TYPE,
                            unsigned_integration.archive_digest,
                            unsigned_integration.archive_size,
                            MATERIALIZER.INTEGRATION_ARCHIVE_NAME,
                        ),
                        (
                            MATERIALIZER.INTEGRATION_INVENTORY_MEDIA_TYPE,
                            unsigned_integration.inventory_digest,
                            unsigned_integration.inventory_size,
                            MATERIALIZER.INTEGRATION_INVENTORY_NAME,
                        ),
                    ),
                    created=CREATED,
                    source_revision=REVISION,
                    source_url=MATERIALIZER.INTEGRATION_SOURCE_URL,
                ),
            )
            integration_manifest = root / "integration-manifest.json"
            integration_manifest.write_bytes(integration_raw)
            integration_reference = (
                f"{MATERIALIZER.INTEGRATION_REPOSITORY}@sha256:"
                f"{hashlib.sha256(integration_raw).hexdigest()}"
            )
            integration_contract = MATERIALIZER.validate_integration_manifest(
                integration_manifest,
                integration_reference,
                REVISION,
            )
            MATERIALIZER.bind_integration_manifest_contract(
                integration_manifest,
                integration_reference,
                REVISION,
                integration_contract,
            )
            forged_contract = MATERIALIZER.IntegrationContract(
                manifest_digest=integration_contract.manifest_digest,
                archive_digest=f"sha256:{'f' * 64}",
                archive_size=integration_contract.archive_size,
                inventory_digest=integration_contract.inventory_digest,
                inventory_size=integration_contract.inventory_size,
                created=integration_contract.created,
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "root reconstruction",
            ):
                MATERIALIZER.bind_integration_manifest_contract(
                    integration_manifest,
                    integration_reference,
                    REVISION,
                    forged_contract,
                )

    def test_root_rejects_a_manifest_contract_forged_by_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            site_blob = root / "site.tar.gz"
            site_blob.write_bytes(b"site")
            routes_blob = root / "routes.json"
            routes_blob.write_bytes(b"routes")

            layer_contracts: dict[str, MATERIALIZER.LayerContract] = {}
            manifest_paths: dict[str, Path] = {}
            references: dict[str, str] = {}
            for kind, blob, repository in (
                ("site", site_blob, profile.site_repository),
                ("routes", routes_blob, profile.routes_repository),
            ):
                unsigned_layer = layer_for(blob)
                raw = producer_json(
                    manifest_value(
                        profile,
                        REVISION,
                        unsigned_layer,
                        kind=kind,
                    )
                )
                manifest_path = root / f"{kind}-manifest.json"
                manifest_path.write_bytes(raw)
                manifest_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
                layer_contracts[kind] = MATERIALIZER.LayerContract(
                    manifest_digest=manifest_digest,
                    layer_digest=unsigned_layer.layer_digest,
                    layer_size=unsigned_layer.layer_size,
                    created=CREATED,
                )
                manifest_paths[kind] = manifest_path
                references[kind] = f"{repository}@{manifest_digest}"

            integration_unsigned = MATERIALIZER.IntegrationContract(
                manifest_digest=f"sha256:{'0' * 64}",
                archive_digest=f"sha256:{'3' * 64}",
                archive_size=123,
                inventory_digest=f"sha256:{'4' * 64}",
                inventory_size=456,
                created=CREATED,
            )
            integration_raw = MATERIALIZER.producer_oci_manifest_bytes(
                artifact_type=MATERIALIZER.INTEGRATION_ARTIFACT_TYPE,
                layers=(
                    (
                        MATERIALIZER.INTEGRATION_ARCHIVE_MEDIA_TYPE,
                        integration_unsigned.archive_digest,
                        integration_unsigned.archive_size,
                        MATERIALIZER.INTEGRATION_ARCHIVE_NAME,
                    ),
                    (
                        MATERIALIZER.INTEGRATION_INVENTORY_MEDIA_TYPE,
                        integration_unsigned.inventory_digest,
                        integration_unsigned.inventory_size,
                        MATERIALIZER.INTEGRATION_INVENTORY_NAME,
                    ),
                ),
                created=CREATED,
                source_revision=REVISION,
                source_url=MATERIALIZER.INTEGRATION_SOURCE_URL,
            )
            integration_path = root / "integration-manifest.json"
            integration_path.write_bytes(integration_raw)
            integration_digest = f"sha256:{hashlib.sha256(integration_raw).hexdigest()}"
            integration_reference = (
                f"{MATERIALIZER.INTEGRATION_REPOSITORY}@{integration_digest}"
            )
            integration_contract = MATERIALIZER.IntegrationContract(
                manifest_digest=integration_digest,
                archive_digest=integration_unsigned.archive_digest,
                archive_size=integration_unsigned.archive_size,
                inventory_digest=integration_unsigned.inventory_digest,
                inventory_size=integration_unsigned.inventory_size,
                created=CREATED,
            )
            forged_site = dataclasses.replace(
                layer_contracts["site"],
                layer_digest=f"sha256:{'f' * 64}",
            )

            worker_home = root / "worker"
            worker_home.mkdir(mode=0o700)

            def malicious_isolated_worker(phase, command, **kwargs):
                self.assertEqual(phase, "manifest")
                output = worker_home / "validated-manifests.json"
                descriptor = os.open(
                    output,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o444,
                )
                try:
                    os.write(
                        descriptor,
                        canonical_json(
                            {
                                "integration": dataclasses.asdict(integration_contract),
                                "routes_layer": dataclasses.asdict(
                                    layer_contracts["routes"]
                                ),
                                "site_layer": dataclasses.asdict(forged_site),
                            }
                        ),
                    )
                    os.fchmod(descriptor, 0o444)
                finally:
                    os.close(descriptor)
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="1" * 32,
                    logical_root=worker_home,
                    physical_root=worker_home,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=malicious_isolated_worker,
            ):
                with mock.patch.object(MATERIALIZER, "cleanup_isolated_worker_state"):
                    with self.assertRaisesRegex(
                        MATERIALIZER.StaticDeploymentError,
                        "root reconstruction",
                    ):
                        MATERIALIZER.validate_manifests_isolated(
                            profile,
                            REVISION,
                            manifest_paths["site"],
                            references["site"],
                            manifest_paths["routes"],
                            references["routes"],
                            integration_path,
                            integration_reference,
                            REVISION,
                        )

    def test_isolated_manifest_worker_uses_dynamic_identity_and_cgroup_exit_barrier(
        self,
    ) -> None:
        state_name = "1" * 32
        input_path = Path("/root/protected/manifest.json")
        properties = MATERIALIZER.isolated_worker_properties(
            state_name,
            runtime_seconds=60,
            memory_max="256M",
            memory_swap_max="256M",
            file_size_max="1M",
            network=False,
            inputs=(input_path,),
        )
        self.assertIn("DynamicUser=yes", properties)
        self.assertIn(f"RuntimeDirectory=vps-static-jobs/{state_name}", properties)
        self.assertIn("RuntimeDirectoryPreserve=yes", properties)
        self.assertIn("ExitType=cgroup", properties)
        self.assertIn("KillMode=control-group", properties)
        self.assertIn("RuntimeMaxSec=60s", properties)
        self.assertIn("MemoryMax=256M", properties)
        self.assertIn("MemorySwapMax=256M", properties)
        self.assertIn("PrivateNetwork=yes", properties)
        self.assertIn("RestrictAddressFamilies=", properties)
        self.assertIn("PrivateIPC=yes", properties)
        self.assertIn("SystemCallFilter=@system-service", properties)
        self.assertIn("SystemCallFilter=~@mount", properties)
        self.assertNotIn("PrivatePIDs=yes", properties)
        self.assertIn(
            f"BindReadOnlyPaths={input_path}:{input_path}:norbind",
            properties,
        )
        command = MATERIALIZER.isolated_worker_command(
            MATERIALIZER.SYSTEMD_WORKER_UNIT,
            properties,
            ["/usr/bin/python3", "/root/deploy-static", "--worker"],
        )
        self.assertEqual(command[0], str(MATERIALIZER.SYSTEMD_RUN_PATH))
        self.assertIn("--service-type=exec", command)
        self.assertIn("--wait", command)
        self.assertIn("--collect", command)
        self.assertIn("--expand-environment=no", command)
        self.assertNotIn("--pipe", command)
        self.assertNotIn("--pty", command)
        self.assertNotIn("--replace", command)
        self.assertNotIn("--scope", command)

        other_command = MATERIALIZER.isolated_worker_command(
            MATERIALIZER.SYSTEMD_WORKER_UNIT,
            properties,
            ["/usr/bin/python3", "/root/deploy-static", "--other-worker"],
        )
        self.assertEqual(
            command[command.index("--system") + 1],
            other_command[other_command.index("--system") + 1],
        )
        self.assertIn(
            f"--unit={MATERIALIZER.SYSTEMD_WORKER_UNIT}",
            command,
        )
        self.assertNotIn("--replace", other_command)

    def test_global_host_addresses_accept_atlas_iproute2_inventory(self) -> None:
        fixture = [
            {
                "ifname": "ens3",
                "addr_info": [
                    {"family": "inet", "local": "137.74.174.163", "scope": "global"},
                    {"family": "inet6", "local": "2001:41d0:302:2200::1", "scope": "global"},
                    {},
                ],
            },
            {
                "ifname": "docker0",
                "addr_info": [
                    {"family": "inet", "local": "172.17.0.1", "scope": "global"},
                ],
            },
        ]
        completed = subprocess.CompletedProcess(
            [str(MATERIALIZER.IP_PATH)],
            0,
            json.dumps(fixture).encode("utf-8"),
            b"",
        )
        with mock.patch.object(MATERIALIZER.subprocess, "run", return_value=completed):
            addresses = MATERIALIZER.global_host_address_prefixes()
        self.assertEqual(
            addresses,
            (
                "137.74.174.163/32",
                "172.17.0.1/32",
                "2001:41d0:302:2200::1/128",
            ),
        )

    def test_isolated_worker_cleanup_refuses_symlinks_and_mount_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            child.mkdir()
            (child / "result.json").write_bytes(b"ok\n")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                MATERIALIZER.remove_isolated_tree(
                    parent_fd,
                    "child",
                    expected_device=root.stat().st_dev,
                )
                self.assertFalse(child.exists())
                target = root / "target"
                target.mkdir()
                link = root / "link"
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises(OSError):
                    MATERIALIZER.remove_isolated_tree(
                        parent_fd,
                        "link",
                        expected_device=root.stat().st_dev,
                    )
            finally:
                os.close(parent_fd)

    def test_isolated_worker_cleanup_handles_deep_and_wide_trees_iteratively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            deep = root / "deep"
            deep.mkdir()
            descriptor = os.open(deep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for _ in range(1100):
                    os.mkdir("d", dir_fd=descriptor)
                    child = os.open(
                        "d",
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = child
            finally:
                os.close(descriptor)
            wide = root / "wide"
            wide.mkdir()
            for index in range(2000):
                (wide / f"f{index}").write_bytes(b"x")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            started = time.monotonic()
            try:
                MATERIALIZER.remove_isolated_tree(
                    parent_fd,
                    "deep",
                    expected_device=root.stat().st_dev,
                )
                MATERIALIZER.remove_isolated_tree(
                    parent_fd,
                    "wide",
                    expected_device=root.stat().st_dev,
                )
            finally:
                os.close(parent_fd)
            self.assertFalse(deep.exists())
            self.assertFalse(wide.exists())
            self.assertLess(time.monotonic() - started, 10)

    def test_stale_worker_preflight_accepts_absent_logical_root_and_refuses_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            logical = root / "logical"
            cgroups = root / "cgroups"
            cgroups.mkdir()
            clean = subprocess.CompletedProcess([], 0, "", "")
            patches = (
                mock.patch.object(MATERIALIZER, "SYSTEMD_WORKER_PRIVATE_ROOT", private),
                mock.patch.object(MATERIALIZER, "SYSTEMD_WORKER_LOGICAL_ROOT", logical),
                mock.patch.object(MATERIALIZER, "SYSTEMD_CGROUP_ROOT", cgroups),
                mock.patch.object(MATERIALIZER.subprocess, "run", return_value=clean),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                MATERIALIZER.refuse_isolated_worker_residue_locked()

            active = subprocess.CompletedProcess(
                [],
                0,
                "vpsw-registry-" + "8" * 32 + ".service loaded activating start job\n",
                "",
            )
            with patches[0], patches[1], patches[2], mock.patch.object(
                MATERIALIZER.subprocess,
                "run",
                return_value=active,
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "residual isolated worker unit",
                ):
                    MATERIALIZER.refuse_isolated_worker_residue_locked()

    def test_rejects_unsafe_tar_member_types_and_paths(self) -> None:
        files = {"index.html": b"home\n"}
        unsafe_entries = {
            "traversal": {"name": "site/../escape", "data": b"escape"},
            "absolute": {"name": "/site/escape", "data": b"escape"},
            "symlink": {
                "name": "site/link",
                "type_flag": tarfile.SYMTYPE,
                "linkname": "index.html",
            },
            "hardlink": {
                "name": "site/hard",
                "type_flag": tarfile.LNKTYPE,
                "linkname": "site/index.html",
            },
            "character-device": {
                "name": "site/device",
                "type_flag": tarfile.CHRTYPE,
            },
            "block-device": {
                "name": "site/device",
                "type_flag": tarfile.BLKTYPE,
            },
            "fifo": {"name": "site/fifo", "type_flag": tarfile.FIFOTYPE},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            for name, entry in unsafe_entries.items():
                archive = create_papers_archive(
                    root,
                    files,
                    extra_entries=[entry],
                    name=name,
                )
                candidate_root = root / f"inventory-{name}"
                candidate_root.mkdir()
                value = inventory_value(profile, archive, REVISION, files)
                inventory_path = write_inventory(candidate_root, value)
                with self.subTest(name=name), self.assertRaises(
                    MATERIALIZER.StaticDeploymentError
                ):
                    MATERIALIZER.validate_bundle(
                        archive,
                        inventory_path,
                        profile,
                        REVISION,
                        layer_for(archive),
                    )

    def test_rejects_duplicate_members_and_concatenated_gzip(self) -> None:
        files = {"index.html": b"home\n"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            duplicate = create_papers_archive(
                root,
                files,
                extra_entries=[{"name": "site/index.html", "data": b"home\n"}],
                name="duplicate",
            )
            value = inventory_value(profile, duplicate, REVISION, files)
            path = write_inventory(root, value, "duplicate-routes.json")
            with self.assertRaisesRegex(MATERIALIZER.StaticDeploymentError, "duplicate"):
                MATERIALIZER.validate_bundle(
                    duplicate,
                    path,
                    profile,
                    REVISION,
                    layer_for(duplicate),
                )

            valid = create_papers_archive(root, files, name="single")
            concatenated = root / "concatenated.tar.gz"
            concatenated.write_bytes(valid.read_bytes() + valid.read_bytes())
            value = inventory_value(profile, concatenated, REVISION, files)
            path = write_inventory(root, value, "concatenated-routes.json")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "concatenated gzip",
            ):
                MATERIALIZER.validate_bundle(
                    concatenated,
                    path,
                    profile,
                    REVISION,
                    layer_for(concatenated),
                )

    def test_rejects_inventory_mismatch_and_duplicate_json_keys(self) -> None:
        files = {"index.html": b"home\n", "asset.txt": b"asset\n"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            archive = create_papers_archive(root, files)
            value = inventory_value(profile, archive, REVISION, files)
            value["routes"][1]["sha256"] = "0" * 64
            path = write_inventory(root, value)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "does not match its inventory entry",
            ):
                MATERIALIZER.validate_bundle(
                    archive,
                    path,
                    profile,
                    REVISION,
                    layer_for(archive),
                )

    def test_rejects_nondeterministic_gzip_and_profile_limit_overrun(self) -> None:
        files = {"index.html": b"home\n"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            valid = create_papers_archive(root, files)
            invalid = root / "nonzero-mtime.tar.gz"
            content = bytearray(valid.read_bytes())
            content[4] = 1
            invalid.write_bytes(content)
            value = inventory_value(profile, invalid, REVISION, files)
            path = write_inventory(root, value, "nonzero-mtime-routes.json")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "deterministic gzip header",
            ):
                MATERIALIZER.validate_bundle(
                    invalid,
                    path,
                    profile,
                    REVISION,
                    layer_for(invalid),
                )

            value = inventory_value(profile, valid, REVISION, files)
            value["site"]["file_count"] = profile.max_files + 1
            path = write_inventory(root, value, "over-limit-routes.json")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "file count exceeds",
            ):
                MATERIALIZER.validate_inventory(
                    path,
                    profile,
                    REVISION,
                    layer_for(valid),
                )

            with self.assertRaisesRegex(MATERIALIZER.StaticDeploymentError, "duplicate key"):
                MATERIALIZER.strict_json_bytes(
                    b'{"schema":1,"schema":1}\n',
                    "duplicate fixture",
                    canonical=False,
                )

    def test_extracts_with_normalized_modes_and_exact_inventory(self) -> None:
        files = {"index.html": b"home\n", "assets/site.css": b"body{}\n"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = MATERIALIZER.PROFILES["papersempire"]
            archive = create_papers_archive(root, files)
            contract_root = root / "contract"
            contract_root.mkdir()
            inventory = self.validate_fixture(
                contract_root,
                profile,
                archive,
                REVISION,
                files,
            )
            destination = root / "release"
            destination.mkdir(mode=0o700)
            MATERIALIZER.extract_archive(archive, destination, inventory)
            MATERIALIZER.filesystem_inventory(
                destination,
                inventory,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
            self.assertEqual((destination / "index.html").stat().st_mode & 0o777, 0o644)

    def test_site_extract_dispatch_uses_isolated_dynamic_worker(self) -> None:
        files = {"index.html": b"home\n", "assets/site.css": b"body{}\n"}
        captured: list[tuple[str, list[str], dict[str, object]]] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            profile = MATERIALIZER.PROFILES["papersempire"]
            archive = create_papers_archive(root, files)
            inventory_path = write_inventory(
                root,
                inventory_value(profile, archive, REVISION, files),
            )
            site_layer = layer_for(archive)
            inventory_raw = inventory_path.read_bytes()
            routes_layer = MATERIALIZER.LayerContract(
                manifest_digest=f"sha256:{'2' * 64}",
                layer_digest=f"sha256:{hashlib.sha256(inventory_raw).hexdigest()}",
                layer_size=len(inventory_raw),
                created=CREATED,
            )
            trusted_parent = root / "trusted"
            trusted_parent.mkdir(mode=0o700)
            staging = trusted_parent / "release"

            def emulate_worker(phase, command, **kwargs):
                captured.append((phase, command, kwargs))
                self.assertEqual(phase, "extract")
                state_root = root / "isolated-site-state"
                state_root.mkdir(mode=0o700)
                resolved = [
                    item.replace("{STATE_DIRECTORY}", str(state_root))
                    for item in command
                ]
                flag = resolved.index("--extract-worker")
                worker_args = resolved[flag + 1 :]
                self.assertEqual(len(worker_args), 7)
                with mock.patch.dict(
                    MATERIALIZER.os.environ,
                    {"HOME": str(state_root)},
                    clear=False,
                ):
                    MATERIALIZER.run_extract_worker(
                        profile,
                        worker_args[1],
                        Path(worker_args[2]),
                        Path(worker_args[3]),
                        Path(worker_args[4]),
                        Path(worker_args[5]),
                        Path(worker_args[6]),
                    )
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="2" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            def cleanup_worker(state):
                for candidate in sorted(
                    state.physical_root.rglob("*"),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    if candidate.is_dir():
                        candidate.chmod(0o700)
                state.physical_root.chmod(0o700)
                shutil.rmtree(state.physical_root)

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=cleanup_worker,
            ):
                inventory = MATERIALIZER.create_release_isolated(
                    profile,
                    REVISION,
                    archive,
                    inventory_path,
                    site_layer,
                    routes_layer,
                    staging,
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            MATERIALIZER.filesystem_inventory(
                staging,
                inventory,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            self.assertEqual((staging / "index.html").read_bytes(), files["index.html"])
            phase, command, kwargs = captured[0]
            self.assertEqual(phase, "extract")
            self.assertFalse(kwargs["network"])
            self.assertIn(archive, kwargs["inputs"])
            self.assertNotIn("--reuid", " ".join(command))

    def test_trusted_release_copy_is_detached_from_surviving_worker_fd(self) -> None:
        files = {
            "index.html": b"trusted home\n",
            "assets/site.css": b"body{}\n",
        }
        inventory = MATERIALIZER.InventoryContract(
            archive_bytes=1,
            archive_sha256="1" * 64,
            file_count=len(files),
            uncompressed_bytes=sum(len(content) for content in files.values()),
            files=tuple(
                MATERIALIZER.RouteFile(
                    file=path,
                    route=MATERIALIZER.route_for_file(path),
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
                for path, content in files.items()
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            worker = root / "worker"
            worker.mkdir(mode=0o700)
            source = worker / "untrusted-site"
            source.mkdir(mode=0o700)
            populate_worker_tree(source, files, file_mode=0o644, directory_mode=0o755)
            trusted_parent = root / "trusted"
            trusted_parent.mkdir(mode=0o700)
            trusted = trusted_parent / "release"
            surviving_fd = os.open(source / "index.html", os.O_WRONLY | os.O_NOFOLLOW)
            try:
                MATERIALIZER.copy_trusted_release(
                    source,
                    trusted,
                    inventory,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )
                os.ftruncate(surviving_fd, 0)
                os.write(surviving_fd, b"mutated worker source\n")
            finally:
                os.close(surviving_fd)
            shutil.rmtree(source)
            self.assertEqual((trusted / "index.html").read_bytes(), files["index.html"])
            MATERIALIZER.filesystem_inventory(
                trusted,
                inventory,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )

    def test_trusted_release_copy_rejects_entries_outside_inventory(self) -> None:
        content = b"home\n"
        inventory = MATERIALIZER.InventoryContract(
            archive_bytes=1,
            archive_sha256="1" * 64,
            file_count=1,
            uncompressed_bytes=len(content),
            files=(
                MATERIALIZER.RouteFile(
                    file="index.html",
                    route="/",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            worker = root / "worker"
            worker.mkdir(mode=0o700)
            source = worker / "untrusted-site"
            source.mkdir(mode=0o700)
            populate_worker_tree(
                source,
                {"index.html": content, "unexpected.txt": b"not allowlisted\n"},
                file_mode=0o644,
                directory_mode=0o755,
            )
            trusted_parent = root / "trusted"
            trusted_parent.mkdir(mode=0o700)
            trusted = trusted_parent / "release"
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "exact allowlist",
            ):
                MATERIALIZER.copy_trusted_release(
                    source,
                    trusted,
                    inventory,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )
            self.assertFalse(trusted.exists())

    def test_worker_result_read_is_bounded_and_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            worker = Path(temporary_directory)
            worker.chmod(0o700)
            result = worker / "result.json"
            result.write_bytes(b"x" * 33)
            result.chmod(0o444)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "32-byte limit",
            ):
                MATERIALIZER.read_worker_result(
                    result,
                    32,
                    "bounded worker result",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )

    def test_trusted_worker_file_copy_rejects_links_and_concurrent_mutation(self) -> None:
        content = b"immutable\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            trusted = root / "trusted"
            trusted.mkdir(mode=0o700)

            symlink_worker = root / "symlink-worker"
            symlink_worker.mkdir(mode=0o700)
            external = root / "external"
            external.write_bytes(content)
            (symlink_worker / "result").symlink_to(external)
            with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                MATERIALIZER.copy_trusted_worker_file(
                    symlink_worker / "result",
                    trusted / "symlink-copy",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    maximum_size=len(content),
                    label="symlink fixture",
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            hardlink_worker = root / "hardlink-worker"
            hardlink_worker.mkdir(mode=0o700)
            hardlink_source = hardlink_worker / "result"
            hardlink_source.write_bytes(content)
            hardlink_source.chmod(0o444)
            os.link(hardlink_source, root / "second-link")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "unsafe",
            ):
                MATERIALIZER.copy_trusted_worker_file(
                    hardlink_source,
                    trusted / "hardlink-copy",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    maximum_size=len(content),
                    label="hardlink fixture",
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            mutation_worker = root / "mutation-worker"
            mutation_worker.mkdir(mode=0o700)
            mutation_source = mutation_worker / "result"
            mutation_source.write_bytes(content)
            writer = os.open(mutation_source, os.O_WRONLY | os.O_NOFOLLOW)
            mutation_source.chmod(0o444)
            real_read = MATERIALIZER.os.read
            mutated = False

            def mutate_after_read(descriptor, size):
                nonlocal mutated
                value = real_read(descriptor, size)
                if value and not mutated:
                    mutated = True
                    os.pwrite(writer, b"M", 0)
                return value

            try:
                with mock.patch.object(MATERIALIZER.os, "read", side_effect=mutate_after_read):
                    with self.assertRaisesRegex(
                        MATERIALIZER.StaticDeploymentError,
                        "changed while it was copied",
                    ):
                        MATERIALIZER.copy_trusted_worker_file(
                            mutation_source,
                            trusted / "mutation-copy",
                            expected_uid=os.geteuid(),
                            expected_gid=os.getegid(),
                            maximum_size=len(content),
                            label="mutation fixture",
                            trusted_uid=os.geteuid(),
                            trusted_gid=os.getegid(),
                        )
            finally:
                os.close(writer)
            self.assertFalse((trusted / "mutation-copy").exists())

    def test_integration_worker_dispatch_copies_validated_contract_to_trusted_tree(self) -> None:
        integration_created = "2026-08-12T07:00:00Z"
        contents = {
            path: f"fixture:{path}\n".encode("ascii")
            for path in MATERIALIZER.INTEGRATION_RUNTIME_PATHS
        }
        worker_files = [
            {
                "mode": "0644",
                "path": path,
                "sha256": f"sha256:{hashlib.sha256(contents[path]).hexdigest()}",
                "size": len(contents[path]),
            }
            for path in MATERIALIZER.INTEGRATION_RUNTIME_PATHS
        ]
        file_contracts = tuple(
            MATERIALIZER.IntegrationFileContract(
                path=item["path"],
                size=item["size"],
                sha256=item["sha256"],
            )
            for item in worker_files
        )
        inventory_bytes = MATERIALIZER.canonical_integration_inventory_bytes(
            file_contracts,
            REVISION,
            integration_created,
        )
        archive_bytes = MATERIALIZER.build_integration_archive(
            [
                MATERIALIZER.IntegrationRuntimeFile(
                    path=path,
                    mode=0o644,
                    content=contents[path],
                )
                for path in MATERIALIZER.INTEGRATION_RUNTIME_PATHS
            ],
            epoch=MATERIALIZER.integration_epoch_from_created(integration_created),
        )
        contract = MATERIALIZER.IntegrationContract(
            manifest_digest=f"sha256:{'1' * 64}",
            archive_digest=f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}",
            archive_size=len(archive_bytes),
            inventory_digest=f"sha256:{hashlib.sha256(inventory_bytes).hexdigest()}",
            inventory_size=len(inventory_bytes),
            created=integration_created,
        )
        captured: list[tuple[str, list[str], dict[str, object]]] = []

        def emulate_worker(phase, command, **kwargs):
            captured.append((phase, command, kwargs))
            self.assertEqual(phase, "integration")
            flag = command.index("--integration-worker")
            self.assertEqual(len(command[flag + 1 :]), 8)
            state_root = root / "isolated-state"
            state_root.mkdir(mode=0o700)
            resolved = [item.replace("{STATE_DIRECTORY}", str(state_root)) for item in command]
            destination = Path(resolved[flag + 7])
            output = Path(resolved[flag + 8])
            destination.mkdir(mode=0o700)
            populate_worker_tree(
                destination,
                contents,
                file_mode=0o444,
                directory_mode=0o555,
            )
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o444,
            )
            try:
                os.write(
                    descriptor,
                    canonical_json(
                        {
                            "archive_digest": contract.archive_digest,
                            "created": contract.created,
                            "files": worker_files,
                            "inventory_digest": contract.inventory_digest,
                            "source_revision": REVISION,
                        }
                    ),
                )
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
            return MATERIALIZER.IsolatedWorkerState(
                unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                state_name="1" * 32,
                logical_root=state_root,
                physical_root=state_root,
                uid=os.geteuid(),
                gid=os.getegid(),
            )

        def cleanup_worker(state):
            for candidate in sorted(
                state.physical_root.rglob("*"),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                if candidate.is_dir():
                    candidate.chmod(0o700)
            state.physical_root.chmod(0o700)
            shutil.rmtree(state.physical_root)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            trusted_parent = root / "trusted"
            trusted_parent.mkdir(mode=0o700)
            trusted = trusted_parent / "integration"
            (root / "inventory.json").write_bytes(inventory_bytes)
            (root / "archive.tar.gz").write_bytes(archive_bytes)
            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=cleanup_worker,
            ):
                files = MATERIALIZER.extract_integration_isolated(
                    "personal",
                    REVISION,
                    "ghcr.io/nclsppr/vps-infra/caddy@sha256:" + "4" * 64,
                    root / "archive.tar.gz",
                    root / "inventory.json",
                    contract,
                    trusted,
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )
            self.assertEqual(tuple(item.path for item in files), MATERIALIZER.INTEGRATION_RUNTIME_PATHS)
            self.assertEqual(
                (trusted / MATERIALIZER.INTEGRATION_RUNTIME_PATHS[0]).read_bytes(),
                contents[MATERIALIZER.INTEGRATION_RUNTIME_PATHS[0]],
            )
            MATERIALIZER.integration_filesystem_inventory(
                trusted,
                files,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
                expected_file_mode=0o444,
                expected_directory_mode=0o555,
            )
            archive_path = root / "archive.tar.gz"
            forged_archive = archive_bytes + b"forged"
            archive_path.write_bytes(forged_archive)
            forged_contract = dataclasses.replace(
                contract,
                archive_digest=(
                    f"sha256:{hashlib.sha256(forged_archive).hexdigest()}"
                ),
                archive_size=len(forged_archive),
            )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "reconstruct",
            ):
                MATERIALIZER.bind_integration_archive_contract(
                    archive_path,
                    trusted,
                    files,
                    forged_contract,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )
            archive_path.unlink()
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "cannot read platform integration archive",
            ):
                MATERIALIZER.bind_integration_archive_contract(
                    archive_path,
                    trusted,
                    files,
                    contract,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )
        phase, command, kwargs = captured[0]
        self.assertEqual(phase, "integration")
        self.assertEqual(command[command.index("--integration-worker") + 8], command[-1])
        self.assertFalse(kwargs["network"])
        self.assertIn(root / "archive.tar.gz", kwargs["inputs"])
        self.assertNotIn("--reuid", " ".join(command))

    def test_activation_replaces_only_a_safe_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_root = Path(temporary_directory) / "personal"
            releases = app_root / "releases"
            old_release = releases / f"sha256-{'1' * 64}"
            new_release = releases / f"sha256-{'2' * 64}"
            old_release.mkdir(parents=True)
            new_release.mkdir()
            current = app_root / "current"
            current.symlink_to(f"releases/{old_release.name}")
            MATERIALIZER.activate_release(
                current,
                new_release.name,
                releases,
                expected_uid=os.geteuid(),
            )
            self.assertTrue(current.is_symlink())
            self.assertEqual(os.readlink(current), f"releases/{new_release.name}")

            current.unlink()
            current.write_text("unsafe", encoding="ascii")
            with self.assertRaisesRegex(MATERIALIZER.StaticDeploymentError, "not a symlink"):
                MATERIALIZER.activate_release(
                    current,
                    old_release.name,
                    releases,
                    expected_uid=os.geteuid(),
                )

    def test_activation_rejects_a_release_symlink_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_root = Path(temporary_directory) / "personal"
            releases = app_root / "releases"
            real_release = releases / f"sha256-{'1' * 64}"
            linked_release = releases / f"sha256-{'2' * 64}"
            real_release.mkdir(parents=True)
            linked_release.symlink_to(real_release, target_is_directory=True)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "not a real directory",
            ):
                MATERIALIZER.activate_release(
                    app_root / "current",
                    linked_release.name,
                    releases,
                    expected_uid=os.geteuid(),
                )

    def test_activation_rolls_back_after_post_rename_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_root = Path(temporary_directory) / "personal"
            releases = app_root / "releases"
            old_release = releases / f"sha256-{'1' * 64}"
            new_release = releases / f"sha256-{'2' * 64}"
            old_release.mkdir(parents=True)
            new_release.mkdir()
            current = app_root / "current"
            current.symlink_to(f"releases/{old_release.name}")
            real_fsync = MATERIALIZER.os.fsync
            calls = 0

            def fail_after_rename(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected post-rename fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(MATERIALIZER.os, "fsync", side_effect=fail_after_rename):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "previous current was restored",
                ):
                    MATERIALIZER.activate_release(
                        current,
                        new_release.name,
                        releases,
                        expected_uid=os.geteuid(),
                    )
            self.assertEqual(os.readlink(current), f"releases/{old_release.name}")

    def test_provenance_fetch_and_offline_verification_are_distinct_units(self) -> None:
        calls: list[tuple[str, list[str], dict[str, object]]] = []
        events: list[str] = []
        subject = b'{"schemaVersion":2}\n'
        reference = (
            "ghcr.io/example/site@sha256:"
            + hashlib.sha256(subject).hexdigest()
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            subject_path = root / "manifest.json"
            subject_path.write_bytes(subject)
            subject_path.chmod(0o444)
            state_number = 0

            def emulate_worker(phase, command, **kwargs):
                nonlocal state_number
                state_number += 1
                calls.append((phase, command, kwargs))
                state_root = root / f"state-{state_number}"
                state_root.mkdir(mode=0o700)
                if phase == "attestfetch":
                    output = state_root / "attestation-bundles.jsonl"
                    output.write_bytes(b'{"bundle":true}\n')
                    output.chmod(0o444)
                elif phase == "attestverify":
                    self.assertIn("fetch-cleaned", events)
                else:
                    self.fail(f"unexpected worker phase {phase}")
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="3" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            def cleanup_worker(state):
                if state.physical_root.name == "state-1":
                    events.append("fetch-cleaned")
                shutil.rmtree(state.physical_root)

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=cleanup_worker,
            ):
                MATERIALIZER.verify_github_provenance_isolated(
                    reference,
                    subject_path=subject_path,
                    repository="nclsppr/example",
                    source_revision=REVISION,
                    source_ref="refs/heads/main",
                    signer_workflow="nclsppr/example/.github/workflows/vps-release.yml",
                    trusted_directory=root,
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            self.assertEqual([item[0] for item in calls], ["attestfetch", "attestverify"])
            fetch = calls[0]
            verify = calls[1]
            self.assertTrue(fetch[2]["network"])
            self.assertFalse(verify[2]["network"])
            self.assertEqual(verify[1][0], str(MATERIALIZER.GH_PATH))
            self.assertIn(str(subject_path), verify[1])
            self.assertIn("--bundle", verify[1])
            self.assertIn("--digest-alg", verify[1])
            self.assertIn("sha256", verify[1])
            self.assertIn("--deny-self-hosted-runners", verify[1])
            self.assertNotIn("--bundle-from-oci", verify[1])
            self.assertFalse(any("TOKEN" in argument for argument in verify[1]))
            self.assertEqual(list(root.glob("attestation-*.jsonl")), [])

    def test_provenance_does_not_accept_worker_declared_success(self) -> None:
        subject = b'{"schemaVersion":2}\n'
        reference = (
            "ghcr.io/example/site@sha256:"
            + hashlib.sha256(subject).hexdigest()
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            subject_path = root / "manifest.json"
            subject_path.write_bytes(subject)
            subject_path.chmod(0o444)
            fetch_root = root / "fetch-state"
            fetch_root.mkdir(mode=0o700)
            fake = fetch_root / "attestation-bundles.jsonl"
            fake.write_bytes(b'{"verified":1}\n')
            fake.chmod(0o444)
            fetch_state = MATERIALIZER.IsolatedWorkerState(
                unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                state_name="4" * 32,
                logical_root=fetch_root,
                physical_root=fetch_root,
                uid=os.geteuid(),
                gid=os.getegid(),
            )
            calls = 0

            def fail_verifier(phase, command, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return fetch_state
                raise MATERIALIZER.StaticDeploymentError("GitHub verifier failed")

            def cleanup_worker(state):
                shutil.rmtree(state.physical_root)

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=fail_verifier,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=cleanup_worker,
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "GitHub verifier failed",
                ):
                    MATERIALIZER.verify_github_provenance_isolated(
                        reference,
                        subject_path=subject_path,
                        repository="nclsppr/example",
                        source_revision=REVISION,
                        source_ref="refs/heads/main",
                        signer_workflow=(
                            "nclsppr/example/.github/workflows/vps-release.yml"
                        ),
                        trusted_directory=root,
                        trusted_uid=os.geteuid(),
                        trusted_gid=os.getegid(),
                    )
            self.assertEqual(list(root.glob("attestation-*.jsonl")), [])

    def test_registry_download_is_bounded_in_transit(self) -> None:
        calls: list[list[str]] = []

        def record(command, *, environment, timeout=120):
            calls.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"bounded")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "download"
            with mock.patch.object(MATERIALIZER, "run_checked", side_effect=record):
                MATERIALIZER.download_https_bounded(
                    "https://example.invalid/object",
                    destination,
                    7,
                    MATERIALIZER.safe_environment(Path(temporary_directory)),
                )
            self.assertEqual(destination.read_bytes(), b"bounded")
        command = calls[0]
        self.assertEqual(command[command.index("--max-filesize") + 1], "7")
        self.assertIn("--remove-on-error", command)
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--noproxy") + 1], "*")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with mock.patch.object(MATERIALIZER.os, "geteuid", return_value=0):
                with mock.patch.object(MATERIALIZER, "run_checked") as forbidden:
                    with self.assertRaisesRegex(
                        MATERIALIZER.StaticDeploymentError,
                        "network download must be unprivileged",
                    ):
                        MATERIALIZER.download_https_bounded(
                            "https://example.invalid/object",
                            Path(temporary_directory) / "root-download",
                            7,
                            MATERIALIZER.safe_environment(Path(temporary_directory)),
                        )
                    forbidden.assert_not_called()

    def test_registry_object_download_runs_in_a_network_worker_then_root_copies(self) -> None:
        content = b"immutable registry object\n"
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        contract = MATERIALIZER.RegistryFetchContract(
            repository=MATERIALIZER.PROFILES["personal"].site_repository,
            kind="blob",
            digest=digest,
            maximum_size=len(content),
            expected_size=len(content),
        )
        calls: list[tuple[str, list[str], dict[str, object]]] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            destination = root / "site.tar.gz"

            def emulate_worker(phase, command, **kwargs):
                calls.append((phase, command, kwargs))
                state_root = root / "registry-state"
                state_root.mkdir(mode=0o700)
                output = state_root / "registry-object"
                output.write_bytes(content)
                output.chmod(0o444)
                request = Path(command[command.index("--registry-fetch-worker") + 1])
                request_value = json.loads(request.read_text(encoding="ascii"))
                self.assertEqual(request_value, dataclasses.asdict(contract))
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="5" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=lambda state: shutil.rmtree(state.physical_root),
            ):
                MATERIALIZER.fetch_registry_object_isolated(
                    contract,
                    destination,
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o444)
            self.assertEqual(list(root.glob(".registry-request-*.json")), [])
            phase, command, kwargs = calls[0]
            self.assertEqual(phase, "registry")
            self.assertTrue(kwargs["network"])
            network_properties = MATERIALIZER.isolated_worker_properties(
                "6" * 32,
                runtime_seconds=390,
                memory_max="128M",
                memory_swap_max="64M",
                file_size_max=str(MATERIALIZER.MAX_REGISTRY_AUTH_RESPONSE_BYTES),
                network=True,
                inputs=(),
                denied_host_addresses=("8.8.8.8/32",),
            )
            self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6", network_properties)
            self.assertIn("IPAddressDeny=8.8.8.8/32", network_properties)
            self.assertIn("SocketBindDeny=any", network_properties)
            self.assertNotIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", network_properties)
            self.assertNotIn("Authorization: Bearer", " ".join(command))

    def test_registry_root_copy_rejects_a_worker_digest_lie(self) -> None:
        expected = b"expected\n"
        contract = MATERIALIZER.RegistryFetchContract(
            repository=MATERIALIZER.PROFILES["personal"].site_repository,
            kind="blob",
            digest=f"sha256:{hashlib.sha256(expected).hexdigest()}",
            maximum_size=len(expected),
            expected_size=len(expected),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            destination = root / "site.tar.gz"
            state_root = root / "registry-state"

            def emulate_worker(phase, command, **kwargs):
                state_root.mkdir(mode=0o700)
                output = state_root / "registry-object"
                output.write_bytes(b"forged!!\n")
                output.chmod(0o444)
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="7" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=lambda state: shutil.rmtree(state.physical_root),
            ):
                with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                    MATERIALIZER.fetch_registry_object_isolated(
                        contract,
                        destination,
                        trusted_uid=os.geteuid(),
                        trusted_gid=os.getegid(),
                    )
            self.assertFalse(destination.exists())
            self.assertFalse(state_root.exists())
            self.assertEqual(list(root.glob(".registry-request-*.json")), [])

    def test_deploy_validates_manifests_and_provenance_before_payload_fetch(self) -> None:
        events: list[str] = []
        profile = MATERIALIZER.PROFILES["personal"]
        site_layer = MATERIALIZER.LayerContract(
            manifest_digest=f"sha256:{'1' * 64}",
            layer_digest=f"sha256:{'2' * 64}",
            layer_size=10,
            created=CREATED,
        )
        routes_layer = MATERIALIZER.LayerContract(
            manifest_digest=f"sha256:{'3' * 64}",
            layer_digest=f"sha256:{'4' * 64}",
            layer_size=10,
            created=CREATED,
        )
        integration = MATERIALIZER.IntegrationContract(
            manifest_digest=f"sha256:{'5' * 64}",
            archive_digest=f"sha256:{'6' * 64}",
            archive_size=10,
            inventory_digest=f"sha256:{'7' * 64}",
            inventory_size=10,
            created=CREATED,
        )

        def manifest(*args, **kwargs):
            events.append("manifest-fetch")

        def validate(*args, **kwargs):
            events.append("root-reconstruction")
            return site_layer, routes_layer, integration

        def provenance(*args, **kwargs):
            events.append("provenance")

        def blob(*args, **kwargs):
            events.append("payload-fetch")
            raise MATERIALIZER.StaticDeploymentError("stop after ordering proof")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            releases = root / "releases"
            releases.mkdir()
            app_root = root / "app"
            app_root.mkdir()
            temporary = root / "temporary"
            temporary.mkdir()
            with mock.patch.object(MATERIALIZER, "fetch_manifest", side_effect=manifest), \
                mock.patch.object(
                    MATERIALIZER,
                    "validate_manifests_isolated",
                    side_effect=validate,
                ), mock.patch.object(
                    MATERIALIZER,
                    "verify_github_provenance_isolated",
                    side_effect=provenance,
                ), mock.patch.object(MATERIALIZER, "fetch_blob", side_effect=blob):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "ordering proof",
                ):
                    MATERIALIZER.deploy_locked(
                        profile,
                        REVISION,
                        profile.site_repository + "@sha256:" + "1" * 64,
                        profile.routes_repository + "@sha256:" + "3" * 64,
                        REVISION,
                        MATERIALIZER.INTEGRATION_REPOSITORY + "@sha256:" + "5" * 64,
                        "ghcr.io/nclsppr/vps-infra/caddy@sha256:" + "8" * 64,
                        temporary,
                        releases,
                        app_root,
                    )

        reconstruction_index = events.index("root-reconstruction")
        payload_index = events.index("payload-fetch")
        self.assertLess(reconstruction_index, payload_index)
        self.assertEqual(events.count("provenance"), 3)
        self.assertTrue(
            all(index < payload_index for index, event in enumerate(events) if event == "provenance")
        )


class StaticRepositoryIntegrationTests(unittest.TestCase):
    def test_inventory_schema_accepts_a_valid_common_fixture(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/static-route-inventory-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        value = {
            "contract": "vps-infra.route-inventory.v1",
            "schema": 1,
            "site": {
                "archive_bytes": 10,
                "archive_sha256": "0" * 64,
                "file_count": 1,
                "uncompressed_bytes": 5,
            },
            "source": {
                "repository": "nclsppr/personal",
                "revision": REVISION,
            },
            "routes": [
                {
                    "bytes": 5,
                    "file": "index.html",
                    "path": "/",
                    "sha256": "1" * 64,
                    "status": 200,
                }
            ],
        }
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(value, schema)

    def test_ansible_pins_github_cli_without_installing_oras_on_atlas(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertFalse(any(key.startswith("vps_oras_") for key in defaults))
        self.assertIn("deploy-static", defaults["vps_deploy_executables"])
        tasks = (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(encoding="utf-8")
        doctor = (ROOT / "scripts/doctor").read_text(encoding="utf-8")
        materializer = SCRIPT.read_text(encoding="utf-8")
        for atlas_surface in (tasks, doctor, materializer):
            self.assertNotIn("vps_oras_", atlas_surface)
            self.assertNotIn("/usr/local/bin/oras", atlas_surface)
        self.assertEqual(defaults["vps_gh_version"], "2.97.0")
        self.assertEqual(defaults["vps_gh_install_path"], "/usr/local/bin/gh")
        self.assertEqual(set(defaults["vps_gh_archives"]), {"x86_64", "aarch64"})
        for definition in defaults["vps_gh_archives"].values():
            self.assertRegex(definition["archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(definition["binary_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            MATERIALIZER.GH_BINARY_SHA256,
            {
                architecture: definition["binary_sha256"]
                for architecture, definition in defaults["vps_gh_archives"].items()
            },
        )
        self.assertIn("checksum: \"sha256:{{ vps_gh_archive.archive_sha256 }}\"", tasks)
        self.assertIn("vps_gh_extracted.stat.checksum == vps_gh_archive.binary_sha256", tasks)
        self.assertIn("Install the platform integration verifier", tasks)

    def test_caddy_probe_image_must_match_the_promotion_point(self) -> None:
        promoted = MATERIALIZER.read_promoted_caddy_image(
            ROOT / "platform/.env.example",
            require_root_owner=False,
        )
        self.assertRegex(promoted, MATERIALIZER.CADDY_IMAGE_RE)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "platform.env"
            expected = (
                "ghcr.io/nclsppr/vps-infra/caddy:sha-test@sha256:"
                + "1" * 64
            )
            path.write_text(
                f"# exact platform image\nCADDY_PLATFORM_IMAGE={expected}\n",
                encoding="ascii",
            )
            self.assertEqual(
                MATERIALIZER.read_promoted_caddy_image(path, require_root_owner=False),
                expected,
            )
            path.write_text(
                f"CADDY_PLATFORM_IMAGE={expected}\nCADDY_PLATFORM_IMAGE={expected}\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(MATERIALIZER.StaticDeploymentError, "duplicates"):
                MATERIALIZER.read_promoted_caddy_image(path, require_root_owner=False)

    def test_probe_package_injects_local_certs_once_and_activates_only_requested_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            release = root / "release"
            release.mkdir(mode=0o755)
            probe_root = root / "probe"
            probe_root.mkdir(mode=0o755)
            caddyfile, routes = MATERIALIZER.prepare_probe_integration(
                ROOT,
                "personal",
                release,
                probe_root,
            )
            original = (ROOT / "platform/caddy/Caddyfile").read_text(encoding="utf-8")
            expected = original.replace(
                "{\n\tadmin off\n",
                "{\n\tadmin off\n\tlocal_certs\n",
                1,
            )
            self.assertEqual(caddyfile.read_text(encoding="utf-8"), expected)
            self.assertEqual(expected.count("\tlocal_certs\n"), 1)
            self.assertNotIn("auto_https off", expected)
            self.assertEqual(
                [path.name for path in routes.iterdir()],
                ["personal.caddy"],
            )
            self.assertEqual(
                (routes / "personal.caddy").read_bytes(),
                (ROOT / "platform/caddy/routes/personal.caddy.disabled").read_bytes(),
            )

    def test_probe_budget_and_bind_permissions_fail_closed(self) -> None:
        with mock.patch.object(MATERIALIZER.time, "monotonic", return_value=10.1):
            self.assertEqual(
                MATERIALIZER.remaining_probe_seconds(30.0, "unit", cap=7),
                7,
            )
        with mock.patch.object(MATERIALIZER.time, "monotonic", return_value=30.0):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "total time limit",
            ):
                MATERIALIZER.remaining_probe_seconds(30.0, "unit")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tree = root / "tree"
            tree.mkdir(mode=0o755)
            child = tree / "index.html"
            child.write_bytes(b"ok\n")
            child.chmod(0o644)
            MATERIALIZER.require_probe_bind_tree(tree, "test tree")
            child.chmod(0o664)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "writable outside its owner",
            ):
                MATERIALIZER.require_probe_bind_tree(tree, "test tree")

    def test_probe_response_files_and_container_are_cleaned(self) -> None:
        body = b"verified body\n"
        headers = (
            "HTTP/2 200\r\n"
            "strict-transport-security: max-age=31536000; includeSubDomains\r\n"
            "x-content-type-options: nosniff\r\n"
            "x-frame-options: SAMEORIGIN\r\n"
            "referrer-policy: strict-origin-when-cross-origin\r\n"
            "permissions-policy: camera=(), microphone=(), geolocation=()\r\n"
            "cache-control: no-cache\r\n"
            "\r\n"
        )

        def respond(command, *, environment, timeout=120):
            response_path = Path(command[command.index("--output") + 1])
            header_path = Path(command[command.index("--dump-header") + 1])
            response_path.write_bytes(body)
            header_path.write_text(headers, encoding="latin-1")
            return subprocess.CompletedProcess(command, 0, "200", "")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(MATERIALIZER, "run_checked", side_effect=respond):
                MATERIALIZER.assert_probe_response(
                    "example.com",
                    44301,
                    "/",
                    root,
                    MATERIALIZER.time.monotonic() + 10,
                    expected_status=200,
                    expected_sha256=hashlib.sha256(body).hexdigest(),
                    expected_cache_control="no-cache",
                )
            self.assertEqual(list(root.iterdir()), [])

        calls = 0

        def absent_after_remove(command, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            MATERIALIZER.subprocess,
            "run",
            side_effect=absent_after_remove,
        ):
            MATERIALIZER.cleanup_probe_container("probe-test", {})

        calls = 0

        def residue_after_remove(command, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")

        with mock.patch.object(
            MATERIALIZER.subprocess,
            "run",
            side_effect=residue_after_remove,
        ):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "left a container behind",
            ):
                MATERIALIZER.cleanup_probe_container("probe-test", {})

    def test_exact_promoted_caddy_serves_the_probe_contract_when_docker_is_available(
        self,
    ) -> None:
        docker = shutil.which("docker")
        curl = shutil.which("curl")
        if docker is None or curl is None:
            self.skipTest("Docker or curl is unavailable")
        available = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if available.returncode != 0:
            self.skipTest("Docker daemon is unavailable")
        caddy_image = MATERIALIZER.read_promoted_caddy_image(
            ROOT / "platform/.env.example",
            require_root_owner=False,
        )
        files = {
            "index.html": b"<!doctype html><title>Probe</title>\n" + b"probe " * 300,
            "404.html": b"<!doctype html><title>Missing</title>missing\n",
            "assets/probe.css": b"body { color: #111; }\n",
        }
        routes = tuple(
            MATERIALIZER.RouteFile(
                file=relative,
                route=MATERIALIZER.route_for_file(relative),
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for relative, content in files.items()
        )
        inventory = MATERIALIZER.InventoryContract(
            archive_bytes=1,
            archive_sha256="0" * 64,
            file_count=len(files),
            uncompressed_bytes=sum(map(len, files.values())),
            files=routes,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            work.chmod(0o755)
            release = work / "release"
            populate_worker_tree(
                release,
                files,
                file_mode=0o644,
                directory_mode=0o755,
            )
            with mock.patch.object(MATERIALIZER, "DOCKER_PATH", Path(docker)):
                with mock.patch.object(MATERIALIZER, "CURL_PATH", Path(curl)):
                    MATERIALIZER.probe_release(
                        release,
                        inventory,
                        caddy_image,
                        ROOT,
                        MATERIALIZER.PROFILES["personal"],
                        work,
                        180,
                    )
            self.assertEqual(
                [path.name for path in work.iterdir()],
                ["release"],
            )

    def test_static_release_roots_and_materializer_are_protected(self) -> None:
        layout = (ROOT / "ansible/roles/layout/tasks/main.yml").read_text(encoding="utf-8")
        release_task = layout.split("- name: Create protected per-site release roots", 1)[1]
        release_task = release_task.split("- name: Inspect managed external Docker networks", 1)[0]
        self.assertIn("owner: root", release_task)
        self.assertIn("group: root", release_task)
        self.assertNotIn("vps_static_user", layout)
        self.assertNotIn("Create the non-login static release account", layout)
        self.assertIn("Remove the obsolete fixed static worker account", layout)
        self.assertIn("name: vps-static", layout)
        group_vars = (
            ROOT / "ansible/inventories/production/group_vars/all.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("vps_static_user", group_vars)
        self.assertTrue(os.access(SCRIPT, os.X_OK), "deploy-static must be executable")
        source = SCRIPT.read_text(encoding="utf-8")
        staging_position = source.index("staging = releases")
        self.assertLess(
            source.index("probe_release(", staging_position),
            source.index("activate_release(app_root / \"current\""),
        )
        self.assertIn(
            'release_name = f"sha256-{site_layer.manifest_digest.removeprefix(\'sha256:\')}"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
