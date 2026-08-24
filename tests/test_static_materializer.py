#!/usr/bin/env python3

from __future__ import annotations

import copy
import contextlib
import dataclasses
import gzip
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import shutil
import socket
import stat
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


def deployment_state(
    application: str = "personal",
    *,
    source_revision: str = REVISION,
    site_digit: str = "1",
    routes_digit: str = "2",
) -> object:
    profile = MATERIALIZER.PROFILES[application]
    return MATERIALIZER.DeploymentState(
        application=application,
        source_revision=source_revision,
        site_reference=f"{profile.site_repository}@sha256:{site_digit * 64}",
        routes_reference=f"{profile.routes_repository}@sha256:{routes_digit * 64}",
        integration_revision=REVISION,
        integration_reference=(
            f"{MATERIALIZER.INTEGRATION_REPOSITORY}@sha256:{'3' * 64}"
        ),
        caddy_image=(
            "ghcr.io/nclsppr/vps-infra/caddy@sha256:" + "4" * 64
        ),
    )


def probe_inventory() -> object:
    files = (
        MATERIALIZER.RouteFile("index.html", "/", 2048, "a" * 64),
        MATERIALIZER.RouteFile("404.html", "/404.html", 512, "b" * 64),
        MATERIALIZER.RouteFile(
            "assets/application.css",
            "/assets/application.css",
            128,
            "c" * 64,
        ),
    )
    return MATERIALIZER.InventoryContract(
        archive_bytes=1024,
        archive_sha256="d" * 64,
        file_count=len(files),
        uncompressed_bytes=sum(item.size for item in files),
        files=files,
    )


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


