#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


POLICY = load_module(
    "application_release_policy",
    ROOT / "scripts/lib/application_release.py",
)


def release_value(policy, revision=REVISION):
    integration = f"{policy.integration_repository}@{DIGEST_B}"
    return {
        "schema": 1,
        "contract": POLICY.APPLICATION_RELEASE_CONTRACT,
        "application": policy.name,
        "source": {
            "repository": policy.source_repository,
            "branch": policy.source_branch,
            "revision": revision,
        },
        "components": {
            name: {
                "source_revision": revision,
                "image": f"{repository}@{DIGEST_A}",
            }
            for name, repository in policy.component_repositories.items()
        },
        "integration": {
            "source_revision": revision,
            "artifact": integration,
        },
        "migrations": {
            "strategy": "dedicated",
            "runtime_auto_migrate": False,
            "inventory_artifact": integration,
            "inventory_sha256": DIGEST_C,
        },
        "probes": {
            "inventory_artifact": integration,
            "inventory_sha256": DIGEST_A,
        },
    }


def release_bytes(policy, revision=REVISION):
    return POLICY.canonical_json(release_value(policy, revision))


def manifest_value(policy, descriptor):
    return {
        "schemaVersion": 2,
        "mediaType": POLICY.OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": POLICY.APPLICATION_RELEASE_ARTIFACT_TYPE,
        "config": POLICY.OCI_EMPTY_CONFIG,
        "layers": [
            {
                "mediaType": POLICY.APPLICATION_RELEASE_LAYER_MEDIA_TYPE,
                "digest": POLICY.content_digest(descriptor),
                "size": len(descriptor),
                "annotations": {
                    POLICY.TITLE_ANNOTATION: POLICY.APPLICATION_RELEASE_TITLE
                },
            }
        ],
        "annotations": {
            POLICY.CREATED_ANNOTATION: "2026-08-17T12:00:00Z",
            POLICY.SOURCE_ANNOTATION: (
                f"https://github.com/{policy.source_repository}"
            ),
            POLICY.REVISION_ANNOTATION: REVISION,
        },
    }


