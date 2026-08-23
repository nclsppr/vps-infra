#!/usr/bin/env python3
"""Validate immutable application integration bundles for Atlas."""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit


MIB = 1024 * 1024
MAX_MANIFEST_BYTES = 2 * MIB
MAX_INVENTORY_BYTES = 2 * MIB
MAX_ARCHIVE_BYTES = 16 * MIB
MAX_EXPANDED_BYTES = 32 * MIB
MAX_FILE_BYTES = 5 * MIB
MAX_COMPONENT_MANIFEST_BYTES = 4 * MIB
MAX_IMAGE_CONFIG_BYTES = 2 * MIB
MAX_RUNTIME_LAYER_COUNT = 64
MAX_RUNTIME_COMPRESSED_BYTES = 4 * 1024 * MIB

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_EMPTY_CONFIG = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2,
    "data": "e30=",
}
INTEGRATION_ARTIFACT_TYPE = "application/vnd.vps-infra.application-integration.v1"
INTEGRATION_ARCHIVE_MEDIA_TYPE = (
    "application/vnd.vps-infra.application-integration.v1+tar+gzip"
)
INTEGRATION_INVENTORY_MEDIA_TYPE = (
    "application/vnd.vps-infra.application-integration.inventory.v1+json"
)
ARCHIVE_TITLE = "integration.tar.gz"
INVENTORY_TITLE = "inventory.json"
SOURCE_ANNOTATION = "org.opencontainers.image.source"
REVISION_ANNOTATION = "org.opencontainers.image.revision"
CREATED_ANNOTATION = "org.opencontainers.image.created"
TITLE_ANNOTATION = "org.opencontainers.image.title"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_NAME_RE = re.compile(r"^V([1-9][0-9]*)__[A-Za-z0-9_]+\.sql$")
RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
SURPLASSE_PILOT_SCHEMA_CANONICAL_SHA256 = (
    "sha256:d27c4895cb2508d7344930430c8beb7bb0339a2679879e4827064fe75e1fbf51"
)

SURPLASSE_PAYMENT_PROFILE: Mapping[str, object] = {
    "audience": "testers",
    "mode": "test",
    "schema": 1,
}


class ApplicationBundleError(ValueError):
    """An integration bundle is outside the exact Atlas contract."""


@dataclasses.dataclass(frozen=True)
class BundleProfile:
    application: str
    source_repository: str
    integration_repository: str
    release_signer_workflow: str
    integration_signer_workflow: str
    component_signer_workflow: str
    component_repositories: Mapping[str, str]
    runtime_paths: tuple[str, ...]
    archive_directories: tuple[str, ...]
    contract_name: str
    migration_contract: str
    migration_strategy: str
    migration_runner: str | None
    probe_contract: str
    default_public_host: str
    public_hosts: tuple[str, ...]
    runtime_configuration_keys: tuple[str, ...]
    credential_files: Mapping[str, str]
    service_credentials: Mapping[str, tuple[str, ...]]
    credential_gid: int
    image_version_prefix: str


@dataclasses.dataclass(frozen=True)
class LayerDescriptor:
    digest: str
    size: int


@dataclasses.dataclass(frozen=True)
class IntegrationManifest:
    archive: LayerDescriptor
    inventory: LayerDescriptor
    created: str


@dataclasses.dataclass(frozen=True)
class ComponentIndex:
    runtime_manifest: LayerDescriptor
    attestation_manifest: LayerDescriptor


@dataclasses.dataclass(frozen=True)
class ApplicationBundle:
    application: str
    source_revision: str
    created: str
    files: Mapping[str, bytes]
    contract: Mapping[str, object]
    migrations: Mapping[str, object]
    probes: Mapping[str, object]


SURPLASSE_PATHS = tuple(
    sorted(
        (
            "caddy/surplasse.caddy",
            "compose.yaml",
            "contract.json",
            "expected-images.json",
            "grafana/dashboards/surplasse-overview.json",
            "migrations.json",
            "pilot-bootstrap.schema.json",
            "probes.json",
            "prometheus/rules.yml",
            "prometheus/targets.yml",
        )
    )
)
PARKVENTORY_PATHS = tuple(
    sorted(
        (
            "caddy/parkventory.caddy",
            "compose.yaml",
            "contract.json",
            "migrations.json",
            "probes.json",
            "prometheus/rules.yml",
            "prometheus/targets.json",
        )
    )
)
MONFLORIAN_PATHS = tuple(
    sorted(
        (
            "caddy/monflorian.caddy",
            "compose.yaml",
            "contract.json",
            "expected-images.json",
            "migrations.json",
            "probes.json",
        )
    )
)

