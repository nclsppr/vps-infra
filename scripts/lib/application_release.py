#!/usr/bin/env python3
"""Validate immutable application release admission contracts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


MAX_CONTRACT_BYTES = 64 * 1024
MAX_RELEASE_BYTES = 64 * 1024
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_EMPTY_CONFIG = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2,
    "data": "e30=",
}
APPLICATION_RELEASE_CONTRACT = "vps-infra.application-release.v1"
APPLICATION_RELEASE_ARTIFACT_TYPE = "application/vnd.vps-infra.application-release.v1"
APPLICATION_RELEASE_LAYER_MEDIA_TYPE = (
    "application/vnd.vps-infra.application-release.v1+json"
)
APPLICATION_RELEASE_TITLE = "application-release.json"
SOURCE_ANNOTATION = "org.opencontainers.image.source"
REVISION_ANNOTATION = "org.opencontainers.image.revision"
CREATED_ANNOTATION = "org.opencontainers.image.created"
TITLE_ANNOTATION = "org.opencontainers.image.title"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)

EXPECTED_APPLICATIONS: Mapping[str, Mapping[str, object]] = {
    "surplasse": {
        "mode": "compose",
        "source_repository": "nclsppr/surplasse",
        "source_branch": "main",
        "release_repository": "ghcr.io/nclsppr/surplasse/application-release",
        "integration_repository": "ghcr.io/nclsppr/surplasse/vps-integration",
        "component_repositories": {
            "backend": "ghcr.io/nclsppr/surplasse/backend",
            "onboarding": "ghcr.io/nclsppr/surplasse/onboarding",
            "commande": "ghcr.io/nclsppr/surplasse/commande",
            "dashboard": "ghcr.io/nclsppr/surplasse/dashboard",
            "docs": "ghcr.io/nclsppr/surplasse/docs",
        },
        "required_checks": ("Publish immutable application release",),
    },
    "parkventory": {
        "mode": "compose",
        "source_repository": "nclsppr/parkventory",
        "source_branch": "main",
        "release_repository": "ghcr.io/nclsppr/parkventory/application-release",
        "integration_repository": "ghcr.io/nclsppr/parkventory/vps-integration",
        "component_repositories": {
            "backend": "ghcr.io/nclsppr/parkventory/backend",
            "frontend": "ghcr.io/nclsppr/parkventory/frontend",
        },
        "required_checks": ("Publish immutable application release", "verify"),
    },
}


class ApplicationReleaseError(ValueError):
    """The application release contract or evidence is invalid."""


@dataclasses.dataclass(frozen=True)
class ApplicationPolicy:
    name: str
    enabled: bool
    mode: str
    source_repository: str
    source_branch: str
    release_repository: str
    integration_repository: str
    component_repositories: Mapping[str, str]
    required_checks: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ProductionContract:
    applications: tuple[ApplicationPolicy, ...]


@dataclasses.dataclass(frozen=True)
class ReleaseDescriptor:
    application: str
    source_revision: str
    component_references: Mapping[str, str]
    integration_reference: str
    migration_inventory_digest: str
    probe_inventory_digest: str


@dataclasses.dataclass(frozen=True)
class ReleaseLayer:
    digest: str
    size: int


def _reject_constant(value: str) -> None:
    raise ApplicationReleaseError(f"JSON constant {value!r} is not permitted")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationReleaseError(f"JSON object contains duplicate key {key!r}")
        result[key] = value
    return result


def strict_json_bytes(raw: bytes, label: str, *, maximum: int) -> object:
    if not 1 <= len(raw) <= maximum:
        raise ApplicationReleaseError(f"{label} exceeds its size limit")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ApplicationReleaseError(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _fail(path: str, message: str) -> None:
    raise ApplicationReleaseError(f"{path}: {message}")


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], path: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing:
        _fail(path, f"missing keys: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown keys: {', '.join(sorted(unknown))}")


def _literal(value: object, expected: object, path: str) -> None:
    if value != expected or type(value) is not type(expected):
        _fail(path, f"must equal {expected!r}")


def _sha40(value: object, path: str) -> str:
    if not isinstance(value, str) or SHA40_RE.fullmatch(value) is None:
        _fail(path, "must be a full lowercase 40-character Git SHA")
    return value


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail(path, "must be sha256 followed by 64 lowercase hexadecimal characters")
    return value


def _digest_reference(value: object, repository: str, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    prefix = f"{repository}@"
    if not value.startswith(prefix) or SHA256_RE.fullmatch(value.removeprefix(prefix)) is None:
        _fail(path, f"must be an untagged immutable reference in {repository!r}")
    return value


def _read(path: Path, label: str, maximum: int) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ApplicationReleaseError(f"cannot read {label} {path}: {exc}") from exc
    if not 1 <= len(raw) <= maximum:
        raise ApplicationReleaseError(f"{label} exceeds its size limit")
    return raw


def load_production_contract(
    path: Path,
    static_contract_path: Path | None = None,
) -> ProductionContract:
    value = strict_json_bytes(
        _read(path, "application production contract", MAX_CONTRACT_BYTES),
        "application production contract",
        maximum=MAX_CONTRACT_BYTES,
    )
    contract = _object(value, "application production contract")
    _exact_keys(contract, {"schema", "applications"}, "application production contract")
    _literal(contract["schema"], 1, "application production contract.schema")
    applications = _object(
        contract["applications"], "application production contract.applications"
    )
    if set(applications) != set(EXPECTED_APPLICATIONS):
        _fail(
            "application production contract.applications",
            "must contain the exact application allowlist",
        )

    parsed: list[ApplicationPolicy] = []
    for name, expected in EXPECTED_APPLICATIONS.items():
        path_prefix = f"application production contract.applications.{name}"
        candidate = _object(applications[name], path_prefix)
        fields = {
            "enabled",
            "mode",
            "source_repository",
            "source_branch",
            "release_repository",
            "integration_repository",
            "component_repositories",
            "required_checks",
        }
        _exact_keys(candidate, fields, path_prefix)
        enabled = candidate["enabled"]
        if type(enabled) is not bool:
            _fail(f"{path_prefix}.enabled", "must be a boolean")
        for field in (
            "mode",
            "source_repository",
            "source_branch",
            "release_repository",
            "integration_repository",
        ):
            _literal(candidate[field], expected[field], f"{path_prefix}.{field}")

        components = _object(
            candidate["component_repositories"],
            f"{path_prefix}.component_repositories",
        )
        expected_components = expected["component_repositories"]
        if components != expected_components:
            _fail(
                f"{path_prefix}.component_repositories",
                "must match the exact component repository allowlist",
            )
        required_checks = candidate["required_checks"]
        if not isinstance(required_checks, list) or tuple(required_checks) != expected[
            "required_checks"
        ]:
            _fail(
                f"{path_prefix}.required_checks",
                "must match the exact required check allowlist",
            )
        parsed.append(
            ApplicationPolicy(
                name=name,
                enabled=enabled,
                mode=str(expected["mode"]),
                source_repository=str(expected["source_repository"]),
                source_branch=str(expected["source_branch"]),
                release_repository=str(expected["release_repository"]),
                integration_repository=str(expected["integration_repository"]),
                component_repositories=dict(expected_components),
                required_checks=tuple(str(item) for item in expected["required_checks"]),
            )
        )

    result = ProductionContract(applications=tuple(parsed))
    if static_contract_path is not None:
        validate_static_exclusivity(result, static_contract_path)
    return result


def validate_static_exclusivity(
    contract: ProductionContract,
    static_contract_path: Path,
) -> None:
    raw = _read(static_contract_path, "static production contract", MAX_CONTRACT_BYTES)
    value = strict_json_bytes(
        raw, "static production contract", maximum=MAX_CONTRACT_BYTES
    )
    static_contract = _object(value, "static production contract")
    applications = _object(
        static_contract.get("applications"), "static production contract.applications"
    )
    parkventory = _object(
        applications.get("parkventory"),
        "static production contract.applications.parkventory",
    )
    static_enabled = parkventory.get("enabled")
    if type(static_enabled) is not bool:
        _fail(
            "static production contract.applications.parkventory.enabled",
            "must be a boolean",
        )
    application_enabled = next(
        application.enabled
        for application in contract.applications
        if application.name == "parkventory"
    )
    if application_enabled and static_enabled:
        raise ApplicationReleaseError(
            "Parkventory static and Compose production contracts cannot both be enabled"
        )


def validate_release_descriptor(
    raw: bytes,
    policy: ApplicationPolicy,
    revision: str,
) -> ReleaseDescriptor:
    _sha40(revision, "expected source revision")
    value = strict_json_bytes(
        raw, "application release descriptor", maximum=MAX_RELEASE_BYTES
    )
    if raw != canonical_json(value):
        raise ApplicationReleaseError(
            "application release descriptor must use canonical JSON with one final newline"
        )
    release = _object(value, "application release descriptor")
    _exact_keys(
        release,
        {
            "schema",
            "contract",
            "application",
            "source",
            "components",
            "integration",
            "migrations",
            "probes",
        },
        "application release descriptor",
    )
    _literal(release["schema"], 1, "application release descriptor.schema")
    _literal(
        release["contract"],
        APPLICATION_RELEASE_CONTRACT,
        "application release descriptor.contract",
    )
    _literal(
        release["application"], policy.name, "application release descriptor.application"
    )

    source = _object(release["source"], "application release descriptor.source")
    _exact_keys(
        source,
        {"repository", "branch", "revision"},
        "application release descriptor.source",
    )
    _literal(
        source["repository"],
        policy.source_repository,
        "application release descriptor.source.repository",
    )
    _literal(
        source["branch"],
        policy.source_branch,
        "application release descriptor.source.branch",
    )
    _literal(
        source["revision"], revision, "application release descriptor.source.revision"
    )

    components = _object(
        release["components"], "application release descriptor.components"
    )
    if set(components) != set(policy.component_repositories):
        _fail(
            "application release descriptor.components",
            "must contain the exact component allowlist",
        )
    component_references: dict[str, str] = {}
    for name, repository in policy.component_repositories.items():
        component = _object(
            components[name], f"application release descriptor.components.{name}"
        )
        _exact_keys(
            component,
            {"source_revision", "image"},
            f"application release descriptor.components.{name}",
        )
        _literal(
            component["source_revision"],
            revision,
            f"application release descriptor.components.{name}.source_revision",
        )
        component_references[name] = _digest_reference(
            component["image"],
            repository,
            f"application release descriptor.components.{name}.image",
        )

    integration = _object(
        release["integration"], "application release descriptor.integration"
    )
    _exact_keys(
        integration,
        {"source_revision", "artifact"},
        "application release descriptor.integration",
    )
    _literal(
        integration["source_revision"],
        revision,
        "application release descriptor.integration.source_revision",
    )
    integration_reference = _digest_reference(
        integration["artifact"],
        policy.integration_repository,
        "application release descriptor.integration.artifact",
    )

    migrations = _object(
        release["migrations"], "application release descriptor.migrations"
    )
    _exact_keys(
        migrations,
        {
            "strategy",
            "runtime_auto_migrate",
            "inventory_artifact",
            "inventory_sha256",
        },
        "application release descriptor.migrations",
    )
    _literal(
        migrations["strategy"],
        "dedicated",
        "application release descriptor.migrations.strategy",
    )
    _literal(
        migrations["runtime_auto_migrate"],
        False,
        "application release descriptor.migrations.runtime_auto_migrate",
    )
    _literal(
        migrations["inventory_artifact"],
        integration_reference,
        "application release descriptor.migrations.inventory_artifact",
    )
    migration_inventory_digest = _sha256(
        migrations["inventory_sha256"],
        "application release descriptor.migrations.inventory_sha256",
    )

    probes = _object(release["probes"], "application release descriptor.probes")
    _exact_keys(
        probes,
        {"inventory_artifact", "inventory_sha256"},
        "application release descriptor.probes",
    )
    _literal(
        probes["inventory_artifact"],
        integration_reference,
        "application release descriptor.probes.inventory_artifact",
    )
    probe_inventory_digest = _sha256(
        probes["inventory_sha256"],
        "application release descriptor.probes.inventory_sha256",
    )

    return ReleaseDescriptor(
        application=policy.name,
        source_revision=revision,
        component_references=component_references,
        integration_reference=integration_reference,
        migration_inventory_digest=migration_inventory_digest,
        probe_inventory_digest=probe_inventory_digest,
    )


def validate_release_manifest(
    raw: bytes,
    policy: ApplicationPolicy,
    revision: str,
) -> ReleaseLayer:
    value = strict_json_bytes(raw, "application release manifest", maximum=MAX_RELEASE_BYTES)
    manifest = _object(value, "application release manifest")
    _exact_keys(
        manifest,
        {"schemaVersion", "mediaType", "artifactType", "config", "layers", "annotations"},
        "application release manifest",
    )
    _literal(manifest["schemaVersion"], 2, "application release manifest.schemaVersion")
    _literal(
        manifest["mediaType"],
        OCI_MANIFEST_MEDIA_TYPE,
        "application release manifest.mediaType",
    )
    _literal(
        manifest["artifactType"],
        APPLICATION_RELEASE_ARTIFACT_TYPE,
        "application release manifest.artifactType",
    )
    _literal(manifest["config"], OCI_EMPTY_CONFIG, "application release manifest.config")

    layers = manifest["layers"]
    if not isinstance(layers, list) or len(layers) != 1:
        _fail("application release manifest.layers", "must contain exactly one layer")
    layer = _object(layers[0], "application release manifest.layers[0]")
    _exact_keys(
        layer,
        {"mediaType", "digest", "size", "annotations"},
        "application release manifest.layers[0]",
    )
    _literal(
        layer["mediaType"],
        APPLICATION_RELEASE_LAYER_MEDIA_TYPE,
        "application release manifest.layers[0].mediaType",
    )
    digest = _sha256(layer["digest"], "application release manifest.layers[0].digest")
    size = layer["size"]
    if type(size) is not int or not 1 <= size <= MAX_RELEASE_BYTES:
        _fail(
            "application release manifest.layers[0].size",
            "must be a positive integer within the release size limit",
        )
    _literal(
        layer["annotations"],
        {TITLE_ANNOTATION: APPLICATION_RELEASE_TITLE},
        "application release manifest.layers[0].annotations",
    )

    annotations = _object(
        manifest["annotations"], "application release manifest.annotations"
    )
    _exact_keys(
        annotations,
        {SOURCE_ANNOTATION, REVISION_ANNOTATION, CREATED_ANNOTATION},
        "application release manifest.annotations",
    )
    _literal(
        annotations[SOURCE_ANNOTATION],
        f"https://github.com/{policy.source_repository}",
        f"application release manifest.annotations.{SOURCE_ANNOTATION}",
    )
    _literal(
        annotations[REVISION_ANNOTATION],
        revision,
        f"application release manifest.annotations.{REVISION_ANNOTATION}",
    )
    created = annotations[CREATED_ANNOTATION]
    if not isinstance(created, str) or RFC3339_RE.fullmatch(created) is None:
        _fail(
            f"application release manifest.annotations.{CREATED_ANNOTATION}",
            "must be an RFC 3339 timestamp",
        )
    return ReleaseLayer(digest=digest, size=size)


def content_digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