class ProductionContractTests(unittest.TestCase):
    def setUp(self):
        self.contract_path = ROOT / "releases/application-production.json"
        self.static_path = ROOT / "releases/static-production.json"

    def test_repository_contract_is_exact_and_both_apps_are_disabled(self):
        contract = POLICY.load_production_contract(
            self.contract_path, self.static_path
        )
        self.assertEqual(
            [(application.name, application.enabled) for application in contract.applications],
            [("surplasse", False), ("parkventory", False)],
        )
        self.assertEqual(
            contract.applications[1].required_checks,
            ("Publish immutable application release", "verify"),
        )

    def test_contract_schema_is_valid_and_accepts_repository_contract(self):
        schema = json.loads(
            (ROOT / "schemas/application-production.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(
            json.loads(self.contract_path.read_text())
        )

    def test_static_and_compose_parkventory_cannot_both_be_enabled(self):
        value = json.loads(self.contract_path.read_text())
        value["applications"]["parkventory"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "application-production.json"
            contract_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                POLICY.ApplicationReleaseError,
                "static and Compose production contracts cannot both be enabled",
            ):
                POLICY.load_production_contract(contract_path, self.static_path)

    def test_disabled_static_parkventory_still_requires_readiness_evidence(self):
        application = json.loads(self.contract_path.read_text())
        application["applications"]["parkventory"]["enabled"] = True
        static = json.loads(self.static_path.read_text())
        static["applications"]["parkventory"]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_path = root / "application-production.json"
            static_path = root / "static-production.json"
            application_path.write_text(json.dumps(application), encoding="utf-8")
            static_path.write_text(json.dumps(static), encoding="utf-8")
            with self.assertRaisesRegex(
                POLICY.ApplicationReleaseError,
                "must bind enabled Parkventory to exact PostgreSQL readiness evidence",
            ):
                POLICY.load_production_contract(application_path, static_path)

            application["applications"]["parkventory"]["readiness_evidence"][
                "postgres"
            ]["sha256"] = "sha256:" + "a" * 64
            application_path.write_text(json.dumps(application), encoding="utf-8")
            with self.assertRaisesRegex(
                POLICY.ApplicationReleaseError,
                "verified encrypted off-site backup evidence",
            ):
                POLICY.load_production_contract(application_path, static_path)

    def test_contract_rejects_unknown_field_and_non_boolean_enablement(self):
        original = json.loads(self.contract_path.read_text())
        cases = (
            (
                "unknown",
                lambda value: value["applications"]["surplasse"].__setitem__(
                    "fallback", True
                ),
                "unknown keys",
            ),
            (
                "integer-enabled",
                lambda value: value["applications"]["surplasse"].__setitem__(
                    "enabled", 0
                ),
                "must be a boolean",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                value = json.loads(json.dumps(original))
                mutate(value)
                path = Path(directory) / "contract.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(POLICY.ApplicationReleaseError, message):
                    POLICY.load_production_contract(path)


class ReleaseDescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = POLICY.load_production_contract(
            ROOT / "releases/application-production.json"
        )

    def test_shared_schema_and_strict_policy_accept_both_applications(self):
        schema = json.loads(
            (ROOT / "schemas/application-release.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for policy in self.contract.applications:
            with self.subTest(application=policy.name):
                value = release_value(policy)
                validator.validate(value)
                descriptor = POLICY.validate_release_descriptor(
                    POLICY.canonical_json(value), policy, REVISION
                )
                self.assertEqual(descriptor.application, policy.name)
                self.assertEqual(
                    set(descriptor.component_references),
                    set(policy.component_repositories),
                )

    def test_shared_schema_rejects_component_repository_swaps(self):
        schema = json.loads(
            (ROOT / "schemas/application-release.schema.json").read_text()
        )
        validator = Draft202012Validator(schema)
        for policy in self.contract.applications:
            component_names = tuple(policy.component_repositories)
            first, second = component_names[:2]
            value = release_value(policy)
            value["components"][first]["image"] = (
                f"{policy.component_repositories[second]}@{DIGEST_A}"
            )
            with self.subTest(application=policy.name, component=first):
                self.assertTrue(list(validator.iter_errors(value)))

    def test_descriptor_requires_canonical_bytes(self):
        policy = self.contract.applications[1]
        noncanonical = json.dumps(release_value(policy), indent=2).encode("utf-8")
        with self.assertRaisesRegex(
            POLICY.ApplicationReleaseError, "must use canonical JSON"
        ):
            POLICY.validate_release_descriptor(noncanonical, policy, REVISION)

    def test_descriptor_rejects_duplicate_json_keys(self):
        policy = self.contract.applications[0]
        with self.assertRaisesRegex(POLICY.ApplicationReleaseError, "duplicate key"):
            POLICY.validate_release_descriptor(
                b'{"schema":1,"schema":1}\n', policy, REVISION
            )

    def test_descriptor_rejects_wrong_revision_component_set_and_repository(self):
        policy = self.contract.applications[1]
        cases = (
            (
                "source-revision",
                lambda value: value["source"].__setitem__("revision", "f" * 40),
                "source.revision",
            ),
            (
                "component-revision",
                lambda value: value["components"]["backend"].__setitem__(
                    "source_revision", "f" * 40
                ),
                "components.backend.source_revision",
            ),
            (
                "missing-component",
                lambda value: value["components"].pop("frontend"),
                "exact component allowlist",
            ),
            (
                "wrong-repository",
                lambda value: value["components"]["backend"].__setitem__(
                    "image", f"ghcr.io/nclsppr/other/backend@{DIGEST_A}"
                ),
                "untagged immutable reference",
            ),
            (
                "tagged-reference",
                lambda value: value["components"]["backend"].__setitem__(
                    "image", f"ghcr.io/nclsppr/parkventory/backend:latest@{DIGEST_A}"
                ),
                "untagged immutable reference",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                value = release_value(policy)
                mutate(value)
                with self.assertRaisesRegex(POLICY.ApplicationReleaseError, message):
                    POLICY.validate_release_descriptor(
                        POLICY.canonical_json(value), policy, REVISION
                    )

    def test_descriptor_rejects_automatic_migration_and_unbound_inventories(self):
        policy = self.contract.applications[0]
        cases = (
            (
                "auto-migrate",
                lambda value: value["migrations"].__setitem__(
                    "runtime_auto_migrate", True
                ),
                "runtime_auto_migrate",
            ),
            (
                "migration-artifact",
                lambda value: value["migrations"].__setitem__(
                    "inventory_artifact",
                    f"{policy.integration_repository}@{DIGEST_C}",
                ),
                "migrations.inventory_artifact",
            ),
            (
                "probe-digest",
                lambda value: value["probes"].__setitem__(
                    "inventory_sha256", "a" * 64
                ),
                "probes.inventory_sha256",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                value = release_value(policy)
                mutate(value)
                with self.assertRaisesRegex(POLICY.ApplicationReleaseError, message):
                    POLICY.validate_release_descriptor(
                        POLICY.canonical_json(value), policy, REVISION
                    )

    def test_manifest_has_one_exact_release_layer_and_exact_source(self):
        policy = self.contract.applications[0]
        descriptor = release_bytes(policy)
        value = manifest_value(policy, descriptor)
        layer = POLICY.validate_release_manifest(
            json.dumps(value, separators=(",", ":")).encode(), policy, REVISION
        )
        self.assertEqual((layer.digest, layer.size), (POLICY.content_digest(descriptor), len(descriptor)))

        value["layers"].append(value["layers"][0])
        with self.assertRaisesRegex(POLICY.ApplicationReleaseError, "exactly one layer"):
            POLICY.validate_release_manifest(
                json.dumps(value, separators=(",", ":")).encode(), policy, REVISION
            )


if __name__ == "__main__":
    unittest.main()