PROFILES: Mapping[str, BundleProfile] = {
    "surplasse": BundleProfile(
        application="surplasse",
        source_repository="nclsppr/surplasse",
        integration_repository="ghcr.io/nclsppr/surplasse/vps-integration",
        release_signer_workflow=(
            "nclsppr/surplasse/.github/workflows/vps-integration.yml"
        ),
        integration_signer_workflow=(
            "nclsppr/surplasse/.github/workflows/vps-integration.yml"
        ),
        component_signer_workflow=(
            "nclsppr/surplasse/.github/workflows/images.yml"
        ),
        component_repositories={
            "backend": "ghcr.io/nclsppr/surplasse/backend",
            "onboarding": "ghcr.io/nclsppr/surplasse/onboarding",
            "commande": "ghcr.io/nclsppr/surplasse/commande",
            "dashboard": "ghcr.io/nclsppr/surplasse/dashboard",
            "docs": "ghcr.io/nclsppr/surplasse/docs",
        },
        runtime_paths=SURPLASSE_PATHS,
        archive_directories=(
            "integration",
            "integration/caddy",
            "integration/grafana",
            "integration/prometheus",
            "integration/grafana/dashboards",
        ),
        contract_name="surplasse.vps-integration",
        migration_contract="surplasse.flyway-migrations",
        migration_strategy="dedicated",
        migration_runner="/opt/surplasse/scripts/backend-migrate.sh",
        probe_contract="surplasse.probes",
        default_public_host="surplasse.com",
        public_hosts=(
            "surplasse.com",
            "www.surplasse.com",
            "api.surplasse.com",
            "dashboard.surplasse.com",
            "docs.surplasse.com",
            "*.surplasse.com",
        ),
        runtime_configuration_keys=(
            "SURPLASSE_AUTH_JWT_KEY_ID",
            "SURPLASSE_SMTP_HOST",
        ),
        credential_files={
            name: f"/etc/vps/secrets/surplasse/{name.replace('_', '-')}"
            for name in (
                "surplasse_jwt_jwks",
                "surplasse_jwt_private_key",
                "surplasse_postgres_migrator_password",
                "surplasse_postgres_runtime_password",
                "surplasse_smtp_password",
                "surplasse_smtp_username",
                "surplasse_stripe_account_webhook_secret",
                "surplasse_stripe_payment_webhook_secret",
                "surplasse_stripe_secret_key",
            )
        },
        service_credentials={
            "backend": (
                "surplasse_jwt_jwks",
                "surplasse_jwt_private_key",
                "surplasse_postgres_runtime_password",
                "surplasse_smtp_password",
                "surplasse_smtp_username",
                "surplasse_stripe_account_webhook_secret",
                "surplasse_stripe_payment_webhook_secret",
                "surplasse_stripe_secret_key",
            ),
            "commande": (),
            "dashboard": (),
            "docs": (),
            "migrator": ("surplasse_postgres_migrator_password",),
            "onboarding": (),
            "pilot-bootstrap": (
                "surplasse_postgres_runtime_password",
                "surplasse_stripe_secret_key",
            ),
        },
        credential_gid=10001,
        image_version_prefix="",
    ),
    "parkventory": BundleProfile(
        application="parkventory",
        source_repository="nclsppr/parkventory",
        integration_repository="ghcr.io/nclsppr/parkventory/vps-integration",
        release_signer_workflow=(
            "nclsppr/parkventory/.github/workflows/application-release.yml"
        ),
        integration_signer_workflow=(
            "nclsppr/parkventory/.github/workflows/application-release.yml"
        ),
        component_signer_workflow=(
            "nclsppr/parkventory/.github/workflows/application-release.yml"
        ),
        component_repositories={
            "backend": "ghcr.io/nclsppr/parkventory/backend",
            "frontend": "ghcr.io/nclsppr/parkventory/frontend",
        },
        runtime_paths=PARKVENTORY_PATHS,
        archive_directories=(
            "integration",
            "integration/caddy",
            "integration/prometheus",
        ),
        contract_name="parkventory.vps-integration",
        migration_contract="parkventory.flyway-migrations",
        migration_strategy="dedicated",
        migration_runner="/opt/parkventory/bin/backend-migrate",
        probe_contract="parkventory.probes",
        default_public_host="parkventory.com",
        public_hosts=("parkventory.com", "www.parkventory.com"),
        runtime_configuration_keys=(
            "PARKVENTORY_DB_MIGRATOR_USER",
            "PARKVENTORY_DB_RUNTIME_USER",
            "PARKVENTORY_JDBC_URL",
            "PARKVENTORY_OIDC_AUTH_SERVER_URL",
            "PARKVENTORY_OIDC_CLIENT_ID",
            "PARKVENTORY_OIDC_ISSUER",
            "PARKVENTORY_SMTP_FROM",
            "PARKVENTORY_SMTP_HOST",
            "PARKVENTORY_SMTP_PORT",
            "PARKVENTORY_WEB_BASE_URL",
        ),
        credential_files={
            name: f"/etc/vps/secrets/parkventory/{name.replace('_', '-')}"
            for name in (
                "parkventory_postgres_migrator_password",
                "parkventory_postgres_runtime_password",
                "parkventory_oidc_client_secret",
                "parkventory_oidc_state_secret",
                "parkventory_oidc_token_encryption_secret",
                "parkventory_smtp_password",
                "parkventory_smtp_username",
            )
        },
        service_credentials={
            "backend": (
                "parkventory_postgres_runtime_password",
                "parkventory_oidc_client_secret",
                "parkventory_oidc_state_secret",
                "parkventory_oidc_token_encryption_secret",
                "parkventory_smtp_password",
                "parkventory_smtp_username",
            ),
            "frontend": (),
            "migrator": ("parkventory_postgres_migrator_password",),
        },
        credential_gid=10001,
        image_version_prefix="sha-",
    ),
    "monflorian": BundleProfile(
        application="monflorian",
        source_repository="nclsppr/monflorian",
        integration_repository="ghcr.io/nclsppr/monflorian/vps-integration",
        release_signer_workflow=(
            "nclsppr/monflorian/.github/workflows/vps-integration.yml"
        ),
        integration_signer_workflow=(
            "nclsppr/monflorian/.github/workflows/vps-integration.yml"
        ),
        component_signer_workflow=(
            "nclsppr/monflorian/.github/workflows/images.yml"
        ),
        component_repositories={
            "backend": "ghcr.io/nclsppr/monflorian/backend",
        },
        runtime_paths=MONFLORIAN_PATHS,
        archive_directories=("integration", "integration/caddy"),
        contract_name="monflorian.vps-integration",
        migration_contract="monflorian.migrations",
        migration_strategy="none",
        migration_runner=None,
        probe_contract="monflorian.probes",
        default_public_host="monflorian.com",
        public_hosts=("monflorian.com", "www.monflorian.com"),
        runtime_configuration_keys=(),
        credential_files={
            "monflorian_openai_api_key": (
                "/etc/vps/secrets/monflorian/monflorian-openai-api-key"
            )
        },
        service_credentials={
            "backend": ("monflorian_openai_api_key",),
        },
        credential_gid=10001,
        image_version_prefix="",
    ),
}

PARKVENTORY_PROMETHEUS_RULES = b"""groups:
  - name: parkventory
    rules:
      - alert: ParkventoryBackendUnavailable
        expr: >-
          up{application=\"parkventory\"} != 1
          or absent(up{application=\"parkventory\"})
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: Parkventory backend is unavailable
"""

