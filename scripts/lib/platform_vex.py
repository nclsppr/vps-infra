#!/usr/bin/env python3
"""Strict, digest-bound VEX evaluation for platform image scan reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from release_policy import PolicyError, strict_json_loads


VEX_CONTRACT = "vps-infra.platform-vex.v1"
MAX_VEX_BYTES = 64 * 1024


class VexError(ValueError):
    """Raised when a VEX policy or Trivy report fails closed."""


@dataclass(frozen=True)
class FindingIdentity:
    """Fields that bind one exception to one exact Trivy finding."""

    service: str
    image: str
    platform: str
    target: str
    package_name: str
    package_purl: str
    installed_version: str
    vulnerability_id: str
    severity: str


APPROVED_EXCEPTION_EXPIRY_LIMITS = {
    FindingIdentity(
        service="grafana",
        image=(
            "docker.io/grafana/grafana:13.1.3-slim@sha256:"
            "0d2091fa32b712dd93c20376f21f3feb32789afcda6d1a3595da463fbff303c7"
        ),
        platform="linux/amd64",
        target="usr/share/grafana/bin/grafana",
        package_name="github.com/grafana/tempo",
        package_purl=(
            "pkg:golang/github.com/grafana/tempo@"
            "v1.5.1-0.20260427112133-525d1bab07e0"
        ),
        installed_version="v1.5.1-0.20260427112133-525d1bab07e0",
        vulnerability_id="CVE-2026-21728",
        severity="HIGH",
    ): date(2026, 9, 11),
    FindingIdentity(
        service="grafana",
        image=(
            "docker.io/grafana/grafana:13.1.3-slim@sha256:"
            "0d2091fa32b712dd93c20376f21f3feb32789afcda6d1a3595da463fbff303c7"
        ),
        platform="linux/amd64",
        target="usr/share/grafana/bin/grafana",
        package_name="github.com/grafana/tempo",
        package_purl=(
            "pkg:golang/github.com/grafana/tempo@"
            "v1.5.1-0.20260427112133-525d1bab07e0"
        ),
        installed_version="v1.5.1-0.20260427112133-525d1bab07e0",
        vulnerability_id="CVE-2026-28377",
        severity="HIGH",
    ): date(2026, 9, 11),
    FindingIdentity(
        service="postgres_exporter",
        image=(
            "docker.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:"
            "4f3d82803c1f99ea5e767890de3557d2479ebbc711f63f2e04c663daa840057a"
        ),
        platform="linux/amd64",
        target="bin/postgres_exporter",
        package_name="golang.org/x/text",
        package_purl="pkg:golang/golang.org/x/text@v0.38.0",
        installed_version="v0.38.0",
        vulnerability_id="CVE-2026-56852",
        severity="HIGH",
    ): date(2026, 8, 26),
    FindingIdentity(
        service="postgres_exporter",
        image=(
            "docker.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:"
            "4f3d82803c1f99ea5e767890de3557d2479ebbc711f63f2e04c663daa840057a"
        ),
        platform="linux/amd64",
        target="bin/postgres_exporter",
        package_name="stdlib",
        package_purl="pkg:golang/stdlib@v1.26.4",
        installed_version="v1.26.4",
        vulnerability_id="CVE-2026-39822",
        severity="HIGH",
    ): date(2026, 8, 26),
}


def _read_strict_object(path: Path, subject: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise VexError(f"cannot read {subject}: {exc}") from exc
    if len(payload) > MAX_VEX_BYTES:
        raise VexError(f"{subject} exceeds the safety limit")
    try:
        value = strict_json_loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, PolicyError) as exc:
        raise VexError(f"{subject} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise VexError(f"{subject} must be a JSON object")
    return value, payload


def _exception_identity(statement: dict[str, Any]) -> FindingIdentity:
    package = statement["package"]
    return FindingIdentity(
        service=statement["service"],
        image=statement["image"],
        platform=statement["platform"],
        target=statement["target"],
        package_name=package["name"],
        package_purl=package["purl"],
        installed_version=package["installed_version"],
        vulnerability_id=statement["vulnerability_id"],
        severity=statement["severity"],
    )


def _finding_identity(
    *,
    service: str,
    image: str,
    platform: str,
    result: dict[str, Any],
    vulnerability: dict[str, Any],
) -> FindingIdentity:
    target = result.get("Target")
    package_name = vulnerability.get("PkgName")
    package_identifier = vulnerability.get("PkgIdentifier")
    package_purl = (
        package_identifier.get("PURL")
        if isinstance(package_identifier, dict)
        else None
    )
    installed_version = vulnerability.get("InstalledVersion")
    vulnerability_id = vulnerability.get("VulnerabilityID")
    severity = vulnerability.get("Severity")
    values = {
        "target": target,
        "package name": package_name,
        "package PURL": package_purl,
        "installed version": installed_version,
        "vulnerability ID": vulnerability_id,
        "severity": severity,
    }
    invalid = [
        name
        for name, value in values.items()
        if not isinstance(value, str) or not value
    ]
    if invalid:
        raise VexError(
            f"Trivy finding for {service} has invalid {', '.join(invalid)}"
        )
    return FindingIdentity(
        service=service,
        image=image,
        platform=platform,
        target=target,
        package_name=package_name,
        package_purl=package_purl,
        installed_version=installed_version,
        vulnerability_id=vulnerability_id,
        severity=severity,
    )


class VexPolicy:
    """Consume every approved exception exactly by finding identity."""

    def __init__(
        self,
        statements: dict[FindingIdentity, dict[str, Any]],
        *,
        policy_digest: str,
        valid_until: date,
    ) -> None:
        self._statements = statements
        self._used: set[FindingIdentity] = set()
        self._policy_digest = policy_digest
        self._valid_until = valid_until

    def evaluate_report(
        self,
        *,
        service: str,
        image: str,
        platform: str,
        report: dict[str, Any],
    ) -> None:
        results = report.get("Results")
        if not isinstance(results, list):
            raise VexError(
                f"Trivy report for {service} must contain a Results array"
            )
        for result in results:
            if not isinstance(result, dict):
                raise VexError(
                    f"Trivy report for {service} contains an invalid result"
                )
            vulnerabilities = result.get("Vulnerabilities")
            if vulnerabilities is None:
                continue
            if not isinstance(vulnerabilities, list):
                raise VexError(
                    f"Trivy report for {service} contains an invalid vulnerability list"
                )
            for vulnerability in vulnerabilities:
                if not isinstance(vulnerability, dict):
                    raise VexError(
                        f"Trivy report for {service} contains an invalid vulnerability"
                    )
                identity = _finding_identity(
                    service=service,
                    image=image,
                    platform=platform,
                    result=result,
                    vulnerability=vulnerability,
                )
                if identity.severity == "CRITICAL":
                    raise VexError(
                        f"CRITICAL finding {identity.vulnerability_id} is not "
                        "eligible for VEX"
                    )
                if identity.severity != "HIGH":
                    raise VexError(
                        f"unexpected Trivy severity {identity.severity!r} for "
                        f"{identity.vulnerability_id}"
                    )
                if identity not in self._statements:
                    raise VexError(
                        f"uncovered HIGH finding {identity.vulnerability_id} for "
                        f"{service} package {identity.package_name}"
                    )
                if identity in self._used:
                    raise VexError(
                        f"duplicate Trivy finding {identity.vulnerability_id} "
                        f"for {service}"
                    )
                self._used.add(identity)

    def assert_all_used(self) -> None:
        unused = set(self._statements) - self._used
        if unused:
            first = sorted(
                unused,
                key=lambda item: (
                    item.service,
                    item.vulnerability_id,
                    item.package_name,
                ),
            )[0]
            raise VexError(
                f"unused VEX exception {first.vulnerability_id} for {first.service}"
            )

    def metadata(self) -> dict[str, str]:
        """Return the strict policy identity and its earliest expiry date."""

        return {
            "contract": VEX_CONTRACT,
            "digest": self._policy_digest,
            "valid_until": self._valid_until.isoformat(),
        }

    def proof_metadata(self) -> dict[str, str]:
        """Return metadata only after every approved exception was consumed."""

        self.assert_all_used()
        return self.metadata()


def load_vex_policy(
    policy_path: Path,
    schema_path: Path,
    *,
    on_date: date | None = None,
) -> VexPolicy:
    """Load a schema-valid policy and reject duplicate or expired statements."""

    policy, policy_bytes = _read_strict_object(policy_path, "platform VEX policy")
    schema, _schema_bytes = _read_strict_object(schema_path, "platform VEX schema")
    try:
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(policy)
    except ValidationError as exc:
        raise VexError(
            f"platform VEX policy does not match its schema: {exc.message}"
        ) from exc
    if policy.get("contract") != VEX_CONTRACT:
        raise VexError(f"platform VEX contract must be {VEX_CONTRACT}")

    effective_date = on_date or datetime.now(timezone.utc).date()
    statements: dict[FindingIdentity, dict[str, Any]] = {}
    for statement in policy["statements"]:
        identity = _exception_identity(statement)
        if identity in statements:
            raise VexError(
                f"duplicate VEX exception {identity.vulnerability_id} for {identity.service}"
            )
        expires_on = date.fromisoformat(statement["expires_on"])
        maximum_expiry = APPROVED_EXCEPTION_EXPIRY_LIMITS.get(identity)
        if maximum_expiry is None:
            raise VexError(
                f"unapproved VEX exception {identity.vulnerability_id} "
                f"for {identity.service}"
            )
        if expires_on > maximum_expiry:
            raise VexError(
                f"VEX exception {identity.vulnerability_id} for {identity.service} "
                f"expires after its approved limit {maximum_expiry.isoformat()}"
            )
        if expires_on < effective_date:
            raise VexError(
                f"expired VEX exception {identity.vulnerability_id} for {identity.service}"
            )
        statements[identity] = statement
    missing = set(APPROVED_EXCEPTION_EXPIRY_LIMITS) - set(statements)
    if missing:
        first = sorted(
            missing,
            key=lambda item: (item.service, item.vulnerability_id),
        )[0]
        raise VexError(
            f"missing approved VEX exception {first.vulnerability_id} "
            f"for {first.service}"
        )
    valid_until = min(
        date.fromisoformat(statement["expires_on"])
        for statement in statements.values()
    )
    policy_digest = f"sha256:{hashlib.sha256(policy_bytes).hexdigest()}"
    return VexPolicy(
        statements,
        policy_digest=policy_digest,
        valid_until=valid_until,
    )
