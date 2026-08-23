#!/usr/bin/env python3

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas/secret-registry.schema.json"
REGISTRY_PATH = ROOT / "secrets/registry.json"

FORBIDDEN_FIELDS = {
    "access_key",
    "access_key_id",
    "address",
    "api_key",
    "api_key_id",
    "checksum",
    "ciphertext",
    "content",
    "data",
    "digest",
    "encrypted_value",
    "fingerprint",
    "hash",
    "host",
    "hostname",
    "inventory",
    "ip",
    "plaintext",
    "project_id",
    "secret_key",
    "sha256",
    "sops",
    "token",
    "value",
    "value_hash",
    "value_sha256",
}

EXPECTED_PLATFORM_PATHS = {
    "/etc/vps/secrets/platform/grafana-admin-password",
    "/etc/vps/secrets/platform/grafana-secret-key",
    "/etc/vps/secrets/platform/postgres-exporter-password",
    "/etc/vps/secrets/platform/postgres-superuser-password",
}

EXPECTED_TEM_CONTRACTS = {
    "monflorian": {
        "credential_set": "scaleway-tem:monflorian-prod-smtp",
        "declared_state": "planned",
        "ids": {
            "monflorian.smtp-password",
            "monflorian.smtp-username",
        },
        "materializer": "not-implemented",
    },
    "parkventory": {
        "credential_set": "scaleway-tem:parkventory-prod-smtp",
        "declared_state": "required",
        "ids": {
            "parkventory.smtp-password",
            "parkventory.smtp-username",
        },
        "materializer": "materialize-parkventory-provider-secrets",
    },
    "surplasse": {
        "credential_set": "scaleway-tem:surplasse-prod-smtp",
        "declared_state": "required",
        "ids": {
            "surplasse.smtp-host",
            "surplasse.smtp-password",
            "surplasse.smtp-username",
        },
        "materializer": "materialize-surplasse-secrets",
    },
}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"forbidden JSON constant: {value}")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SecretRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_json(SCHEMA_PATH)
        cls.registry = load_json(REGISTRY_PATH)
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )

    def assert_invalid(self, candidate: dict[str, Any]) -> None:
        with self.assertRaises(ValidationError):
            self.validator.validate(candidate)

    def test_registry_matches_the_schema(self) -> None:
        errors = sorted(
            self.validator.iter_errors(self.registry),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        self.assertEqual(
            errors,
            [],
            "\n".join(error.message for error in errors),
        )

    def test_value_recovery_is_explicit_and_not_configured(self) -> None:
        self.assertEqual(
            self.registry["value_recovery_state"],
            "not-configured",
        )
        generated_entries = [
            entry
            for entry in self.registry["secrets"]
            if entry["source"] == "generated-on-atlas"
        ]
        self.assertTrue(generated_entries)
        self.assertEqual(
            {entry["rebuild"] for entry in generated_entries},
            {"restore-from-external-store"},
        )

    def test_schema_closes_the_registry_and_each_entry(self) -> None:
        self.assertFalse(self.schema["additionalProperties"])
        self.assertFalse(self.schema["$defs"]["secret"]["additionalProperties"])

        candidate = copy.deepcopy(self.registry)
        candidate["unexpected"] = "redacted"
        self.assert_invalid(candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][0]["unexpected"] = "redacted"
        self.assert_invalid(candidate)

    def test_registry_cannot_store_values_hashes_or_provider_identifiers(self) -> None:
        allowed_entry_fields = set(
            self.schema["$defs"]["secret"]["properties"]
        )
        self.assertTrue(FORBIDDEN_FIELDS.isdisjoint(allowed_entry_fields))
        self.assertTrue(FORBIDDEN_FIELDS.isdisjoint(self.registry))

        for secret in self.registry["secrets"]:
            self.assertTrue(FORBIDDEN_FIELDS.isdisjoint(secret), secret["id"])

        for field in sorted(FORBIDDEN_FIELDS):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                candidate["secrets"][0][field] = "redacted"
                self.assert_invalid(candidate)

    def test_ids_paths_and_consumers_are_unique_and_sorted(self) -> None:
        secrets = self.registry["secrets"]
        identifiers = [secret["id"] for secret in secrets]
        paths = [secret["path"] for secret in secrets]

        self.assertEqual(identifiers, sorted(identifiers))
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(paths), len(set(paths)))
        for secret in secrets:
            self.assertEqual(
                secret["consumers"],
                sorted(secret["consumers"]),
                secret["id"],
            )

    def test_schema_rejects_unbounded_secret_paths(self) -> None:
        unsafe_paths = (
            "/etc/vps/secrets/../shadow",
            "/etc/vps/secrets/a/../../shadow",
            "/etc/vps/secrets/a/./value",
            "/etc/vps/secrets/a//value",
        )
        for path in unsafe_paths:
            with self.subTest(path=path):
                candidate = copy.deepcopy(self.registry)
                candidate["secrets"][0]["path"] = path
                self.assert_invalid(candidate)

        registry_root = Path(self.registry["secret_root"]).resolve()
        for secret in self.registry["secrets"]:
            with self.subTest(secret=secret["id"]):
                self.assertTrue(
                    Path(secret["path"]).resolve().is_relative_to(registry_root)
                )

    def test_state_and_generation_are_consistent(self) -> None:
        for secret in self.registry["secrets"]:
            with self.subTest(secret=secret["id"]):
                self.assertGreaterEqual(secret["target_generation"], 1)
                self.assertGreaterEqual(
                    secret["target_generation"],
                    secret["generation"],
                )
                self.assertLessEqual(
                    secret["target_generation"] - secret["generation"],
                    1,
                )
                if secret["generation_binding"] == "unlinked":
                    self.assertEqual(secret["generation"], 0)
                    self.assertNotEqual(secret["host_state"], "runtime-loaded")
                else:
                    self.assertGreaterEqual(secret["generation"], 1)
                if secret["declared_state"] == "planned":
                    self.assertEqual(secret["generation"], 0)
                    self.assertEqual(secret["target_generation"], 1)
                    self.assertEqual(secret["host_state"], "absent")
                if secret["provider_state"] == "revoked":
                    self.assertEqual(secret["host_state"], "absent")
                    self.assertEqual(secret["rebuild"], "do-not-restore")
                if secret["source"] in {"provider-identifier", "provider-secret"}:
                    self.assertNotEqual(secret["provider_state"], "not-applicable")

        materialized_entries = [
            secret
            for secret in self.registry["secrets"]
            if secret["host_state"] == "materialized"
        ]
        self.assertEqual(len(materialized_entries), 6)
        self.assertEqual(
            {secret["generation_binding"] for secret in self.registry["secrets"]},
            {"unlinked"},
        )
        self.assertEqual(
            {secret["generation"] for secret in materialized_entries},
            {0},
        )

        materialized = materialized_entries[0]
        materialized_index = self.registry["secrets"].index(materialized)
        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][materialized_index]["generation"] = 1
        self.assert_invalid(candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][materialized_index]["generation_binding"] = (
            "materializer-marker"
        )
        self.assert_invalid(candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][materialized_index]["host_state"] = "runtime-loaded"
        self.assert_invalid(candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][materialized_index]["generation"] = 1
        candidate["secrets"][materialized_index]["generation_binding"] = (
            "materializer-marker"
        )
        self.validator.validate(candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["secrets"][0]["target_generation"] = 0
        self.assert_invalid(candidate)

    def test_three_scaleway_tem_credential_contracts_are_separate(self) -> None:
        tem_entries = [
            secret
            for secret in self.registry["secrets"]
            if secret["provider"] == "scaleway-tem"
        ]
        entries_by_scope = {
            scope: [entry for entry in tem_entries if entry["scope"] == scope]
            for scope in EXPECTED_TEM_CONTRACTS
        }
        self.assertEqual(
            {entry["scope"] for entry in tem_entries},
            set(EXPECTED_TEM_CONTRACTS),
        )

        credential_sets: set[str] = set()
        for scope, expected in EXPECTED_TEM_CONTRACTS.items():
            with self.subTest(scope=scope):
                entries = entries_by_scope[scope]
                by_id = {entry["id"]: entry for entry in entries}
                self.assertEqual(set(by_id), expected["ids"])
                credential_entries = [
                    entry
                    for entry in entries
                    if entry["source"] in {"provider-identifier", "provider-secret"}
                ]
                self.assertEqual(
                    {entry["credential_set"] for entry in credential_entries},
                    {expected["credential_set"]},
                )
                self.assertEqual(
                    {entry["permission_set"] for entry in credential_entries},
                    {"TransactionalEmailEmailSmtpCreate"},
                )
                self.assertEqual(
                    {entry["materializer"] for entry in entries},
                    {expected["materializer"]},
                )
                self.assertEqual(
                    {entry["declared_state"] for entry in entries},
                    {expected["declared_state"]},
                )
                smtp_credential = by_id[f"{scope}.smtp-password"]
                username = by_id[f"{scope}.smtp-username"]
                credential_sets.add(smtp_credential["credential_set"])
                self.assertEqual(smtp_credential["classification"], "secret")
                self.assertEqual(smtp_credential["source"], "provider-secret")
                self.assertEqual(username["classification"], "protected-input")
                self.assertEqual(username["source"], "provider-identifier")
                for entry in (smtp_credential, username):
                    self.assertEqual(entry["group"], 10001)
                    self.assertEqual(entry["mode"], "0440")
                    self.assertEqual(entry["consumers"], [f"{scope}-backend"])
                    self.assertEqual(
                        entry["path"],
                        f"/etc/vps/secrets/{scope}/{scope}-smtp-"
                        f"{entry['id'].rsplit('-', maxsplit=1)[-1]}",
                    )
                    self.assertEqual(entry["provider_state"], "planned")

                if scope == "surplasse":
                    host = by_id["surplasse.smtp-host"]
                    self.assertNotIn("credential_set", host)
                    self.assertNotIn("permission_set", host)
                    self.assertEqual(host["source"], "operator-file")
                    self.assertEqual(host["provider_state"], "not-applicable")

        self.assertEqual(len(credential_sets), 3)
        self.assertFalse(
            any("access-key" in entry["id"] for entry in tem_entries)
        )

    def test_registry_paths_match_existing_executable_contracts(self) -> None:
        bundle = load_script_module(
            "secret_registry_application_bundle",
            ROOT / "scripts/lib/application_bundle.py",
        )
        surplasse_materializer = load_script_module(
            "secret_registry_surplasse_materializer",
            ROOT / "scripts/materialize-surplasse-secrets",
        )
        parkventory_materializer = load_script_module(
            "secret_registry_parkventory_materializer",
            ROOT / "scripts/materialize-parkventory-secrets",
        )
        parkventory_provider_materializer = load_script_module(
            "secret_registry_parkventory_provider_materializer",
            ROOT / "scripts/materialize-parkventory-provider-secrets",
        )
        dns_materializer = load_script_module(
            "secret_registry_dns_materializer",
            ROOT / "scripts/materialize-surplasse-dns-secrets",
        )
        registry_paths = {secret["path"] for secret in self.registry["secrets"]}

        for application, profile in bundle.PROFILES.items():
            with self.subTest(application=application):
                self.assertTrue(
                    set(profile.credential_files.values()).issubset(registry_paths)
                )

        paths_by_materializer = {
            materializer: {
                secret["path"]
                for secret in self.registry["secrets"]
                if secret["materializer"] == materializer
            }
            for materializer in (
                "materialize-internal-platform-secrets",
                "materialize-parkventory-provider-secrets",
                "materialize-parkventory-secrets",
                "materialize-surplasse-dns-secrets",
                "materialize-surplasse-secrets",
            )
        }
        self.assertEqual(
            paths_by_materializer["materialize-internal-platform-secrets"],
            EXPECTED_PLATFORM_PATHS,
        )
        self.assertEqual(
            paths_by_materializer["materialize-parkventory-provider-secrets"],
            {
                str(parkventory_provider_materializer.PRODUCTION_CREDENTIAL_ROOT / name)
                for name in parkventory_provider_materializer.CREDENTIAL_FILES
            },
        )
        self.assertEqual(
            paths_by_materializer["materialize-parkventory-secrets"],
            {
                str(parkventory_materializer.PRODUCTION_ROOT / name)
                for name in parkventory_materializer.CREDENTIAL_FILES
            },
        )
        self.assertEqual(
            paths_by_materializer["materialize-surplasse-secrets"],
            {
                str(surplasse_materializer.PRODUCTION_ROOT / spec.name)
                for spec in surplasse_materializer.SPECS
            },
        )
        self.assertEqual(
            paths_by_materializer["materialize-surplasse-dns-secrets"],
            {
                str(dns_materializer.PRODUCTION_ROOT / spec.name)
                for spec in dns_materializer.CREDENTIAL_SPECS
            },
        )

    def test_parkventory_marker_matches_the_registered_generation(self) -> None:
        materializer = load_script_module(
            "secret_registry_parkventory_marker",
            ROOT / "scripts/materialize-parkventory-secrets",
        )
        registered = sorted(
            (
                secret
                for secret in self.registry["secrets"]
                if secret["materializer"] == "materialize-parkventory-secrets"
            ),
            key=lambda secret: secret["id"],
        )
        marker = json.loads(materializer.generation_marker())

        self.assertEqual(
            set(marker),
            {"contract", "materializer", "secrets", "target_generation"},
        )
        self.assertEqual(marker["materializer"], "materialize-parkventory-secrets")
        self.assertEqual(
            marker["secrets"],
            [
                {"file": Path(secret["path"]).name, "id": secret["id"]}
                for secret in registered
            ],
        )
        self.assertEqual(
            {secret["target_generation"] for secret in registered},
            {marker["target_generation"]},
        )
        self.assertEqual(
            {secret["declared_state"] for secret in registered},
            {"required"},
        )
        self.assertTrue(
            all(secret["generation_binding"] == "unlinked" for secret in registered)
        )

    def test_parkventory_provider_marker_matches_registered_generation(self) -> None:
        materializer = load_script_module(
            "secret_registry_parkventory_provider_marker",
            ROOT / "scripts/materialize-parkventory-provider-secrets",
        )
        registered = sorted(
            (
                secret
                for secret in self.registry["secrets"]
                if secret["materializer"]
                == "materialize-parkventory-provider-secrets"
            ),
            key=lambda secret: secret["id"],
        )
        marker = json.loads(materializer.generation_marker())

        self.assertEqual(
            marker["secrets"],
            [
                {"file": Path(secret["path"]).name, "id": secret["id"]}
                for secret in registered
            ],
        )
        self.assertEqual(
            {secret["target_generation"] for secret in registered},
            {marker["target_generation"]},
        )
        self.assertEqual(
            {secret["declared_state"] for secret in registered},
            {"required"},
        )


if __name__ == "__main__":
    unittest.main()