PARKVENTORY_PROMETHEUS_TARGETS: object = [
    {
        "labels": {
            "__metrics_path__": "/q/metrics",
            "application": "parkventory",
            "environment": "production",
        },
        "targets": ["parkventory-backend:8080"],
    }
]


def _fail(path: str, message: str) -> None:
    raise ApplicationBundleError(f"{path}: {message}")


def _reject_constant(value: str) -> None:
    raise ApplicationBundleError(f"JSON constant {value!r} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationBundleError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def strict_json(raw: bytes, label: str, *, maximum: int) -> object:
    if not 1 <= len(raw) <= maximum:
        _fail(label, "size is outside the limit")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ApplicationBundleError(f"{label}: invalid strict UTF-8 JSON") from exc


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def content_digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _exact_keys(value: Mapping[str, object], keys: set[str], path: str) -> None:
    if set(value) != keys:
        _fail(path, "has missing or unknown fields")


def _literal(value: object, expected: object, path: str) -> None:
    if value != expected or type(value) is not type(expected):
        _fail(path, f"must equal {expected!r}")


def _digest(value: object, path: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail(path, "must be one lowercase sha256 digest")
    return value


def _size(value: object, path: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        _fail(path, "size is outside the limit")
    return value


def _created(value: object, path: str) -> str:
    if not isinstance(value, str) or RFC3339_RE.fullmatch(value) is None:
        _fail(path, "must be an RFC 3339 timestamp")
    normalized = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApplicationBundleError(f"{path}: invalid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(path, "must include a timezone")
    return value


def _safe_relative_path(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "must be a non-empty path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        _fail(path, "must be a safe relative path")
    return value


def validate_integration_manifest(
    raw: bytes,
    profile: BundleProfile,
    revision: str,
    expected_digest: str,
) -> IntegrationManifest:
    if SHA40_RE.fullmatch(revision) is None:
        _fail("integration manifest revision", "must be one full Git SHA")
    if content_digest(raw) != _digest(expected_digest, "integration manifest digest"):
        _fail("integration manifest", "bytes do not match the immutable reference")
    manifest = _object(
        strict_json(raw, "integration manifest", maximum=MAX_MANIFEST_BYTES),
        "integration manifest",
    )
    _exact_keys(
        manifest,
        {"schemaVersion", "mediaType", "artifactType", "config", "layers", "annotations"},
        "integration manifest",
    )
    _literal(manifest["schemaVersion"], 2, "integration manifest.schemaVersion")
    _literal(manifest["mediaType"], OCI_MANIFEST_MEDIA_TYPE, "integration manifest.mediaType")
    _literal(
        manifest["artifactType"],
        INTEGRATION_ARTIFACT_TYPE,
        "integration manifest.artifactType",
    )
    _literal(manifest["config"], OCI_EMPTY_CONFIG, "integration manifest.config")
    annotations = _object(manifest["annotations"], "integration manifest.annotations")
    expected_annotations = {
        SOURCE_ANNOTATION: f"https://github.com/{profile.source_repository}",
        REVISION_ANNOTATION: revision,
    }
    if set(annotations) != {*expected_annotations, CREATED_ANNOTATION}:
        _fail("integration manifest.annotations", "must match the exact allowlist")
    for key, expected in expected_annotations.items():
        _literal(annotations[key], expected, f"integration manifest.annotations.{key}")
    created = _created(
        annotations[CREATED_ANNOTATION],
        f"integration manifest.annotations.{CREATED_ANNOTATION}",
    )
    layers = manifest["layers"]
    if not isinstance(layers, list) or len(layers) != 2:
        _fail("integration manifest.layers", "must contain exactly two ordered layers")
    result: list[LayerDescriptor] = []
    expected_layers = (
        (ARCHIVE_TITLE, INTEGRATION_ARCHIVE_MEDIA_TYPE, MAX_ARCHIVE_BYTES),
        (INVENTORY_TITLE, INTEGRATION_INVENTORY_MEDIA_TYPE, MAX_INVENTORY_BYTES),
    )
    for index, (title, media_type, maximum) in enumerate(expected_layers):
        layer = _object(layers[index], f"integration manifest.layers[{index}]")
        _exact_keys(
            layer,
            {"mediaType", "digest", "size", "annotations"},
            f"integration manifest.layers[{index}]",
        )
        _literal(layer["mediaType"], media_type, f"integration manifest.layers[{index}].mediaType")
        _literal(
            layer["annotations"],
            {TITLE_ANNOTATION: title},
            f"integration manifest.layers[{index}].annotations",
        )
        result.append(
            LayerDescriptor(
                digest=_digest(layer["digest"], f"integration manifest.layers[{index}].digest"),
                size=_size(layer["size"], f"integration manifest.layers[{index}].size", maximum),
            )
        )
    return IntegrationManifest(archive=result[0], inventory=result[1], created=created)


def _validate_inventory(
    raw: bytes,
    profile: BundleProfile,
    revision: str,
) -> tuple[dict[str, Any], dict[str, tuple[int, str]]]:
    value = _object(
        strict_json(raw, "integration inventory", maximum=MAX_INVENTORY_BYTES),
        "integration inventory",
    )
    if raw != canonical_json(value):
        _fail("integration inventory", "must be canonical JSON")
    _exact_keys(value, {"schema", "contract", "source", "files"}, "integration inventory")
    _literal(value["schema"], 1, "integration inventory.schema")
    _literal(
        value["contract"],
        "vps-infra.application-integration.v1",
        "integration inventory.contract",
    )
    source = _object(value["source"], "integration inventory.source")
    _exact_keys(source, {"repository", "revision"}, "integration inventory.source")
    _literal(source["repository"], profile.source_repository, "integration inventory.source.repository")
    _literal(source["revision"], revision, "integration inventory.source.revision")
    files = value["files"]
    if not isinstance(files, list) or len(files) != len(profile.runtime_paths):
        _fail("integration inventory.files", "must match the exact file allowlist")
    result: dict[str, tuple[int, str]] = {}
    for index, expected_path in enumerate(profile.runtime_paths):
        entry = _object(files[index], f"integration inventory.files[{index}]")
        _exact_keys(entry, {"bytes", "path", "sha256"}, f"integration inventory.files[{index}]")
        _literal(entry["path"], expected_path, f"integration inventory.files[{index}].path")
        size = _size(entry["bytes"], f"integration inventory.files[{index}].bytes", MAX_FILE_BYTES)
        digest = entry["sha256"]
        if not isinstance(digest, str) or HEX_SHA256_RE.fullmatch(digest) is None:
            _fail(f"integration inventory.files[{index}].sha256", "must be 64 lowercase hexadecimal characters")
        result[expected_path] = (size, f"sha256:{digest}")
    return value, result


def _decompress(raw: bytes) -> bytes:
    if not 1 <= len(raw) <= MAX_ARCHIVE_BYTES:
        _fail("integration archive", "compressed size is outside the limit")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as archive:
            expanded = archive.read(MAX_EXPANDED_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise ApplicationBundleError("integration archive: invalid gzip data") from exc
    if len(expanded) > MAX_EXPANDED_BYTES:
        _fail("integration archive", "expanded size exceeds the limit")
    return expanded


def _archive_files(
    archive_raw: bytes,
    expected: Mapping[str, tuple[int, str]],
    profile: BundleProfile,
) -> dict[str, bytes]:
    tar_raw = _decompress(archive_raw)
    result: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_raw), mode="r:") as archive:
            members = archive.getmembers()
            expected_names = (
                *profile.archive_directories,
                *(f"integration/{path}" for path in profile.runtime_paths),
            )
            if tuple(member.name for member in members) != tuple(expected_names):
                _fail("integration archive", "member order or allowlist is invalid")
            for member in members:
                if member.pax_headers or member.issym() or member.islnk():
                    _fail("integration archive", "links and extended headers are forbidden")
                if member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                    _fail("integration archive", "ownership metadata is invalid")
                if member.name in profile.archive_directories:
                    if not member.isdir() or member.mode != 0o755 or member.mtime != 0:
                        _fail("integration archive", "directory metadata is invalid")
                    continue
                relative = member.name.removeprefix("integration/")
                _safe_relative_path(relative, "integration archive path")
                if not member.isfile() or member.type != tarfile.REGTYPE or member.mode != 0o644:
                    _fail("integration archive", "file type or mode is invalid")
                if member.mtime != 0:
                    _fail("integration archive", "file timestamp is invalid")
                expected_size, expected_digest = expected[relative]
                if member.size != expected_size:
                    _fail("integration archive", f"size differs for {relative}")
                source = archive.extractfile(member)
                if source is None:
                    _fail("integration archive", f"cannot read {relative}")
                content = source.read(MAX_FILE_BYTES + 1)
                if len(content) != expected_size or content_digest(content) != expected_digest:
                    _fail("integration archive", f"content differs for {relative}")
                if b"\0" in content:
                    _fail("integration archive", f"NUL byte is forbidden in {relative}")
                try:
                    content.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ApplicationBundleError(
                        f"integration archive: {relative} is not UTF-8 text"
                    ) from exc
                result[relative] = content
    except (tarfile.TarError, EOFError) as exc:
        raise ApplicationBundleError("integration archive: invalid tar data") from exc
    if tuple(sorted(result)) != profile.runtime_paths:
        _fail("integration archive", "extracted file allowlist is incomplete")
    if sum(len(content) for content in result.values()) > MAX_EXPANDED_BYTES:
        _fail("integration archive", "file total exceeds the limit")
    return result


def _expected_contract(profile: BundleProfile, revision: str) -> dict[str, object]:
    dedicated_migrations = profile.migration_strategy == "dedicated"
    common: dict[str, object] = {
        "application": profile.application,
        "compose_project": profile.application,
        "compose_file": "compose.yaml",
        "contract": profile.contract_name,
        "migration": (
            {
                "entrypoint": profile.migration_runner,
                "published_in_backend_image": True,
                "runtime_auto_migrate": False,
            }
            if dedicated_migrations
            else {"runtime_auto_migrate": False, "strategy": "none"}
        ),
        "networks": (
            [f"app_{profile.application}", f"db_{profile.application}"]
            if dedicated_migrations
            else [f"app_{profile.application}"]
        ),
        "public_hosts": list(profile.public_hosts),
        "runtime_services": sorted(profile.component_repositories),
        "schema": 1,
        "source_repository": profile.source_repository,
        "source_revision": revision,
        "transient_services": ["migrator"] if dedicated_migrations else [],
    }
    if profile.application == "surplasse":
        common.update(
            {
                "image_variables": {
                    "backend": "SURPLASSE_BACKEND_IMAGE",
                    "commande": "SURPLASSE_COMMANDE_IMAGE",
                    "dashboard": "SURPLASSE_DASHBOARD_IMAGE",
                    "docs": "SURPLASSE_DOCS_IMAGE",
                    "onboarding": "SURPLASSE_ONBOARDING_IMAGE",
                },
                "payment": dict(SURPLASSE_PAYMENT_PROFILE),
                "route_owner": "compose",
                "pilot_bootstrap": {
                    "apply_command": "apply",
                    "database_role": "surplasse_runtime",
                    "entrypoint": "/opt/surplasse/scripts/backend-pilot-bootstrap.sh",
                    "flyway_version": 15,
                    "initial_order_intake_status": "paused",
                    "manifest": {
                        "container_path": "/run/surplasse/pilot-bootstrap.json",
                        "group": 10001,
                        "host_path": (
                            "/etc/vps/applications/surplasse-pilot-bootstrap.json"
                        ),
                        "maximum_bytes": 16384,
                        "mode": "0440",
                        "owner": 0,
                        "schema": "pilot-bootstrap.schema.json",
                    },
                    "networks": ["app_surplasse", "db_surplasse"],
                    "payment_mode": "test",
                    "profile": "pilot-bootstrap",
                    "published_in_backend_image": True,
                    "secrets": [
                        "surplasse_postgres_runtime_password",
                        "surplasse_stripe_secret_key",
                    ],
                    "status_command": "status",
                },
                "secrets": [
                    name.replace("_", "-") for name in profile.credential_files
                ],
                "transient_services": ["migrator", "pilot-bootstrap"],
            }
        )
    elif profile.application == "parkventory":
        common.update(
            {
                "image_variables": {
                    "backend": "PARKVENTORY_BACKEND_IMAGE",
                    "frontend": "PARKVENTORY_FRONTEND_IMAGE",
                },
                "route_owner": "compose",
                "secrets": [
                    name.replace("_", "-") for name in profile.credential_files
                ],
            }
        )
    else:
        common.update(
            {
                "image_variables": {"backend": "MONFLORIAN_BACKEND_IMAGE"},
                "route_owner": "compose",
                "secrets": [
                    name.replace("_", "-") for name in profile.credential_files
                ],
            }
        )
    return common


def _validate_contract(raw: bytes, profile: BundleProfile, revision: str) -> dict[str, Any]:
    value = _object(strict_json(raw, "integration contract", maximum=MAX_FILE_BYTES), "integration contract")
    expected = _expected_contract(profile, revision)
    if raw != canonical_json(value) or raw != canonical_json(expected):
        _fail("integration contract", "differs from the exact canonical profile")
    return value


def _validate_expected_images(
    raw: bytes,
    profile: BundleProfile,
    revision: str,
    components: Mapping[str, str],
) -> None:
    value = _object(strict_json(raw, "expected image inventory", maximum=MAX_FILE_BYTES), "expected image inventory")
    expected_images = dict(components)
    if profile.migration_strategy == "dedicated":
        expected_images["migrator"] = components["backend"]
    if profile.application == "surplasse":
        expected_images["pilot-bootstrap"] = components["backend"]
    expected = {"images": expected_images, "schema": 1, "source_revision": revision}
    if raw != canonical_json(value) or value != expected:
        _fail("expected image inventory", "does not match the release components")


def _validate_pilot_bootstrap_schema(raw: bytes) -> None:
    value = strict_json(
        raw,
        "pilot bootstrap schema",
        maximum=16 * 1024,
    )
    if (
        not isinstance(value, dict)
        or content_digest(canonical_json(value))
        != SURPLASSE_PILOT_SCHEMA_CANONICAL_SHA256
    ):
        _fail(
            "pilot bootstrap schema",
            "differs from the exact canonical policy",
        )


def _validate_surplasse_pilot_source_compose(raw: bytes) -> None:
    try:
        compose = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ApplicationBundleError(
            "integration compose: pilot bootstrap source is not UTF-8"
        ) from exc
    marker = "\n  pilot-bootstrap:\n"
    if compose.count(marker) != 1:
        _fail("integration compose", "pilot bootstrap service is not unique")
    block = compose.split(marker, 1)[1].split("\nnetworks:\n", 1)[0]
    required_fragments = (
        "    profiles:\n      - pilot-bootstrap\n",
        "    entrypoint:\n      - /opt/surplasse/scripts/backend-pilot-bootstrap.sh\n",
        "    image: ${SURPLASSE_BACKEND_IMAGE:?SURPLASSE_BACKEND_IMAGE is required}\n",
        "      - type: bind\n"
        "        source: /etc/vps/applications/surplasse-pilot-bootstrap.json\n"
        "        target: /run/surplasse/pilot-bootstrap.json\n"
        "        read_only: true\n"
        "        bind:\n"
        "          create_host_path: false\n",
    )
    if any(block.count(fragment) != 1 for fragment in required_fragments):
        _fail(
            "integration compose",
            "pilot bootstrap source contract differs from the exact policy",
        )


def _validate_migrations(raw: bytes, profile: BundleProfile, revision: str) -> dict[str, Any]:
    value = _object(strict_json(raw, "migration inventory", maximum=MAX_FILE_BYTES), "migration inventory")
    if raw != canonical_json(value):
        _fail("migration inventory", "must be canonical JSON")
    if profile.migration_strategy == "none":
        expected = {
            "contract": profile.migration_contract,
            "migrations": [],
            "runtime_auto_migrate": False,
            "schema": 1,
            "source_repository": profile.source_repository,
            "source_revision": revision,
            "strategy": "none",
        }
        if value != expected:
            _fail("migration inventory", "differs from the exact no-migration policy")
        return value
    expected_keys = {
        "contract",
        "database",
        "migrations",
        "runtime_auto_migrate",
        "schema",
        "source_repository",
        "source_revision",
    }
    if profile.application == "surplasse":
        expected_keys.add("runner")
    _exact_keys(value, expected_keys, "migration inventory")
    literals: dict[str, object] = {
        "contract": profile.migration_contract,
        "database": profile.application,
        "runtime_auto_migrate": False,
        "schema": 1,
        "source_repository": profile.source_repository,
        "source_revision": revision,
    }
    if profile.application == "surplasse":
        literals["runner"] = profile.migration_runner
    for key, expected in literals.items():
        _literal(value[key], expected, f"migration inventory.{key}")
    migrations = value["migrations"]
    if not isinstance(migrations, list) or not migrations:
        _fail("migration inventory.migrations", "must contain at least one migration")
    for expected_version, item in enumerate(migrations, start=1):
        entry = _object(item, f"migration inventory.migrations[{expected_version - 1}]")
        _exact_keys(entry, {"version", "path", "sha256"}, "migration inventory entry")
        _literal(entry["version"], expected_version, "migration inventory entry.version")
        path = _safe_relative_path(entry["path"], "migration inventory entry.path")
        match = MIGRATION_NAME_RE.fullmatch(PurePosixPath(path).name)
        if match is None or int(match.group(1)) != expected_version:
            _fail("migration inventory entry.path", "versioned SQL name is invalid")
        digest = entry["sha256"]
        if not isinstance(digest, str) or HEX_SHA256_RE.fullmatch(digest) is None:
            _fail("migration inventory entry.sha256", "must be 64 lowercase hexadecimal characters")
    return value


def _validate_probe_url(
    value: object,
    profile: BundleProfile,
    service: str,
    path: str,
) -> None:
    if not isinstance(value, str) or len(value) > 512:
        _fail(path, "must be one bounded URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != f"{profile.application}-{service}"
        or parsed.port != 8080
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        _fail(path, "must target the exact internal service alias on port 8080")


def _expected_probes(
    profile: BundleProfile,
    revision: str | None = None,
) -> dict[str, object]:
    if profile.application == "surplasse":
        return {
            "contract": "surplasse.probes",
            "internal": [
                {
                    "body_contains": "UP",
                    "service": "backend",
                    "status": 200,
                    "url": "http://surplasse-backend:8080/q/health/ready",
                },
                {
                    "body_contains": "ready",
                    "service": "onboarding",
                    "status": 200,
                    "url": "http://surplasse-onboarding:8080/__health",
                },
                {
                    "body_contains": "ready",
                    "service": "commande",
                    "status": 200,
                    "url": "http://surplasse-commande:8080/healthz",
                },
                {
                    "body_contains": "ready",
                    "service": "dashboard",
                    "status": 200,
                    "url": "http://surplasse-dashboard:8080/healthz",
                },
                {
                    "body_contains": "ready",
                    "service": "docs",
                    "status": 200,
                    "url": "http://surplasse-docs:8080/healthz",
                },
            ],
            "public": [
                {
                    "body_contains": "surplasse-edge-v1",
                    "host": "surplasse.com",
                    "path": "/.well-known/surplasse-edge",
                    "status": 200,
                },
                {"host": "surplasse.com", "path": "/", "status": 200},
                {
                    "host": "dashboard.surplasse.com",
                    "path": "/",
                    "status": 200,
                },
                {"host": "docs.surplasse.com", "path": "/", "status": 200},
                {"host": "probe.surplasse.com", "path": "/", "status": 200},
                {
                    "host": "api.surplasse.com",
                    "path": "/q/health/ready",
                    "status": 404,
                },
            ],
            "schema": 1,
        }
    if profile.application == "parkventory":
        return {
            "contract": "parkventory.probes",
            "internal": [
                {
                    "body_contains": "UP",
                    "service": "backend",
                    "status": 200,
                    "url": "http://parkventory-backend:8080/q/health/ready",
                },
                {
                    "body_contains": "parkventory-frontend-v1",
                    "service": "frontend",
                    "status": 200,
                    "url": "http://parkventory-frontend:8080/__health",
                },
            ],
            "public": [
                {"body_contains": "Parkventory", "path": "/", "status": 200},
                {
                    "body_contains": "Parkventory",
                    "path": "/app",
                    "status": 200,
                },
                {
                    "body_contains": "parkventory-compose-v1",
                    "path": "/.well-known/parkventory-release",
                    "status": 200,
                },
            ],
            "schema": 1,
        }
    if revision is None or SHA40_RE.fullmatch(revision) is None:
        _fail("probe inventory", "Mon Florian probes require one source revision")
    return {
        "contract": "monflorian.probes",
        "internal": [
            {
                "body_contains": '"status":"ok"',
                "service": "backend",
                "status": 200,
                "url": "http://monflorian-backend:8080/api/health",
            }
        ],
        "public": [
            {
                "body_contains": revision,
                "host": "monflorian.com",
                "path": "/.well-known/monflorian-release",
                "status": 200,
            },
            {"host": "monflorian.com", "path": "/", "status": 200},
            {
                "body_contains": '"serviceReady":false',
                "host": "monflorian.com",
                "path": "/api/config",
                "status": 200,
            },
            {"host": "www.monflorian.com", "path": "/", "status": 308},
        ],
        "schema": 1,
    }


def _validate_probes(
    raw: bytes,
    profile: BundleProfile,
    revision: str,
) -> dict[str, Any]:
    value = _object(strict_json(raw, "probe inventory", maximum=MAX_FILE_BYTES), "probe inventory")
    if raw != canonical_json(value):
        _fail("probe inventory", "must be canonical JSON")
    _exact_keys(value, {"contract", "internal", "public", "schema"}, "probe inventory")
    _literal(value["contract"], profile.probe_contract, "probe inventory.contract")
    _literal(value["schema"], 1, "probe inventory.schema")
    internal = value["internal"]
    public = value["public"]
    if not isinstance(internal, list) or not internal:
        _fail("probe inventory.internal", "must be a non-empty list")
    if not isinstance(public, list) or not public:
        _fail("probe inventory.public", "must be a non-empty list")
    seen_services: set[str] = set()
    for index, item in enumerate(internal):
        probe = _object(item, f"probe inventory.internal[{index}]")
        allowed = {"service", "url", "status", "body_contains"}
        if set(probe) not in ({"service", "url", "status"}, allowed):
            _fail(f"probe inventory.internal[{index}]", "fields are invalid")
        service = probe["service"]
        if not isinstance(service, str) or service not in profile.component_repositories:
            _fail(f"probe inventory.internal[{index}].service", "is outside the service allowlist")
        if service in seen_services:
            _fail("probe inventory.internal", "contains a duplicate service")
        seen_services.add(service)
        _validate_probe_url(probe["url"], profile, service, f"probe inventory.internal[{index}].url")
        if type(probe["status"]) is not int or not 100 <= probe["status"] <= 599:
            _fail(f"probe inventory.internal[{index}].status", "is invalid")
        if "body_contains" in probe and (
            not isinstance(probe["body_contains"], str)
            or not 1 <= len(probe["body_contains"].encode("utf-8")) <= 256
        ):
            _fail(f"probe inventory.internal[{index}].body_contains", "is invalid")
    if seen_services != set(profile.component_repositories):
        _fail("probe inventory.internal", "must cover every runtime service exactly once")
    seen_public: set[tuple[str, str]] = set()
    for index, item in enumerate(public):
        probe = _object(item, f"probe inventory.public[{index}]")
        allowed = {"host", "path", "status", "body_contains"}
        required = {"path", "status"}
        if not required.issubset(probe) or set(probe) - allowed:
            _fail(f"probe inventory.public[{index}]", "fields are invalid")
        host = probe.get("host", profile.default_public_host)
        host_allowed = isinstance(host, str) and (
            host in profile.public_hosts
            or any(
                allowed.startswith("*.") and host.endswith(allowed[1:])
                for allowed in profile.public_hosts
            )
        )
        if not host_allowed:
            _fail(f"probe inventory.public[{index}].host", "is outside the host allowlist")
        route = probe["path"]
        if (
            not isinstance(route, str)
            or not route.startswith("/")
            or "?" in route
            or "#" in route
            or "//" in route
            or len(route) > 512
        ):
            _fail(f"probe inventory.public[{index}].path", "is invalid")
        identity = (host, route)
        if identity in seen_public:
            _fail("probe inventory.public", "contains a duplicate host and path")
        seen_public.add(identity)
        if type(probe["status"]) is not int or not 100 <= probe["status"] <= 599:
            _fail(f"probe inventory.public[{index}].status", "is invalid")
        if "body_contains" in probe and (
            not isinstance(probe["body_contains"], str)
            or not 1 <= len(probe["body_contains"].encode("utf-8")) <= 256
        ):
            _fail(f"probe inventory.public[{index}].body_contains", "is invalid")
    if value != _expected_probes(profile, revision):
        _fail("probe inventory", "differs from the exact application profile")
    return value


def _validate_parkventory_observability(files: Mapping[str, bytes]) -> None:
    rules = files["prometheus/rules.yml"]
    if rules != PARKVENTORY_PROMETHEUS_RULES:
        _fail(
            "Parkventory Prometheus rules",
            "must contain the exact backend-unavailable alert",
        )
    targets_raw = files["prometheus/targets.json"]
    targets = strict_json(
        targets_raw,
        "Parkventory Prometheus targets",
        maximum=MAX_FILE_BYTES,
    )
    if targets != PARKVENTORY_PROMETHEUS_TARGETS:
        _fail(
            "Parkventory Prometheus targets",
            "must scrape the exact backend metrics endpoint",
        )


def validate_bundle(
    archive_raw: bytes,
    inventory_raw: bytes,
    *,
    profile: BundleProfile,
    revision: str,
    created: str,
    component_references: Mapping[str, str],
    migration_inventory_digest: str,
    probe_inventory_digest: str,
) -> ApplicationBundle:
    if set(component_references) != set(profile.component_repositories):
        _fail("application release components", "must match the bundle profile")
    for name, repository in profile.component_repositories.items():
        reference = component_references[name]
        prefix = f"{repository}@"
        if not isinstance(reference, str) or not reference.startswith(prefix) or SHA256_RE.fullmatch(reference.removeprefix(prefix)) is None:
            _fail(f"application release components.{name}", "must be an immutable allowlisted reference")
    _, expected = _validate_inventory(inventory_raw, profile, revision)
    files = _archive_files(archive_raw, expected, profile)
    contract = _validate_contract(files["contract.json"], profile, revision)
    migrations = _validate_migrations(files["migrations.json"], profile, revision)
    probes = _validate_probes(files["probes.json"], profile, revision)
    if profile.application == "parkventory":
        _validate_parkventory_observability(files)
    if content_digest(files["migrations.json"]) != _digest(
        migration_inventory_digest, "application release migration digest"
    ):
        _fail("migration inventory", "digest does not match the application release")
    if content_digest(files["probes.json"]) != _digest(
        probe_inventory_digest, "application release probe digest"
    ):
        _fail("probe inventory", "digest does not match the application release")
    if "expected-images.json" in files:
        _validate_expected_images(
            files["expected-images.json"],
            profile,
            revision,
            component_references,
        )
    if profile.application == "surplasse":
        _validate_pilot_bootstrap_schema(files["pilot-bootstrap.schema.json"])
        _validate_surplasse_pilot_source_compose(files["compose.yaml"])
    compose = files["compose.yaml"].decode("utf-8")
    if profile.application == "monflorian":
        required_preview_settings = (
            '      MONFLORIAN_ACCESS_MODE: public\n',
            '      MONFLORIAN_GENERATION_ENABLED: "false"\n',
            '      MONFLORIAN_ILLUSTRATION_ENABLED: "false"\n',
        )
        if any(setting not in compose for setting in required_preview_settings):
            _fail(
                "Mon Florian integration compose",
                "must keep the exact public preview feature gates",
            )
    if not (
        compose.startswith(f"---\nname: {profile.application}\n")
        or compose.startswith(f"name: {profile.application}\n")
    ):
        _fail("integration compose", "must declare the exact project name")
    return ApplicationBundle(
        application=profile.application,
        source_revision=revision,
        created=created,
        files=files,
        contract=contract,
        migrations=migrations,
        probes=probes,
    )


def validate_component_index(
    raw: bytes,
    *,
    profile: BundleProfile,
    component: str,
    revision: str,
    expected_digest: str,
) -> ComponentIndex:
    if component not in profile.component_repositories:
        _fail("component image", "is outside the allowlist")
    if SHA40_RE.fullmatch(revision) is None:
        _fail("component image revision", "must be one full Git SHA")
    if content_digest(raw) != _digest(expected_digest, "component index digest"):
        _fail("component index", "bytes do not match the immutable reference")
    index = _object(
        strict_json(raw, "component index", maximum=MAX_COMPONENT_MANIFEST_BYTES),
        "component index",
    )
    if set(index) not in (
        {"schemaVersion", "mediaType", "manifests"},
        {"schemaVersion", "mediaType", "manifests", "annotations"},
    ):
        _fail("component index", "fields are invalid")
    _literal(index["schemaVersion"], 2, "component index.schemaVersion")
    _literal(index["mediaType"], OCI_INDEX_MEDIA_TYPE, "component index.mediaType")
    manifests = index["manifests"]
    if not isinstance(manifests, list) or len(manifests) != 2:
        _fail("component index.manifests", "must contain one runtime and one attestation manifest")
    runtime: LayerDescriptor | None = None
    attestation: LayerDescriptor | None = None
    for item in manifests:
        descriptor = _object(item, "component index descriptor")
        allowed = {"mediaType", "digest", "size", "platform", "annotations"}
        if not {"mediaType", "digest", "size", "platform"}.issubset(descriptor) or set(descriptor) - allowed:
            _fail("component index descriptor", "fields are invalid")
        _literal(descriptor["mediaType"], OCI_MANIFEST_MEDIA_TYPE, "component index descriptor.mediaType")
        layer = LayerDescriptor(
            digest=_digest(descriptor["digest"], "component index descriptor.digest"),
            size=_size(descriptor["size"], "component index descriptor.size", MAX_COMPONENT_MANIFEST_BYTES),
        )
        platform = _object(descriptor["platform"], "component index descriptor.platform")
        if platform == {"architecture": "amd64", "os": "linux"}:
            if runtime is not None or "annotations" in descriptor:
                _fail("component index", "runtime descriptor is ambiguous")
            runtime = layer
        elif platform == {"architecture": "unknown", "os": "unknown"}:
            annotations = _object(descriptor.get("annotations"), "component attestation descriptor.annotations")
            _exact_keys(
                annotations,
                {"vnd.docker.reference.digest", "vnd.docker.reference.type"},
                "component attestation descriptor.annotations",
            )
            _literal(
                annotations["vnd.docker.reference.type"],
                "attestation-manifest",
                "component attestation descriptor reference type",
            )
            attestation = layer
        else:
            _fail("component index descriptor.platform", "is outside linux/amd64 plus attestation")
    if runtime is None or attestation is None:
        _fail("component index", "runtime or attestation descriptor is missing")
    attestation_descriptor = next(
        item
        for item in manifests
        if item["platform"] == {"architecture": "unknown", "os": "unknown"}
    )
    _literal(
        attestation_descriptor["annotations"]["vnd.docker.reference.digest"],
        runtime.digest,
        "component attestation descriptor reference digest",
    )
    return ComponentIndex(runtime_manifest=runtime, attestation_manifest=attestation)


def validate_runtime_manifest(
    raw: bytes,
    *,
    expected_digest: str,
    expected_size: int,
) -> LayerDescriptor:
    if len(raw) != expected_size or content_digest(raw) != _digest(expected_digest, "runtime manifest digest"):
        _fail("runtime image manifest", "bytes do not match the index descriptor")
    manifest = _object(
        strict_json(raw, "runtime image manifest", maximum=MAX_COMPONENT_MANIFEST_BYTES),
        "runtime image manifest",
    )
    if set(manifest) not in (
        {"schemaVersion", "mediaType", "config", "layers"},
        {"schemaVersion", "mediaType", "config", "layers", "annotations"},
    ):
        _fail("runtime image manifest", "fields are invalid")
    _literal(manifest["schemaVersion"], 2, "runtime image manifest.schemaVersion")
    _literal(manifest["mediaType"], OCI_MANIFEST_MEDIA_TYPE, "runtime image manifest.mediaType")
    config = _object(manifest["config"], "runtime image manifest.config")
    if not {"mediaType", "digest", "size"}.issubset(config) or set(config) - {"mediaType", "digest", "size", "annotations"}:
        _fail("runtime image manifest.config", "fields are invalid")
    _literal(config["mediaType"], OCI_CONFIG_MEDIA_TYPE, "runtime image manifest.config.mediaType")
    layer = LayerDescriptor(
        digest=_digest(config["digest"], "runtime image manifest.config.digest"),
        size=_size(config["size"], "runtime image manifest.config.size", MAX_IMAGE_CONFIG_BYTES),
    )
    layers = manifest["layers"]
    if (
        not isinstance(layers, list)
        or not 1 <= len(layers) <= MAX_RUNTIME_LAYER_COUNT
    ):
        _fail("runtime image manifest.layers", "count is outside the limit")
    total_size = 0
    for index, item in enumerate(layers):
        descriptor = _object(item, f"runtime image manifest.layers[{index}]")
        if not {"mediaType", "digest", "size"}.issubset(descriptor) or set(descriptor) - {"mediaType", "digest", "size", "annotations"}:
            _fail(f"runtime image manifest.layers[{index}]", "fields are invalid")
        _digest(descriptor["digest"], f"runtime image manifest.layers[{index}].digest")
        total_size += _size(
            descriptor["size"],
            f"runtime image manifest.layers[{index}].size",
            2 * 1024 * MIB,
        )
        if total_size > MAX_RUNTIME_COMPRESSED_BYTES:
            _fail(
                "runtime image manifest.layers",
                "total compressed size is outside the limit",
            )
    return layer


def validate_image_config(
    raw: bytes,
    *,
    profile: BundleProfile,
    revision: str,
    expected_digest: str,
    expected_size: int,
) -> None:
    if len(raw) != expected_size or content_digest(raw) != _digest(expected_digest, "image config digest"):
        _fail("image config", "bytes do not match the runtime manifest")
    value = _object(
        strict_json(raw, "image config", maximum=MAX_IMAGE_CONFIG_BYTES),
        "image config",
    )
    _literal(value.get("architecture"), "amd64", "image config.architecture")
    _literal(value.get("os"), "linux", "image config.os")
    config = _object(value.get("config"), "image config.config")
    labels = _object(config.get("Labels"), "image config.config.Labels")
    expected_labels = {
        "org.opencontainers.image.source": f"https://github.com/{profile.source_repository}",
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": f"{profile.image_version_prefix}{revision}",
    }
    for key, expected in expected_labels.items():
        _literal(labels.get(key), expected, f"image config.config.Labels.{key}")


def materialize_files(bundle: ApplicationBundle, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        _fail("bundle destination", "must not exist")
    destination.mkdir(mode=0o700)
    for relative, content in sorted(bundle.files.items()):
        pure = PurePosixPath(_safe_relative_path(relative, "bundle file path"))
        target = destination.joinpath(*pure.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o444,
            )
            try:
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        _fail("bundle destination", "short write while materializing")
                    view = view[written:]
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ApplicationBundleError(f"cannot create bundle file {target}: {exc}") from exc
    for current, directories, _files in os.walk(destination, topdown=False):
        for name in directories:
            descriptor = os.open(
                Path(current) / name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    descriptor = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