def trusted_root_jsonl() -> bytes:
    record = producer_json({"mediaType": MATERIALIZER.TRUSTED_ROOT_MEDIA_TYPE})
    return record + b"\n" + record + b"\n"


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

    def test_isolated_worker_remaps_protected_runtime_inputs_into_its_home(
        self,
    ) -> None:
        state_name = "2" * 32
        script_path = Path("/usr/local/libexec/vps/deploy-static")
        protected_input = Path("/run/root-only/deployment/request.json")
        inputs = (script_path, protected_input)
        mapping = MATERIALIZER.isolated_worker_input_mapping(
            state_name,
            inputs,
            (protected_input,),
        )
        mapped_input = (
            MATERIALIZER.SYSTEMD_WORKER_LOGICAL_ROOT
            / state_name
            / ".inputs"
            / "01.json"
        )
        self.assertEqual(mapping, {protected_input: mapped_input})

        properties = MATERIALIZER.isolated_worker_properties(
            state_name,
            runtime_seconds=60,
            memory_max="128M",
            memory_swap_max="64M",
            file_size_max="1M",
            network=False,
            inputs=inputs,
            input_mapping=mapping,
        )
        self.assertIn(
            f"BindReadOnlyPaths={protected_input}:{mapped_input}:norbind",
            properties,
        )
        self.assertNotIn(
            f"BindReadOnlyPaths={protected_input}:{protected_input}:norbind",
            properties,
        )
        resolved = MATERIALIZER.resolved_isolated_worker_command(
            state_name,
            [
                str(MATERIALIZER.PYTHON_PATH),
                str(script_path),
                "--registry-fetch-worker",
                str(protected_input),
                "{STATE_DIRECTORY}/registry-object",
            ],
            mapping,
        )
        self.assertEqual(resolved[3], str(mapped_input))
        self.assertEqual(
            resolved[4],
            str(
                MATERIALIZER.SYSTEMD_WORKER_LOGICAL_ROOT
                / state_name
                / "registry-object"
            ),
        )

        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "undeclared",
        ):
            MATERIALIZER.isolated_worker_input_mapping(
                state_name,
                inputs,
                (Path("/run/not-an-input"),),
            )

        attestation_bundle = Path("/run/root-only/deployment/bundle.jsonl")
        archive = Path("/run/root-only/deployment/site.tar.gz")
        integration_inventory = Path(
            "/run/root-only/deployment/platform-integration.inventory.json"
        )
        extension_mapping = MATERIALIZER.isolated_worker_input_mapping(
            state_name,
            (
                script_path,
                protected_input,
                attestation_bundle,
                archive,
                integration_inventory,
            ),
            (
                protected_input,
                attestation_bundle,
                archive,
                integration_inventory,
            ),
        )
        self.assertEqual(
            tuple(path.name for path in extension_mapping.values()),
            ("01.json", "02.jsonl", "03.tar.gz", "04.inventory.json"),
        )
        uppercase_bundle = Path("/run/root-only/deployment/bundle.JSONL")
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "suffix is invalid",
        ):
            MATERIALIZER.isolated_worker_input_mapping(
                state_name,
                (script_path, uppercase_bundle),
                (uppercase_bundle,),
            )
        overlong_suffix = Path(
            "/run/root-only/deployment/input.abcdefghijklmnopq"
        )
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "suffix is invalid",
        ):
            MATERIALIZER.isolated_worker_input_mapping(
                state_name,
                (script_path, overlong_suffix),
                (overlong_suffix,),
            )

    def test_registry_worker_accepts_only_its_exact_json_remap(self) -> None:
        contract = MATERIALIZER.RegistryFetchContract(
            repository=MATERIALIZER.PROFILES["personal"].site_repository,
            kind="manifest",
            digest="sha256:" + "a" * 64,
            maximum_size=MATERIALIZER.MAX_MANIFEST_BYTES,
            expected_size=None,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            worker = Path(temporary_directory)
            worker.chmod(0o700)
            input_root = worker / ".inputs"
            input_root.mkdir(mode=0o755)
            request = input_root / "02"
            request.touch(mode=0o444)
            output = worker / "registry-object"
            metadata = list(input_root.lstat())
            metadata[4] = 0
            metadata[5] = 0
            root_owned_input = os.stat_result(metadata)

            def emulate_fetch(
                _repository,
                _digest,
                destination,
                _maximum_size,
                _environment,
                *,
                kind,
                expected_size,
            ):
                self.assertEqual(kind, "manifest")
                self.assertIsNone(expected_size)
                destination.write_bytes(b"manifest")

            with mock.patch.dict(
                MATERIALIZER.os.environ,
                {"HOME": str(worker)},
                clear=True,
            ), mock.patch.object(
                MATERIALIZER.Path,
                "lstat",
                return_value=root_owned_input,
            ), mock.patch.object(
                MATERIALIZER,
                "read_registry_fetch_contract",
                return_value=contract,
            ) as read_contract, mock.patch.object(
                MATERIALIZER,
                "fetch_registry_object",
                side_effect=emulate_fetch,
            ) as fetch:
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "input root is invalid",
                ):
                    MATERIALIZER.run_registry_fetch_worker(request, output)
                read_contract.assert_not_called()
                fetch.assert_not_called()

                request = request.rename(input_root / "02.json")
                MATERIALIZER.run_registry_fetch_worker(request, output)
                read_contract.assert_called_once_with(request)
                fetch.assert_called_once()

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

            remapped_worker = root / "remapped-worker"
            remapped_worker.mkdir(mode=0o700)
            remapped_source = remapped_worker / "result"
            remapped_source.write_bytes(content)
            remapped_source.chmod(0o444)
            input_root = remapped_worker / ".inputs"
            input_root.mkdir(mode=0o755)
            (input_root / "02").touch(mode=0o644)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "declared entries",
            ):
                MATERIALIZER.copy_trusted_worker_file(
                    remapped_source,
                    trusted / "strict-copy",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    maximum_size=len(content),
                    label="strict cardinality fixture",
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )
            MATERIALIZER.copy_trusted_worker_file(
                remapped_source,
                trusted / "remapped-copy",
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
                maximum_size=len(content),
                label="remapped cardinality fixture",
                trusted_uid=os.geteuid(),
                trusted_gid=os.getegid(),
                expected_remapped_input_names=("02",),
            )
            self.assertEqual((trusted / "remapped-copy").read_bytes(), content)

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

    def test_remapped_worker_input_root_rejects_unsafe_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            external = root / "external"
            external.write_bytes(b"")

            def validate_fixture(
                name: str,
                mutate,
                *,
                expected_uid: int | None = None,
            ) -> None:
                worker = root / name
                worker.mkdir(mode=0o700)
                input_root = worker / ".inputs"
                input_root.mkdir(mode=0o755)
                placeholder = input_root / "02"
                placeholder.touch(mode=0o644)
                mutate(input_root, placeholder)
                worker_fd = os.open(
                    worker,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                try:
                    with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                        MATERIALIZER.validate_remapped_worker_inputs(
                            worker_fd,
                            os.fstat(worker_fd),
                            ("02",),
                            expected_uid=(
                                os.geteuid() if expected_uid is None else expected_uid
                            ),
                            expected_gid=os.getegid(),
                            label=f"{name} fixture",
                        )
                finally:
                    os.close(worker_fd)

            validate_fixture(
                "symlink",
                lambda _root, placeholder: (
                    placeholder.unlink(),
                    placeholder.symlink_to(external),
                ),
            )
            validate_fixture(
                "writable",
                lambda _root, placeholder: placeholder.chmod(0o664),
            )
            validate_fixture(
                "wrong-root-owner",
                lambda _root, _placeholder: None,
                expected_uid=os.geteuid() + 1,
            )

            wrong_owner_worker = root / "wrong-placeholder-owner"
            wrong_owner_worker.mkdir(mode=0o700)
            wrong_owner_input_root = wrong_owner_worker / ".inputs"
            wrong_owner_input_root.mkdir(mode=0o755)
            (wrong_owner_input_root / "02").touch(mode=0o644)
            wrong_owner_worker_fd = os.open(
                wrong_owner_worker,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            real_fstat = os.fstat

            def report_wrong_placeholder_owner(descriptor):
                metadata = real_fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    return metadata
                values = list(metadata)
                values[4] = metadata.st_uid + 1
                return os.stat_result(values)

            try:
                with mock.patch.object(
                    MATERIALIZER.os,
                    "fstat",
                    side_effect=report_wrong_placeholder_owner,
                ):
                    with self.assertRaisesRegex(
                        MATERIALIZER.StaticDeploymentError,
                        "placeholder is unsafe",
                    ):
                        MATERIALIZER.validate_remapped_worker_inputs(
                            wrong_owner_worker_fd,
                            real_fstat(wrong_owner_worker_fd),
                            ("02",),
                            expected_uid=os.geteuid(),
                            expected_gid=os.getegid(),
                            label="wrong placeholder owner fixture",
                        )
            finally:
                os.close(wrong_owner_worker_fd)

            validate_fixture(
                "extra",
                lambda input_root, _placeholder: (input_root / "03").touch(
                    mode=0o644
                ),
            )
            validate_fixture(
                "wrong-name",
                lambda _root, placeholder: placeholder.rename(
                    placeholder.with_name("03")
                ),
            )
            validate_fixture(
                "nonzero",
                lambda _root, placeholder: placeholder.write_bytes(b"x"),
            )

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

    def test_deployment_state_is_canonical_protected_and_complete(self) -> None:
        state = deployment_state()
        encoded = MATERIALIZER.canonical_deployment_state(state)
        self.assertEqual(MATERIALIZER.parse_deployment_state(encoded, "state"), state)
        self.assertEqual(
            MATERIALIZER.release_target_for_state(state),
            f"releases/sha256-{'1' * 64}",
        )
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "not canonical",
        ):
            MATERIALIZER.parse_deployment_state(
                json.dumps(state.as_dict(), indent=2).encode("ascii"),
                "state",
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "active"
            directory.mkdir(mode=0o700)
            MATERIALIZER.write_deployment_state_file(
                directory,
                "personal.json",
                state,
                "active state",
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            path = directory / "personal.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                MATERIALIZER.read_deployment_state_file(
                    directory,
                    "personal.json",
                    "active state",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                ),
                state,
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "protected state file",
            ):
                MATERIALIZER.read_deployment_state_file(
                    directory,
                    "personal.json",
                    "active state",
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )

    def test_deployment_transaction_binds_previous_and_expected_targets(self) -> None:
        state = deployment_state()
        transaction = MATERIALIZER.DeploymentTransaction(
            candidate=state,
            previous_state=None,
            previous_target=f"releases/sha256-{'9' * 64}",
            expected_target=MATERIALIZER.release_target_for_state(state),
            phase="prepared",
        )
        encoded = MATERIALIZER.canonical_deployment_transaction(transaction)
        self.assertEqual(
            MATERIALIZER.parse_deployment_transaction(encoded, "transaction"),
            transaction,
        )
        forged = dataclasses.replace(
            transaction,
            expected_target=f"releases/sha256-{'8' * 64}",
        )
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "does not match",
        ):
            MATERIALIZER.canonical_deployment_transaction(forged)

    def test_live_activation_commits_state_only_after_public_probe(self) -> None:
        state = deployment_state()
        profile = MATERIALIZER.PROFILES["personal"]
        inventory = probe_inventory()
        events: list[str] = []

        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
            side_effect=lambda *args: events.append("previous-release"),
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
            side_effect=lambda *args: events.append("head"),
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
            side_effect=lambda *args, **kwargs: events.append("caddy-runtime"),
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=f"releases/sha256-{'9' * 64}",
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
            side_effect=lambda *args: events.append("transaction"),
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            side_effect=lambda *args: (
                events.append("activate")
                or (f"releases/sha256-{'9' * 64}", True)
            ),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
            side_effect=lambda *args: events.append("public-probe"),
        ), mock.patch.object(
            MATERIALIZER,
            "write_persisted_inventory",
            side_effect=lambda *args: events.append("persisted-inventory"),
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
            side_effect=lambda *args: events.append("active-state"),
        ), mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
            side_effect=lambda *args: events.append("transaction-removed"),
        ):
            MATERIALIZER.activate_live_release(
                state,
                profile,
                inventory,
                Path("/srv/www/personal/current"),
                Path("/srv/www/personal/releases"),
                f"sha256-{'1' * 64}",
                Path("/var/tmp/test"),
                previous_active_state=deployment_state(site_digit="9"),
            )
        self.assertEqual(
            events,
            [
                "previous-release",
                "caddy-runtime",
                "head",
                "transaction",
                "activate",
                "transaction",
                "public-probe",
                "persisted-inventory",
                "caddy-runtime",
                "head",
                "transaction",
                "active-state",
                "transaction-removed",
            ],
        )

    def test_live_probe_failure_restores_and_quarantines_candidate(self) -> None:
        state = deployment_state()
        profile = MATERIALIZER.PROFILES["personal"]
        inventory = MATERIALIZER.InventoryContract(1, "0" * 64, 0, 0, ())
        previous = f"releases/sha256-{'9' * 64}"
        writes: list[tuple[Path, str]] = []
        events: list[str] = []
        runtime_calls = 0
        head_calls = 0

        def check_runtime(*_args, **_kwargs):
            nonlocal runtime_calls
            runtime_calls += 1
            events.append(f"caddy-{runtime_calls}")
            return "a" * 64

        def check_head(*_args, **_kwargs):
            nonlocal head_calls
            head_calls += 1
            events.append(f"head-{head_calls}")

        def write_state(directory, name, *_args):
            writes.append((directory, name))
            events.append(
                "quarantine"
                if directory == MATERIALIZER.STATIC_QUARANTINE_ROOT
                else "active-state"
            )

        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
            side_effect=check_head,
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
            side_effect=check_runtime,
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=previous,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
            side_effect=lambda *args: events.append("transaction"),
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
            side_effect=MATERIALIZER.StaticDeploymentError("bad public response"),
        ), mock.patch.object(
            MATERIALIZER,
            "restore_current_target",
            side_effect=lambda *args: events.append("rollback"),
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
            side_effect=write_state,
        ), mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
            side_effect=lambda *args: events.append("transaction-removed"),
        ):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "restored and the candidate quarantined",
            ):
                MATERIALIZER.activate_live_release(
                    state,
                    profile,
                    inventory,
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=deployment_state(site_digit="9"),
                )
        self.assertEqual(
            events,
            [
                "caddy-1",
                "head-1",
                "transaction",
                "transaction",
                "transaction",
                "rollback",
                "active-state",
                "caddy-2",
                "head-2",
                "quarantine",
                "transaction-removed",
            ],
        )
        self.assertEqual(
            writes,
            [
                (MATERIALIZER.STATIC_ACTIVE_STATE_ROOT, "personal.json"),
                (
                    MATERIALIZER.STATIC_QUARANTINE_ROOT,
                    MATERIALIZER.quarantine_name(state),
                ),
            ],
        )

    def test_quarantine_write_failure_keeps_the_rejected_transaction(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        transactions: list[object] = []

        def write_state(directory, *_args):
            if directory == MATERIALIZER.STATIC_QUARANTINE_ROOT:
                raise MATERIALIZER.StaticDeploymentError("quarantine disk full")

        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
            return_value="a" * 64,
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=previous_target,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
            side_effect=transactions.append,
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous_target, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
            side_effect=MATERIALIZER.StaticDeploymentError("bad public response"),
        ), mock.patch.object(
            MATERIALIZER,
            "restore_deployment_transaction",
        ) as restore, mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
            side_effect=write_state,
        ), mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
        ) as remove_transaction:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "quarantine state could not be made durable",
            ):
                MATERIALIZER.activate_live_release(
                    candidate,
                    MATERIALIZER.PROFILES["personal"],
                    probe_inventory(),
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=previous_state,
                )
        self.assertEqual(transactions[-1].phase, "probe-rejected")
        restore.assert_called_once()
        remove_transaction.assert_not_called()

    def test_failed_bootstrap_probe_does_not_quarantine_an_unchanged_link(self) -> None:
        state = deployment_state()
        profile = MATERIALIZER.PROFILES["personal"]
        inventory = MATERIALIZER.InventoryContract(1, "0" * 64, 0, 0, ())
        expected = MATERIALIZER.release_target_for_state(state)
        with mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=expected,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(expected, False),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
            side_effect=MATERIALIZER.StaticDeploymentError("bad public response"),
        ), mock.patch.object(
            MATERIALIZER,
            "restore_current_target",
        ) as restore, mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
        ) as write_state, mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
        ) as remove_transaction:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "existing untracked release",
            ):
                MATERIALIZER.activate_live_release(
                    state,
                    profile,
                    inventory,
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=None,
                )
        restore.assert_not_called()
        write_state.assert_not_called()
        remove_transaction.assert_called_once()

    def test_interrupted_activation_recovers_before_new_candidate(self) -> None:
        state = deployment_state()
        previous = f"releases/sha256-{'9' * 64}"
        transaction = MATERIALIZER.DeploymentTransaction(
            candidate=state,
            previous_state=None,
            previous_target=previous,
            expected_target=MATERIALIZER.release_target_for_state(state),
            phase="switched",
        )
        events: list[str] = []
        with mock.patch.object(
            MATERIALIZER,
            "read_deployment_transaction",
            return_value=transaction,
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=transaction.expected_target,
        ), mock.patch.object(
            MATERIALIZER,
            "read_deployment_state_file",
            return_value=None,
        ), mock.patch.object(
            MATERIALIZER,
            "restore_current_target",
            side_effect=lambda *args: events.append("rollback"),
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
            side_effect=lambda *args: events.append("quarantine"),
        ), mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
            side_effect=lambda directory, *args: events.append(
                "active-removed"
                if directory == MATERIALIZER.STATIC_ACTIVE_STATE_ROOT
                else "transaction-removed"
            ),
        ):
            MATERIALIZER.recover_interrupted_deployment(
                "personal",
                Path("/srv/www/personal/current"),
                Path("/srv/www/personal/releases"),
            )
        self.assertEqual(
            events,
            ["rollback", "active-removed", "quarantine", "transaction-removed"],
        )

    def test_interrupted_activation_recovery_respects_durable_phase(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        expected_target = MATERIALIZER.release_target_for_state(candidate)
        cases = (
            ("prepared", previous_target, None),
            ("prepared", expected_target, True),
            ("switched", expected_target, True),
            ("probe-rejected", expected_target, True),
            ("probe-rejected", previous_target, True),
            ("probe-failed", previous_target, True),
            ("superseded", expected_target, False),
        )
        for phase, actual_target, expected_quarantine in cases:
            with self.subTest(phase=phase, target=actual_target):
                encoded_transaction = MATERIALIZER.canonical_deployment_transaction(
                    MATERIALIZER.DeploymentTransaction(
                        candidate=candidate,
                        previous_state=previous_state,
                        previous_target=previous_target,
                        expected_target=expected_target,
                        phase=phase,
                    )
                )
                transaction = MATERIALIZER.parse_deployment_transaction(
                    encoded_transaction,
                    "durable recovery transaction",
                )
                with mock.patch.object(
                    MATERIALIZER,
                    "read_deployment_transaction",
                    return_value=transaction,
                ), mock.patch.object(
                    MATERIALIZER,
                    "get_current_target",
                    return_value=actual_target,
                ), mock.patch.object(
                    MATERIALIZER,
                    "read_deployment_state_file",
                    return_value=previous_state,
                ), mock.patch.object(
                    MATERIALIZER,
                    "validate_persisted_release",
                ), mock.patch.object(
                    MATERIALIZER,
                    "rollback_deployment_transaction",
                ) as rollback, mock.patch.object(
                    MATERIALIZER,
                    "write_deployment_state_file",
                ) as write_state, mock.patch.object(
                    MATERIALIZER,
                    "remove_protected_state_file",
                ) as remove_state:
                    MATERIALIZER.recover_interrupted_deployment(
                        "personal",
                        Path("/srv/www/personal/current"),
                        Path("/srv/www/personal/releases"),
                    )
                if expected_quarantine is None:
                    rollback.assert_not_called()
                    write_state.assert_called_once_with(
                        MATERIALIZER.STATIC_ACTIVE_STATE_ROOT,
                        "personal.json",
                        previous_state,
                        "active deployment state",
                    )
                    remove_state.assert_called_once()
                else:
                    rollback.assert_called_once_with(
                        transaction,
                        Path("/srv/www/personal/current"),
                        Path("/srv/www/personal/releases"),
                        quarantine=expected_quarantine,
                    )

    def test_interrupted_probed_activation_commits_or_preserves_previous(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        transaction = MATERIALIZER.DeploymentTransaction(
            candidate=candidate,
            previous_state=previous_state,
            previous_target=MATERIALIZER.release_target_for_state(previous_state),
            expected_target=MATERIALIZER.release_target_for_state(candidate),
            phase="probed",
        )
        for actual_target, expected_state in (
            (transaction.expected_target, candidate),
            (transaction.previous_target, previous_state),
        ):
            with self.subTest(target=actual_target):
                with mock.patch.object(
                    MATERIALIZER,
                    "read_deployment_transaction",
                    return_value=transaction,
                ), mock.patch.object(
                    MATERIALIZER,
                    "get_current_target",
                    return_value=actual_target,
                ), mock.patch.object(
                    MATERIALIZER,
                    "read_deployment_state_file",
                    return_value=previous_state,
                ), mock.patch.object(
                    MATERIALIZER,
                    "validate_persisted_release",
                    return_value=probe_inventory(),
                ) as validate_release, mock.patch.object(
                    MATERIALIZER,
                    "write_deployment_state_file",
                ) as write_state, mock.patch.object(
                    MATERIALIZER,
                    "remove_protected_state_file",
                ) as remove_state:
                    MATERIALIZER.recover_interrupted_deployment(
                        "personal",
                        Path("/srv/www/personal/current"),
                        Path("/srv/www/personal/releases"),
                    )
                self.assertEqual(validate_release.call_count, 1)
                expected_validated_state = (
                    candidate
                    if actual_target == transaction.expected_target
                    else previous_state
                )
                validate_release.assert_called_once_with(
                    expected_validated_state,
                    Path("/srv/www/personal/releases"),
                )
                write_state.assert_called_once_with(
                    MATERIALIZER.STATIC_ACTIVE_STATE_ROOT,
                    "personal.json",
                    expected_state,
                    "active deployment state",
                )
                remove_state.assert_called_once()

    def test_recovery_refuses_to_commit_or_restore_a_corrupted_managed_release(
        self,
    ) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        releases = Path("/srv/www/personal/releases")
        current = Path("/srv/www/personal/current")
        transaction = MATERIALIZER.DeploymentTransaction(
            candidate=candidate,
            previous_state=previous_state,
            previous_target=MATERIALIZER.release_target_for_state(previous_state),
            expected_target=MATERIALIZER.release_target_for_state(candidate),
            phase="probed",
        )
        with mock.patch.object(
            MATERIALIZER,
            "read_deployment_transaction",
            return_value=transaction,
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=transaction.expected_target,
        ), mock.patch.object(
            MATERIALIZER,
            "read_deployment_state_file",
            return_value=previous_state,
        ), mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
            side_effect=MATERIALIZER.StaticDeploymentError("filesystem changed"),
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
        ) as write_state, mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
        ) as remove_state:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "filesystem changed",
            ):
                MATERIALIZER.recover_interrupted_deployment(
                    "personal",
                    current,
                    releases,
                )
        write_state.assert_not_called()
        remove_state.assert_not_called()

        rollback = dataclasses.replace(transaction, phase="probe-failed")
        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
            side_effect=MATERIALIZER.StaticDeploymentError("previous changed"),
        ), mock.patch.object(
            MATERIALIZER,
            "restore_current_target",
        ) as restore:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "previous changed",
            ):
                MATERIALIZER.rollback_deployment_transaction(
                    rollback,
                    current,
                    releases,
                    quarantine=True,
                )
        restore.assert_not_called()

    def test_probed_phase_write_failure_rolls_back_without_quarantine(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        profile = MATERIALIZER.PROFILES["personal"]
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        writes: list[object] = []

        def write_transaction(transaction: object) -> None:
            writes.append(transaction)
            if getattr(transaction, "phase") == "probed":
                raise MATERIALIZER.StaticDeploymentError("fsync failed")

        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=previous_target,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
            side_effect=write_transaction,
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous_target, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
        ), mock.patch.object(
            MATERIALIZER,
            "write_persisted_inventory",
        ), mock.patch.object(
            MATERIALIZER,
            "read_deployment_transaction",
            side_effect=lambda _application: writes[-2],
        ), mock.patch.object(
            MATERIALIZER,
            "rollback_deployment_transaction",
        ) as rollback:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "restored without quarantine",
            ):
                MATERIALIZER.activate_live_release(
                    candidate,
                    profile,
                    probe_inventory(),
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=previous_state,
                )
        rollback.assert_called_once()
        self.assertFalse(rollback.call_args.kwargs["quarantine"])

    def test_failed_live_probe_rolls_back_before_slow_classification(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        events: list[str] = []
        runtime_calls = 0

        def check_runtime(*_args, **_kwargs):
            nonlocal runtime_calls
            runtime_calls += 1
            events.append(f"runtime-{runtime_calls}")
            if runtime_calls == 2:
                raise MATERIALIZER.StaticDeploymentError("runtime changed")
            return "a" * 64

        def fail_probe(*_args, **_kwargs):
            events.append("probe-failed")
            raise MATERIALIZER.StaticDeploymentError("bad public response")

        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
            side_effect=check_runtime,
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
            side_effect=lambda *_args: events.append("head"),
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=previous_target,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous_target, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
            side_effect=fail_probe,
        ), mock.patch.object(
            MATERIALIZER,
            "restore_deployment_transaction",
            side_effect=lambda *_args, **_kwargs: events.append("rollback"),
        ) as restore, mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
        ) as write_state, mock.patch.object(
            MATERIALIZER,
            "remove_protected_state_file",
            side_effect=lambda *_args: events.append("transaction-removed"),
        ):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "already restored without quarantine",
            ):
                MATERIALIZER.activate_live_release(
                    candidate,
                    MATERIALIZER.PROFILES["personal"],
                    probe_inventory(),
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=previous_state,
                )
        self.assertLess(events.index("rollback"), events.index("runtime-2"))
        self.assertLess(events.index("runtime-2"), events.index("transaction-removed"))
        restore.assert_called_once()
        write_state.assert_not_called()

    def test_active_state_commit_failure_is_recovered_as_committed(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        expected_target = MATERIALIZER.release_target_for_state(candidate)
        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            side_effect=[previous_target, expected_target],
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous_target, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
        ), mock.patch.object(
            MATERIALIZER,
            "write_persisted_inventory",
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_state_file",
            side_effect=MATERIALIZER.StaticDeploymentError("ambiguous fsync"),
        ), mock.patch.object(
            MATERIALIZER,
            "recover_interrupted_deployment",
        ) as recover, mock.patch.object(
            MATERIALIZER,
            "read_deployment_state_file",
            return_value=candidate,
        ):
            MATERIALIZER.activate_live_release(
                candidate,
                MATERIALIZER.PROFILES["personal"],
                probe_inventory(),
                Path("/srv/www/personal/current"),
                Path("/srv/www/personal/releases"),
                f"sha256-{'1' * 64}",
                Path("/var/tmp/test"),
                previous_active_state=previous_state,
            )
        recover.assert_called_once_with(
            "personal",
            Path("/srv/www/personal/current"),
            Path("/srv/www/personal/releases"),
        )

    def test_inventory_persistence_failure_restores_without_quarantine(self) -> None:
        candidate = deployment_state()
        previous_state = deployment_state(site_digit="9")
        previous_target = MATERIALIZER.release_target_for_state(previous_state)
        with mock.patch.object(
            MATERIALIZER,
            "validate_persisted_release",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ), mock.patch.object(
            MATERIALIZER,
            "get_current_target",
            return_value=previous_target,
        ), mock.patch.object(
            MATERIALIZER,
            "write_deployment_transaction",
        ), mock.patch.object(
            MATERIALIZER,
            "activate_release",
            return_value=(previous_target, True),
        ), mock.patch.object(
            MATERIALIZER,
            "probe_live_release",
        ), mock.patch.object(
            MATERIALIZER,
            "write_persisted_inventory",
            side_effect=MATERIALIZER.StaticDeploymentError("state disk full"),
        ), mock.patch.object(
            MATERIALIZER,
            "rollback_deployment_transaction",
        ) as rollback:
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "protected inventory could not be persisted",
            ):
                MATERIALIZER.activate_live_release(
                    candidate,
                    MATERIALIZER.PROFILES["personal"],
                    probe_inventory(),
                    Path("/srv/www/personal/current"),
                    Path("/srv/www/personal/releases"),
                    f"sha256-{'1' * 64}",
                    Path("/var/tmp/test"),
                    previous_active_state=previous_state,
                )
        rollback.assert_called_once()
        self.assertFalse(rollback.call_args.kwargs["quarantine"])

    def test_persisted_inventory_is_canonical_and_bound_to_source(self) -> None:
        state = deployment_state()
        inventory = probe_inventory()
        profile = MATERIALIZER.PROFILES["personal"]
        encoded = MATERIALIZER.canonical_route_inventory_bytes(
            inventory,
            profile,
            state.source_revision,
        )
        self.assertEqual(
            MATERIALIZER.persisted_inventory_from_bytes(encoded, state),
            inventory,
        )
        value = json.loads(encoded)
        value["source"]["revision"] = "f" * 40
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "source revision",
        ):
            MATERIALIZER.persisted_inventory_from_bytes(
                canonical_json(value),
                state,
            )

    def test_exact_active_candidate_uses_local_health_path_without_registry(self) -> None:
        candidate = deployment_state()
        profile = MATERIALIZER.PROFILES["personal"]
        inventory = probe_inventory()
        safe_directory = mock.Mock(
            st_mode=MATERIALIZER.stat.S_IFDIR | 0o700,
            st_uid=0,
            st_gid=0,
        )
        pins = (
            candidate.integration_revision,
            candidate.integration_reference,
            candidate.caddy_image,
            frozenset(MATERIALIZER.PROFILES),
        )
        lock_state = {"held": False}

        @contextlib.contextmanager
        def tracked_deployment_lock():
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

        def assert_owner_guard_is_locked(_application):
            self.assertTrue(lock_state["held"])

        with mock.patch.object(
            MATERIALIZER,
            "validate_runtime",
        ), mock.patch.object(
            MATERIALIZER,
            "read_promoted_caddy_image",
            return_value=candidate.caddy_image,
        ), mock.patch.object(
            MATERIALIZER,
            "read_static_production_pins",
            return_value=pins,
        ), mock.patch.object(
            MATERIALIZER,
            "read_application_production_enablement",
            return_value={
                application: False for application in MATERIALIZER.PROFILES
            },
        ) as read_application_enablement, mock.patch.object(
            MATERIALIZER,
            "read_active_application_enablement",
            return_value={
                application: False for application in MATERIALIZER.PROFILES
            },
        ) as read_active_enablement, mock.patch.object(
            MATERIALIZER.Path,
            "lstat",
            return_value=safe_directory,
        ), mock.patch.object(
            MATERIALIZER,
            "deployment_lock",
            side_effect=tracked_deployment_lock,
        ), mock.patch.object(
            MATERIALIZER,
            "refuse_public_edge_base_transaction_locked",
            side_effect=lambda: self.assertTrue(lock_state["held"]),
        ) as check_base_transaction, mock.patch.object(
            MATERIALIZER,
            "refuse_compose_application_owner",
            side_effect=assert_owner_guard_is_locked,
        ) as check_compose_owner, mock.patch.object(
            MATERIALIZER,
            "cleanup_probe_containers",
        ), mock.patch.object(
            MATERIALIZER,
            "cleanup_static_filesystem_residue",
        ), mock.patch.object(
            MATERIALIZER,
            "deployment_temporary_root",
            return_value=Path("/var/tmp"),
        ), mock.patch.object(
            MATERIALIZER,
            "refuse_isolated_worker_residue_locked",
        ), mock.patch.object(
            MATERIALIZER,
            "assert_current_source_revision",
        ) as check_head, mock.patch.object(
            MATERIALIZER,
            "prepare_live_deployment",
            return_value=(False, candidate),
        ), mock.patch.object(
            MATERIALIZER,
            "read_persisted_inventory",
            return_value=inventory,
        ) as read_inventory, mock.patch.object(
            MATERIALIZER,
            "filesystem_inventory",
        ) as check_files, mock.patch.object(
            MATERIALIZER,
            "assert_live_caddy_runtime",
        ) as check_caddy, mock.patch.object(
            MATERIALIZER,
            "probe_live_health",
        ) as check_https, mock.patch.object(
            MATERIALIZER,
            "deploy_locked",
        ) as deploy_locked:
            MATERIALIZER.deploy(
                candidate.application,
                candidate.source_revision,
                candidate.site_reference,
                candidate.routes_reference,
                candidate.integration_revision,
                candidate.integration_reference,
                candidate.caddy_image,
                activate_live=True,
            )
        read_inventory.assert_called_once_with(candidate)
        check_base_transaction.assert_called_once_with()
        check_compose_owner.assert_called_once_with(candidate.application)
        read_application_enablement.assert_called_once_with(
            MATERIALIZER.APPLICATION_PRODUCTION_CONTRACT_PATH,
            require_root_owner=True,
        )
        read_active_enablement.assert_called_once_with(
            MATERIALIZER.APPLICATION_ACTIVE_MANIFEST_PATH,
            MATERIALIZER.APPLICATION_ACTIVE_STATE_PATH,
            require_root_owner=True,
        )
        check_files.assert_called_once()
        self.assertEqual(check_caddy.call_count, 2)
        self.assertEqual(
            check_caddy.call_args_list[1].kwargs["expected_identifier"],
            check_caddy.return_value,
        )
        check_https.assert_called_once()
        self.assertEqual(check_head.call_count, 3)
        deploy_locked.assert_not_called()

    def test_live_caddy_identity_must_stay_stable_through_probe(self) -> None:
        image = deployment_state().caddy_image
        first_identifier = "a" * 64
        second_identifier = "b" * 64
        responses = (
            subprocess.CompletedProcess([], 0, f"{first_identifier}\n", ""),
            subprocess.CompletedProcess([], 0, f"{image}\thealthy\n", ""),
            subprocess.CompletedProcess([], 0, f"{second_identifier}\n", ""),
        )
        with mock.patch.object(
            MATERIALIZER,
            "run_checked",
            side_effect=responses,
        ):
            observed = MATERIALIZER.assert_live_caddy_runtime(
                image,
                Path("/var/tmp/test"),
            )
            self.assertEqual(observed, first_identifier)
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "changed during deployment",
            ):
                MATERIALIZER.assert_live_caddy_runtime(
                    image,
                    Path("/var/tmp/test"),
                    expected_identifier=first_identifier,
                )

    def test_internal_live_recovery_cli_is_exact(self) -> None:
        with mock.patch.object(MATERIALIZER, "recover_live_deployments") as recover:
            self.assertEqual(MATERIALIZER.main(["--recover-live", "personal"]), 0)
        recover.assert_called_once_with("personal")
        with self.assertRaises(SystemExit):
            MATERIALIZER.build_parser().parse_args(
                [
                    "--acti",
                    "personal",
                    REVISION,
                    "site",
                    "routes",
                    REVISION,
                    "integration",
                    "caddy",
                ]
            )

    def test_canonical_source_head_is_checked_exactly_with_retry(self) -> None:
        profile = MATERIALIZER.PROFILES["personal"]
        failure = subprocess.CompletedProcess([], 128, "", "temporary")
        success = subprocess.CompletedProcess(
            [],
            0,
            f"{REVISION}\t{profile.source_ref}\n",
            "",
        )
        with mock.patch.object(
            MATERIALIZER.subprocess,
            "run",
            side_effect=[failure, success],
        ) as run, mock.patch.object(MATERIALIZER.time, "sleep") as sleep:
            MATERIALIZER.assert_current_source_revision(profile, REVISION, Path("/tmp"))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)

        stale = subprocess.CompletedProcess(
            [],
            0,
            f"{'f' * 40}\t{profile.source_ref}\n",
            "",
        )
        with mock.patch.object(MATERIALIZER.subprocess, "run", return_value=stale):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "exact canonical branch head",
            ):
                MATERIALIZER.assert_current_source_revision(
                    profile,
                    REVISION,
                    Path("/tmp"),
                )

    def test_source_ancestry_accepts_descendants_and_rejects_force_resets(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is unavailable")
        profile = MATERIALIZER.PROFILES["personal"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "source"
            subprocess.run(
                [git, "init", "--quiet", "--initial-branch=main", repository],
                check=True,
            )
            subprocess.run(
                [git, "-C", repository, "config", "user.name", "Static Test"],
                check=True,
            )
            subprocess.run(
                [git, "-C", repository, "config", "user.email", "static@example.test"],
                check=True,
            )
            tracked = repository / "index.html"
            tracked.write_text("one\n", encoding="utf-8")
            subprocess.run([git, "-C", repository, "add", "index.html"], check=True)
            subprocess.run(
                [git, "-C", repository, "commit", "--quiet", "-m", "one"],
                check=True,
            )
            active = subprocess.run(
                [git, "-C", repository, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("two\n", encoding="utf-8")
            subprocess.run([git, "-C", repository, "commit", "--quiet", "-am", "two"], check=True)
            descendant = subprocess.run(
                [git, "-C", repository, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            descendant_work = root / "descendant-work"
            descendant_work.mkdir(mode=0o700)
            with mock.patch.object(MATERIALIZER, "GIT_PATH", Path(git)):
                MATERIALIZER.verify_source_ancestry_repository(
                    profile,
                    active,
                    descendant,
                    descendant_work,
                    repository_url=str(repository),
                    allowed_protocol="file",
                )

            subprocess.run(
                [git, "-C", repository, "reset", "--hard", "--quiet", active],
                check=True,
            )
            tracked.write_text("divergent\n", encoding="utf-8")
            subprocess.run([git, "-C", repository, "commit", "--quiet", "-am", "divergent"], check=True)
            divergent = subprocess.run(
                [git, "-C", repository, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            divergent_work = root / "divergent-work"
            divergent_work.mkdir(mode=0o700)
            with mock.patch.object(MATERIALIZER, "GIT_PATH", Path(git)):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "does not descend",
                ):
                    MATERIALIZER.verify_source_ancestry_repository(
                        profile,
                        descendant,
                        divergent,
                        divergent_work,
                        repository_url=str(repository),
                        allowed_protocol="file",
                    )

    def test_source_ancestry_runs_only_in_a_bounded_network_worker(self) -> None:
        profile = MATERIALIZER.PROFILES["personal"]
        state = mock.Mock()
        with mock.patch.object(
            MATERIALIZER,
            "run_isolated_worker",
            return_value=state,
        ) as run_worker, mock.patch.object(
            MATERIALIZER,
            "cleanup_isolated_worker_state",
        ) as cleanup:
            MATERIALIZER.assert_source_revision_descends_from_active(
                profile,
                "a" * 40,
                "b" * 40,
                Path("/var/tmp/unused-root-work"),
            )
        worker_call = run_worker.call_args
        self.assertEqual(worker_call.args[0], "ancestry")
        self.assertEqual(
            worker_call.args[1][-4:],
            ["--source-ancestry-worker", "personal", "a" * 40, "b" * 40],
        )
        self.assertTrue(worker_call.kwargs["network"])
        self.assertEqual(worker_call.kwargs["runtime_seconds"], 240)
        self.assertEqual(worker_call.kwargs["memory_max"], "256M")
        self.assertEqual(worker_call.kwargs["file_size_max"], "128M")
        cleanup.assert_called_once_with(state)

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
            trusted_root = root / "trusted-root.jsonl"
            trusted_root.write_bytes(trusted_root_jsonl())
            trusted_root.chmod(0o444)
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
                    trusted_root=trusted_root,
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
            self.assertIn("--custom-trusted-root", verify[1])
            self.assertIn(str(trusted_root), verify[1])
            self.assertIn("--digest-alg", verify[1])
            self.assertIn("sha256", verify[1])
            self.assertIn("--hostname", verify[1])
            self.assertIn("github.com", verify[1])
            self.assertIn("--deny-self-hosted-runners", verify[1])
            self.assertNotIn("--bundle-from-oci", verify[1])
            self.assertFalse(any("TOKEN" in argument for argument in verify[1]))
            self.assertIn(trusted_root, verify[2]["inputs"])
            bundle_path = Path(verify[1][verify[1].index("--bundle") + 1])
            self.assertEqual(
                verify[2]["remapped_inputs"],
                (subject_path, bundle_path, trusted_root),
            )
            mapping = MATERIALIZER.isolated_worker_input_mapping(
                "9" * 32,
                verify[2]["inputs"],
                verify[2]["remapped_inputs"],
            )
            resolved = MATERIALIZER.resolved_isolated_worker_command(
                "9" * 32,
                verify[1],
                mapping,
            )
            self.assertEqual(
                Path(resolved[resolved.index("--bundle") + 1]).suffix,
                ".jsonl",
            )
            self.assertEqual(
                Path(resolved[resolved.index("--custom-trusted-root") + 1]).suffix,
                ".jsonl",
            )
            self.assertEqual(list(root.glob("attestation-*.jsonl")), [])

    def test_trusted_root_validation_requires_two_lf_terminated_records(self) -> None:
        valid = trusted_root_jsonl()
        MATERIALIZER.validate_trusted_root_jsonl(valid)
        invalid_values = (
            valid.rstrip(b"\n"),
            valid.splitlines(keepends=True)[0],
            valid + valid.splitlines(keepends=True)[0],
            b"x" * (MATERIALIZER.MAX_TRUSTED_ROOT_BYTES + 1),
            b'{"mediaType":"wrong"}\n{"mediaType":"wrong"}\n',
            (
                b'{"mediaType":"'
                + MATERIALIZER.TRUSTED_ROOT_MEDIA_TYPE.encode("ascii")
                + b'","mediaType":"duplicate"}\n'
                + valid.splitlines(keepends=True)[1]
            ),
            b'{"mediaType":"\xff"}\n' + valid.splitlines(keepends=True)[1],
        )
        for raw in invalid_values:
            with self.subTest(raw=raw[:80]):
                with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                    MATERIALIZER.validate_trusted_root_jsonl(raw)

    def test_trusted_root_worker_uses_fixed_gh_command_and_removes_cache(self) -> None:
        root_bytes = trusted_root_jsonl()
        calls: list[tuple[list[str], dict[str, object]]] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            worker_home = Path(temporary_directory)
            output = worker_home / "trusted-root.jsonl"

            def emulate_gh(command, **kwargs):
                calls.append((command, kwargs))
                os.write(kwargs["stdout"], root_bytes)
                cache = Path(kwargs["env"]["XDG_CACHE_HOME"])
                cache.mkdir(parents=True)
                (cache / "metadata.json").write_text("cached", encoding="ascii")
                state = Path(kwargs["env"]["XDG_STATE_HOME"])
                state.mkdir(parents=True)
                (state / "device-id").write_text("state", encoding="ascii")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.dict(os.environ, {"HOME": str(worker_home)}), mock.patch.object(
                MATERIALIZER.subprocess,
                "run",
                side_effect=emulate_gh,
            ), mock.patch.object(MATERIALIZER.os, "geteuid", return_value=1000), mock.patch.object(
                MATERIALIZER.os,
                "getegid",
                return_value=1000,
            ):
                MATERIALIZER.run_trusted_root_fetch_worker(output)

            self.assertEqual(output.read_bytes(), root_bytes)
            self.assertEqual(output.stat().st_mode & 0o777, 0o444)
            self.assertEqual(set(worker_home.iterdir()), {output})
            self.assertEqual(
                calls[0][0],
                [
                    str(MATERIALIZER.GH_PATH),
                    "attestation",
                    "trusted-root",
                    "--hostname",
                    "github.com",
                ],
            )
            self.assertEqual(calls[0][1]["stdin"], subprocess.DEVNULL)
            self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
            self.assertNotIn("GH_TOKEN", calls[0][1]["env"])
            self.assertNotIn("GITHUB_TOKEN", calls[0][1]["env"])

    def test_trusted_root_worker_rejects_failed_or_malformed_fetches(self) -> None:
        cases = (
            (1, trusted_root_jsonl()),
            (0, b'{"mediaType":"wrong"}\n'),
        )
        for returncode, payload in cases:
            with self.subTest(returncode=returncode, payload=payload[:40]):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    worker_home = Path(temporary_directory)
                    output = worker_home / "trusted-root.jsonl"

                    def emulate_gh(command, **kwargs):
                        os.write(kwargs["stdout"], payload)
                        return subprocess.CompletedProcess(command, returncode)

                    with mock.patch.dict(
                        os.environ,
                        {"HOME": str(worker_home)},
                    ), mock.patch.object(
                        MATERIALIZER.subprocess,
                        "run",
                        side_effect=emulate_gh,
                    ), mock.patch.object(
                        MATERIALIZER.os,
                        "geteuid",
                        return_value=1000,
                    ), mock.patch.object(
                        MATERIALIZER.os,
                        "getegid",
                        return_value=1000,
                    ):
                        with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                            MATERIALIZER.run_trusted_root_fetch_worker(output)
                    self.assertFalse(output.exists())
                    self.assertFalse((worker_home / "scratch").exists())

    def test_trusted_root_worker_rejects_root_and_an_external_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            worker_home = Path(temporary_directory)
            output = worker_home / "trusted-root.jsonl"
            with mock.patch.object(MATERIALIZER.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "must be unprivileged",
                ):
                    MATERIALIZER.run_trusted_root_fetch_worker(output)
            with mock.patch.dict(
                os.environ,
                {"HOME": str(worker_home)},
            ), mock.patch.object(
                MATERIALIZER.os,
                "geteuid",
                return_value=1000,
            ), mock.patch.object(
                MATERIALIZER.os,
                "getegid",
                return_value=1000,
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "protected worker home",
                ):
                    MATERIALIZER.run_trusted_root_fetch_worker(
                        worker_home.parent / "trusted-root.jsonl"
                    )

    def test_trusted_root_fetch_crosses_root_boundary_after_worker_exit(self) -> None:
        calls: list[tuple[str, list[str], dict[str, object]]] = []
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            state_root = root / "worker-state"

            def emulate_worker(phase, command, **kwargs):
                calls.append((phase, command, kwargs))
                state_root.mkdir(mode=0o700)
                output = state_root / "trusted-root.jsonl"
                output.write_bytes(trusted_root_jsonl())
                output.chmod(0o444)
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="5" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                )

            def cleanup_worker(state):
                events.append("worker-cleaned")
                shutil.rmtree(state.physical_root)

            with mock.patch.object(
                MATERIALIZER,
                "run_isolated_worker",
                side_effect=emulate_worker,
            ), mock.patch.object(
                MATERIALIZER,
                "TRUSTED_ROOT_SHA256",
                f"sha256:{hashlib.sha256(trusted_root_jsonl()).hexdigest()}",
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=cleanup_worker,
            ):
                trusted_root = MATERIALIZER.fetch_github_trusted_root_isolated(
                    root,
                    trusted_uid=os.geteuid(),
                    trusted_gid=os.getegid(),
                )

            self.assertEqual(events, ["worker-cleaned"])
            self.assertEqual(trusted_root.read_bytes(), trusted_root_jsonl())
            self.assertEqual(trusted_root.stat().st_mode & 0o777, 0o444)
            phase, command, kwargs = calls[0]
            self.assertEqual(phase, "rootfetch")
            self.assertTrue(kwargs["network"])
            self.assertIn(MATERIALIZER.GH_PATH, kwargs["inputs"])
            self.assertIn("--trusted-root-fetch-worker", command)
            MATERIALIZER.unlink_trusted_file(
                trusted_root,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            self.assertEqual(list(root.iterdir()), [])

    def test_trusted_root_fetch_removes_copied_authority_on_cleanup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            state_root = root / "worker-state"

            def emulate_worker(phase, command, **kwargs):
                state_root.mkdir(mode=0o700)
                output = state_root / "trusted-root.jsonl"
                output.write_bytes(trusted_root_jsonl())
                output.chmod(0o444)
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="6" * 32,
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
                "TRUSTED_ROOT_SHA256",
                f"sha256:{hashlib.sha256(trusted_root_jsonl()).hexdigest()}",
            ), mock.patch.object(
                MATERIALIZER,
                "cleanup_isolated_worker_state",
                side_effect=MATERIALIZER.StaticDeploymentError("cleanup failed"),
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "cleanup failed",
                ):
                    MATERIALIZER.fetch_github_trusted_root_isolated(
                        root,
                        trusted_uid=os.geteuid(),
                        trusted_gid=os.getegid(),
                    )
            self.assertEqual(list(root.glob("trusted-root-*.jsonl")), [])

    def test_trusted_root_fetch_rejects_an_unpinned_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.chmod(0o700)
            state_root = root / "worker-state"

            def emulate_worker(phase, command, **kwargs):
                state_root.mkdir(mode=0o700)
                output = state_root / "trusted-root.jsonl"
                output.write_bytes(trusted_root_jsonl())
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
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "digest does not match",
                ):
                    MATERIALIZER.fetch_github_trusted_root_isolated(
                        root,
                        trusted_uid=os.geteuid(),
                        trusted_gid=os.getegid(),
                    )
            self.assertEqual(list(root.iterdir()), [])

    def test_attestation_bundle_accepts_utf8_json_at_external_boundary(self) -> None:
        profile = MATERIALIZER.PROFILES["personal"]
        subject_digest = f"sha256:{'1' * 64}"
        reference = f"{profile.site_repository}@{subject_digest}"
        bundle = json.dumps(
            {"note": "signed provenance \u2713"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        bundle_digest = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
        manifest = producer_json(
            {
                "annotations": {},
                "artifactType": MATERIALIZER.SIGSTORE_BUNDLE_MEDIA_TYPE,
                "config": {
                    "digest": MATERIALIZER.OCI_EMPTY_CONFIG["digest"],
                    "mediaType": MATERIALIZER.OCI_EMPTY_CONFIG["mediaType"],
                    "size": MATERIALIZER.OCI_EMPTY_CONFIG["size"],
                },
                "layers": [
                    {
                        "digest": bundle_digest,
                        "mediaType": MATERIALIZER.SIGSTORE_BUNDLE_MEDIA_TYPE,
                        "size": len(bundle),
                    }
                ],
                "mediaType": MATERIALIZER.OCI_MANIFEST_MEDIA_TYPE,
                "schemaVersion": 2,
                "subject": {
                    "digest": subject_digest,
                    "mediaType": MATERIALIZER.OCI_MANIFEST_MEDIA_TYPE,
                    "size": 1,
                },
            }
        )
        manifest_digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
        index = producer_json(
            {
                "manifests": [
                    {
                        "annotations": {
                            "dev.sigstore.bundle.predicateType": (
                                MATERIALIZER.SLSA_PROVENANCE_TYPE
                            )
                        },
                        "artifactType": MATERIALIZER.SIGSTORE_BUNDLE_MEDIA_TYPE,
                        "digest": manifest_digest,
                        "mediaType": MATERIALIZER.OCI_MANIFEST_MEDIA_TYPE,
                        "size": len(manifest),
                    }
                ],
                "mediaType": MATERIALIZER.OCI_INDEX_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            def write_index(
                repository,
                name,
                destination,
                maximum_size,
                environment,
                *,
                accept,
            ):
                self.assertEqual(repository, profile.site_repository)
                self.assertEqual(
                    name,
                    f"sha256-{subject_digest.removeprefix('sha256:')}",
                )
                self.assertLessEqual(len(index), maximum_size)
                self.assertEqual(accept, MATERIALIZER.OCI_INDEX_MEDIA_TYPE)
                destination.write_bytes(index)

            def write_object(
                repository,
                digest,
                destination,
                maximum_size,
                environment,
                *,
                kind,
                expected_size=None,
            ):
                self.assertEqual(repository, profile.site_repository)
                expected = {
                    manifest_digest: (manifest, "manifest"),
                    bundle_digest: (bundle, "blob"),
                }
                self.assertIn(digest, expected)
                content, expected_kind = expected[digest]
                self.assertEqual(kind, expected_kind)
                self.assertLessEqual(len(content), maximum_size)
                if expected_size is not None:
                    self.assertEqual(len(content), expected_size)
                destination.write_bytes(content)

            with mock.patch.object(
                MATERIALIZER,
                "fetch_registry_named_manifest",
                side_effect=write_index,
            ), mock.patch.object(
                MATERIALIZER,
                "fetch_registry_object",
                side_effect=write_object,
            ):
                combined = MATERIALIZER.fetch_attestation_bundles_bounded(
                    reference,
                    root,
                    MATERIALIZER.safe_environment(root),
                )

            self.assertEqual(combined.read_bytes(), bundle + b"\n")
            manifest_value = json.loads(manifest)
            manifest_value["subject"]["mediaType"] = MATERIALIZER.OCI_INDEX_MEDIA_TYPE
            manifest = producer_json(manifest_value)
            manifest_digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
            index_value = json.loads(index)
            index_value["manifests"][0]["digest"] = manifest_digest
            index_value["manifests"][0]["size"] = len(manifest)
            index = producer_json(index_value)
            index_root = root / "index-subject"
            index_root.mkdir()
            with mock.patch.object(
                MATERIALIZER,
                "fetch_registry_named_manifest",
                side_effect=write_index,
            ), mock.patch.object(
                MATERIALIZER,
                "fetch_registry_object",
                side_effect=write_object,
            ):
                index_combined = MATERIALIZER.fetch_attestation_bundles_bounded(
                    reference,
                    index_root,
                    MATERIALIZER.safe_environment(index_root),
                    subject_media_type=MATERIALIZER.OCI_INDEX_MEDIA_TYPE,
                )
            self.assertEqual(index_combined.read_bytes(), bundle + b"\n")
            mismatch_root = root / "mismatch-subject"
            mismatch_root.mkdir()
            with mock.patch.object(
                MATERIALIZER,
                "fetch_registry_named_manifest",
                side_effect=write_index,
            ), mock.patch.object(
                MATERIALIZER,
                "fetch_registry_object",
                side_effect=write_object,
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "subject is invalid",
                ):
                    MATERIALIZER.fetch_attestation_bundles_bounded(
                        reference,
                        mismatch_root,
                        MATERIALIZER.safe_environment(mismatch_root),
                    )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "must be UTF-8 JSON",
            ):
                MATERIALIZER.strict_json_bytes(
                    b'{"note":"\xff"}',
                    "malformed attestation fixture",
                    canonical=False,
                    allow_utf8=True,
                )
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "must be ASCII JSON",
            ):
                MATERIALIZER.strict_json_bytes(
                    bundle,
                    "non-attestation fixture",
                    canonical=False,
                )

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
            trusted_root = root / "trusted-root.jsonl"
            trusted_root.write_bytes(trusted_root_jsonl())
            trusted_root.chmod(0o444)
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
                        trusted_root=trusted_root,
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
                input_root = state_root / ".inputs"
                input_root.mkdir(mode=0o755)
                (input_root / "02.json").touch()
                output = state_root / "registry-object"
                output.write_bytes(content)
                output.chmod(0o444)
                request = Path(command[command.index("--registry-fetch-worker") + 1])
                request_value = json.loads(request.read_text(encoding="ascii"))
                self.assertEqual(request_value, dataclasses.asdict(contract))
                self.assertEqual(kwargs["remapped_inputs"], (request,))
                return MATERIALIZER.IsolatedWorkerState(
                    unit=MATERIALIZER.SYSTEMD_WORKER_UNIT,
                    state_name="5" * 32,
                    logical_root=state_root,
                    physical_root=state_root,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                    remapped_input_names=("02.json",),
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
                input_root = state_root / ".inputs"
                input_root.mkdir(mode=0o755)
                (input_root / "02.json").touch()
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
                    remapped_input_names=("02.json",),
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
            self.assertEqual(kwargs["trusted_root"], trusted_root)

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
            trusted_root = temporary / "trusted-root.jsonl"
            trusted_root.write_bytes(trusted_root_jsonl())
            trusted_root.chmod(0o444)
            with mock.patch.object(MATERIALIZER, "fetch_manifest", side_effect=manifest), \
                mock.patch.object(
                    MATERIALIZER,
                    "validate_manifests_isolated",
                    side_effect=validate,
                ), mock.patch.object(
                    MATERIALIZER,
                    "verify_github_provenance_isolated",
                    side_effect=provenance,
                ), mock.patch.object(
                    MATERIALIZER,
                    "fetch_github_trusted_root_isolated",
                    return_value=trusted_root,
                ) as root_fetch, mock.patch.object(
                    MATERIALIZER,
                    "unlink_trusted_file",
                ) as root_cleanup, mock.patch.object(
                    MATERIALIZER,
                    "fetch_blob",
                    side_effect=blob,
                ):
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
                root_fetch.assert_called_once_with(temporary / "downloads")
                root_cleanup.assert_called_once_with(trusted_root)

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

    def test_live_integration_and_caddy_are_bound_to_the_reviewed_contract(self) -> None:
        (
            revision,
            integration,
            caddy,
            enabled_applications,
        ) = MATERIALIZER.read_static_production_pins(
            ROOT / "releases/static-production.json",
            require_root_owner=False,
        )
        value = json.loads(
            (ROOT / "releases/static-production.json").read_text(encoding="utf-8")
        )
        self.assertEqual(revision, value["integration"]["source_revision"])
        self.assertEqual(integration, value["integration"]["artifact"])
        self.assertEqual(caddy, value["caddy_image"])
        self.assertEqual(enabled_applications, frozenset({"personal", "parkventory"}))
        self.assertEqual(
            caddy,
            MATERIALIZER.read_promoted_caddy_image(
                ROOT / "platform/.env.example",
                require_root_owner=False,
            ),
        )

        value["integration"]["artifact"] = "ghcr.io/example/integration@sha256:" + "1" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "static-production.json"
            path.write_text(json.dumps(value), encoding="ascii")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "must use repository",
            ):
                MATERIALIZER.read_static_production_pins(
                    path,
                    require_root_owner=False,
                )

    def test_static_and_application_release_modes_are_mutually_exclusive(self) -> None:
        enablement = MATERIALIZER.read_application_production_enablement(
            ROOT / "releases/production.yaml",
            require_root_owner=False,
        )
        self.assertEqual(
            enablement,
            {application: False for application in MATERIALIZER.PROFILES},
        )
        MATERIALIZER.assert_static_activation_mode(
            "parkventory",
            frozenset(MATERIALIZER.PROFILES),
            enablement,
            enablement,
        )
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "disabled by its promotion contract",
        ):
            MATERIALIZER.assert_static_activation_mode(
                "parkventory",
                frozenset({"personal", "papersempire"}),
                enablement,
                enablement,
            )
        conflicting = dict(enablement)
        conflicting["parkventory"] = True
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "conflicts with its enabled application release",
        ):
            MATERIALIZER.assert_static_activation_mode(
                "parkventory",
                frozenset(MATERIALIZER.PROFILES),
                conflicting,
                enablement,
            )
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "conflicts with its active application state",
        ):
            MATERIALIZER.assert_static_activation_mode(
                "parkventory",
                frozenset(MATERIALIZER.PROFILES),
                enablement,
                conflicting,
            )

    def test_parkventory_static_refuses_compose_state_or_current_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            active_root = root / "active"
            transaction_root = root / "transactions"
            application_root = root / "applications"
            active_root.mkdir()
            transaction_root.mkdir()
            (application_root / "parkventory").mkdir(parents=True)
            with (
                mock.patch.object(
                    MATERIALIZER,
                    "COMPOSE_APPLICATION_ACTIVE_ROOT",
                    active_root,
                ),
                mock.patch.object(
                    MATERIALIZER,
                    "COMPOSE_APPLICATION_ROOT",
                    application_root,
                ),
                mock.patch.object(
                    MATERIALIZER,
                    "COMPOSE_APPLICATION_TRANSACTION_ROOT",
                    transaction_root,
                ),
            ):
                MATERIALIZER.refuse_compose_application_owner("parkventory")
                (active_root / "parkventory.json").write_text(
                    "{}\n",
                    encoding="ascii",
                )
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "conflicts with its Compose",
                ):
                    MATERIALIZER.refuse_compose_application_owner("parkventory")
                (active_root / "parkventory.json").unlink()
                (transaction_root / "parkventory.json").write_text(
                    "{}\n",
                    encoding="ascii",
                )
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "conflicts with its Compose",
                ):
                    MATERIALIZER.refuse_compose_application_owner("parkventory")
                (transaction_root / "parkventory.json").unlink()
                (application_root / "parkventory/current").symlink_to(
                    "releases/sha256-" + "a" * 64
                )
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "conflicts with its Compose",
                ):
                    MATERIALIZER.refuse_compose_application_owner("parkventory")

    def test_active_application_state_also_blocks_static_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "manifest.json"
            state_path = root / "state.json"
            self.assertEqual(
                MATERIALIZER.read_active_application_enablement(
                    manifest_path,
                    state_path,
                    require_root_owner=False,
                ),
                {application: False for application in MATERIALIZER.PROFILES},
            )
            manifest_path.write_bytes(b"{}\n")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "state pair is incomplete",
            ):
                MATERIALIZER.read_active_application_enablement(
                    manifest_path,
                    state_path,
                    require_root_owner=False,
                )

            manifest = json.loads(
                (ROOT / "releases/production.yaml").read_text(encoding="utf-8")
            )
            manifest["applications"]["parkventory"]["enabled"] = True
            manifest_raw = canonical_json(manifest)
            manifest_path.write_bytes(manifest_raw)
            state_path.write_bytes(
                canonical_json(
                    {
                        "commit": "a" * 40,
                        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                        "recorded_at": "2026-08-17T18:00:00Z",
                        "schema": 1,
                    }
                )
            )
            enablement = MATERIALIZER.read_active_application_enablement(
                manifest_path,
                state_path,
                require_root_owner=False,
            )
            self.assertTrue(enablement["parkventory"])

            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["manifest_sha256"] = "0" * 64
            state_path.write_bytes(canonical_json(state))
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "does not match its metadata digest",
            ):
                MATERIALIZER.read_active_application_enablement(
                    manifest_path,
                    state_path,
                    require_root_owner=False,
                )

    def test_static_contract_binds_the_temporary_parkventory_demo_mode(self) -> None:
        value = json.loads(
            (ROOT / "releases/static-production.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "static-production.json"
            value["applications"]["parkventory"]["enabled"] = False
            path.write_text(json.dumps(value), encoding="ascii")
            *_, enabled_applications = MATERIALIZER.read_static_production_pins(
                path,
                require_root_owner=False,
            )
            self.assertNotIn("parkventory", enabled_applications)

            value["applications"]["parkventory"]["mode"] = "static-site"
            path.write_text(json.dumps(value), encoding="ascii")
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "parkventory mode is invalid",
            ):
                MATERIALIZER.read_static_production_pins(
                    path,
                    require_root_owner=False,
                )

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

    def test_parkventory_static_profile_is_digest_bound_and_demo_only(self) -> None:
        profile = MATERIALIZER.PROFILES["parkventory"]
        self.assertEqual(profile.source_repository, "nclsppr/parkventory")
        self.assertEqual(profile.source_ref, "refs/heads/main")
        self.assertEqual(
            profile.signer_workflow,
            "nclsppr/parkventory/.github/workflows/vps-release.yml",
        )
        self.assertEqual(profile.canonical_domain, "parkventory.com")
        self.assertEqual(profile.redirect_domains, ("www.parkventory.com",))
        self.assertEqual(
            profile.live_redirects,
            (
                MATERIALIZER.RedirectContract(
                    "www.parkventory.com",
                    expected_hsts=True,
                ),
            ),
        )
        self.assertEqual(
            profile.site_repository,
            "ghcr.io/nclsppr/parkventory-static-site",
        )
        self.assertEqual(
            profile.routes_repository,
            "ghcr.io/nclsppr/parkventory-static-routes",
        )
        route = (
            ROOT / "platform/caddy/routes/parkventory.caddy.disabled"
        ).read_text(encoding="utf-8")
        self.assertIn("root * /srv/www/parkventory/current", route)
        self.assertNotIn("reverse_proxy", route)
        self.assertNotIn("/api", route)

    def test_live_redirect_contract_matches_the_public_edge(self) -> None:
        self.assertEqual(
            MATERIALIZER.PROFILES["personal"].redirect_domains,
            ("www.nicolaspieper.com", "nicolas.pieper.fr"),
        )
        personal_live_redirects = MATERIALIZER.PROFILES["personal"].live_redirects
        self.assertEqual(
            personal_live_redirects,
            (
                MATERIALIZER.RedirectContract(
                    "www.nicolaspieper.com",
                    expected_hsts=False,
                ),
                MATERIALIZER.RedirectContract("pieper.fr", expected_hsts=False),
                MATERIALIZER.RedirectContract(
                    "www.pieper.fr",
                    expected_hsts=False,
                ),
                MATERIALIZER.RedirectContract(
                    "nicolas.pieper.fr",
                    expected_hsts=False,
                ),
            ),
        )
        self.assertEqual(MATERIALIZER.PROFILES["papersempire"].redirect_domains, ())
        self.assertEqual(
            MATERIALIZER.PROFILES["papersempire"].live_redirects,
            (
                MATERIALIZER.RedirectContract(
                    "www.papersempire.com",
                    expected_hsts=True,
                ),
            ),
        )
        self.assertEqual(
            MATERIALIZER.PROFILES["parkventory"].live_redirects,
            (
                MATERIALIZER.RedirectContract(
                    "www.parkventory.com",
                    expected_hsts=True,
                ),
            ),
        )
        edge_defaults = yaml.safe_load(
            (
                ROOT / "ansible/roles/public_static_edge/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        edge_redirects = edge_defaults["vps_public_static_edge_redirects"]
        for application, profile in MATERIALIZER.PROFILES.items():
            expected_live_domains = tuple(
                redirect["source"]
                for redirect in edge_redirects
                if redirect["target"] == profile.canonical_domain
            )
            self.assertEqual(
                tuple(redirect.domain for redirect in profile.live_redirects),
                expected_live_domains,
            )
            route = (
                ROOT
                / "platform/public-static-edge/routes-activate"
                / f"{application}.caddy"
            ).read_text(encoding="utf-8")
            self.assertIn(profile.canonical_domain, route)
            for redirect in profile.live_redirects:
                self.assertIn(redirect.domain, route)
        integration_route = (
            ROOT / "platform/caddy/routes/personal.caddy.disabled"
        ).read_text(encoding="utf-8")
        for domain in MATERIALIZER.PROFILES["personal"].redirect_domains:
            self.assertIn(domain, integration_route)
        self.assertNotIn("PERSONAL_LEGACY_APEX_DOMAIN", integration_route)
        self.assertNotIn("PERSONAL_LEGACY_WWW_DOMAIN", integration_route)
        self.assertNotIn("PERSONAL_LEGACY_NESTED_WWW_DOMAIN", integration_route)

    def test_live_probe_requires_valid_tls_and_the_public_redirect_set(self) -> None:
        profile = MATERIALIZER.PROFILES["personal"]
        inventory = probe_inventory()
        for live_probe in (
            MATERIALIZER.probe_live_release,
            MATERIALIZER.probe_live_health,
        ):
            with self.subTest(live_probe=live_probe.__name__), mock.patch.object(
                MATERIALIZER,
                "wait_for_probe",
            ) as wait, mock.patch.object(
                MATERIALIZER,
                "assert_release_http_contract",
            ) as contract:
                live_probe(
                    inventory,
                    profile,
                    Path("/var/tmp/test"),
                    30,
                )
                self.assertFalse(wait.call_args.kwargs["insecure"])
                self.assertFalse(contract.call_args.kwargs["insecure"])
                self.assertEqual(
                    contract.call_args.kwargs["redirect_contracts"],
                    profile.live_redirects,
                )
        with mock.patch.object(
            MATERIALIZER.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            MATERIALIZER.wait_for_probe(
                profile.canonical_domain,
                443,
                Path("/var/tmp/test"),
                MATERIALIZER.time.monotonic() + 5,
                insecure=False,
            )
        self.assertEqual(run.call_args.args[0][1], "--disable")

    def test_redirect_probes_preserve_each_hsts_contract(self) -> None:
        inventory = probe_inventory()
        for profile in MATERIALIZER.PROFILES.values():
            with self.subTest(application=profile.application, phase="temporary"), \
                mock.patch.object(MATERIALIZER, "assert_probe_response") as probe:
                MATERIALIZER.assert_release_http_contract(
                    profile,
                    inventory,
                    443,
                    Path("/var/tmp/test"),
                    MATERIALIZER.time.monotonic() + 30,
                    insecure=False,
                )
                temporary_redirects = [
                    call
                    for call in probe.call_args_list
                    if call.kwargs["expected_status"] == 308
                ]
                self.assertEqual(
                    [
                        (call.args[0], call.kwargs["expected_hsts"])
                        for call in temporary_redirects
                    ],
                    [(domain, True) for domain in profile.redirect_domains],
                )

            with self.subTest(application=profile.application, phase="live"), \
                mock.patch.object(MATERIALIZER, "assert_probe_response") as probe:
                MATERIALIZER.assert_release_http_contract(
                    profile,
                    inventory,
                    443,
                    Path("/var/tmp/test"),
                    MATERIALIZER.time.monotonic() + 30,
                    insecure=False,
                    redirect_contracts=profile.live_redirects,
                )
                live_redirects = [
                    call
                    for call in probe.call_args_list
                    if call.kwargs["expected_status"] == 308
                ]
                self.assertEqual(
                    [
                        (call.args[0], call.kwargs["expected_hsts"])
                        for call in live_redirects
                    ],
                    [
                        (redirect.domain, redirect.expected_hsts)
                        for redirect in profile.live_redirects
                    ],
                )

    def test_live_readiness_rejects_a_self_signed_certificate(self) -> None:
        openssl = shutil.which("openssl")
        curl = shutil.which("curl")
        if openssl is None or curl is None:
            self.skipTest("OpenSSL or curl is unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            configuration = root / "certificate.cnf"
            certificate = root / "certificate.pem"
            private_key_path = root / "private-key.pem"
            configuration.write_text(
                "[req]\n"
                "distinguished_name=dn\n"
                "x509_extensions=ext\n"
                "prompt=no\n"
                "[dn]\n"
                "CN=localhost\n"
                "[ext]\n"
                "subjectAltName=DNS:localhost\n",
                encoding="ascii",
            )
            generated = subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-keyout",
                    private_key_path,
                    "-out",
                    certificate,
                    "-config",
                    configuration,
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if generated.returncode != 0:
                self.skipTest("OpenSSL cannot generate a test certificate")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            server = subprocess.Popen(
                [
                    openssl,
                    "s_server",
                    "-quiet",
                    "-accept",
                    str(port),
                    "-cert",
                    certificate,
                    "-key",
                    private_key_path,
                    "-www",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                with mock.patch.object(MATERIALIZER, "CURL_PATH", Path(curl)):
                    MATERIALIZER.wait_for_probe(
                        "localhost",
                        port,
                        root,
                        MATERIALIZER.time.monotonic() + 5,
                        insecure=True,
                    )
                    with self.assertRaises(MATERIALIZER.StaticDeploymentError):
                        MATERIALIZER.wait_for_probe(
                            "localhost",
                            port,
                            root,
                            MATERIALIZER.time.monotonic() + 1,
                            insecure=False,
                        )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

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
        curl_commands: list[list[str]] = []
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
            curl_commands.append(command)
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
                MATERIALIZER.assert_probe_response(
                    "example.com",
                    443,
                    "/",
                    root,
                    MATERIALIZER.time.monotonic() + 10,
                    expected_status=200,
                    expected_sha256=hashlib.sha256(body).hexdigest(),
                    expected_cache_control="no-cache",
                    insecure=False,
                )
            self.assertEqual(list(root.iterdir()), [])
        self.assertIn("--insecure", curl_commands[0])
        self.assertNotIn("--insecure", curl_commands[1])
        self.assertTrue(all(command[1] == "--disable" for command in curl_commands))

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

    def test_probe_response_enforces_hsts_presence_and_absence(self) -> None:
        common_headers = (
            "x-content-type-options: nosniff\r\n"
            "x-frame-options: SAMEORIGIN\r\n"
            "referrer-policy: strict-origin-when-cross-origin\r\n"
            "permissions-policy: camera=(), microphone=(), geolocation=()\r\n"
            "location: https://example.com/probe?value=1\r\n"
        )
        hsts_header = (
            "strict-transport-security: max-age=31536000; includeSubDomains\r\n"
        )

        def probe_response(headers: str, *, expected_hsts: bool) -> None:
            def respond(command, *, environment, timeout=120):
                response_path = Path(command[command.index("--output") + 1])
                header_path = Path(command[command.index("--dump-header") + 1])
                response_path.write_bytes(b"")
                header_path.write_text(
                    f"HTTP/2 308\r\n{headers}\r\n",
                    encoding="latin-1",
                )
                return subprocess.CompletedProcess(command, 0, "308", "")

            with tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                with mock.patch.object(
                    MATERIALIZER,
                    "run_checked",
                    side_effect=respond,
                ):
                    MATERIALIZER.assert_probe_response(
                        "www.example.com",
                        443,
                        "/probe?value=1",
                        root,
                        MATERIALIZER.time.monotonic() + 10,
                        expected_status=308,
                        expected_location="https://example.com/probe?value=1",
                        expected_hsts=expected_hsts,
                        insecure=False,
                    )

        probe_response(hsts_header + common_headers, expected_hsts=True)
        probe_response(common_headers, expected_hsts=False)
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "omitted the required HSTS policy",
        ):
            probe_response(common_headers, expected_hsts=True)
        with self.assertRaisesRegex(
            MATERIALIZER.StaticDeploymentError,
            "returned an unexpected HSTS policy",
        ):
            probe_response(hsts_header + common_headers, expected_hsts=False)

    def test_residual_probe_cleanup_is_batched_bounded_and_exact(self) -> None:
        identifier = "a" * 64
        record = (
            f"{identifier}\tvps-static-probe-{'b' * 16}\ttrue\tpersonal\n"
        )
        listed = subprocess.CompletedProcess([], 0, record, "")
        empty = subprocess.CompletedProcess([], 0, "", "")
        removed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            MATERIALIZER,
            "run_checked",
            side_effect=[listed, empty],
        ) as run_checked, mock.patch.object(
            MATERIALIZER.subprocess,
            "run",
            return_value=removed,
        ) as run:
            MATERIALIZER.cleanup_probe_containers("personal")
        self.assertEqual(run_checked.call_count, 2)
        for call in run_checked.call_args_list:
            self.assertEqual(
                call.kwargs["timeout"],
                MATERIALIZER.PROBE_CONTAINER_LIST_TIMEOUT_SECONDS,
            )
            self.assertEqual(call.kwargs["environment"]["HOME"], "/nonexistent")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [str(MATERIALIZER.DOCKER_PATH), "rm", "--force", identifier],
        )
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            MATERIALIZER.PROBE_CONTAINER_REMOVE_TIMEOUT_SECONDS,
        )

        malformed = subprocess.CompletedProcess(
            [],
            0,
            f"{identifier}\tother-container\ttrue\tpersonal\n",
            "",
        )
        with mock.patch.object(MATERIALIZER, "run_checked", return_value=malformed):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "unexpected container identity",
            ):
                MATERIALIZER.cleanup_probe_containers("personal")

    def test_static_residue_cleanup_removes_only_exact_protected_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            production_root = Path(temporary_directory)
            app_root = production_root / "personal"
            releases = app_root / "releases"
            releases.mkdir(parents=True, mode=0o755)
            staging = releases / f".sha256-{'a' * 64}-{'b' * 16}"
            staging.mkdir(mode=0o700)
            (staging / "partial").write_text("partial\n", encoding="utf-8")
            temporary_link = app_root / f".current-{'c' * 16}"
            temporary_link.symlink_to(f"releases/sha256-{'d' * 64}")
            with mock.patch.object(
                MATERIALIZER,
                "PRODUCTION_ROOT",
                production_root,
            ):
                MATERIALIZER.cleanup_static_filesystem_residue(
                    "personal",
                    app_root,
                    releases,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                )
            self.assertFalse(staging.exists())
            self.assertFalse(temporary_link.is_symlink())

            outside = production_root / "outside"
            outside.mkdir(mode=0o700)
            hostile = releases / f".sha256-{'e' * 64}-{'f' * 16}"
            hostile.symlink_to(outside, target_is_directory=True)
            with mock.patch.object(
                MATERIALIZER,
                "PRODUCTION_ROOT",
                production_root,
            ):
                with self.assertRaisesRegex(
                    MATERIALIZER.StaticDeploymentError,
                    "not one protected directory",
                ):
                    MATERIALIZER.cleanup_static_filesystem_residue(
                        "personal",
                        app_root,
                        releases,
                        expected_uid=os.geteuid(),
                        expected_gid=os.getegid(),
                    )
            self.assertTrue(outside.is_dir())

    def test_live_runtime_directory_contract_is_exact_and_protected(self) -> None:
        runtime = f"/run/vps-static-live-personal-{'a' * 24}"
        protected = mock.Mock(
            st_mode=MATERIALIZER.stat.S_IFDIR | 0o700,
            st_uid=0,
            st_gid=0,
        )
        with mock.patch.dict(
            MATERIALIZER.os.environ,
            {MATERIALIZER.STATIC_RUNTIME_DIRECTORY_ENV: runtime},
            clear=True,
        ), mock.patch.object(MATERIALIZER.Path, "lstat", return_value=protected):
            self.assertEqual(
                MATERIALIZER.deployment_temporary_root("personal", True),
                Path(runtime),
            )
        with mock.patch.dict(
            MATERIALIZER.os.environ,
            {MATERIALIZER.STATIC_RUNTIME_DIRECTORY_ENV: runtime},
            clear=True,
        ):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "reserved for live activation",
            ):
                MATERIALIZER.deployment_temporary_root("personal", False)
        with mock.patch.dict(
            MATERIALIZER.os.environ,
            {MATERIALIZER.STATIC_RUNTIME_DIRECTORY_ENV: runtime + "x"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                MATERIALIZER.StaticDeploymentError,
                "exact transient runtime directory",
            ):
                MATERIALIZER.deployment_temporary_root("personal", True)

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
            source.index("activate_live_release(", staging_position),
        )
        self.assertNotIn('activate_release(app_root / "current"', source)
        self.assertIn(
            "the current link was left unchanged and public TLS was not tested",
            source,
        )
        self.assertIn(
            'release_name = f"sha256-{site_layer.manifest_digest.removeprefix(\'sha256:\')}"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
