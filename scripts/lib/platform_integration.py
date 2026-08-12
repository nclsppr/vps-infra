"""Build and verify the secret-free platform integration artifact."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


ARTIFACT_TYPE = "application/vnd.vps-infra.platform-integration.v1"
ARCHIVE_MEDIA_TYPE = (
    "application/vnd.vps-infra.platform-integration.v1+tar+gzip"
)
INVENTORY_MEDIA_TYPE = (
    "application/vnd.vps-infra.platform-integration.inventory.v1+json"
)
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
OCI_EMPTY_CONFIG_DIGEST = (
    "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)
SOURCE_URL = "https://github.com/nclsppr/vps-infra"
SIGNER_WORKFLOW = "nclsppr/vps-infra/.github/workflows/platform-integration.yml"
ARCHIVE_NAME = "platform-integration.tar.gz"
INVENTORY_NAME = "platform-integration.inventory.json"
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_TOTAL_SIZE = 25 * 1024 * 1024
MAX_ARCHIVE_SIZE = 10 * 1024 * 1024
MAX_TAR_SIZE = MAX_TOTAL_SIZE + (1024 * 1024)
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")

RUNTIME_PATHS = (
    "platform/.env.example",
    "platform/caddy/Caddyfile",
    "platform/caddy/routes/papersempire.caddy.disabled",
    "platform/caddy/routes/parkventory.caddy.disabled",
    "platform/caddy/routes/personal.caddy.disabled",
    "platform/caddy/routes/surplasse.caddy.disabled",
    "platform/compose.yaml",
    "platform/observability/grafana/dashboards/platform/overview.json",
    "platform/observability/grafana/provisioning/dashboards/dashboards.yml",
    "platform/observability/grafana/provisioning/datasources/prometheus.yml",
    "platform/observability/prometheus/prometheus.yml",
    "platform/observability/prometheus/rules/platform.yml",
    "platform/observability/prometheus/rules/surplasse.yml.disabled",
    "platform/observability/prometheus/targets/caddy.yml",
    "platform/observability/prometheus/targets/node-exporter.yml",
    "platform/observability/prometheus/targets/postgres-exporter.yml",
    "platform/observability/prometheus/targets/surplasse.yml.disabled",
    "platform/postgres/initdb/10-platform-exporter.sh",
    "platform/postgres/pg_hba.conf",
    "platform/postgres/postgresql.conf",
)

RUNTIME_PATHS_TO_SCAN = (
    "platform/.env.example",
    "platform/compose.yaml",
    "platform/caddy/Caddyfile",
    "platform/caddy/routes",
    "platform/observability",
    "platform/postgres/initdb",
    "platform/postgres/pg_hba.conf",
    "platform/postgres/postgresql.conf",
)


class IntegrationError(ValueError):
    """Raised when the platform integration contract is not satisfied."""


@dataclass(frozen=True)
class RuntimeFile:
    path: str
    mode: int
    content: bytes

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"


@dataclass(frozen=True)
class Package:
    revision: str
    created: str
    archive: bytes
    inventory: bytes


def _command(argv: list[str], *, cwd: Path) -> bytes:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise IntegrationError(f"cannot execute {argv[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise IntegrationError(
            f"{argv[0]} failed with code {completed.returncode}: {detail}"
        )
    if len(completed.stdout) > MAX_TOTAL_SIZE:
        raise IntegrationError(f"{argv[0]} output exceeds the safety limit")
    return completed.stdout


def _strict_object(data: bytes, subject: str) -> dict[str, Any]:
    if len(data) > MAX_TOTAL_SIZE:
        raise IntegrationError(f"{subject} exceeds the safety limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise IntegrationError(f"{subject} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"{subject} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise IntegrationError(f"{subject} must be a JSON object")
    return value


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _decompress_gzip_bounded(archive_bytes: bytes) -> bytes:
    """Expand a gzip stream without allocating beyond the tar safety limit."""

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive_bytes), mode="rb") as compressed:
            tar_bytes = compressed.read(MAX_TAR_SIZE + 1)
    except (EOFError, OSError) as exc:
        raise IntegrationError("platform integration archive is not valid gzip") from exc
    if len(tar_bytes) > MAX_TAR_SIZE:
        raise IntegrationError("expanded platform integration archive exceeds the limit")
    return tar_bytes


def _revision_and_epoch(repository: Path, revision: str) -> tuple[str, int]:
    if REVISION_RE.fullmatch(revision) is None:
        raise IntegrationError("source revision must be a full lowercase Git commit ID")
    resolved = _command(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repository,
    ).decode("ascii", errors="strict").strip()
    if resolved != revision:
        raise IntegrationError("source revision did not resolve to itself")
    raw_epoch = _command(
        ["git", "show", "-s", "--format=%ct", revision], cwd=repository
    ).decode("ascii", errors="strict").strip()
    try:
        epoch = int(raw_epoch)
    except ValueError as exc:
        raise IntegrationError("source commit timestamp is invalid") from exc
    if epoch < 0 or epoch > 0xFFFFFFFF:
        raise IntegrationError("source commit timestamp is outside the gzip range")
    return resolved, epoch


def created_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def epoch_from_created(created: str) -> int:
    if not isinstance(created, str) or not created.endswith("Z"):
        raise IntegrationError("created must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(created.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise IntegrationError("created must be a UTC RFC 3339 timestamp") from exc
    if parsed.microsecond != 0 or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IntegrationError("created must use whole UTC seconds")
    canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if canonical != created:
        raise IntegrationError("created timestamp is not canonical")
    epoch = int(parsed.timestamp())
    if epoch < 0 or epoch > 0xFFFFFFFF:
        raise IntegrationError("created timestamp is outside the gzip range")
    return epoch


def _git_runtime_entries(repository: Path, revision: str) -> list[tuple[str, str, str, str]]:
    output = _command(
        [
            "git",
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
            "--",
            *RUNTIME_PATHS_TO_SCAN,
        ],
        cwd=repository,
    )
    entries: list[tuple[str, str, str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", maxsplit=1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise IntegrationError("Git returned an invalid runtime tree entry") from exc
        entries.append((path, mode, object_type, object_id))
    return entries


def load_runtime_files(repository: Path, revision: str) -> tuple[list[RuntimeFile], int]:
    repository = repository.resolve()
    resolved_revision, epoch = _revision_and_epoch(repository, revision)
    entries = _git_runtime_entries(repository, resolved_revision)
    actual_paths = [entry[0] for entry in entries]
    if len(actual_paths) != len(set(actual_paths)):
        raise IntegrationError("runtime tree contains duplicate paths")
    expected_paths = list(RUNTIME_PATHS)
    if actual_paths != expected_paths:
        missing = sorted(set(expected_paths) - set(actual_paths))
        unexpected = sorted(set(actual_paths) - set(expected_paths))
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if not details:
            details.append("paths are not in canonical order")
        raise IntegrationError(
            "runtime tree does not match the exact allowlist (" + "; ".join(details) + ")"
        )

    runtime_files: list[RuntimeFile] = []
    total_size = 0
    for path, mode, object_type, object_id in entries:
        if mode != "100644" or object_type != "blob":
            raise IntegrationError(f"unsafe tracked file type or mode for {path}")
        content = _command(["git", "cat-file", "blob", object_id], cwd=repository)
        if len(content) > MAX_FILE_SIZE:
            raise IntegrationError(f"runtime file exceeds the size limit: {path}")
        total_size += len(content)
        if total_size > MAX_TOTAL_SIZE:
            raise IntegrationError("runtime files exceed the total size limit")
        if b"\0" in content:
            raise IntegrationError(f"runtime file contains a NUL byte: {path}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrationError(f"runtime file is not UTF-8 text: {path}") from exc
        runtime_files.append(RuntimeFile(path=path, mode=0o644, content=content))
    return runtime_files, epoch


def inventory_for(
    runtime_files: list[RuntimeFile], *, revision: str, created: str
) -> dict[str, Any]:
    return {
        "archive_media_type": ARCHIVE_MEDIA_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "created": created,
        "files": [
            {
                "mode": f"{runtime_file.mode:04o}",
                "path": runtime_file.path,
                "sha256": runtime_file.digest,
                "size": len(runtime_file.content),
            }
            for runtime_file in runtime_files
        ],
        "inventory_media_type": INVENTORY_MEDIA_TYPE,
        "schema": 1,
        "source": SOURCE_URL,
        "source_revision": revision,
    }


def archive_for(runtime_files: list[RuntimeFile], *, epoch: int) -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for runtime_file in runtime_files:
            entry = tarfile.TarInfo(name=runtime_file.path)
            entry.type = tarfile.REGTYPE
            entry.mode = runtime_file.mode
            entry.uid = 0
            entry.gid = 0
            entry.uname = ""
            entry.gname = ""
            entry.mtime = epoch
            entry.size = len(runtime_file.content)
            archive.addfile(entry, io.BytesIO(runtime_file.content))
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=compressed,
        mtime=epoch,
    ) as output:
        output.write(tar_buffer.getvalue())
    result = compressed.getvalue()
    if len(result) > MAX_ARCHIVE_SIZE:
        raise IntegrationError("platform integration archive exceeds the size limit")
    return result


def build_package(repository: Path, revision: str) -> Package:
    runtime_files, epoch = load_runtime_files(repository, revision)
    created = created_from_epoch(epoch)
    inventory = canonical_json(
        inventory_for(runtime_files, revision=revision, created=created)
    )
    archive = archive_for(runtime_files, epoch=epoch)
    return Package(
        revision=revision,
        created=created,
        archive=archive,
        inventory=inventory,
    )


def validate_inventory(
    inventory_bytes: bytes,
    *,
    expected_revision: str | None = None,
    expected_created: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = _strict_object(inventory_bytes, "platform integration inventory")
    if canonical_json(inventory) != inventory_bytes:
        raise IntegrationError("platform integration inventory is not canonical JSON")
    expected_keys = {
        "archive_media_type",
        "artifact_type",
        "created",
        "files",
        "inventory_media_type",
        "schema",
        "source",
        "source_revision",
    }
    if set(inventory) != expected_keys:
        raise IntegrationError("platform integration inventory has unexpected fields")
    if inventory["schema"] != 1 or isinstance(inventory["schema"], bool):
        raise IntegrationError("platform integration inventory schema must be 1")
    if inventory["artifact_type"] != ARTIFACT_TYPE:
        raise IntegrationError("platform integration artifact type is invalid")
    if inventory["archive_media_type"] != ARCHIVE_MEDIA_TYPE:
        raise IntegrationError("platform integration archive media type is invalid")
    if inventory["inventory_media_type"] != INVENTORY_MEDIA_TYPE:
        raise IntegrationError("platform integration inventory media type is invalid")
    if inventory["source"] != SOURCE_URL:
        raise IntegrationError("platform integration source is invalid")
    revision = inventory["source_revision"]
    if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
        raise IntegrationError("platform integration revision is invalid")
    if expected_revision is not None and revision != expected_revision:
        raise IntegrationError("platform integration revision does not match")
    created = inventory["created"]
    epoch_from_created(created)
    if expected_created is not None and created != expected_created:
        raise IntegrationError("platform integration creation time does not match")
    files = inventory["files"]
    if not isinstance(files, list) or len(files) != len(RUNTIME_PATHS):
        raise IntegrationError("platform integration file count is invalid")
    validated_files: list[dict[str, Any]] = []
    for index, value in enumerate(files):
        if not isinstance(value, dict) or set(value) != {"mode", "path", "sha256", "size"}:
            raise IntegrationError("platform integration file entry is invalid")
        if value["path"] != RUNTIME_PATHS[index]:
            raise IntegrationError("platform integration file allowlist is invalid")
        path = PurePosixPath(value["path"])
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise IntegrationError("platform integration path is unsafe")
        if value["mode"] != "0644":
            raise IntegrationError("platform integration file mode is invalid")
        if not isinstance(value["size"], int) or isinstance(value["size"], bool):
            raise IntegrationError("platform integration file size is invalid")
        if value["size"] < 0 or value["size"] > MAX_FILE_SIZE:
            raise IntegrationError("platform integration file size is outside the limit")
        if not isinstance(value["sha256"], str) or SHA256_RE.fullmatch(value["sha256"]) is None:
            raise IntegrationError("platform integration file digest is invalid")
        validated_files.append(value)
    if sum(value["size"] for value in validated_files) > MAX_TOTAL_SIZE:
        raise IntegrationError("platform integration inventory exceeds the total size limit")
    return inventory, validated_files


def verify_package(
    archive_bytes: bytes,
    inventory_bytes: bytes,
    *,
    expected_revision: str | None = None,
    expected_created: str | None = None,
) -> tuple[str, str]:
    if len(archive_bytes) > MAX_ARCHIVE_SIZE:
        raise IntegrationError("platform integration archive exceeds the size limit")
    inventory, expected_files = validate_inventory(
        inventory_bytes,
        expected_revision=expected_revision,
        expected_created=expected_created,
    )
    tar_bytes = _decompress_gzip_bounded(archive_bytes)

    runtime_files: list[RuntimeFile] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) != len(expected_files):
                raise IntegrationError("platform integration archive file count is invalid")
            for member, expected in zip(members, expected_files, strict=True):
                if member.name != expected["path"]:
                    raise IntegrationError("platform integration archive order or path is invalid")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or "." in path.parts:
                    raise IntegrationError("platform integration archive path is unsafe")
                if not member.isfile() or member.type != tarfile.REGTYPE:
                    raise IntegrationError("platform integration archive contains a special file")
                if member.pax_headers:
                    raise IntegrationError(
                        "platform integration archive contains extended headers"
                    )
                if member.mode != 0o644 or member.uid != 0 or member.gid != 0:
                    raise IntegrationError("platform integration archive metadata is invalid")
                if member.uname or member.gname:
                    raise IntegrationError("platform integration archive owner names are invalid")
                expected_epoch = epoch_from_created(inventory["created"])
                if member.mtime != expected_epoch:
                    raise IntegrationError("platform integration archive timestamp is invalid")
                if member.size != expected["size"]:
                    raise IntegrationError("platform integration archive size is invalid")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise IntegrationError("platform integration file cannot be read")
                content = extracted.read(MAX_FILE_SIZE + 1)
                if len(content) != member.size:
                    raise IntegrationError("platform integration file length is invalid")
                if sha256(content) != expected["sha256"]:
                    raise IntegrationError("platform integration file digest is invalid")
                if b"\0" in content:
                    raise IntegrationError(
                        "platform integration archive contains a NUL byte"
                    )
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise IntegrationError(
                        "platform integration archive contains non-UTF-8 text"
                    ) from exc
                runtime_files.append(
                    RuntimeFile(path=member.name, mode=member.mode, content=content)
                )
    except (tarfile.TarError, EOFError) as exc:
        raise IntegrationError("platform integration archive is not valid tar") from exc

    rebuilt = archive_for(
        runtime_files,
        epoch=epoch_from_created(inventory["created"]),
    )
    if rebuilt != archive_bytes:
        raise IntegrationError("platform integration archive is not canonical")
    return sha256(archive_bytes), sha256(inventory_bytes)


def validate_manifest(
    manifest_bytes: bytes,
    *,
    expected_digest: str,
    archive_bytes: bytes,
    inventory_bytes: bytes,
    expected_revision: str,
    expected_created: str,
) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_digest) is None:
        raise IntegrationError("published manifest digest is invalid")
    if sha256(manifest_bytes) != expected_digest:
        raise IntegrationError("published manifest bytes do not match the digest")
    manifest = _strict_object(manifest_bytes, "published OCI manifest")
    expected_keys = {
        "annotations",
        "artifactType",
        "config",
        "layers",
        "mediaType",
        "schemaVersion",
    }
    if set(manifest) != expected_keys:
        raise IntegrationError("published OCI manifest has unexpected fields")
    if manifest["schemaVersion"] != 2 or isinstance(manifest["schemaVersion"], bool):
        raise IntegrationError("published OCI manifest schema is invalid")
    if manifest["mediaType"] != OCI_MANIFEST_MEDIA_TYPE:
        raise IntegrationError("published OCI manifest media type is invalid")
    if manifest["artifactType"] != ARTIFACT_TYPE:
        raise IntegrationError("published OCI artifact type is invalid")
    if manifest["config"] != {
        "data": "e30=",
        "digest": OCI_EMPTY_CONFIG_DIGEST,
        "mediaType": OCI_EMPTY_CONFIG_MEDIA_TYPE,
        "size": 2,
    }:
        raise IntegrationError("published OCI empty config is invalid")
    if manifest["annotations"] != {
        "org.opencontainers.image.created": expected_created,
        "org.opencontainers.image.revision": expected_revision,
        "org.opencontainers.image.source": SOURCE_URL,
    }:
        raise IntegrationError("published OCI annotations are invalid")
    layers = manifest["layers"]
    expected_layers = (
        (ARCHIVE_NAME, ARCHIVE_MEDIA_TYPE, archive_bytes),
        (INVENTORY_NAME, INVENTORY_MEDIA_TYPE, inventory_bytes),
    )
    if not isinstance(layers, list) or len(layers) != len(expected_layers):
        raise IntegrationError("published OCI layer count is invalid")
    for layer, (title, media_type, content) in zip(layers, expected_layers, strict=True):
        if layer != {
            "annotations": {"org.opencontainers.image.title": title},
            "digest": sha256(content),
            "mediaType": media_type,
            "size": len(content),
        }:
            raise IntegrationError(f"published OCI layer is invalid: {title}")
    return manifest


def evidence_bytes(
    *,
    artifact_reference: str,
    archive_bytes: bytes,
    inventory_bytes: bytes,
    revision: str,
    created: str,
    run_id: int,
    run_attempt: int,
) -> bytes:
    repository, separator, digest = artifact_reference.rpartition("@")
    if not separator or repository != "ghcr.io/nclsppr/vps-infra/platform-integration":
        raise IntegrationError("evidence artifact repository is invalid")
    if SHA256_RE.fullmatch(digest) is None:
        raise IntegrationError("evidence artifact digest is invalid")
    if REVISION_RE.fullmatch(revision) is None:
        raise IntegrationError("evidence revision is invalid")
    epoch_from_created(created)
    if isinstance(run_id, bool) or run_id < 1:
        raise IntegrationError("evidence run ID is invalid")
    if isinstance(run_attempt, bool) or run_attempt < 1:
        raise IntegrationError("evidence run attempt is invalid")
    return canonical_json(
        {
            "archive": {
                "media_type": ARCHIVE_MEDIA_TYPE,
                "sha256": sha256(archive_bytes),
                "size": len(archive_bytes),
                "title": ARCHIVE_NAME,
            },
            "artifact": artifact_reference,
            "artifact_type": ARTIFACT_TYPE,
            "created": created,
            "inventory": {
                "media_type": INVENTORY_MEDIA_TYPE,
                "sha256": sha256(inventory_bytes),
                "size": len(inventory_bytes),
                "title": INVENTORY_NAME,
            },
            "run_attempt": run_attempt,
            "run_id": run_id,
            "schema": 1,
            "signer_workflow": SIGNER_WORKFLOW,
            "source": SOURCE_URL,
            "source_revision": revision,
            "verified_gates": [
                "canonical-content",
                "github-provenance",
                "oci-annotations",
                "oci-layer-descriptors",
                "published-manifest-digest",
                "registry-round-trip",
            ],
        }
    )
