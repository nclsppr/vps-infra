#!/usr/bin/env python3
"""Dependency-free validation for the production desired-state manifest."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REF_RE = re.compile(
    r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@sha256:[0-9a-f]{64}$"
)
BLOCK_REASON_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
EVIDENCE_WORKFLOW_PATH = ".github/workflows/vps-release.yml"
PLATFORM_CANDIDATE_CONTRACT = "vps-infra.platform-candidate.v1"
PLATFORM_PROOF_ARTIFACT_PREFIX = "platform-candidate-proof-"
PLATFORM_PROOF_BASELINE_GATES = frozenset(
    {"caddy-ovh-image", "immutable-image-digests"}
)

EXPECTED_APPLICATIONS = {
    "personal": ("static", "nclsppr/personal", "main", None),
    "papersempire": ("static", "nclsppr/papersempire", "master", None),
    "surplasse": ("compose", "nclsppr/surplasse", "main", "surplasse"),
    "parkventory": ("compose", "nclsppr/parkventory", "main", "parkventory"),
}

PLATFORM_IMAGE_REPOSITORIES = {
    "caddy": "ghcr.io/nclsppr/vps-infra/caddy",
    "postgres": "docker.io/library/postgres",
    "prometheus": "docker.io/prom/prometheus",
    "grafana": "docker.io/grafana/grafana",
    "node_exporter": "docker.io/prom/node-exporter",
    "postgres_exporter": "docker.io/prometheuscommunity/postgres-exporter",
}
PLATFORM_INTEGRATION_REPOSITORY = "ghcr.io/nclsppr/vps-infra/platform-integration"
PLATFORM_CANDIDATE_KEYS = frozenset(
    {"images", "integration", "postgres", "readiness_evidence"}
)

STATIC_ARTIFACT_REPOSITORIES = {
    "personal": (
        "ghcr.io/nclsppr/personal/site",
        "ghcr.io/nclsppr/personal/routes",
    ),
    "papersempire": (
        "ghcr.io/nclsppr/papersempire/site",
        "ghcr.io/nclsppr/papersempire/routes",
    ),
}

COMPOSE_COMPONENT_REPOSITORIES = {
    "surplasse": {
        "backend": "ghcr.io/nclsppr/surplasse/backend",
        "onboarding": "ghcr.io/nclsppr/surplasse/onboarding",
        "commande": "ghcr.io/nclsppr/surplasse/commande",
        "dashboard": "ghcr.io/nclsppr/surplasse/dashboard",
        "docs": "ghcr.io/nclsppr/surplasse/docs",
    },
    "parkventory": {
        "backend": "ghcr.io/nclsppr/parkventory/backend",
        "frontend": "ghcr.io/nclsppr/parkventory/frontend",
    },
}

INTEGRATION_REPOSITORIES = {
    "surplasse": "ghcr.io/nclsppr/surplasse/vps-integration",
    "parkventory": "ghcr.io/nclsppr/parkventory/vps-integration",
}

SURPLASSE_READINESS_GATES = frozenset(
    {
        "platform-extraction",
        "postgres-compatibility",
        "postgres-role-provisioning",
        "production-digests",
        "protected-main",
        "public-smoke",
        "restore-proof",
        "separated-migrations",
        "stripe-connect-production-adapter",
        "vps-integration-bundle",
    }
)

PARKVENTORY_READINESS_GATES = frozenset(
    {
        "domain-and-dns",
        "file-based-secrets",
        "postgres-adr-alignment",
        "postgres-compatibility",
        "postgres-role-provisioning",
        "production-images",
        "production-oidc-provider",
        "production-secrets",
        "prometheus-metrics",
        "protected-main",
        "public-smoke",
        "restore-proof",
        "separated-migrations",
        "structured-logs",
        "tenant-isolation-and-rls",
        "vps-integration-bundle",
    }
)

PLATFORM_READINESS_GATES = frozenset(
    {
        "alert-routing-and-delivery",
        "caddy-ovh-image",
        "external-networks-and-cidrs",
        "immutable-image-digests",
        "ovh-dns-credentials",
        "platform-config-and-probes",
        "platform-secrets-and-permissions",
        "postgres-pgdata-and-compatibility",
    }
)

STATIC_READINESS_GATES = frozenset(
    {
        "protected-source-branch",
        "public-smoke",
        "route-inventory",
        "static-artifact",
    }
)

ALLOWED_PLATFORM_PORTS = frozenset(
    {"80/tcp", "443/tcp", "443/udp", "127.0.0.1:3000/tcp"}
)


class PolicyError(ValueError):
    """Raised when a release is structurally invalid or unsafe."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PolicyError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def strict_json_loads(data: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise PolicyError(
            f"JSON:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc


def _fail(path: str, message: str) -> None:
    raise PolicyError(f"{path}: {message}")


def _expect_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _expect_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _expect_exact_keys(
    value: dict[str, Any],
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        _fail(path, f"missing keys: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown keys: {', '.join(sorted(unknown))}")


def _expect_literal(value: Any, expected: Any, path: str) -> None:
    if value != expected or type(value) is not type(expected):
        _fail(path, f"must equal {expected!r}")


def _validate_sha(value: Any, path: str) -> str:
    if not isinstance(value, str) or not SHA40_RE.fullmatch(value):
        _fail(path, "must be a full lowercase 40-character Git SHA")
    return value


def _image_repository(reference: str) -> str:
    before_digest = reference.rsplit("@", 1)[0]
    last_segment = before_digest.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return before_digest.rsplit(":", 1)[0]
    return before_digest


def _image_tag(reference: str) -> str | None:
    before_digest = reference.rsplit("@", 1)[0]
    last_segment = before_digest.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    return last_segment.rsplit(":", 1)[1]


def _validate_image_reference(value: Any, expected_repository: str, path: str) -> str:
    if not isinstance(value, str) or not IMAGE_REF_RE.fullmatch(value):
        _fail(path, "must be an immutable registry reference ending in @sha256:<64 lowercase hex>")
    actual_repository = _image_repository(value)
    if actual_repository != expected_repository:
        _fail(path, f"must use repository {expected_repository!r}, got {actual_repository!r}")
    return value


def _validate_blocked_by(value: Any, path: str, *, required_nonempty: bool) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail(path, "must be an array of strings")
    if len(value) != len(set(value)):
        _fail(path, "must not contain duplicates")
    for item in value:
        if not BLOCK_REASON_RE.fullmatch(item):
            _fail(path, f"invalid blocker name {item!r}")
    if required_nonempty and not value:
        _fail(path, "must contain at least one blocker while disabled")
    if not required_nonempty and value:
        _fail(path, "must be empty while enabled")
    return value


def _validate_ports(value: Any, path: str, *, platform: bool, enabled: bool) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail(path, "must be an array of strings")
    if len(value) != len(set(value)):
        _fail(path, "must not contain duplicates")
    if not platform and value:
        _fail(path, "applications may not publish host ports")
    if platform:
        unknown = set(value) - ALLOWED_PLATFORM_PORTS
        if unknown:
            _fail(path, f"ports outside the allowlist: {', '.join(sorted(unknown))}")
        if enabled and not {"80/tcp", "443/tcp"}.issubset(value):
            _fail(path, "enabled platform must publish Caddy 80/tcp and 443/tcp")
        if not enabled and value:
            _fail(path, "disabled platform may not publish host ports")


def _validate_evidence(
    value: Any,
    path: str,
    repository: str,
    allowed_revisions: set[str] | None,
    platform_subject: str | None = None,
) -> None:
    evidence = _expect_dict(value, path)
    required = {
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow_path",
        "conclusion",
        "url",
    }
    proof_keys: set[str] = set()
    if platform_subject is not None:
        proof_keys = {
            "subject_sha256",
            "artifact_name",
            "artifact_digest",
            "artifact_gates",
            "artifact_id",
        }
    _expect_exact_keys(evidence, path, required, proof_keys)
    revision = _validate_sha(evidence["source_revision"], f"{path}.source_revision")
    if allowed_revisions is not None and revision not in allowed_revisions:
        _fail(f"{path}.source_revision", "must match a declared component or integration revision")
    run_id = evidence["run_id"]
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        _fail(f"{path}.run_id", "must be a positive immutable GitHub Actions run id")
    run_attempt = evidence["run_attempt"]
    if not isinstance(run_attempt, int) or isinstance(run_attempt, bool) or run_attempt < 1:
        _fail(f"{path}.run_attempt", "must be a positive GitHub Actions run attempt")
    _expect_literal(
        evidence["workflow_path"],
        EVIDENCE_WORKFLOW_PATH,
        f"{path}.workflow_path",
    )
    _expect_literal(evidence["conclusion"], "success", f"{path}.conclusion")
    expected_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    _expect_literal(evidence["url"], expected_url, f"{path}.url")
    declared_proof_keys = proof_keys & evidence.keys()
    if declared_proof_keys and declared_proof_keys != proof_keys:
        missing = proof_keys - declared_proof_keys
        _fail(path, f"platform proof fields are incomplete: missing {', '.join(sorted(missing))}")
    if platform_subject is not None and declared_proof_keys:
        _expect_literal(
            evidence["subject_sha256"],
            platform_subject,
            f"{path}.subject_sha256",
        )
        artifact_gates = evidence["artifact_gates"]
        if (
            not isinstance(artifact_gates, list)
            or not artifact_gates
            or any(not isinstance(gate, str) for gate in artifact_gates)
        ):
            _fail(f"{path}.artifact_gates", "must contain readiness gate names")
        if artifact_gates != sorted(set(artifact_gates)):
            _fail(f"{path}.artifact_gates", "must be sorted and unique")
        if set(artifact_gates) != PLATFORM_PROOF_BASELINE_GATES:
            _fail(
                f"{path}.artifact_gates",
                "must contain exactly the digest-bound baseline gates",
            )
        expected_artifact_name = (
            f"{PLATFORM_PROOF_ARTIFACT_PREFIX}{platform_subject.removeprefix('sha256:')}"
            f"-{run_id}-{run_attempt}.json"
        )
        _expect_literal(
            evidence["artifact_name"],
            expected_artifact_name,
            f"{path}.artifact_name",
        )
        artifact_digest = evidence["artifact_digest"]
        if not isinstance(artifact_digest, str) or not DIGEST_RE.fullmatch(artifact_digest):
            _fail(f"{path}.artifact_digest", "must be a lowercase sha256 digest")
        artifact_id = evidence["artifact_id"]
        if not isinstance(artifact_id, str) or not RUN_ID_RE.fullmatch(artifact_id):
            _fail(f"{path}.artifact_id", "must be a positive immutable artifact id")


def _validate_migrations(
    value: Any,
    path: str,
    *,
    enabled: bool,
    repository: str,
    allowed_revisions: set[str],
) -> None:
    migrations = _expect_dict(value, path)
    required = {"strategy", "runtime_auto_migrate", "proven"}
    optional = {"evidence"} if enabled else set()
    _expect_exact_keys(migrations, path, required, optional)
    _expect_literal(migrations["runtime_auto_migrate"], False, f"{path}.runtime_auto_migrate")
    if enabled:
        _expect_literal(migrations["strategy"], "dedicated", f"{path}.strategy")
        _expect_literal(migrations["proven"], True, f"{path}.proven")
        if "evidence" not in migrations:
            _fail(path, "enabled application requires immutable migration evidence")
        _validate_evidence(
            migrations["evidence"],
            f"{path}.evidence",
            repository,
            allowed_revisions,
        )
    else:
        _expect_literal(migrations["strategy"], "blocked", f"{path}.strategy")
        _expect_literal(migrations["proven"], False, f"{path}.proven")


def _validate_platform_candidate_fields(value: Any, path: str) -> str:
    candidate = _expect_dict(value, path)
    _expect_exact_keys(
        candidate,
        path,
        {"contract", "compose_project", "images", "integration", "postgres"},
    )
    _expect_literal(candidate["contract"], PLATFORM_CANDIDATE_CONTRACT, f"{path}.contract")
    _expect_literal(candidate["compose_project"], "vps-platform", f"{path}.compose_project")
    images = _expect_dict(candidate["images"], f"{path}.images")
    _expect_exact_keys(images, f"{path}.images", set(PLATFORM_IMAGE_REPOSITORIES))
    for service, repository in PLATFORM_IMAGE_REPOSITORIES.items():
        _validate_image_reference(images[service], repository, f"{path}.images.{service}")

    integration = _expect_dict(candidate["integration"], f"{path}.integration")
    _expect_exact_keys(integration, f"{path}.integration", {"source_revision", "artifact"})
    integration_revision = _validate_sha(
        integration["source_revision"],
        f"{path}.integration.source_revision",
    )
    _validate_image_reference(
        integration["artifact"],
        PLATFORM_INTEGRATION_REPOSITORY,
        f"{path}.integration.artifact",
    )

    postgres = _expect_dict(candidate["postgres"], f"{path}.postgres")
    _expect_exact_keys(postgres, f"{path}.postgres", {"major", "pgdata"})
    _expect_literal(postgres["major"], 17, f"{path}.postgres.major")
    postgres_tag = _image_tag(images["postgres"])
    if postgres_tag is None or not re.fullmatch(r"17(?:[.-][A-Za-z0-9_.-]+)?", postgres_tag):
        _fail(
            f"{path}.images.postgres",
            "tag must be present and its major must match platform.postgres.major (17)",
        )
    _expect_literal(
        postgres["pgdata"],
        "/var/lib/postgresql/data/pgdata",
        f"{path}.postgres.pgdata",
    )
    return integration_revision


def validate_platform_candidate(value: Any) -> dict[str, Any]:
    """Validate the secret-free input for one platform proof run."""

    candidate = _expect_dict(value, "platform_candidate")
    _validate_platform_candidate_fields(candidate, "platform_candidate")
    return candidate


def platform_candidate(platform: dict[str, Any]) -> dict[str, Any]:
    """Return the domain-separated platform fields that online proof binds."""

    return {
        "contract": PLATFORM_CANDIDATE_CONTRACT,
        "compose_project": platform["compose_project"],
        "images": platform["images"],
        "integration": platform["integration"],
        "postgres": platform["postgres"],
    }


def platform_candidate_subject(value: dict[str, Any]) -> str:
    """Return the canonical SHA-256 subject for one platform candidate."""

    candidate = validate_platform_candidate(value)
    digest = hashlib.sha256(canonical_json(candidate).encode("ascii")).hexdigest()
    return f"sha256:{digest}"


def _validate_platform(value: Any) -> None:
    path = "platform"
    platform = _expect_dict(value, path)
    required = {"enabled", "compose_project", "published_ports", "blocked_by"}
    optional = {"images", "integration", "postgres", "readiness_evidence"}
    _expect_exact_keys(platform, path, required, optional)
    enabled = _expect_bool(platform["enabled"], f"{path}.enabled")
    _expect_literal(platform["compose_project"], "vps-platform", f"{path}.compose_project")
    _validate_ports(platform["published_ports"], f"{path}.published_ports", platform=True, enabled=enabled)
    _validate_blocked_by(platform["blocked_by"], f"{path}.blocked_by", required_nonempty=not enabled)

    candidate_keys = PLATFORM_CANDIDATE_KEYS & platform.keys()
    missing_candidate_keys = PLATFORM_CANDIDATE_KEYS - candidate_keys
    if candidate_keys and missing_candidate_keys:
        _fail(
            path,
            "platform candidate fields must be declared together; missing keys: "
            f"{', '.join(sorted(missing_candidate_keys))}",
        )

    if not enabled:
        if set(platform["blocked_by"]) != PLATFORM_READINESS_GATES:
            missing = PLATFORM_READINESS_GATES - set(platform["blocked_by"])
            extra = set(platform["blocked_by"]) - PLATFORM_READINESS_GATES
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unknown {', '.join(sorted(extra))}")
            _fail(f"{path}.blocked_by", "; ".join(details))
        if not candidate_keys:
            return

    if missing_candidate_keys:
        _fail(
            path,
            "enabled platform requires images, an immutable integration bundle, "
            "postgres metadata, and readiness evidence",
        )
    candidate = platform_candidate(platform)
    integration_revision = _validate_platform_candidate_fields(candidate, path)
    subject = platform_candidate_subject(candidate)
    readiness = _expect_dict(platform["readiness_evidence"], f"{path}.readiness_evidence")
    expected_readiness = (
        PLATFORM_READINESS_GATES if enabled else PLATFORM_PROOF_BASELINE_GATES
    )
    if set(readiness) != expected_readiness:
        missing = expected_readiness - set(readiness)
        extra = set(readiness) - expected_readiness
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unknown {', '.join(sorted(extra))}")
        _fail(f"{path}.readiness_evidence", "; ".join(details))
    for gate, evidence in readiness.items():
        _validate_evidence(
            evidence,
            f"{path}.readiness_evidence.{gate}",
            "nclsppr/vps-infra",
            {integration_revision},
            subject if gate in PLATFORM_PROOF_BASELINE_GATES else None,
        )


def _validate_static_application(name: str, app: dict[str, Any], enabled: bool) -> None:
    app_path = f"applications.{name}"
    required = {
        "enabled",
        "type",
        "source_repository",
        "source_branch",
        "published_ports",
        "blocked_by",
    }
    release_keys = {
        "source_revision",
        "artifact",
        "route_inventory_artifact",
        "readiness_evidence",
    }
    _expect_exact_keys(app, app_path, required, release_keys)
    _validate_ports(app["published_ports"], f"{app_path}.published_ports", platform=False, enabled=enabled)
    blockers = _validate_blocked_by(
        app["blocked_by"],
        f"{app_path}.blocked_by",
        required_nonempty=not enabled,
    )

    if not enabled:
        unexpected = release_keys & app.keys()
        if unexpected:
            _fail(app_path, f"disabled application must omit unknown artifacts: {', '.join(sorted(unexpected))}")
        if set(blockers) != STATIC_READINESS_GATES:
            missing = STATIC_READINESS_GATES - set(blockers)
            extra = set(blockers) - STATIC_READINESS_GATES
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unknown {', '.join(sorted(extra))}")
            _fail(f"{app_path}.blocked_by", "; ".join(details))
        return

    missing = release_keys - app.keys()
    if missing:
        _fail(app_path, f"enabled application missing keys: {', '.join(sorted(missing))}")
    source_revision = _validate_sha(app["source_revision"], f"{app_path}.source_revision")
    artifact_repo, routes_repo = STATIC_ARTIFACT_REPOSITORIES[name]
    _validate_image_reference(app["artifact"], artifact_repo, f"{app_path}.artifact")
    _validate_image_reference(
        app["route_inventory_artifact"],
        routes_repo,
        f"{app_path}.route_inventory_artifact",
    )
    readiness = _expect_dict(app["readiness_evidence"], f"{app_path}.readiness_evidence")
    if set(readiness) != STATIC_READINESS_GATES:
        missing = STATIC_READINESS_GATES - set(readiness)
        extra = set(readiness) - STATIC_READINESS_GATES
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unknown {', '.join(sorted(extra))}")
        _fail(f"{app_path}.readiness_evidence", "; ".join(details))
    for gate, evidence in readiness.items():
        _validate_evidence(
            evidence,
            f"{app_path}.readiness_evidence.{gate}",
            app["source_repository"],
            {source_revision},
        )


def _validate_compose_application(name: str, app: dict[str, Any], enabled: bool) -> None:
    app_path = f"applications.{name}"
    required = {
        "enabled",
        "type",
        "source_repository",
        "source_branch",
        "compose_project",
        "published_ports",
        "blocked_by",
        "migrations",
    }
    release_keys = {"components", "integration", "readiness_evidence"}
    _expect_exact_keys(app, app_path, required, release_keys)
    _validate_ports(app["published_ports"], f"{app_path}.published_ports", platform=False, enabled=enabled)
    blockers = _validate_blocked_by(
        app["blocked_by"],
        f"{app_path}.blocked_by",
        required_nonempty=not enabled,
    )
    expected_gates = (
        PARKVENTORY_READINESS_GATES if name == "parkventory" else SURPLASSE_READINESS_GATES
    )

    if not enabled:
        unexpected = release_keys & app.keys()
        if unexpected:
            _fail(app_path, f"disabled application must omit unknown artifacts: {', '.join(sorted(unexpected))}")
        if set(blockers) != expected_gates:
            missing = expected_gates - set(blockers)
            extra = set(blockers) - expected_gates
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unknown {', '.join(sorted(extra))}")
            _fail(f"{app_path}.blocked_by", "; ".join(details))
        _validate_migrations(
            app["migrations"],
            f"{app_path}.migrations",
            enabled=False,
            repository=app["source_repository"],
            allowed_revisions=set(),
        )
        return

    missing = release_keys - app.keys()
    if missing:
        _fail(app_path, f"enabled application missing keys: {', '.join(sorted(missing))}")

    components = _expect_dict(app["components"], f"{app_path}.components")
    expected_components = COMPOSE_COMPONENT_REPOSITORIES[name]
    _expect_exact_keys(components, f"{app_path}.components", set(expected_components))
    declared_revisions: set[str] = set()
    for component_name, repository in expected_components.items():
        component_path = f"{app_path}.components.{component_name}"
        component = _expect_dict(components[component_name], component_path)
        _expect_exact_keys(component, component_path, {"source_revision", "image"})
        declared_revisions.add(
            _validate_sha(component["source_revision"], f"{component_path}.source_revision")
        )
        _validate_image_reference(component["image"], repository, f"{component_path}.image")

    integration_path = f"{app_path}.integration"
    integration = _expect_dict(app["integration"], integration_path)
    _expect_exact_keys(integration, integration_path, {"source_revision", "artifact"})
    declared_revisions.add(
        _validate_sha(integration["source_revision"], f"{integration_path}.source_revision")
    )
    _validate_image_reference(
        integration["artifact"],
        INTEGRATION_REPOSITORIES[name],
        f"{integration_path}.artifact",
    )

    readiness = _expect_dict(app["readiness_evidence"], f"{app_path}.readiness_evidence")
    actual_gates = set(readiness)
    if actual_gates != expected_gates:
        missing_gates = expected_gates - actual_gates
        extra_gates = actual_gates - expected_gates
        details = []
        if missing_gates:
            details.append(f"missing {', '.join(sorted(missing_gates))}")
        if extra_gates:
            details.append(f"unknown {', '.join(sorted(extra_gates))}")
        _fail(f"{app_path}.readiness_evidence", "; ".join(details))
    for gate, evidence in readiness.items():
        _validate_evidence(
            evidence,
            f"{app_path}.readiness_evidence.{gate}",
            app["source_repository"],
            declared_revisions,
        )

    _validate_migrations(
        app["migrations"],
        f"{app_path}.migrations",
        enabled=True,
        repository=app["source_repository"],
        allowed_revisions=declared_revisions,
    )


def _validate_applications(value: Any, *, platform_enabled: bool) -> None:
    applications = _expect_dict(value, "applications")
    _expect_exact_keys(applications, "applications", set(EXPECTED_APPLICATIONS))
    any_enabled = False
    for name, (app_type, repository, branch, compose_project) in EXPECTED_APPLICATIONS.items():
        path = f"applications.{name}"
        app = _expect_dict(applications[name], path)
        if "enabled" not in app:
            _fail(path, "missing key: enabled")
        enabled = _expect_bool(app["enabled"], f"{path}.enabled")
        any_enabled = any_enabled or enabled
        _expect_literal(app.get("type"), app_type, f"{path}.type")
        _expect_literal(app.get("source_repository"), repository, f"{path}.source_repository")
        _expect_literal(app.get("source_branch"), branch, f"{path}.source_branch")
        if compose_project is not None:
            _expect_literal(app.get("compose_project"), compose_project, f"{path}.compose_project")
            _validate_compose_application(name, app, enabled)
        else:
            _validate_static_application(name, app, enabled)
    if any_enabled and not platform_enabled:
        _fail("applications", "no application may be enabled while the shared platform is disabled")


def validate_manifest(manifest: Any) -> dict[str, Any]:
    root = _expect_dict(manifest, "manifest")
    _expect_exact_keys(
        root,
        "manifest",
        {"schema", "environment", "activation_policy", "platform", "applications"},
    )
    _expect_literal(root["schema"], 1, "schema")
    _expect_literal(root["environment"], "production", "environment")
    _expect_literal(root["activation_policy"], "locked", "activation_policy")
    _validate_platform(root["platform"])
    platform = _expect_dict(root["platform"], "platform")
    _validate_applications(root["applications"], platform_enabled=platform["enabled"])
    if platform["enabled"] or any(
        app["enabled"] for app in root["applications"].values()
    ):
        _fail(
            "activation_policy",
            "locked forbids every enabled unit until a separately audited policy revision",
        )
    return root


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        value = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"{manifest_path}: file does not exist") from exc
    return validate_manifest(value)


def iter_image_references(manifest: dict[str, Any]) -> Iterable[str]:
    platform = manifest["platform"]
    if PLATFORM_CANDIDATE_KEYS.issubset(platform):
        yield from platform["images"].values()
        yield platform["integration"]["artifact"]
    for app in manifest["applications"].values():
        if not app["enabled"]:
            continue
        if app["type"] == "static":
            yield app["artifact"]
            yield app["route_inventory_artifact"]
        else:
            for component in app["components"].values():
                yield component["image"]
            yield app["integration"]["artifact"]


def iter_digests(manifest: dict[str, Any]) -> Iterable[str]:
    for reference in iter_image_references(manifest):
        digest = reference.rsplit("@", 1)[1]
        if not DIGEST_RE.fullmatch(digest):
            raise AssertionError(f"validated image reference has invalid digest: {reference}")
        yield digest


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
