#!/usr/bin/env python3

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REVISION = "0123456789abcdef0123456789abcdef01234567"
CREATED = "2026-08-17T20:00:00Z"


def load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUNDLE = load_module(
    "application_bundle_test_subject",
    ROOT / "scripts/lib/application_bundle.py",
)


def components(profile):
    return {
        name: f"{repository}@sha256:{index + 1:064x}"
        for index, (name, repository) in enumerate(
            profile.component_repositories.items()
        )
    }


def fixture_files(profile):
    component_references = components(profile)
    probes = BUNDLE._expected_probes(profile)
    migrations = {
        "contract": profile.migration_contract,
        "database": profile.application,
        "migrations": [
            {
                "path": "backend/src/main/resources/db/migration/V1__baseline.sql",
                "sha256": "a" * 64,
                "version": 1,
            }
        ],
        "runtime_auto_migrate": False,
        "schema": 1,
        "source_repository": profile.source_repository,
        "source_revision": REVISION,
    }
    if profile.application == "surplasse":
        migrations["runner"] = profile.migration_runner
    raw: dict[str, bytes] = {}
    for path in profile.runtime_paths:
        raw[path] = f"fixture for {path}\n".encode()
    raw["compose.yaml"] = (
        f"---\nname: {profile.application}\nservices:\n  backend:\n"
        f"    image: ${{{profile.application.upper()}_BACKEND_IMAGE}}\n"
    ).encode()
    raw["contract.json"] = BUNDLE.canonical_json(
        BUNDLE._expected_contract(profile, REVISION)
    )
    raw["migrations.json"] = BUNDLE.canonical_json(migrations)
    raw["probes.json"] = BUNDLE.canonical_json(probes)
    if profile.application == "surplasse":
        raw["expected-images.json"] = BUNDLE.canonical_json(
            {
                "images": {
                    **component_references,
                    "migrator": component_references["backend"],
                },
                "schema": 1,
                "source_revision": REVISION,
            }
        )
    return raw, component_references


def build_archive(profile, files, *, special: str | None = None):
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for directory in profile.archive_directories:
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 0
            info.mtime = 0
            archive.addfile(info)
        for path in profile.runtime_paths:
            content = files[path]
            info = tarfile.TarInfo(f"integration/{path}")
            info.mode = 0o644
            info.uid = info.gid = 0
            info.mtime = 0
            if special == path:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                info.size = 0
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=compressed, mtime=0
    ) as output:
        output.write(tar_buffer.getvalue())
    return compressed.getvalue()


def build_inventory(profile, files):
    return BUNDLE.canonical_json(
        {
            "contract": "vps-infra.application-integration.v1",
            "files": [
                {
                    "bytes": len(files[path]),
                    "path": path,
                    "sha256": hashlib.sha256(files[path]).hexdigest(),
                }
                for path in profile.runtime_paths
            ],
            "schema": 1,
            "source": {
                "repository": profile.source_repository,
                "revision": REVISION,
            },
        }
    )


