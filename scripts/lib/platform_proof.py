#!/usr/bin/env python3
"""Canonical platform proof bundle construction and validation."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from release_policy import (
    PLATFORM_PROOF_ARTIFACT_PREFIX,
    PLATFORM_PROOF_BASELINE_GATES,
    PolicyError,
    canonical_json,
    platform_candidate_subject,
    validate_platform_candidate,
)


PLATFORM_PROOF_CONTRACT = "vps-infra.platform-proof.v1"
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


def build_platform_proof(
    candidate_value: Any,
    *,
    source_revision: str,
    run_id: str,
    run_attempt: int,
    verified_gates: Iterable[str],
) -> dict[str, Any]:
    candidate = validate_platform_candidate(candidate_value)
    if source_revision != candidate["integration"]["source_revision"]:
        _fail("source_revision must match candidate integration source revision")
    gates = validate_proof_gates(list(verified_gates))
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
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("platform proof: object required")
    expected = build_platform_proof(
        candidate,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=run_attempt,
        verified_gates=verified_gates,
    )
    if value != expected:
        _fail("platform proof does not match its candidate and workflow run")
    return value
