"""Persist an accepted artifact so it can be run after its runtime is gone.

A Standard runtime is deliberately disposable: it holds generated source, object trees,
and caches that must not become authority. Deleting it is correct, but it also deleted
the only runnable output, which is why there has never been anything for a ``run`` verb
to execute.

This copies the two things a later run actually needs -- the accepted artifact and the
exact command that invokes it -- into a small durable export under the project's
BUILD_DIR. The runtime stays disposable; the export is what ``litai run`` reads, so
running never depends on transient state.

An export records what was accepted at build time. It is not acceptance evidence and
grants no authority: `litai verify` and `litai rebuild` remain the gates.

Library exports retain typed package/import metadata instead of a command, and verify
the copied package against the Standard directory blob. Their separate copy custody
is published by atomically replacing the record; failed publication preserves the
previous export. Neither a copy nor its self-hash grants acceptance authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError, ensure_safe_directory
from literate_ai.cache_directories import DEFAULT_BUILD_DIRECTORY
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ExecutionDispatchRequest,
    ExecutionWorker,
    HashAlgorithm,
    LifecycleDispatchAction,
    canonical_identity,
    execution_artifact_reference,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct

from .directory_artifacts import require_library_package

EXPORT_SCHEMA = "literate-ai/artifact-export@4"
ENTRYPOINT_COMMAND_SCHEMA = "literate-ai/artifact-entrypoint-command@1"
EXPORT_DIRECTORY = "artifacts"
EXPORT_RECORD = "execution.json"


class ArtifactExportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _bounded_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"artifact export {field} must be a nonempty NUL-free string",
        )
    return value


@dataclass(frozen=True, slots=True)
class ArtifactEntrypointCommand:
    """One named command retained for a multi-entrypoint artifact export."""

    name: str
    kind: str
    deployment_unit: str
    argv: tuple[str, ...]
    environment: dict[str, str]

    def __post_init__(self) -> None:
        for field in ("name", "kind", "deployment_unit"):
            _bounded_text(getattr(self, field), field=field)
        if (
            not self.argv
            or len(self.argv) > 128
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 4096
                or "\x00" in item
                for item in self.argv
            )
        ):
            raise ArtifactExportError(
                "artifact_export.invalid",
                "artifact entrypoint argv must contain 1 to 128 nonempty "
                "NUL-free strings",
            )
        if (
            not isinstance(self.environment, dict)
            or len(self.environment) > 256
            or any(
                not isinstance(name, str)
                or not name
                or "\x00" in name
                or not isinstance(value, str)
                or "\x00" in value
                for name, value in self.environment.items()
            )
        ):
            raise ArtifactExportError(
                "artifact_export.invalid",
                "artifact entrypoint environment must contain NUL-free string pairs",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ENTRYPOINT_COMMAND_SCHEMA,
            "name": self.name,
            "kind": self.kind,
            "deployment_unit": self.deployment_unit,
            "argv": list(self.argv),
            "environment": dict(self.environment),
        }

    @classmethod
    def from_dict(cls, value: Any) -> ArtifactEntrypointCommand:
        required = {
            "schema",
            "name",
            "kind",
            "deployment_unit",
            "argv",
            "environment",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value.get("schema") != ENTRYPOINT_COMMAND_SCHEMA
            or not isinstance(value.get("argv"), list)
            or not isinstance(value.get("environment"), dict)
        ):
            raise ArtifactExportError(
                "artifact_export.invalid",
                "artifact entrypoint command violates its closed schema",
            )
        return cls(
            _bounded_text(value["name"], field="name"),
            _bounded_text(value["kind"], field="kind"),
            _bounded_text(value["deployment_unit"], field="deployment_unit"),
            tuple(value["argv"]),
            dict(value["environment"]),
        )


def _validated_entrypoints(
    values: tuple[ArtifactEntrypointCommand, ...],
    default_entrypoint: str | None,
) -> None:
    if (not values) != (default_entrypoint is None):
        raise ArtifactExportError(
            "artifact_export.invalid",
            "entrypoint commands and default_entrypoint must be present together",
        )
    if not values:
        return
    if len(values) < 2 or len(values) > 256:
        raise ArtifactExportError(
            "artifact_export.invalid",
            "multi-entrypoint exports must contain 2 to 256 commands",
        )
    names = tuple(item.name for item in values)
    if len(set(names)) != len(names):
        raise ArtifactExportError(
            "artifact_export.invalid",
            "artifact entrypoint names must be unique",
        )
    if default_entrypoint not in names:
        raise ArtifactExportError(
            "artifact_export.invalid",
            "default_entrypoint must name one retained entrypoint command",
        )


def artifact_execution_identity(
    artifact_reference: ContentReference,
    entrypoints: tuple[ArtifactEntrypointCommand, ...],
    default_entrypoint: str,
) -> ContentIdentity:
    return canonical_identity(
        {
            "schema": "literate-ai/artifact-export-execution@1",
            "artifact_reference": artifact_reference.to_dict(),
            "entrypoints": [item.to_dict() for item in entrypoints],
            "default_entrypoint": default_entrypoint,
        }
    )


@dataclass(frozen=True, slots=True)
class ArtifactExport:
    component: str
    target_profile: str
    worker_id: str
    worker_identity: ContentIdentity
    locality: str
    artifact: Path | None
    artifact_reference: ContentReference
    authority_identity: ContentIdentity
    argv: tuple[str, ...]
    environment: dict[str, str]
    entrypoints: tuple[ArtifactEntrypointCommand, ...] = ()
    default_entrypoint: str | None = None
    execution_identity: ContentIdentity | None = None
    model_selector: str | None = None
    accepted_source_only: bool = False
    library_product: LibraryArtifactProduct | None = None

    def __post_init__(self) -> None:
        if self.library_product is not None:
            if not isinstance(self.library_product, LibraryArtifactProduct) or any(
                (
                    self.argv,
                    self.environment,
                    self.entrypoints,
                    self.default_entrypoint,
                    self.execution_identity,
                )
            ):
                raise ArtifactExportError(
                    "artifact_export.invalid",
                    "library exports require typed import authority "
                    "and no execution command",
                )
        if not isinstance(self.accepted_source_only, bool):
            raise ArtifactExportError(
                "artifact_export.invalid",
                "artifact export accepted_source_only must be boolean",
            )
        if self.model_selector is not None:
            selector = _bounded_text(self.model_selector, field="model_selector")
            if any(character in selector for character in "\r\n"):
                raise ArtifactExportError(
                    "artifact_export.invalid",
                    "artifact export model_selector must be one line",
                )
        _validated_entrypoints(self.entrypoints, self.default_entrypoint)
        if self.entrypoints:
            if self.execution_identity != artifact_execution_identity(
                self.artifact_reference,
                self.entrypoints,
                self.default_entrypoint or "",
            ):
                raise ArtifactExportError(
                    "artifact_export.invalid",
                    "multi-entrypoint execution identity must bind its exact commands",
                )
            default = next(
                item
                for item in self.entrypoints
                if item.name == self.default_entrypoint
            )
            if default.argv != self.argv or default.environment != self.environment:
                raise ArtifactExportError(
                    "artifact_export.invalid",
                    "top-level command must equal the default entrypoint command",
                )
        elif self.execution_identity is not None:
            raise ArtifactExportError(
                "artifact_export.invalid",
                "single-entrypoint exports cannot carry a multi-entrypoint identity",
            )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": EXPORT_SCHEMA,
            "component": self.component,
            "target_profile": self.target_profile,
            "worker_id": self.worker_id,
            "worker_identity": self.worker_identity.uri,
            "locality": self.locality,
            "artifact": None if self.artifact is None else str(self.artifact),
            "artifact_reference": self.artifact_reference.to_dict(),
            "authority_identity": self.authority_identity.uri,
            "argv": list(self.argv),
            "environment": dict(self.environment),
        }
        # The extension is absent, rather than present-and-empty, so existing
        # single-entrypoint export bytes remain unchanged.
        if self.entrypoints:
            value["entrypoints"] = [item.to_dict() for item in self.entrypoints]
            value["default_entrypoint"] = self.default_entrypoint
            value["execution_identity"] = self.execution_identity.uri
        # ``run`` accepts no model argument: retain an explicit build selector so
        # it can reconstruct the build authority internally. Omission stays absent
        # and preserves the established export bytes.
        if self.model_selector is not None:
            value["model_selector"] = self.model_selector
        # Like the model selector, accepted-source-only mode contributes to the
        # shared build/test/run authority but is not a public ``run`` argument.
        # Preserve the non-default mode so run can reconstruct the build authority.
        if self.accepted_source_only:
            value["accepted_source_only"] = True
        if self.library_product is not None:
            value["library_artifact"] = self.library_product.to_dict()
            value["library_identity"] = self.library_identity.uri
        return value

    @property
    def library_identity(self) -> ContentIdentity:
        if self.library_product is None:
            raise ValueError("executable export has no library custody identity")
        return canonical_identity(
            {
                "schema": "literate-ai/library-export-custody@1",
                "artifact_reference": self.artifact_reference.to_dict(),
                "authority_identity": self.authority_identity.uri,
                "library_artifact": self.library_product.to_dict(),
            }
        )


def export_root(project_root: Path, build_dir: Path | None = None) -> Path:
    base = build_dir or (project_root / DEFAULT_BUILD_DIRECTORY)
    return base / EXPORT_DIRECTORY


def _component_key(component: str) -> str:
    """Use the trailing name so `components/hello` and `hello` resolve alike."""

    cleaned = component.strip().rstrip("/").replace("\\", "/")
    return cleaned.rsplit("/", 1)[-1] or cleaned


def _artifact_content_identity(artifact: Path) -> ContentIdentity:
    if artifact.is_file():
        return ContentIdentity(
            HashAlgorithm.SHA256, hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
    entries: list[dict[str, str]] = []
    for path in sorted(artifact.rglob("*")):
        if path.is_symlink():
            raise ArtifactExportError(
                "artifact_export.invalid",
                "accepted artifact directories cannot contain symbolic links",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArtifactExportError(
                "artifact_export.invalid",
                "accepted artifact directories must contain only regular files",
            )
        entries.append(
            {
                "path": path.relative_to(artifact).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return canonical_identity(
        {"schema": "literate-ai/artifact-directory@1", "entries": entries}
    )


def record_artifact_export(
    project_root: Path,
    component: str,
    *,
    artifact: Path,
    execution_command: dict[str, Any],
    target_profile: str,
    worker: ExecutionWorker,
    dispatch_request: ExecutionDispatchRequest,
    execution_entrypoints: tuple[dict[str, Any], ...] = (),
    build_dir: Path | None = None,
    library_product: LibraryArtifactProduct | None = None,
) -> ArtifactExport:
    """Copy the accepted artifact and rewrite its command to point at the copy."""

    artifact = Path(artifact)
    if not artifact.exists() or artifact.is_symlink():
        raise ArtifactExportError(
            "artifact_export.missing",
            f"accepted artifact is absent or symbolic: {artifact}",
        )
    if (
        not isinstance(worker, ExecutionWorker)
        or worker.target_profile != target_profile
    ):
        raise ArtifactExportError(
            "artifact_export.worker_target_mismatch",
            "artifact target profile must equal the selected worker target profile",
        )
    if (
        not isinstance(dispatch_request, ExecutionDispatchRequest)
        or dispatch_request.worker_identity != worker.identity
        or dispatch_request.action
        not in {LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST}
    ):
        raise ArtifactExportError(
            "artifact_export.dispatch_request_mismatch",
            "artifact export must bind the selected worker's exact dispatch authority",
        )
    if library_product is not None:
        if execution_command or execution_entrypoints:
            raise ArtifactExportError(
                "artifact_export.invalid",
                "library export must not carry execution commands",
            )
        return _record_library_export(
            project_root,
            component,
            artifact,
            library_product,
            target_profile,
            worker,
            dispatch_request,
            build_dir=build_dir,
        )
    argv = tuple(str(item) for item in execution_command.get("argv") or ())
    if not argv:
        raise ArtifactExportError(
            "artifact_export.command_missing",
            "accepted artifact has no recorded execution command",
        )

    destination = export_root(project_root, build_dir) / _component_key(component)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    exported = destination / artifact.name
    if artifact.is_dir():
        shutil.copytree(artifact, exported)
    else:
        shutil.copy2(artifact, exported)
    exported = exported.resolve(strict=True)
    artifact_reference = ContentReference(
        "artifact-export", exported.as_uri(), _artifact_content_identity(exported)
    )

    # The recorded command names the runtime copy, which is about to be deleted. Point
    # every occurrence at the export instead, so the command still runs afterwards.
    original = str(artifact)
    rewritten = tuple(
        str(exported) if item == original else item.replace(original, str(exported))
        for item in argv
    )
    environment = {
        str(key): str(value).replace(original, str(exported))
        for key, value in (execution_command.get("environment") or {}).items()
    }
    entrypoints = tuple(
        ArtifactEntrypointCommand.from_dict(item) for item in execution_entrypoints
    )
    rewritten_entrypoints = tuple(
        ArtifactEntrypointCommand(
            item.name,
            item.kind,
            item.deployment_unit,
            tuple(
                str(exported)
                if argument == original
                else argument.replace(original, str(exported))
                for argument in item.argv
            ),
            {
                name: value.replace(original, str(exported))
                for name, value in item.environment.items()
            },
        )
        for item in entrypoints
    )
    default_entrypoint = (
        None if not rewritten_entrypoints else rewritten_entrypoints[0].name
    )
    execution_identity = (
        None
        if default_entrypoint is None
        else artifact_execution_identity(
            artifact_reference, rewritten_entrypoints, default_entrypoint
        )
    )
    export = ArtifactExport(
        component,
        target_profile,
        worker.worker_id,
        worker.identity,
        "local",
        exported,
        artifact_reference,
        dispatch_request.authority_identity,
        rewritten,
        environment,
        rewritten_entrypoints,
        default_entrypoint,
        execution_identity,
        model_selector=dispatch_request.model_selector,
        accepted_source_only=dispatch_request.accepted_source_only,
    )
    (destination / EXPORT_RECORD).write_text(
        json.dumps(export.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return export


def _record_library_export(
    project_root: Path,
    component: str,
    artifact: Path,
    product: LibraryArtifactProduct,
    target_profile: str,
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    *,
    build_dir: Path | None,
) -> ArtifactExport:
    """Publish a checked package copy without removing the preceding product."""

    try:
        require_library_package(product, artifact)
    except (OSError, ValueError) as exc:
        raise ArtifactExportError("artifact_export.library_invalid", str(exc)) from exc
    destination = export_root(project_root.resolve(), build_dir) / _component_key(
        component
    )
    try:
        ensure_safe_directory(destination)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ArtifactExportError("artifact_export.library_invalid", str(exc)) from exc
    custody = Path(tempfile.mkdtemp(prefix="lib-", dir=destination))
    temporary: Path | None = None
    try:
        copied = custody / artifact.name
        shutil.copytree(artifact, copied)
        require_library_package(product, copied)
        reference = ContentReference(
            "artifact-export", copied.as_uri(), _artifact_content_identity(copied)
        )
        export = ArtifactExport(
            component,
            target_profile,
            worker.worker_id,
            worker.identity,
            "local",
            copied,
            reference,
            request.authority_identity,
            (),
            {},
            model_selector=request.model_selector,
            accepted_source_only=request.accepted_source_only,
            library_product=product,
        )
        descriptor, name = tempfile.mkstemp(
            prefix=".record-", suffix=".json", dir=destination
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                (json.dumps(export.to_dict(), indent=2) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination / EXPORT_RECORD)
        return export
    except BaseException as exc:
        # Only this invocation's unpublished paths; older accepted exports survive.
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        shutil.rmtree(custody)
        if isinstance(exc, (OSError, ValueError)) and not isinstance(
            exc, ArtifactExportError
        ):
            raise ArtifactExportError(
                "artifact_export.library_invalid", str(exc)
            ) from exc
        raise


def record_remote_artifact_export(
    project_root: Path,
    component: str,
    *,
    artifact_reference: ContentReference,
    target_profile: str,
    worker: ExecutionWorker,
    dispatch_request: ExecutionDispatchRequest,
    build_dir: Path | None = None,
    library_product: LibraryArtifactProduct | None = None,
) -> ArtifactExport:
    """Persist a durable provider reference returned by a command dispatcher."""

    try:
        reference = execution_artifact_reference(artifact_reference)
    except (ContractValidationError, TypeError, ValueError) as exc:
        raise ArtifactExportError(
            "artifact_export.reference_invalid",
            "remote artifact export reference is invalid",
        ) from exc
    if reference is None or reference.uri.startswith("file:"):
        raise ArtifactExportError(
            "artifact_export.reference_not_durable",
            "remote artifact exports must use a durable non-file URI",
        )
    if (
        not isinstance(worker, ExecutionWorker)
        or worker.target_profile != target_profile
        or not isinstance(dispatch_request, ExecutionDispatchRequest)
        or dispatch_request.worker_identity != worker.identity
        or dispatch_request.artifact_reference is not None
        or dispatch_request.action
        not in {LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST}
    ):
        raise ArtifactExportError(
            "artifact_export.dispatch_request_mismatch",
            "remote artifact export must bind its exact build/test dispatch authority",
        )
    export = ArtifactExport(
        component,
        target_profile,
        worker.worker_id,
        worker.identity,
        "worker",
        None,
        reference,
        dispatch_request.authority_identity,
        (),
        {},
        model_selector=dispatch_request.model_selector,
        accepted_source_only=dispatch_request.accepted_source_only,
        library_product=library_product,
    )
    destination = export_root(project_root.resolve(), build_dir) / _component_key(
        component
    )
    temporary: Path | None = None
    try:
        ensure_safe_directory(destination)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".record-", suffix=".json", dir=destination, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(
                (json.dumps(export.to_dict(), indent=2) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination / EXPORT_RECORD)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ArtifactExportError(
            "artifact_export.publication_failed", str(exc)
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return export


def load_artifact_export(
    project_root: Path, component: str, *, build_dir: Path | None = None
) -> ArtifactExport:
    """Read a previously exported artifact, failing closed when it is absent."""

    key = _component_key(component)
    record = export_root(project_root, build_dir) / key / EXPORT_RECORD
    if not record.is_file():
        raise ArtifactExportError(
            "artifact_export.not_built",
            f"no built artifact for {key!r}; run `litai build {component}` first",
        )
    try:
        value = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactExportError(
            "artifact_export.invalid", f"artifact export for {key!r} is unreadable"
        ) from exc
    required = {
        "schema",
        "component",
        "target_profile",
        "worker_id",
        "worker_identity",
        "locality",
        "artifact",
        "artifact_reference",
        "authority_identity",
        "argv",
        "environment",
    }
    optional = {
        "entrypoints",
        "default_entrypoint",
        "execution_identity",
        "model_selector",
        "accepted_source_only",
        "library_artifact",
        "library_identity",
    }
    keys = set(value) if isinstance(value, dict) else set()
    if (
        not isinstance(value, dict)
        or not required.issubset(keys)
        or not (keys - required).issubset(optional)
        or value.get("schema") != EXPORT_SCHEMA
        or not isinstance(value.get("component"), str)
        or not value["component"]
        or _component_key(value["component"]) != key
        or not isinstance(value.get("target_profile"), str)
        or not value["target_profile"]
        or not isinstance(value.get("worker_id"), str)
        or not value["worker_id"]
        or value.get("locality") not in {"local", "worker"}
        or not (value.get("artifact") is None or isinstance(value.get("artifact"), str))
        or not isinstance(value.get("argv"), list)
        or any(not isinstance(item, str) or not item for item in value["argv"])
        or not isinstance(value.get("environment"), dict)
        or not isinstance(value.get("accepted_source_only", False), bool)
        or any(
            not isinstance(name, str) or not isinstance(environment_value, str)
            for name, environment_value in value["environment"].items()
        )
    ):
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"artifact export for {key!r} violates its closed schema",
        )
    try:
        library_product = (
            LibraryArtifactProduct.from_dict(value["library_artifact"])
            if "library_artifact" in value
            else None
        )
        if (library_product is None) != ("library_identity" not in value):
            raise ValueError(
                "library product and custody identity must appear together"
            )
        entrypoints = tuple(
            ArtifactEntrypointCommand.from_dict(item)
            for item in value.get("entrypoints", [])
        )
        default_entrypoint = value.get("default_entrypoint")
        _validated_entrypoints(entrypoints, default_entrypoint)
        execution_identity = (
            None
            if value.get("execution_identity") is None
            else ContentIdentity.parse_uri(value["execution_identity"])
        )
    except (ArtifactExportError, TypeError, ValueError) as exc:
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"artifact export for {key!r} has invalid entrypoint commands",
        ) from exc
    expected_root = record.parent.resolve(strict=True)
    artifact_value = value["artifact"]
    resolved_artifact: Path | None = None
    if value["locality"] == "local":
        if not isinstance(artifact_value, str) or (
            not value["argv"] and library_product is None
        ):
            raise ArtifactExportError(
                "artifact_export.invalid",
                f"local artifact export for {key!r} lacks its artifact or command",
            )
        artifact = Path(artifact_value)
        try:
            resolved_artifact = artifact.resolve(strict=True)
        except OSError as exc:
            raise ArtifactExportError(
                "artifact_export.missing",
                f"exported artifact for {key!r} is gone; rebuild it with `litai build`",
            ) from exc
        if (
            artifact.is_symlink()
            or not resolved_artifact.is_relative_to(expected_root)
            or resolved_artifact == expected_root
        ):
            raise ArtifactExportError(
                "artifact_export.invalid",
                f"exported artifact for {key!r} escaped its durable export directory",
            )
    elif artifact_value is not None or value["argv"] or value["environment"]:
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"worker artifact export for {key!r} contains local execution state",
        )
    try:
        worker_identity = ContentIdentity.parse_uri(value["worker_identity"])
        authority_identity = ContentIdentity.parse_uri(value["authority_identity"])
        artifact_reference = execution_artifact_reference(
            value["artifact_reference"], path="ArtifactExport.artifact_reference"
        )
    except (ContractValidationError, TypeError, ValueError) as exc:
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"artifact export for {key!r} has an invalid worker identity",
        ) from exc
    if artifact_reference is None:
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"artifact export for {key!r} has no artifact reference",
        )
    if value["locality"] == "local":
        assert resolved_artifact is not None
        if (
            artifact_reference.uri != resolved_artifact.as_uri()
            or artifact_reference.identity
            != _artifact_content_identity(resolved_artifact)
        ):
            raise ArtifactExportError(
                "artifact_export.changed",
                f"exported artifact for {key!r} changed after it was accepted",
            )
    elif artifact_reference.uri.startswith("file:"):
        raise ArtifactExportError(
            "artifact_export.invalid",
            f"worker artifact export for {key!r} is not durably addressable",
        )
    export = ArtifactExport(
        value["component"],
        value["target_profile"],
        value["worker_id"],
        worker_identity,
        value["locality"],
        resolved_artifact,
        artifact_reference,
        authority_identity,
        tuple(value["argv"]),
        dict(value["environment"]),
        entrypoints,
        default_entrypoint,
        execution_identity,
        value.get("model_selector"),
        value.get("accepted_source_only", False),
        library_product,
    )
    if library_product is not None:
        if value["library_identity"] != export.library_identity.uri:
            raise ArtifactExportError(
                "artifact_export.changed", "library custody metadata changed"
            )
        if resolved_artifact is not None:
            try:
                require_library_package(library_product, resolved_artifact)
            except (OSError, ValueError) as exc:
                raise ArtifactExportError("artifact_export.changed", str(exc)) from exc
    return export


def available_exports(
    project_root: Path, build_dir: Path | None = None
) -> tuple[str, ...]:
    base = export_root(project_root, build_dir)
    if not base.is_dir():
        return ()
    return tuple(
        sorted(item.name for item in base.iterdir() if (item / EXPORT_RECORD).is_file())
    )


__all__ = [
    "EXPORT_DIRECTORY",
    "ENTRYPOINT_COMMAND_SCHEMA",
    "EXPORT_RECORD",
    "EXPORT_SCHEMA",
    "ArtifactExport",
    "ArtifactExportError",
    "ArtifactEntrypointCommand",
    "artifact_execution_identity",
    "available_exports",
    "export_root",
    "load_artifact_export",
    "record_artifact_export",
    "record_remote_artifact_export",
]
