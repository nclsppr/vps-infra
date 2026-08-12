#!/usr/bin/env python3
"""Canonical platform proof bundle construction and validation."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any, Iterable

from release_policy import (
    PLATFORM_PROOF_ARTIFACT_PREFIX,
    PLATFORM_PROOF_BASELINE_GATES,
    PolicyError,
    canonical_json,
    platform_candidate_subject,
    validate_platform_candidate,
)


PLATFORM_PROOF_CONTRACT = "vps-infra.platform-proof.v2"
PLATFORM_VEX_CONTRACT = "vps-infra.platform-vex.v1"
PLATFORM_REPOSITORY = "nclsppr/vps-infra"
PLATFORM_WORKFLOW_PATH = ".github/workflows/vps-release.yml"
PLATFORM_PROOF_CONTROLS = {
    "caddy-ovh-module": "passed",
    "exact-oci-references": "passed",
    "high-critical-scans": "passed",
    "first-party-github-attestations": "passed",
    "oci-labels": "passed",
}


def _fail(message: str) -> None:
    raise PolicyError(message)


def validate_proof_gates(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail("verified_gates: non-empty array required")
    if any(not isinstance(gate, str) for gate in value):
        _fail("verified_gates: strings required")
    if value != sorted(set(value)):
        _fail("verified_gates: values must be sorted and unique")
    if set(value) != PLATFORM_PROOF_BASELINE_GATES:
        _fail("verified_gates: exact digest-bound baseline gates required")
    return value


def proof_artifact_name(subject: str, run_id: str, run_attempt: int) -> str:
    if not subject.startswith("sha256:") or len(subject) != 71:
        _fail("candidate_subject: lowercase sha256 digest required")
    if not run_id.isascii() or not run_id.isdigit() or run_id.startswith("0"):
        _fail("run.id: positive decimal string required")
    if type(run_attempt) is not int or run_attempt < 1:
        _fail("run.attempt: positive integer required")
    return (
        f"{PLATFORM_PROOF_ARTIFACT_PREFIX}{subject.removeprefix('sha256:')}"
        f"-{run_id}-{run_attempt}.json"
    )


def validate_vulnerability_policy(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "contract",
        "digest",
        "valid_until",
    }:
        _fail("vulnerability_policy: exact contract, digest, and valid_until required")
    if value["contract"] != PLATFORM_VEX_CONTRACT:
        _fail(f"vulnerability_policy.contract: {PLATFORM_VEX_CONTRACT} required")
    digest = value["digest"]
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        _fail("vulnerability_policy.digest: lowercase sha256 digest required")
    valid_until = value["valid_until"]
    if not isinstance(valid_until, str):
        _fail("vulnerability_policy.valid_until: ISO date required")
    try:
        date.fromisoformat(valid_until)
    except ValueError as exc:
        raise PolicyError(
            "vulnerability_policy.valid_until: ISO date required"
        ) from exc
    return dict(value)


def build_platform_proof(
    candidate_value: Any,
    *,
    source_revision: str,
    run_id: str,
    run_attempt: int,
    verified_gates: Iterable[str],
    vulnerability_policy: Any,
) -> dict[str, Any]:
    candidate = validate_platform_candidate(candidate_value)
    if source_revision != candidate["integration"]["source_revision"]:
        _fail("source_revision must match candidate integration source revision")
    gates = validate_proof_gates(list(verified_gates))
    validated_vulnerability_policy = validate_vulnerability_policy(
        vulnerability_policy
    )
    subject = platform_candidate_subject(candidate)
    artifact_name = proof_artifact_name(subject, run_id, run_attempt)
    return {
        "contract": PLATFORM_PROOF_CONTRACT,
        "candidate": candidate,
        "candidate_subject": subject,
        "source_revision": source_revision,
        "run": {
            "repository": PLATFORM_REPOSITORY,
            "workflow_path": PLATFORM_WORKFLOW_PATH,
            "id": run_id,
            "attempt": run_attempt,
        },
        "artifact_name": artifact_name,
        "vulnerability_policy": validated_vulnerability_policy,
        "verified_gates": gates,
        "controls": dict(PLATFORM_PROOF_CONTROLS),
    }


def platform_proof_bytes(proof: dict[str, Any]) -> bytes:
    return f"{canonical_json(proof)}\n".encode("ascii")


def platform_proof_digest(proof: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(platform_proof_bytes(proof)).hexdigest()}"


def validate_platform_proof(
    value: Any,
    *,
    candidate: dict[str, Any],
    source_revision: str,
    run_id: str,
    run_attempt: int,
    verified_gates: Iterable[str],
    vulnerability_policy: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("platform proof: object required")
    expected = build_platform_proof(
        candidate,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=run_attempt,
        verified_gates=verified_gates,
        vulnerability_policy=vulnerability_policy,
    )
    if value != expected:
        _fail("platform proof does not match its candidate and workflow run")
    return value