def build_manifest(profile, archive, inventory):
    value = {
        "annotations": {
            BUNDLE.CREATED_ANNOTATION: CREATED,
            BUNDLE.REVISION_ANNOTATION: REVISION,
            BUNDLE.SOURCE_ANNOTATION: (
                f"https://github.com/{profile.source_repository}"
            ),
        },
        "artifactType": BUNDLE.INTEGRATION_ARTIFACT_TYPE,
        "config": BUNDLE.OCI_EMPTY_CONFIG,
        "layers": [
            {
                "annotations": {BUNDLE.TITLE_ANNOTATION: BUNDLE.ARCHIVE_TITLE},
                "digest": BUNDLE.content_digest(archive),
                "mediaType": BUNDLE.INTEGRATION_ARCHIVE_MEDIA_TYPE,
                "size": len(archive),
            },
            {
                "annotations": {BUNDLE.TITLE_ANNOTATION: BUNDLE.INVENTORY_TITLE},
                "digest": BUNDLE.content_digest(inventory),
                "mediaType": BUNDLE.INTEGRATION_INVENTORY_MEDIA_TYPE,
                "size": len(inventory),
            },
        ],
        "mediaType": BUNDLE.OCI_MANIFEST_MEDIA_TYPE,
        "schemaVersion": 2,
    }
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class ApplicationBundleTests(unittest.TestCase):
    def test_shared_outer_contract_accepts_both_exact_application_profiles(self):
        for profile in BUNDLE.PROFILES.values():
            with self.subTest(application=profile.application):
                files, component_references = fixture_files(profile)
                archive = build_archive(profile, files)
                inventory = build_inventory(profile, files)
                manifest = build_manifest(profile, archive, inventory)
                parsed_manifest = BUNDLE.validate_integration_manifest(
                    manifest,
                    profile,
                    REVISION,
                    BUNDLE.content_digest(manifest),
                )
                parsed = BUNDLE.validate_bundle(
                    archive,
                    inventory,
                    profile=profile,
                    revision=REVISION,
                    created=parsed_manifest.created,
                    component_references=component_references,
                    migration_inventory_digest=BUNDLE.content_digest(
                        files["migrations.json"]
                    ),
                    probe_inventory_digest=BUNDLE.content_digest(
                        files["probes.json"]
                    ),
                )
                self.assertEqual(parsed.application, profile.application)
                self.assertEqual(tuple(sorted(parsed.files)), profile.runtime_paths)

    def test_release_inventory_hashes_bind_exact_canonical_bytes(self):
        profile = BUNDLE.PROFILES["parkventory"]
        files, component_references = fixture_files(profile)
        archive = build_archive(profile, files)
        inventory = build_inventory(profile, files)
        with self.assertRaisesRegex(
            BUNDLE.ApplicationBundleError,
            "digest does not match the application release",
        ):
            BUNDLE.validate_bundle(
                archive,
                inventory,
                profile=profile,
                revision=REVISION,
                created=CREATED,
                component_references=component_references,
                migration_inventory_digest="sha256:" + "f" * 64,
                probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
            )

    def test_archive_rejects_links_even_when_inventory_names_are_valid(self):
        profile = BUNDLE.PROFILES["surplasse"]
        files, component_references = fixture_files(profile)
        archive = build_archive(profile, files, special="compose.yaml")
        inventory = build_inventory(profile, files)
        with self.assertRaisesRegex(BUNDLE.ApplicationBundleError, "links"):
            BUNDLE.validate_bundle(
                archive,
                inventory,
                profile=profile,
                revision=REVISION,
                created=CREATED,
                component_references=component_references,
                migration_inventory_digest=BUNDLE.content_digest(
                    files["migrations.json"]
                ),
                probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
            )

    def test_runtime_auto_migration_cannot_be_enabled(self):
        profile = BUNDLE.PROFILES["parkventory"]
        files, component_references = fixture_files(profile)
        migrations = json.loads(files["migrations.json"])
        migrations["runtime_auto_migrate"] = True
        files["migrations.json"] = BUNDLE.canonical_json(migrations)
        archive = build_archive(profile, files)
        inventory = build_inventory(profile, files)
        with self.assertRaisesRegex(BUNDLE.ApplicationBundleError, "must equal False"):
            BUNDLE.validate_bundle(
                archive,
                inventory,
                profile=profile,
                revision=REVISION,
                created=CREATED,
                component_references=component_references,
                migration_inventory_digest=BUNDLE.content_digest(
                    files["migrations.json"]
                ),
                probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
            )

    def test_probe_inventory_cannot_weaken_the_exact_health_contract(self):
        profile = BUNDLE.PROFILES["parkventory"]
        files, component_references = fixture_files(profile)
        probes = json.loads(files["probes.json"])
        probes["public"] = [{"path": "/", "status": 200}]
        files["probes.json"] = BUNDLE.canonical_json(probes)
        archive = build_archive(profile, files)
        inventory = build_inventory(profile, files)
        with self.assertRaisesRegex(
            BUNDLE.ApplicationBundleError,
            "exact application profile",
        ):
            BUNDLE.validate_bundle(
                archive,
                inventory,
                profile=profile,
                revision=REVISION,
                created=CREATED,
                component_references=component_references,
                migration_inventory_digest=BUNDLE.content_digest(
                    files["migrations.json"]
                ),
                probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
            )

    def test_surplasse_bundle_rejects_every_tester_payment_profile_divergence(self):
        profile = BUNDLE.PROFILES["surplasse"]
        invalid_profiles = {
            "missing": None,
            "live": {"audience": "testers", "mode": "live", "schema": 1},
            "public": {"audience": "public", "mode": "test", "schema": 1},
            "schema": {"audience": "testers", "mode": "test", "schema": 2},
            "boolean-schema": {
                "audience": "testers",
                "mode": "test",
                "schema": True,
            },
            "extra": {
                "audience": "testers",
                "mode": "test",
                "schema": 1,
                "operator_override": True,
            },
        }
        for label, payment in invalid_profiles.items():
            with self.subTest(divergence=label):
                files, component_references = fixture_files(profile)
                contract = json.loads(files["contract.json"])
                if payment is None:
                    contract.pop("payment")
                else:
                    contract["payment"] = payment
                files["contract.json"] = BUNDLE.canonical_json(contract)
                archive = build_archive(profile, files)
                inventory = build_inventory(profile, files)
                with self.assertRaisesRegex(
                    BUNDLE.ApplicationBundleError,
                    "exact canonical profile",
                ):
                    BUNDLE.validate_bundle(
                        archive,
                        inventory,
                        profile=profile,
                        revision=REVISION,
                        created=CREATED,
                        component_references=component_references,
                        migration_inventory_digest=BUNDLE.content_digest(
                            files["migrations.json"]
                        ),
                        probe_inventory_digest=BUNDLE.content_digest(
                            files["probes.json"]
                        ),
                    )

    def test_surplasse_expected_images_cannot_diverge_from_release(self):
        profile = BUNDLE.PROFILES["surplasse"]
        files, component_references = fixture_files(profile)
        expected = json.loads(files["expected-images.json"])
        expected["images"]["backend"] = (
            "ghcr.io/nclsppr/surplasse/backend@sha256:" + "f" * 64
        )
        files["expected-images.json"] = BUNDLE.canonical_json(expected)
        archive = build_archive(profile, files)
        inventory = build_inventory(profile, files)
        with self.assertRaisesRegex(BUNDLE.ApplicationBundleError, "release components"):
            BUNDLE.validate_bundle(
                archive,
                inventory,
                profile=profile,
                revision=REVISION,
                created=CREATED,
                component_references=component_references,
                migration_inventory_digest=BUNDLE.content_digest(
                    files["migrations.json"]
                ),
                probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
            )

    def test_component_index_and_config_are_revision_and_platform_bound(self):
        profile = BUNDLE.PROFILES["parkventory"]
        runtime_digest = "sha256:" + "b" * 64
        index = {
            "manifests": [
                {
                    "digest": runtime_digest,
                    "mediaType": BUNDLE.OCI_MANIFEST_MEDIA_TYPE,
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": 512,
                },
                {
                    "annotations": {
                        "vnd.docker.reference.digest": runtime_digest,
                        "vnd.docker.reference.type": "attestation-manifest",
                    },
                    "digest": "sha256:" + "c" * 64,
                    "mediaType": BUNDLE.OCI_MANIFEST_MEDIA_TYPE,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "size": 256,
                },
            ],
            "mediaType": BUNDLE.OCI_INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
        raw_index = json.dumps(index, separators=(",", ":"), sort_keys=True).encode()
        parsed = BUNDLE.validate_component_index(
            raw_index,
            profile=profile,
            component="backend",
            revision=REVISION,
            expected_digest=BUNDLE.content_digest(raw_index),
        )
        self.assertEqual(parsed.runtime_manifest.digest, runtime_digest)

        config = {
            "architecture": "amd64",
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": REVISION,
                    "org.opencontainers.image.source": (
                        "https://github.com/nclsppr/parkventory"
                    ),
                    "org.opencontainers.image.version": f"sha-{REVISION}",
                }
            },
            "os": "linux",
        }
        raw_config = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
        BUNDLE.validate_image_config(
            raw_config,
            profile=profile,
            revision=REVISION,
            expected_digest=BUNDLE.content_digest(raw_config),
            expected_size=len(raw_config),
        )
        config["architecture"] = "arm64"
        invalid = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
        with self.assertRaisesRegex(BUNDLE.ApplicationBundleError, "amd64"):
            BUNDLE.validate_image_config(
                invalid,
                profile=profile,
                revision=REVISION,
                expected_digest=BUNDLE.content_digest(invalid),
                expected_size=len(invalid),
            )

    def test_runtime_manifest_bounds_layer_count_and_compressed_total(self):
        config = {
            "digest": "sha256:" + "a" * 64,
            "mediaType": BUNDLE.OCI_CONFIG_MEDIA_TYPE,
            "size": 1,
        }

        def manifest(layers):
            return json.dumps(
                {
                    "config": config,
                    "layers": layers,
                    "mediaType": BUNDLE.OCI_MANIFEST_MEDIA_TYPE,
                    "schemaVersion": 2,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()

        too_many = manifest(
            [
                {
                    "digest": f"sha256:{index + 1:064x}",
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "size": 1,
                }
                for index in range(BUNDLE.MAX_RUNTIME_LAYER_COUNT + 1)
            ]
        )
        with self.assertRaisesRegex(BUNDLE.ApplicationBundleError, "count"):
            BUNDLE.validate_runtime_manifest(
                too_many,
                expected_digest=BUNDLE.content_digest(too_many),
                expected_size=len(too_many),
            )

        oversized_total = manifest(
            [
                {
                    "digest": f"sha256:{index + 1:064x}",
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "size": 2 * 1024 * BUNDLE.MIB,
                }
                for index in range(3)
            ]
        )
        with self.assertRaisesRegex(
            BUNDLE.ApplicationBundleError,
            "total compressed size",
        ):
            BUNDLE.validate_runtime_manifest(
                oversized_total,
                expected_digest=BUNDLE.content_digest(oversized_total),
                expected_size=len(oversized_total),
            )

    def test_materializer_creates_only_allowlisted_read_only_files(self):
        profile = BUNDLE.PROFILES["parkventory"]
        files, component_references = fixture_files(profile)
        bundle = BUNDLE.validate_bundle(
            build_archive(profile, files),
            build_inventory(profile, files),
            profile=profile,
            revision=REVISION,
            created=CREATED,
            component_references=component_references,
            migration_inventory_digest=BUNDLE.content_digest(
                files["migrations.json"]
            ),
            probe_inventory_digest=BUNDLE.content_digest(files["probes.json"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "integration"
            BUNDLE.materialize_files(bundle, destination)
            actual = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual, set(profile.runtime_paths))
            self.assertTrue(
                all((destination / path).stat().st_mode & 0o777 == 0o444 for path in actual)
            )


if __name__ == "__main__":
    unittest.main()
