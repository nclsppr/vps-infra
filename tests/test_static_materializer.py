#!/usr/bin/env python3

from __future__ import annotations

import copy
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
import unittest
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
            raw = canonical_json(manifest)
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

            invalid = copy.deepcopy(manifest)
            invalid["annotations"][MATERIALIZER.SOURCE_ANNOTATION] = "https://github.com/example/wrong"
            invalid_raw = canonical_json(invalid)
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
            MATERIALIZER.activate_release(current, new_release.name, releases)
            self.assertTrue(current.is_symlink())
            self.assertEqual(os.readlink(current), f"releases/{new_release.name}")

            current.unlink()
            current.write_text("unsafe", encoding="ascii")
            with self.assertRaisesRegex(MATERIALIZER.StaticDeploymentError, "not a symlink"):
                MATERIALIZER.activate_release(current, old_release.name, releases)


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

    def test_ansible_pins_and_verifies_oras_and_installs_materializer(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deploy/defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(defaults["vps_oras_version"], "1.3.0")
        self.assertEqual(defaults["vps_oras_install_path"], "/usr/local/bin/oras")
        self.assertEqual(set(defaults["vps_oras_archives"]), {"x86_64", "aarch64"})
        for definition in defaults["vps_oras_archives"].values():
            self.assertRegex(definition["archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(definition["binary_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            MATERIALIZER.ORAS_BINARY_SHA256,
            {
                architecture: definition["binary_sha256"]
                for architecture, definition in defaults["vps_oras_archives"].items()
            },
        )
        self.assertIn("deploy-static", defaults["vps_deploy_executables"])
        tasks = (ROOT / "ansible/roles/deploy/tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("checksum: \"sha256:{{ vps_oras_archive.archive_sha256 }}\"", tasks)
        self.assertIn("vps_oras_extracted.stat.checksum == vps_oras_archive.binary_sha256", tasks)
        self.assertIn("Extract only the ORAS executable", tasks)

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

    def test_static_release_roots_and_materializer_are_protected(self) -> None:
        layout = (ROOT / "ansible/roles/layout/tasks/main.yml").read_text(encoding="utf-8")
        release_task = layout.split("- name: Create protected per-site release roots", 1)[1]
        release_task = release_task.split("- name: Inspect managed external Docker networks", 1)[0]
        self.assertIn("owner: root", release_task)
        self.assertIn("group: root", release_task)
        self.assertNotIn('owner: "{{ vps_static_user }}"', release_task)
        static_user_task = layout.split("- name: Create the non-login static release account", 1)[1]
        static_user_task = static_user_task.split("- name: Create stable platform directories", 1)[0]
        self.assertIn('groups: ""', static_user_task)
        self.assertIn("append: false", static_user_task)
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
