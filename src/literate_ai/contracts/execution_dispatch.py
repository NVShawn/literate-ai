"""Minimal routing contracts for local, SSH, or external lifecycle dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    optional_string,
    string_value,
)
from .identity import ContentIdentity, ContentReference, canonical_identity
from .library_products import LibraryArtifactProduct, library_worker_manifest
from .remote_evidence import (
    RemoteEvidenceCustodyReceipt,
    RemoteLifecycleEvidenceManifest,
)

GPU_REQUIREMENT_SCHEMA = "urn:literate-ai:schema:v1:gpu-requirement"
EXECUTION_REQUIREMENTS_SCHEMA = "urn:literate-ai:schema:v1:execution-requirements"
EXECUTION_WORKER_PARAMETER_SCHEMA = (
    "urn:literate-ai:schema:v1:execution-worker-parameter"
)
EXECUTION_WORKER_PARAMETER_VALUE_SCHEMA = (
    "urn:literate-ai:schema:v1:execution-worker-parameter-value"
)
EXECUTION_WORKER_ENVIRONMENT_SCHEMA = (
    "urn:literate-ai:schema:v1:execution-worker-environment"
)
EXECUTION_WORKER_SCHEMA = "urn:literate-ai:schema:v1:execution-worker"
EXECUTION_WORKER_CATALOG_SCHEMA = "urn:literate-ai:schema:v1:execution-worker-catalog"
EXECUTION_DISPATCH_REQUEST_SCHEMA = (
    "urn:literate-ai:schema:v2:execution-dispatch-request"
)
EXECUTION_SOURCE_MATERIALIZATION_SCHEMA = (
    "urn:literate-ai:schema:v2:execution-source-materialization"
)
OBSERVED_EXECUTION_ENVIRONMENT_SCHEMA = (
    "urn:literate-ai:schema:v2:observed-execution-environment"
)
EXECUTION_DISPATCH_RESULT_SCHEMA = "urn:literate-ai:schema:v2:execution-dispatch-result"
REMOTE_EXECUTION_CONTROL_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v1:remote-execution-control-result"
)

EXECUTION_ARTIFACT_REFERENCE_KIND = "artifact-export"
EXECUTION_EVIDENCE_REFERENCE_KIND = "remote-lifecycle-evidence"

MAX_EXECUTION_WORKERS = 256
MAX_WORKER_PARAMETERS = 64
MAX_WORKER_ARGUMENTS = 128
MAX_REMOTE_CONTROL_SUMMARY_BYTES = 8192
MAX_REMOTE_EVIDENCE_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_REMOTE_EVIDENCE_BUNDLE_BYTES = 8 * 1024 * 1024 * 1024

_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,255}$")
_ENVIRONMENT = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SSH_ENDPOINT = re.compile(
    r"^[A-Za-z0-9._-]+@[A-Za-z0-9](?:[A-Za-z0-9.:-]{0,253}[A-Za-z0-9])?$"
)
_WORKSPACE = re.compile(r"^(?:~/|/)[A-Za-z0-9._/-]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OS_FAMILIES = frozenset({"linux", "macos", "windows"})
_REQUEST_FILE_PLACEHOLDER = "{request_file}"


def _identifier(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=64)
    if not _ID.fullmatch(raw):
        fail(path, "must be a portable lower-case identifier")
    return raw


def _name(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=128)
    if not _NAME.fullmatch(raw):
        fail(path, "must be a portable lower-case dotted name")
    return raw


def _value(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=256)
    if not _VALUE.fullmatch(raw):
        fail(path, "must be a portable scalar value")
    return raw


def _hardware_model(value: Any, path: str) -> str:
    return string_value(value, path, max_length=256)


def _identity(value: Any, path: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=path)
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))


def _artifact_reference(value: Any, path: str) -> ContentReference | None:
    if value is None:
        return None
    try:
        reference = (
            value
            if isinstance(value, ContentReference)
            else ContentReference.from_dict(value, path=path)
        )
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))
    if reference.kind != EXECUTION_ARTIFACT_REFERENCE_KIND:
        fail(path, f"kind must be {EXECUTION_ARTIFACT_REFERENCE_KIND!r}")
    if not isinstance(reference.identity, ContentIdentity):
        fail(f"{path}.identity", "must be a ContentIdentity")
    parsed = urlsplit(reference.uri)
    if not parsed.scheme or not re.fullmatch(r"[a-z][a-z0-9+.-]*", parsed.scheme):
        fail(f"{path}.uri", "must be an absolute URI with a lower-case scheme")
    if parsed.username is not None or parsed.password is not None:
        fail(f"{path}.uri", "must not embed credentials")
    if parsed.query or parsed.fragment:
        fail(f"{path}.uri", "must not contain a query or fragment")
    if parsed.scheme == "file" and (
        parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/")
    ):
        fail(f"{path}.uri", "file references must name an absolute local path")
    return reference


def execution_artifact_reference(
    value: Any, *, path: str = "artifact_reference"
) -> ContentReference | None:
    """Validate one immutable, credential-free artifact-export locator."""

    return _artifact_reference(value, path)


def _evidence_reference(value: Any, path: str) -> ContentReference | None:
    if value is None:
        return None
    try:
        reference = (
            value
            if isinstance(value, ContentReference)
            else ContentReference.from_dict(value, path=path)
        )
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))
    if reference.kind != EXECUTION_EVIDENCE_REFERENCE_KIND:
        fail(path, f"kind must be {EXECUTION_EVIDENCE_REFERENCE_KIND!r}")
    if not isinstance(reference.identity, ContentIdentity):
        fail(f"{path}.identity", "must be a ContentIdentity")
    parsed = urlsplit(reference.uri)
    if (
        parsed.scheme != "staged"
        or parsed.netloc
        or parsed.path != "remote-evidence.tar.gz"
        or parsed.query
        or parsed.fragment
    ):
        fail(
            f"{path}.uri",
            "must be the transport-defined staged:remote-evidence.tar.gz locator",
        )
    return reference


def _source_archive_reference(value: Any, path: str) -> ContentReference | None:
    if value is None:
        return None
    try:
        reference = (
            value
            if isinstance(value, ContentReference)
            else ContentReference.from_dict(value, path=path)
        )
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))
    if reference.kind != "source-archive":
        fail(path, "kind must be 'source-archive'")
    parsed = urlsplit(reference.uri)
    if parsed.scheme != "staged" or parsed.netloc or parsed.path != "source.tar.gz":
        fail(path, "URI must be the transport-defined staged:source.tar.gz locator")
    if parsed.username is not None or parsed.password is not None:
        fail(path, "must not embed credentials")
    if parsed.query or parsed.fragment:
        fail(path, "must not contain a query or fragment")
    return reference


def _accepted_source_cache_reference(value: Any, path: str) -> ContentReference | None:
    if value is None:
        return None
    try:
        reference = (
            value
            if isinstance(value, ContentReference)
            else ContentReference.from_dict(value, path=path)
        )
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))
    if reference.kind != "accepted-source-cache-archive":
        fail(path, "kind must be 'accepted-source-cache-archive'")
    parsed = urlsplit(reference.uri)
    if (
        parsed.scheme != "staged"
        or parsed.netloc
        or parsed.path != "accepted-source-cache.tar.gz"
    ):
        fail(
            path,
            "URI must be the transport-defined "
            "staged:accepted-source-cache.tar.gz locator",
        )
    if parsed.username is not None or parsed.password is not None:
        fail(path, "must not embed credentials")
    if parsed.query or parsed.fragment:
        fail(path, "must not contain a query or fragment")
    return reference


def _repository_url(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=4096)
    if raw.startswith("-") or any(character in raw for character in "\r\n\x00"):
        fail(path, "must be a safe repository URL")
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+(?:\.git)?", raw):
        return raw
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path:
        fail(path, "must be an HTTPS, SSH, or Git SCP-style repository URL")
    if parsed.password is not None or parsed.query or parsed.fragment:
        fail(path, "must not embed credentials, a query, or a fragment")
    if parsed.scheme == "https" and parsed.username is not None:
        fail(path, "HTTPS repository URLs must not embed credentials")
    return raw


def _sorted_unique(values: tuple[str, ...], path: str) -> None:
    if values != tuple(sorted(set(values))):
        fail(path, "must be uniquely sorted")


def _workspace(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=4096)
    if not _WORKSPACE.fullmatch(raw):
        fail(path, "must be an absolute or home-relative POSIX-style path")
    parts = raw.removeprefix("~/").removeprefix("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        fail(path, "cannot contain empty or traversal segments")
    return raw


def _bounded_output(value: Any, path: str) -> str:
    text = string_value(value, path, nonempty=False, max_length=65536)
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        fail(path, "must not contain binary control characters")
    return text


# Nested payload on execution-dispatch-result / remote-execution-control-result.
# Keep the const aligned with literate_ai.adapters.coverage_gaps.
COVERAGE_GAP_REPORT_SCHEMA = "literate-ai/coverage-gap-report@3"
_COVERAGE_GAP_REASONS = frozenset(
    {
        "not_implemented",
        "todo_comment",
        "none_bound_handler",
        "pass_bodied_abstract",
    }
)
_COVERAGE_GAP_GATES = frozenset({"fail_closed", "advisory"})
_MAXIMUM_COVERAGE_GAPS = 256


def _coverage_gaps_payload(value: Any, path: str) -> dict[str, object] | None:
    """Accept null or one typed coverage-gap report. Omitted by older workers."""

    if value is None:
        return None
    data = contract_fields(
        value,
        path=path,
        schema_uri=COVERAGE_GAP_REPORT_SCHEMA,
        required=frozenset({"entrypoint_count", "files_scanned", "gaps", "identity"}),
    )
    entrypoint_count = int_value(
        data["entrypoint_count"], f"{path}.entrypoint_count", maximum=65536
    )
    files_scanned = int_value(
        data["files_scanned"], f"{path}.files_scanned", maximum=4096
    )
    raw_gaps = list_value(data["gaps"], f"{path}.gaps")
    if len(raw_gaps) > _MAXIMUM_COVERAGE_GAPS:
        fail(f"{path}.gaps", f"must contain at most {_MAXIMUM_COVERAGE_GAPS} findings")
    gaps: list[dict[str, str]] = []
    for index, item in enumerate(raw_gaps):
        item_path = f"{path}.gaps[{index}]"
        gap_data = fields(
            item,
            path=item_path,
            required=frozenset({"file_path", "reason", "detail", "gate"}),
        )
        reason = string_value(gap_data["reason"], f"{item_path}.reason", max_length=64)
        gate = string_value(gap_data["gate"], f"{item_path}.gate", max_length=32)
        if reason not in _COVERAGE_GAP_REASONS:
            fail(f"{item_path}.reason", "must be a known stub-marker reason")
        if gate not in _COVERAGE_GAP_GATES:
            fail(f"{item_path}.gate", "must be fail_closed or advisory")
        gaps.append(
            {
                "file_path": string_value(
                    gap_data["file_path"], f"{item_path}.file_path", max_length=1024
                ),
                "reason": reason,
                "detail": string_value(
                    gap_data["detail"], f"{item_path}.detail", max_length=1024
                ),
                "gate": gate,
            }
        )
    identity = string_value(data["identity"], f"{path}.identity", max_length=4096)
    material = {
        "schema": COVERAGE_GAP_REPORT_SCHEMA,
        "entrypoint_count": entrypoint_count,
        "files_scanned": files_scanned,
        "gaps": gaps,
    }
    expected = canonical_identity(material).uri
    if identity != expected:
        fail(f"{path}.identity", "must match the canonical report identity")
    return {**material, "identity": identity}


class ExecutionWorkerKind(StrEnum):
    LOCAL = "local"
    SSH = "ssh"
    COMMAND = "command"


class LifecycleDispatchAction(StrEnum):
    BUILD = "build"
    TEST = "test"
    RUN = "run"


class DispatchResultStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"


class ExecutionSourceMaterializationKind(StrEnum):
    GIT = "git"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class GpuRequirement:
    vendor: str | None = None
    model: str | None = None
    minimum_count: int | None = None
    minimum_memory_mib: int | None = None
    capabilities: tuple[str, ...] | None = None

    SCHEMA: ClassVar[str] = GPU_REQUIREMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.vendor is not None:
            _value(self.vendor, "GpuRequirement.vendor")
        if self.model is not None:
            _hardware_model(self.model, "GpuRequirement.model")
        if self.minimum_count is not None:
            int_value(
                self.minimum_count,
                "GpuRequirement.minimum_count",
                minimum=1,
                maximum=1024,
            )
        if self.minimum_memory_mib is not None:
            int_value(
                self.minimum_memory_mib,
                "GpuRequirement.minimum_memory_mib",
                minimum=1,
            )
        if self.capabilities is not None:
            for index, capability in enumerate(self.capabilities):
                _value(capability, f"GpuRequirement.capabilities[{index}]")
            _sorted_unique(self.capabilities, "GpuRequirement.capabilities")

    @property
    def constrained(self) -> bool:
        return any(
            value is not None
            for value in (
                self.vendor,
                self.model,
                self.minimum_count,
                self.minimum_memory_mib,
                self.capabilities,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "vendor": self.vendor,
            "model": self.model,
            "minimum_count": self.minimum_count,
            "minimum_memory_mib": self.minimum_memory_mib,
            "capabilities": (
                None if self.capabilities is None else list(self.capabilities)
            ),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "GpuRequirement") -> GpuRequirement:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "vendor",
                    "model",
                    "minimum_count",
                    "minimum_memory_mib",
                    "capabilities",
                }
            ),
        )
        return cls(
            None
            if data["vendor"] is None
            else _value(data["vendor"], f"{path}.vendor"),
            (
                None
                if data["model"] is None
                else _hardware_model(data["model"], f"{path}.model")
            ),
            (
                None
                if data["minimum_count"] is None
                else int_value(
                    data["minimum_count"],
                    f"{path}.minimum_count",
                    minimum=1,
                    maximum=1024,
                )
            ),
            (
                None
                if data["minimum_memory_mib"] is None
                else int_value(
                    data["minimum_memory_mib"],
                    f"{path}.minimum_memory_mib",
                    minimum=1,
                )
            ),
            (
                None
                if data["capabilities"] is None
                else tuple(
                    _value(item, f"{path}.capabilities[{index}]")
                    for index, item in enumerate(
                        list_value(data["capabilities"], f"{path}.capabilities")
                    )
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRequirements:
    os_family: str | None = None
    os_version: str | None = None
    cpu_architecture: str | None = None
    minimum_cpu_cores: int | None = None
    minimum_memory_mib: int | None = None
    gpu: GpuRequirement = field(default_factory=GpuRequirement)

    SCHEMA: ClassVar[str] = EXECUTION_REQUIREMENTS_SCHEMA

    def __post_init__(self) -> None:
        if self.os_family is not None and self.os_family not in _OS_FAMILIES:
            fail("ExecutionRequirements.os_family", "must be linux, macos, or windows")
        for value, path in (
            (self.os_version, "ExecutionRequirements.os_version"),
            (self.cpu_architecture, "ExecutionRequirements.cpu_architecture"),
        ):
            if value is not None:
                _value(value, path)
        if self.minimum_cpu_cores is not None:
            int_value(
                self.minimum_cpu_cores,
                "ExecutionRequirements.minimum_cpu_cores",
                minimum=1,
                maximum=65536,
            )
        if self.minimum_memory_mib is not None:
            int_value(
                self.minimum_memory_mib,
                "ExecutionRequirements.minimum_memory_mib",
                minimum=1,
            )
        if not isinstance(self.gpu, GpuRequirement):
            fail("ExecutionRequirements.gpu", "must be a GpuRequirement")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "os_family": self.os_family,
            "os_version": self.os_version,
            "cpu_architecture": self.cpu_architecture,
            "minimum_cpu_cores": self.minimum_cpu_cores,
            "minimum_memory_mib": self.minimum_memory_mib,
            "gpu": self.gpu.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionRequirements"
    ) -> ExecutionRequirements:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "os_family",
                    "os_version",
                    "cpu_architecture",
                    "minimum_cpu_cores",
                    "minimum_memory_mib",
                    "gpu",
                }
            ),
        )
        return cls(
            optional_string(data["os_family"], f"{path}.os_family"),
            optional_string(data["os_version"], f"{path}.os_version"),
            optional_string(data["cpu_architecture"], f"{path}.cpu_architecture"),
            (
                None
                if data["minimum_cpu_cores"] is None
                else int_value(
                    data["minimum_cpu_cores"],
                    f"{path}.minimum_cpu_cores",
                    minimum=1,
                    maximum=65536,
                )
            ),
            (
                None
                if data["minimum_memory_mib"] is None
                else int_value(
                    data["minimum_memory_mib"],
                    f"{path}.minimum_memory_mib",
                    minimum=1,
                )
            ),
            GpuRequirement.from_dict(data["gpu"], path=f"{path}.gpu"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionWorkerParameter:
    name: str
    allowed_values: tuple[str, ...]
    required: bool = False
    default: str | None = None

    SCHEMA: ClassVar[str] = EXECUTION_WORKER_PARAMETER_SCHEMA

    def __post_init__(self) -> None:
        _name(self.name, "ExecutionWorkerParameter.name")
        if not self.allowed_values or len(self.allowed_values) > 256:
            fail(
                "ExecutionWorkerParameter.allowed_values",
                "must contain 1 to 256 values",
            )
        for index, item in enumerate(self.allowed_values):
            _value(item, f"ExecutionWorkerParameter.allowed_values[{index}]")
        _sorted_unique(self.allowed_values, "ExecutionWorkerParameter.allowed_values")
        if not isinstance(self.required, bool):
            fail("ExecutionWorkerParameter.required", "must be a boolean")
        if self.default is not None:
            _value(self.default, "ExecutionWorkerParameter.default")
            if self.default not in self.allowed_values:
                fail("ExecutionWorkerParameter.default", "must be an allowed value")
        if self.required and self.default is not None:
            fail(
                "ExecutionWorkerParameter.default", "required parameters cannot default"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "name": self.name,
            "allowed_values": list(self.allowed_values),
            "required": self.required,
            "default": self.default,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionWorkerParameter"
    ) -> ExecutionWorkerParameter:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"name", "allowed_values", "required", "default"}),
        )
        return cls(
            _name(data["name"], f"{path}.name"),
            tuple(
                _value(item, f"{path}.allowed_values[{index}]")
                for index, item in enumerate(
                    list_value(data["allowed_values"], f"{path}.allowed_values")
                )
            ),
            bool_value(data["required"], f"{path}.required"),
            None
            if data["default"] is None
            else _value(data["default"], f"{path}.default"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionWorkerEnvironment:
    name: str
    source_variable: str
    required: bool = True

    SCHEMA: ClassVar[str] = EXECUTION_WORKER_ENVIRONMENT_SCHEMA

    def __post_init__(self) -> None:
        for value, path in (
            (self.name, "ExecutionWorkerEnvironment.name"),
            (self.source_variable, "ExecutionWorkerEnvironment.source_variable"),
        ):
            if not isinstance(value, str) or not _ENVIRONMENT.fullmatch(value):
                fail(path, "must be a portable environment-variable name")
        if not isinstance(self.required, bool):
            fail("ExecutionWorkerEnvironment.required", "must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "name": self.name,
            "source_variable": self.source_variable,
            "required": self.required,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionWorkerEnvironment"
    ) -> ExecutionWorkerEnvironment:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"name", "source_variable", "required"}),
        )
        return cls(
            string_value(data["name"], f"{path}.name", max_length=128),
            string_value(
                data["source_variable"], f"{path}.source_variable", max_length=128
            ),
            bool_value(data["required"], f"{path}.required"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionWorker:
    worker_id: str
    kind: ExecutionWorkerKind
    target_profile: str = "host"
    requirements: ExecutionRequirements = field(default_factory=ExecutionRequirements)
    parameters: tuple[ExecutionWorkerParameter, ...] = ()
    endpoint: str | None = None
    workspace: str | None = None
    command: tuple[str, ...] = ()
    environment: tuple[ExecutionWorkerEnvironment, ...] = ()
    transport: str = "ssh"
    lifecycle_executable: str | None = None
    slots: int = 1

    SCHEMA: ClassVar[str] = EXECUTION_WORKER_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.worker_id, "ExecutionWorker.worker_id")
        if not isinstance(self.kind, ExecutionWorkerKind):
            fail("ExecutionWorker.kind", "must be typed")
        _identifier(self.target_profile, "ExecutionWorker.target_profile")
        if not isinstance(self.requirements, ExecutionRequirements):
            fail("ExecutionWorker.requirements", "must be typed ExecutionRequirements")
        if len(self.parameters) > MAX_WORKER_PARAMETERS:
            fail("ExecutionWorker.parameters", "contains too many parameters")
        parameter_names = tuple(item.name for item in self.parameters)
        _sorted_unique(parameter_names, "ExecutionWorker.parameters")
        environment_names = tuple(item.name for item in self.environment)
        _sorted_unique(environment_names, "ExecutionWorker.environment")
        _identifier(self.transport, "ExecutionWorker.transport")
        int_value(self.slots, "ExecutionWorker.slots", minimum=1, maximum=256)
        if self.kind is ExecutionWorkerKind.LOCAL:
            if (
                self.endpoint is not None
                or self.workspace is not None
                or self.command
                or self.environment
                or self.transport != "ssh"
                or self.lifecycle_executable is not None
            ):
                fail(
                    "ExecutionWorker",
                    "local workers cannot configure transport or command fields",
                )
        elif self.kind is ExecutionWorkerKind.SSH:
            if not isinstance(self.endpoint, str) or not _SSH_ENDPOINT.fullmatch(
                self.endpoint
            ):
                fail("ExecutionWorker.endpoint", "must be a username@host SSH endpoint")
            if self.workspace is None:
                fail("ExecutionWorker.workspace", "is required for SSH workers")
            _workspace(self.workspace, "ExecutionWorker.workspace")
            if self.lifecycle_executable is not None:
                _workspace(
                    self.lifecycle_executable,
                    "ExecutionWorker.lifecycle_executable",
                )
            if self.command or self.environment:
                fail(
                    "ExecutionWorker",
                    "SSH workers cannot configure dispatcher command fields",
                )
        else:
            if (
                self.endpoint is not None
                or self.workspace is not None
                or self.transport != "ssh"
                or self.lifecycle_executable is not None
            ):
                fail("ExecutionWorker", "command workers cannot configure SSH fields")
            if not self.command or len(self.command) > MAX_WORKER_ARGUMENTS:
                fail("ExecutionWorker.command", "must contain 1 to 128 arguments")
            if any(
                not isinstance(item, str) or not item or "\x00" in item
                for item in self.command
            ):
                fail(
                    "ExecutionWorker.command",
                    "must contain nonempty NUL-free arguments",
                )
            placeholders = tuple(
                index
                for index, item in enumerate(self.command)
                if item == _REQUEST_FILE_PLACEHOLDER
            )
            if len(placeholders) > 1:
                fail("ExecutionWorker.command", "may contain request_file at most once")
            if self.command[0] == _REQUEST_FILE_PLACEHOLDER:
                fail(
                    "ExecutionWorker.command[0]",
                    "dispatcher executable cannot be the request_file placeholder",
                )
            if any(
                ("{" in item or "}" in item) and item != _REQUEST_FILE_PLACEHOLDER
                for item in self.command
            ):
                fail(
                    "ExecutionWorker.command",
                    "supports only the complete {request_file} placeholder",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "worker_id": self.worker_id,
            "kind": self.kind.value,
            "target_profile": self.target_profile,
            "requirements": self.requirements.to_dict(),
            "parameters": [item.to_dict() for item in self.parameters],
            "endpoint": self.endpoint,
            "workspace": self.workspace,
            "command": list(self.command),
            "environment": [item.to_dict() for item in self.environment],
            "transport": self.transport,
            "lifecycle_executable": self.lifecycle_executable,
            "slots": self.slots,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ExecutionWorker") -> ExecutionWorker:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "worker_id",
                    "kind",
                    "target_profile",
                    "requirements",
                    "parameters",
                    "endpoint",
                    "workspace",
                    "command",
                    "environment",
                }
            ),
            optional=frozenset({"transport", "lifecycle_executable", "slots"}),
        )
        return cls(
            _identifier(data["worker_id"], f"{path}.worker_id"),
            enum_value(ExecutionWorkerKind, data["kind"], f"{path}.kind"),
            _identifier(data["target_profile"], f"{path}.target_profile"),
            ExecutionRequirements.from_dict(
                data["requirements"], path=f"{path}.requirements"
            ),
            tuple(
                ExecutionWorkerParameter.from_dict(
                    item, path=f"{path}.parameters[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["parameters"], f"{path}.parameters")
                )
            ),
            optional_string(data["endpoint"], f"{path}.endpoint"),
            optional_string(data["workspace"], f"{path}.workspace"),
            tuple(
                string_value(item, f"{path}.command[{index}]")
                for index, item in enumerate(
                    list_value(data["command"], f"{path}.command")
                )
            ),
            tuple(
                ExecutionWorkerEnvironment.from_dict(
                    item, path=f"{path}.environment[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["environment"], f"{path}.environment")
                )
            ),
            _identifier(data.get("transport", "ssh"), f"{path}.transport"),
            optional_string(
                data.get("lifecycle_executable"), f"{path}.lifecycle_executable"
            ),
            int_value(data.get("slots", 1), f"{path}.slots", minimum=1, maximum=256),
        )


@dataclass(frozen=True, slots=True)
class ExecutionWorkerCatalog:
    workers: tuple[ExecutionWorker, ...]

    SCHEMA: ClassVar[str] = EXECUTION_WORKER_CATALOG_SCHEMA

    def __post_init__(self) -> None:
        if not self.workers or len(self.workers) > MAX_EXECUTION_WORKERS:
            fail("ExecutionWorkerCatalog.workers", "must contain 1 to 256 workers")
        worker_ids = tuple(item.worker_id for item in self.workers)
        _sorted_unique(worker_ids, "ExecutionWorkerCatalog.workers")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def worker(self, worker_id: str) -> ExecutionWorker:
        selected = _identifier(worker_id, "ExecutionWorkerCatalog.worker_id")
        match = next(
            (item for item in self.workers if item.worker_id == selected), None
        )
        if match is None:
            fail(
                "ExecutionWorkerCatalog.worker_id",
                f"unknown execution worker {selected!r}",
            )
        return match

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "workers": [item.to_dict() for item in self.workers],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionWorkerCatalog"
    ) -> ExecutionWorkerCatalog:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"workers"}),
        )
        return cls(
            tuple(
                ExecutionWorker.from_dict(item, path=f"{path}.workers[{index}]")
                for index, item in enumerate(
                    list_value(data["workers"], f"{path}.workers")
                )
            )
        )


def resolve_worker_parameters(
    worker: ExecutionWorker, supplied: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Validate one exact worker's request parameters without selecting any worker."""

    if not isinstance(worker, ExecutionWorker):
        raise TypeError("worker parameter resolution requires an ExecutionWorker")
    names = tuple(item[0] for item in supplied)
    _sorted_unique(names, "execution worker parameter values")
    supplied_by_name: dict[str, str] = {}
    for index, pair in enumerate(supplied):
        if not isinstance(pair, tuple) or len(pair) != 2:
            fail(
                f"execution worker parameter values[{index}]",
                "must be a name/value pair",
            )
        supplied_by_name[
            _name(pair[0], f"execution worker parameter values[{index}].name")
        ] = _value(pair[1], f"execution worker parameter values[{index}].value")
    definitions = {item.name: item for item in worker.parameters}
    unknown = sorted(set(supplied_by_name) - set(definitions))
    if unknown:
        fail(
            "execution worker parameter values",
            f"unknown parameters: {', '.join(unknown)}",
        )
    resolved: list[tuple[str, str]] = []
    for name, definition in sorted(definitions.items()):
        value = supplied_by_name.get(name, definition.default)
        if value is None:
            if definition.required:
                fail(
                    "execution worker parameter values",
                    f"missing required parameter: {name}",
                )
            continue
        if value not in definition.allowed_values:
            fail(f"execution worker parameter values.{name}", "value is not allowed")
        resolved.append((name, value))
    return tuple(resolved)


@dataclass(frozen=True, slots=True)
class ExecutionSourceMaterialization:
    """Closed transport authority for materializing one exact project source tree."""

    kind: ExecutionSourceMaterializationKind
    request_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    repository_url: str | None = None
    revision: str | None = None
    archive_reference: ContentReference | None = None
    accepted_source_cache_reference: ContentReference | None = None

    SCHEMA: ClassVar[str] = EXECUTION_SOURCE_MATERIALIZATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExecutionSourceMaterializationKind):
            fail("ExecutionSourceMaterialization.kind", "must be typed")
        for value, path in (
            (self.request_identity, "request_identity"),
            (self.source_tree_identity, "source_tree_identity"),
        ):
            if not isinstance(value, ContentIdentity):
                fail(
                    f"ExecutionSourceMaterialization.{path}",
                    "must be a ContentIdentity",
                )
        if self.kind is ExecutionSourceMaterializationKind.GIT:
            if self.repository_url is None:
                fail(
                    "ExecutionSourceMaterialization.repository_url",
                    "is required for Git materialization",
                )
            _repository_url(
                self.repository_url, "ExecutionSourceMaterialization.repository_url"
            )
            if self.revision is None or not _GIT_REVISION.fullmatch(self.revision):
                fail(
                    "ExecutionSourceMaterialization.revision",
                    "must be an exact 40- or 64-character lower-case Git object ID",
                )
            if self.archive_reference is not None:
                fail(
                    "ExecutionSourceMaterialization.archive_reference",
                    "must be null for Git materialization",
                )
            if self.accepted_source_cache_reference is not None:
                fail(
                    "ExecutionSourceMaterialization.accepted_source_cache_reference",
                    "must be null for Git materialization",
                )
        else:
            if self.repository_url is not None or self.revision is not None:
                fail(
                    "ExecutionSourceMaterialization",
                    "archive materialization cannot carry Git authority",
                )
            if self.archive_reference is None:
                fail(
                    "ExecutionSourceMaterialization.archive_reference",
                    "is required for archive materialization",
                )
            _source_archive_reference(
                self.archive_reference,
                "ExecutionSourceMaterialization.archive_reference",
            )
            _accepted_source_cache_reference(
                self.accepted_source_cache_reference,
                "ExecutionSourceMaterialization.accepted_source_cache_reference",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind.value,
            "request_identity": self.request_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "repository_url": self.repository_url,
            "revision": self.revision,
            "archive_reference": (
                None
                if self.archive_reference is None
                else self.archive_reference.to_dict()
            ),
            "accepted_source_cache_reference": (
                None
                if self.accepted_source_cache_reference is None
                else self.accepted_source_cache_reference.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionSourceMaterialization"
    ) -> ExecutionSourceMaterialization:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "kind",
                    "request_identity",
                    "source_tree_identity",
                    "repository_url",
                    "revision",
                    "archive_reference",
                }
            ),
            optional=frozenset({"accepted_source_cache_reference"}),
        )
        return cls(
            enum_value(
                ExecutionSourceMaterializationKind,
                data["kind"],
                f"{path}.kind",
            ),
            _identity(data["request_identity"], f"{path}.request_identity"),
            _identity(data["source_tree_identity"], f"{path}.source_tree_identity"),
            (
                None
                if data["repository_url"] is None
                else _repository_url(data["repository_url"], f"{path}.repository_url")
            ),
            optional_string(data["revision"], f"{path}.revision"),
            _source_archive_reference(
                data["archive_reference"], f"{path}.archive_reference"
            ),
            _accepted_source_cache_reference(
                data.get("accepted_source_cache_reference"),
                f"{path}.accepted_source_cache_reference",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionDispatchRequest:
    action: LifecycleDispatchAction
    component: str
    component_path: str
    target_profile: str
    flavor_selectors: tuple[str, ...]
    worker_identity: ContentIdentity
    requirements: ExecutionRequirements
    parameters: tuple[tuple[str, str], ...]
    arguments: tuple[str, ...]
    project_identity: ContentIdentity
    source_identity: ContentIdentity
    specification_identity: ContentIdentity
    flavor_identity: ContentIdentity
    toolchain_identity: ContentIdentity
    source_index_identity: ContentIdentity
    model_scope_identity: ContentIdentity
    artifact_reference: ContentReference | None
    timeout_seconds: int
    verbose: bool = False
    model_selector: str | None = None
    accepted_source_only: bool = False
    accepted_source_provider_id: str | None = None
    accepted_source_provider_identity: ContentIdentity | None = None
    entrypoint: str | None = None
    jobs: int = 1

    SCHEMA: ClassVar[str] = EXECUTION_DISPATCH_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        int_value(self.jobs, "ExecutionDispatchRequest.jobs", minimum=1, maximum=256)
        if not isinstance(self.action, LifecycleDispatchAction):
            fail("ExecutionDispatchRequest.action", "must be typed")
        string_value(
            self.component, "ExecutionDispatchRequest.component", max_length=4096
        )
        component_path = string_value(
            self.component_path,
            "ExecutionDispatchRequest.component_path",
            max_length=4096,
        )
        path_parts = component_path.split("/")
        if (
            component_path.startswith("/")
            or "\\" in component_path
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            fail(
                "ExecutionDispatchRequest.component_path",
                "must be a canonical project-relative POSIX path",
            )
        _identifier(self.target_profile, "ExecutionDispatchRequest.target_profile")
        if len(self.flavor_selectors) > 256:
            fail(
                "ExecutionDispatchRequest.flavor_selectors",
                "must contain at most 256 selectors",
            )
        for index, selector in enumerate(self.flavor_selectors):
            raw = string_value(
                selector,
                f"ExecutionDispatchRequest.flavor_selectors[{index}]",
                max_length=4096,
            )
            if "\x00" in raw or any(character in raw for character in "\r\n"):
                fail(
                    f"ExecutionDispatchRequest.flavor_selectors[{index}]",
                    "must be a single NUL-free line",
                )
        for identity, path in (
            (self.worker_identity, "worker_identity"),
            (self.project_identity, "project_identity"),
            (self.source_identity, "source_identity"),
            (self.specification_identity, "specification_identity"),
            (self.flavor_identity, "flavor_identity"),
            (self.toolchain_identity, "toolchain_identity"),
            (self.source_index_identity, "source_index_identity"),
            (self.model_scope_identity, "model_scope_identity"),
        ):
            if not isinstance(identity, ContentIdentity):
                fail(f"ExecutionDispatchRequest.{path}", "must be a ContentIdentity")
        if not isinstance(self.requirements, ExecutionRequirements):
            fail(
                "ExecutionDispatchRequest.requirements", "must be ExecutionRequirements"
            )
        _artifact_reference(
            self.artifact_reference, "ExecutionDispatchRequest.artifact_reference"
        )
        if (
            self.action in {LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST}
            and self.artifact_reference is not None
        ):
            fail(
                "ExecutionDispatchRequest.artifact_reference",
                "must be null for build and test requests",
            )
        names = tuple(item[0] for item in self.parameters)
        _sorted_unique(names, "ExecutionDispatchRequest.parameters")
        for index, (name, value) in enumerate(self.parameters):
            _name(name, f"ExecutionDispatchRequest.parameters[{index}].name")
            _value(value, f"ExecutionDispatchRequest.parameters[{index}].value")
        if len(self.arguments) > MAX_WORKER_ARGUMENTS:
            fail(
                "ExecutionDispatchRequest.arguments",
                f"must contain at most {MAX_WORKER_ARGUMENTS} values",
            )
        for index, argument in enumerate(self.arguments):
            value = string_value(
                argument,
                f"ExecutionDispatchRequest.arguments[{index}]",
                nonempty=False,
                max_length=4096,
            )
            if "\x00" in value:
                fail(
                    f"ExecutionDispatchRequest.arguments[{index}]",
                    "must not contain NUL",
                )
        if self.action is not LifecycleDispatchAction.RUN and self.arguments:
            fail(
                "ExecutionDispatchRequest.arguments",
                "must be empty for build and test requests",
            )
        if self.entrypoint is not None:
            selected_entrypoint = string_value(
                self.entrypoint,
                "ExecutionDispatchRequest.entrypoint",
                max_length=4096,
            )
            if "\x00" in selected_entrypoint:
                fail("ExecutionDispatchRequest.entrypoint", "must not contain NUL")
            if self.action is not LifecycleDispatchAction.RUN:
                fail(
                    "ExecutionDispatchRequest.entrypoint",
                    "must be null for build and test requests",
                )
        int_value(
            self.timeout_seconds,
            "ExecutionDispatchRequest.timeout_seconds",
            minimum=1,
            maximum=7 * 24 * 60 * 60,
        )
        if self.model_selector is not None:
            _value(self.model_selector, "ExecutionDispatchRequest.model_selector")
        bool_value(self.verbose, "ExecutionDispatchRequest.verbose")
        bool_value(
            self.accepted_source_only, "ExecutionDispatchRequest.accepted_source_only"
        )
        if self.accepted_source_provider_id is not None:
            _identifier(
                self.accepted_source_provider_id,
                "ExecutionDispatchRequest.accepted_source_provider_id",
            )
        if self.accepted_source_provider_identity is not None and not isinstance(
            self.accepted_source_provider_identity, ContentIdentity
        ):
            fail(
                "ExecutionDispatchRequest.accepted_source_provider_identity",
                "must be a ContentIdentity or null",
            )
        if (self.accepted_source_provider_id is None) != (
            self.accepted_source_provider_identity is None
        ):
            fail(
                "ExecutionDispatchRequest.accepted_source_provider_identity",
                "requires accepted_source_provider_id",
            )
        if (
            self.accepted_source_provider_id is not None
            and not self.accepted_source_only
        ):
            fail(
                "ExecutionDispatchRequest.accepted_source_provider_identity",
                "requires accepted_source_only",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    @property
    def authority_identity(self) -> ContentIdentity:
        """Identity shared by build/test/run for one immutable execution input set."""

        authority: dict[str, object] = {
            "schema": "literate-ai/execution-dispatch-authority@1",
            "component": self.component,
            "component_path": self.component_path,
            "target_profile": self.target_profile,
            "flavor_selectors": list(self.flavor_selectors),
            "worker_identity": self.worker_identity.to_dict(),
            "requirements": self.requirements.to_dict(),
            "parameters": [
                {"name": name, "value": value} for name, value in self.parameters
            ],
            "project_identity": self.project_identity.to_dict(),
            "source_identity": self.source_identity.to_dict(),
            "specification_identity": self.specification_identity.to_dict(),
            "flavor_identity": self.flavor_identity.to_dict(),
            "toolchain_identity": self.toolchain_identity.to_dict(),
            "source_index_identity": self.source_index_identity.to_dict(),
            "model_scope_identity": self.model_scope_identity.to_dict(),
            "model_selector": self.model_selector,
            "accepted_source_only": self.accepted_source_only,
        }
        if self.accepted_source_provider_identity is not None:
            authority["accepted_source_provider_id"] = self.accepted_source_provider_id
            authority["accepted_source_provider_identity"] = (
                self.accepted_source_provider_identity.to_dict()
            )
        return canonical_identity(authority)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "action": self.action.value,
            "component": self.component,
            "component_path": self.component_path,
            "target_profile": self.target_profile,
            "flavor_selectors": list(self.flavor_selectors),
            "worker_identity": self.worker_identity.to_dict(),
            "requirements": self.requirements.to_dict(),
            "parameters": [
                {
                    "schema": EXECUTION_WORKER_PARAMETER_VALUE_SCHEMA,
                    "name": name,
                    "value": value,
                }
                for name, value in self.parameters
            ],
            "arguments": list(self.arguments),
            "project_identity": self.project_identity.to_dict(),
            "source_identity": self.source_identity.to_dict(),
            "specification_identity": self.specification_identity.to_dict(),
            "flavor_identity": self.flavor_identity.to_dict(),
            "toolchain_identity": self.toolchain_identity.to_dict(),
            "source_index_identity": self.source_index_identity.to_dict(),
            "model_scope_identity": self.model_scope_identity.to_dict(),
            "artifact_reference": (
                None
                if self.artifact_reference is None
                else self.artifact_reference.to_dict()
            ),
            "timeout_seconds": self.timeout_seconds,
            "model_selector": self.model_selector,
            "verbose": self.verbose,
            "accepted_source_only": self.accepted_source_only,
        }
        if self.accepted_source_provider_identity is not None:
            value["accepted_source_provider_id"] = self.accepted_source_provider_id
            value["accepted_source_provider_identity"] = (
                self.accepted_source_provider_identity.to_dict()
            )
        if self.entrypoint is not None:
            value["entrypoint"] = self.entrypoint
        # Scheduling is bound to this request, not the shared build-input
        # authority used to replay artifacts. Preserve legacy default bytes.
        if self.jobs != 1:
            value["jobs"] = self.jobs
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionDispatchRequest"
    ) -> ExecutionDispatchRequest:
        required = frozenset(
            {
                "action",
                "component",
                "component_path",
                "target_profile",
                "flavor_selectors",
                "worker_identity",
                "requirements",
                "parameters",
                "arguments",
                "project_identity",
                "source_identity",
                "specification_identity",
                "flavor_identity",
                "toolchain_identity",
                "source_index_identity",
                "model_scope_identity",
                "artifact_reference",
                "timeout_seconds",
                "model_selector",
                "verbose",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=required,
            optional=frozenset(
                {
                    "accepted_source_only",
                    "accepted_source_provider_id",
                    "accepted_source_provider_identity",
                    "entrypoint",
                    "jobs",
                }
            ),
        )
        parameters: list[tuple[str, str]] = []
        for index, item in enumerate(
            list_value(data["parameters"], f"{path}.parameters")
        ):
            entry = contract_fields(
                item,
                path=f"{path}.parameters[{index}]",
                schema_uri=EXECUTION_WORKER_PARAMETER_VALUE_SCHEMA,
                required=frozenset({"name", "value"}),
            )
            parameters.append(
                (
                    _name(entry["name"], f"{path}.parameters[{index}].name"),
                    _value(entry["value"], f"{path}.parameters[{index}].value"),
                )
            )
        return cls(
            enum_value(LifecycleDispatchAction, data["action"], f"{path}.action"),
            string_value(data["component"], f"{path}.component", max_length=4096),
            string_value(
                data["component_path"], f"{path}.component_path", max_length=4096
            ),
            _identifier(data["target_profile"], f"{path}.target_profile"),
            tuple(
                string_value(
                    item,
                    f"{path}.flavor_selectors[{index}]",
                    max_length=4096,
                )
                for index, item in enumerate(
                    list_value(data["flavor_selectors"], f"{path}.flavor_selectors")
                )
            ),
            _identity(data["worker_identity"], f"{path}.worker_identity"),
            ExecutionRequirements.from_dict(
                data["requirements"], path=f"{path}.requirements"
            ),
            tuple(parameters),
            tuple(
                string_value(
                    item,
                    f"{path}.arguments[{index}]",
                    nonempty=False,
                    max_length=4096,
                )
                for index, item in enumerate(
                    list_value(data["arguments"], f"{path}.arguments")
                )
            ),
            _identity(data["project_identity"], f"{path}.project_identity"),
            _identity(data["source_identity"], f"{path}.source_identity"),
            _identity(data["specification_identity"], f"{path}.specification_identity"),
            _identity(data["flavor_identity"], f"{path}.flavor_identity"),
            _identity(data["toolchain_identity"], f"{path}.toolchain_identity"),
            _identity(data["source_index_identity"], f"{path}.source_index_identity"),
            _identity(data["model_scope_identity"], f"{path}.model_scope_identity"),
            _artifact_reference(
                data["artifact_reference"], f"{path}.artifact_reference"
            ),
            int_value(
                data["timeout_seconds"],
                f"{path}.timeout_seconds",
                minimum=1,
                maximum=7 * 24 * 60 * 60,
            ),
            bool_value(data["verbose"], f"{path}.verbose"),
            (
                None
                if data["model_selector"] is None
                else _value(data["model_selector"], f"{path}.model_selector")
            ),
            bool_value(
                data.get("accepted_source_only", False),
                f"{path}.accepted_source_only",
            ),
            (
                None
                if data.get("accepted_source_provider_id") is None
                else _identifier(
                    data["accepted_source_provider_id"],
                    f"{path}.accepted_source_provider_id",
                )
            ),
            (
                None
                if data.get("accepted_source_provider_identity") is None
                else _identity(
                    data["accepted_source_provider_identity"],
                    f"{path}.accepted_source_provider_identity",
                )
            ),
            optional_string(data.get("entrypoint"), f"{path}.entrypoint"),
            int_value(data.get("jobs", 1), f"{path}.jobs", minimum=1, maximum=256),
        )


@dataclass(frozen=True, slots=True)
class ObservedExecutionEnvironment:
    os_family: str
    os_version: str
    cpu_architecture: str
    cpu_cores: int
    memory_mib: int
    gpu_vendor: str | None = None
    gpu_model: str | None = None
    gpu_count: int = 0
    gpu_memory_mib: int | None = None
    gpu_capabilities: tuple[str, ...] = ()
    toolchain_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = OBSERVED_EXECUTION_ENVIRONMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.os_family not in _OS_FAMILIES:
            fail(
                "ObservedExecutionEnvironment.os_family",
                "must be linux, macos, or windows",
            )
        _value(self.os_version, "ObservedExecutionEnvironment.os_version")
        _value(self.cpu_architecture, "ObservedExecutionEnvironment.cpu_architecture")
        int_value(
            self.cpu_cores,
            "ObservedExecutionEnvironment.cpu_cores",
            minimum=1,
            maximum=65536,
        )
        int_value(self.memory_mib, "ObservedExecutionEnvironment.memory_mib", minimum=1)
        if (self.gpu_vendor is None) != (self.gpu_model is None):
            fail(
                "ObservedExecutionEnvironment",
                "GPU vendor and model must appear together",
            )
        if self.gpu_vendor is None:
            if (
                self.gpu_count != 0
                or self.gpu_memory_mib is not None
                or self.gpu_capabilities
            ):
                fail("ObservedExecutionEnvironment", "GPU measurements require a GPU")
        else:
            _value(self.gpu_vendor, "ObservedExecutionEnvironment.gpu_vendor")
            _hardware_model(self.gpu_model, "ObservedExecutionEnvironment.gpu_model")
            int_value(
                self.gpu_count,
                "ObservedExecutionEnvironment.gpu_count",
                minimum=1,
                maximum=1024,
            )
            if self.gpu_memory_mib is not None:
                int_value(
                    self.gpu_memory_mib,
                    "ObservedExecutionEnvironment.gpu_memory_mib",
                    minimum=1,
                )
            for index, capability in enumerate(self.gpu_capabilities):
                _value(
                    capability,
                    f"ObservedExecutionEnvironment.gpu_capabilities[{index}]",
                )
            _sorted_unique(
                self.gpu_capabilities,
                "ObservedExecutionEnvironment.gpu_capabilities",
            )
        if any(
            not isinstance(item, ContentIdentity) for item in self.toolchain_identities
        ):
            fail(
                "ObservedExecutionEnvironment.toolchain_identities",
                "must contain only ContentIdentity values",
            )
        toolchain_uris = tuple(item.uri for item in self.toolchain_identities)
        _sorted_unique(
            toolchain_uris, "ObservedExecutionEnvironment.toolchain_identities"
        )

    def satisfies(self, requirements: ExecutionRequirements) -> bool:
        if (
            requirements.os_family is not None
            and self.os_family != requirements.os_family
        ):
            return False
        if (
            requirements.os_version is not None
            and self.os_version != requirements.os_version
        ):
            return False
        if (
            requirements.cpu_architecture is not None
            and self.cpu_architecture != requirements.cpu_architecture
        ):
            return False
        if (
            requirements.minimum_cpu_cores is not None
            and self.cpu_cores < requirements.minimum_cpu_cores
        ):
            return False
        if (
            requirements.minimum_memory_mib is not None
            and self.memory_mib < requirements.minimum_memory_mib
        ):
            return False
        gpu = requirements.gpu
        if not gpu.constrained:
            return True
        minimum_count = 1 if gpu.minimum_count is None else gpu.minimum_count
        return bool(
            self.gpu_vendor is not None
            and (gpu.vendor is None or self.gpu_vendor == gpu.vendor)
            and (gpu.model is None or self.gpu_model == gpu.model)
            and self.gpu_count >= minimum_count
            and (
                gpu.minimum_memory_mib is None
                or (
                    self.gpu_memory_mib is not None
                    and self.gpu_memory_mib >= gpu.minimum_memory_mib
                )
            )
            and set(gpu.capabilities or ()).issubset(self.gpu_capabilities)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "os_family": self.os_family,
            "os_version": self.os_version,
            "cpu_architecture": self.cpu_architecture,
            "cpu_cores": self.cpu_cores,
            "memory_mib": self.memory_mib,
            "gpu_vendor": self.gpu_vendor,
            "gpu_model": self.gpu_model,
            "gpu_count": self.gpu_count,
            "gpu_memory_mib": self.gpu_memory_mib,
            "gpu_capabilities": list(self.gpu_capabilities),
            "toolchain_identities": [
                item.to_dict() for item in self.toolchain_identities
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ObservedExecutionEnvironment"
    ) -> ObservedExecutionEnvironment:
        required = frozenset(
            {
                "os_family",
                "os_version",
                "cpu_architecture",
                "cpu_cores",
                "memory_mib",
                "gpu_vendor",
                "gpu_model",
                "gpu_count",
                "gpu_memory_mib",
                "gpu_capabilities",
                "toolchain_identities",
            }
        )
        data = contract_fields(
            value, path=path, schema_uri=cls.SCHEMA, required=required
        )
        return cls(
            string_value(data["os_family"], f"{path}.os_family"),
            _value(data["os_version"], f"{path}.os_version"),
            _value(data["cpu_architecture"], f"{path}.cpu_architecture"),
            int_value(data["cpu_cores"], f"{path}.cpu_cores", minimum=1, maximum=65536),
            int_value(data["memory_mib"], f"{path}.memory_mib", minimum=1),
            optional_string(data["gpu_vendor"], f"{path}.gpu_vendor"),
            (
                None
                if data["gpu_model"] is None
                else _hardware_model(data["gpu_model"], f"{path}.gpu_model")
            ),
            int_value(data["gpu_count"], f"{path}.gpu_count", maximum=1024),
            (
                None
                if data["gpu_memory_mib"] is None
                else int_value(
                    data["gpu_memory_mib"], f"{path}.gpu_memory_mib", minimum=1
                )
            ),
            tuple(
                _value(item, f"{path}.gpu_capabilities[{index}]")
                for index, item in enumerate(
                    list_value(data["gpu_capabilities"], f"{path}.gpu_capabilities")
                )
            ),
            tuple(
                _identity(item, f"{path}.toolchain_identities[{index}]")
                for index, item in enumerate(
                    list_value(
                        data["toolchain_identities"],
                        f"{path}.toolchain_identities",
                    )
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionDispatchResult:
    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    external_task_id: str | None
    status: DispatchResultStatus
    observed_environment: ObservedExecutionEnvironment
    exit_status: int
    artifact_reference: ContentReference | None
    evidence_identity: ContentIdentity
    stdout: str = ""
    stderr: str = ""
    diagnostic_digest: str | None = None
    evidence_manifest: RemoteLifecycleEvidenceManifest | None = None
    evidence_reference: ContentReference | None = None
    custody_receipt: RemoteEvidenceCustodyReceipt | None = None
    coverage_gaps: dict[str, object] | None = None
    library_product: LibraryArtifactProduct | None = None

    SCHEMA: ClassVar[str] = EXECUTION_DISPATCH_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for identity, path in (
            (self.request_identity, "request_identity"),
            (self.worker_identity, "worker_identity"),
            (self.evidence_identity, "evidence_identity"),
        ):
            if not isinstance(identity, ContentIdentity):
                fail(f"ExecutionDispatchResult.{path}", "must be a ContentIdentity")
        if self.external_task_id is not None:
            _value(self.external_task_id, "ExecutionDispatchResult.external_task_id")
        if not isinstance(self.status, DispatchResultStatus):
            fail("ExecutionDispatchResult.status", "must be typed")
        if not isinstance(self.observed_environment, ObservedExecutionEnvironment):
            fail(
                "ExecutionDispatchResult.observed_environment",
                "must be observed evidence",
            )
        int_value(
            self.exit_status, "ExecutionDispatchResult.exit_status", maximum=2**31 - 1
        )
        _artifact_reference(
            self.artifact_reference, "ExecutionDispatchResult.artifact_reference"
        )
        if self.status is DispatchResultStatus.PASSED and self.exit_status != 0:
            fail(
                "ExecutionDispatchResult.exit_status", "must be zero for passed results"
            )
        if self.status is not DispatchResultStatus.PASSED and self.exit_status == 0:
            fail(
                "ExecutionDispatchResult.exit_status",
                "must be nonzero for unsuccessful results",
            )
        if self.library_product is not None and (
            not isinstance(self.library_product, LibraryArtifactProduct)
            or self.status is not DispatchResultStatus.PASSED
            or self.artifact_reference is None
        ):
            fail(
                "ExecutionDispatchResult.library_product",
                "requires a typed passed artifact",
            )
        reference = self.artifact_reference
        if reference is not None and reference.uri.startswith("litai-worker-cas:"):
            if self.library_product is not None:
                custody = canonical_identity(
                    library_worker_manifest(
                        reference.identity,
                        self.library_product,
                        self.observed_environment.toolchain_identities,
                    )
                )
                expected_uri = (
                    f"litai-worker-cas:{reference.identity.uri}/library/{custody.uri}"
                )
                if reference.uri != expected_uri:
                    fail(
                        "ExecutionDispatchResult.library_product",
                        "must bind the exact worker library locator and toolchains",
                    )
            elif "/library/" in reference.uri:
                fail(
                    "ExecutionDispatchResult.library_product",
                    "worker library locator requires typed product metadata",
                )
        _bounded_output(self.stdout, "ExecutionDispatchResult.stdout")
        _bounded_output(self.stderr, "ExecutionDispatchResult.stderr")
        if self.diagnostic_digest is not None and not _DIGEST.fullmatch(
            self.diagnostic_digest
        ):
            fail("ExecutionDispatchResult.diagnostic_digest", "must be a sha256 digest")
        if self.evidence_manifest is not None:
            if not isinstance(self.evidence_manifest, RemoteLifecycleEvidenceManifest):
                fail(
                    "ExecutionDispatchResult.evidence_manifest",
                    "must be a RemoteLifecycleEvidenceManifest",
                )
            if (
                self.evidence_manifest.request_identity != self.request_identity
                or self.evidence_manifest.worker_identity != self.worker_identity
                or self.evidence_manifest.outcome != self.status.value
                or self.evidence_manifest.result_evidence_identity
                != self.evidence_identity
                or self.evidence_manifest.library_product != self.library_product
            ):
                fail(
                    "ExecutionDispatchResult.evidence_manifest",
                    "must bind the exact dispatch result",
                )
            if (
                self.evidence_manifest.action in {"build", "test"}
                and self.status is DispatchResultStatus.PASSED
                and (
                    self.artifact_reference is None
                    or self.evidence_manifest.artifact_identity
                    != self.artifact_reference.identity
                )
            ):
                fail(
                    "ExecutionDispatchResult.evidence_manifest",
                    "must bind the accepted artifact identity",
                )
        _evidence_reference(
            self.evidence_reference, "ExecutionDispatchResult.evidence_reference"
        )
        if (self.evidence_manifest is None) != (self.evidence_reference is None):
            fail(
                "ExecutionDispatchResult.evidence_reference",
                "manifest and transfer reference must appear together",
            )
        if self.custody_receipt is not None:
            if not isinstance(self.custody_receipt, RemoteEvidenceCustodyReceipt):
                fail(
                    "ExecutionDispatchResult.custody_receipt",
                    "must be a RemoteEvidenceCustodyReceipt",
                )
            if (
                self.evidence_manifest is None
                or self.evidence_reference is None
                or self.custody_receipt.request_identity != self.request_identity
                or self.custody_receipt.worker_identity != self.worker_identity
                or self.custody_receipt.manifest_identity
                != self.evidence_manifest.identity
                or self.custody_receipt.bundle_identity
                != self.evidence_reference.identity
                or self.custody_receipt.verified_artifact_identity
                != self.evidence_manifest.artifact_identity
            ):
                fail(
                    "ExecutionDispatchResult.custody_receipt",
                    "must bind the transferred manifest and bundle",
                )
        object.__setattr__(
            self,
            "coverage_gaps",
            _coverage_gaps_payload(
                self.coverage_gaps, "ExecutionDispatchResult.coverage_gaps"
            ),
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "external_task_id": self.external_task_id,
            "status": self.status.value,
            "observed_environment": self.observed_environment.to_dict(),
            "exit_status": self.exit_status,
            "artifact_reference": (
                None
                if self.artifact_reference is None
                else self.artifact_reference.to_dict()
            ),
            "evidence_identity": self.evidence_identity.to_dict(),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "diagnostic_digest": self.diagnostic_digest,
            "evidence_manifest": (
                None
                if self.evidence_manifest is None
                else self.evidence_manifest.to_dict()
            ),
            "evidence_reference": (
                None
                if self.evidence_reference is None
                else self.evidence_reference.to_dict()
            ),
            "custody_receipt": (
                None if self.custody_receipt is None else self.custody_receipt.to_dict()
            ),
            "coverage_gaps": self.coverage_gaps,
            **(
                {}
                if self.library_product is None
                else {"library_artifact": self.library_product.to_dict()}
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutionDispatchResult"
    ) -> ExecutionDispatchResult:
        required = frozenset(
            {
                "request_identity",
                "worker_identity",
                "external_task_id",
                "status",
                "observed_environment",
                "exit_status",
                "artifact_reference",
                "evidence_identity",
                "stdout",
                "stderr",
                "diagnostic_digest",
                "evidence_manifest",
                "evidence_reference",
                "custody_receipt",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=required,
            optional=frozenset({"coverage_gaps", "library_artifact"}),
        )
        return cls(
            _identity(data["request_identity"], f"{path}.request_identity"),
            _identity(data["worker_identity"], f"{path}.worker_identity"),
            optional_string(data["external_task_id"], f"{path}.external_task_id"),
            enum_value(DispatchResultStatus, data["status"], f"{path}.status"),
            ObservedExecutionEnvironment.from_dict(
                data["observed_environment"], path=f"{path}.observed_environment"
            ),
            int_value(data["exit_status"], f"{path}.exit_status", maximum=2**31 - 1),
            _artifact_reference(
                data["artifact_reference"], f"{path}.artifact_reference"
            ),
            _identity(data["evidence_identity"], f"{path}.evidence_identity"),
            _bounded_output(data["stdout"], f"{path}.stdout"),
            _bounded_output(data["stderr"], f"{path}.stderr"),
            (
                None
                if data["diagnostic_digest"] is None
                else string_value(
                    data["diagnostic_digest"], f"{path}.diagnostic_digest"
                )
            ),
            (
                None
                if data["evidence_manifest"] is None
                else RemoteLifecycleEvidenceManifest.from_dict(
                    data["evidence_manifest"],
                    path=f"{path}.evidence_manifest",
                )
            ),
            _evidence_reference(
                data["evidence_reference"], f"{path}.evidence_reference"
            ),
            (
                None
                if data["custody_receipt"] is None
                else RemoteEvidenceCustodyReceipt.from_dict(
                    data["custody_receipt"],
                    path=f"{path}.custody_receipt",
                )
            ),
            _coverage_gaps_payload(
                data["coverage_gaps"] if "coverage_gaps" in data else None,
                f"{path}.coverage_gaps",
            ),
            None
            if "library_artifact" not in data
            else LibraryArtifactProduct.from_dict(
                data["library_artifact"], path=f"{path}.library_artifact"
            ),
        )


@dataclass(frozen=True, slots=True)
class RemoteExecutionControlResult:
    """Deterministically bounded SSH control projection for out-of-band custody."""

    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    external_task_id: str | None
    status: DispatchResultStatus
    observed_environment: ObservedExecutionEnvironment
    exit_status: int
    artifact_reference: ContentReference | None
    evidence_identity: ContentIdentity
    diagnostic_digest: str | None
    manifest_identity: ContentIdentity
    manifest_size: int
    evidence_reference: ContentReference
    bundle_size: int
    redacted_summary: str = ""
    coverage_gaps: dict[str, object] | None = None

    SCHEMA: ClassVar[str] = REMOTE_EXECUTION_CONTROL_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for identity, path in (
            (self.request_identity, "request_identity"),
            (self.worker_identity, "worker_identity"),
            (self.evidence_identity, "evidence_identity"),
            (self.manifest_identity, "manifest_identity"),
        ):
            if not isinstance(identity, ContentIdentity):
                fail(
                    f"RemoteExecutionControlResult.{path}", "must be a ContentIdentity"
                )
        if self.external_task_id is not None:
            _value(
                self.external_task_id,
                "RemoteExecutionControlResult.external_task_id",
            )
        if not isinstance(self.status, DispatchResultStatus):
            fail("RemoteExecutionControlResult.status", "must be typed")
        if not isinstance(self.observed_environment, ObservedExecutionEnvironment):
            fail(
                "RemoteExecutionControlResult.observed_environment",
                "must be observed evidence",
            )
        int_value(
            self.exit_status,
            "RemoteExecutionControlResult.exit_status",
            maximum=2**31 - 1,
        )
        _artifact_reference(
            self.artifact_reference,
            "RemoteExecutionControlResult.artifact_reference",
        )
        if self.status is DispatchResultStatus.PASSED and self.exit_status != 0:
            fail(
                "RemoteExecutionControlResult.exit_status",
                "must be zero for passed results",
            )
        if self.status is not DispatchResultStatus.PASSED and self.exit_status == 0:
            fail(
                "RemoteExecutionControlResult.exit_status",
                "must be nonzero for unsuccessful results",
            )
        if self.diagnostic_digest is not None and not _DIGEST.fullmatch(
            self.diagnostic_digest
        ):
            fail(
                "RemoteExecutionControlResult.diagnostic_digest",
                "must be a sha256 digest",
            )
        int_value(
            self.manifest_size,
            "RemoteExecutionControlResult.manifest_size",
            maximum=MAX_REMOTE_EVIDENCE_MANIFEST_BYTES,
        )
        _evidence_reference(
            self.evidence_reference,
            "RemoteExecutionControlResult.evidence_reference",
        )
        int_value(
            self.bundle_size,
            "RemoteExecutionControlResult.bundle_size",
            maximum=MAX_REMOTE_EVIDENCE_BUNDLE_BYTES,
        )
        string_value(
            self.redacted_summary,
            "RemoteExecutionControlResult.redacted_summary",
            nonempty=False,
            max_length=MAX_REMOTE_CONTROL_SUMMARY_BYTES,
        )
        object.__setattr__(
            self,
            "coverage_gaps",
            _coverage_gaps_payload(
                self.coverage_gaps, "RemoteExecutionControlResult.coverage_gaps"
            ),
        )

    @classmethod
    def from_dispatch_result(
        cls,
        result: ExecutionDispatchResult,
        *,
        manifest_size: int,
        bundle_size: int,
        redacted_summary: str,
    ) -> RemoteExecutionControlResult:
        if result.evidence_manifest is None or result.evidence_reference is None:
            fail(
                "RemoteExecutionControlResult",
                "dispatch result must carry transferable evidence custody",
            )
        return cls(
            result.request_identity,
            result.worker_identity,
            result.external_task_id,
            result.status,
            result.observed_environment,
            result.exit_status,
            result.artifact_reference,
            result.evidence_identity,
            result.diagnostic_digest,
            result.evidence_manifest.identity,
            manifest_size,
            result.evidence_reference,
            bundle_size,
            redacted_summary,
            result.coverage_gaps,
        )

    def bind_imported_manifest(
        self, manifest: RemoteLifecycleEvidenceManifest
    ) -> ExecutionDispatchResult:
        if (
            manifest.identity != self.manifest_identity
            or manifest.request_identity != self.request_identity
            or manifest.worker_identity != self.worker_identity
            or manifest.outcome != self.status.value
            or manifest.result_evidence_identity != self.evidence_identity
        ):
            fail(
                "RemoteExecutionControlResult.manifest_identity",
                "imported manifest does not bind the exact control result",
            )
        if (
            manifest.action in {"build", "test"}
            and self.status is DispatchResultStatus.PASSED
            and (
                self.artifact_reference is None
                or manifest.artifact_identity != self.artifact_reference.identity
            )
        ):
            fail(
                "RemoteExecutionControlResult.artifact_reference",
                "imported manifest does not bind the accepted artifact",
            )
        return ExecutionDispatchResult(
            self.request_identity,
            self.worker_identity,
            self.external_task_id,
            self.status,
            self.observed_environment,
            self.exit_status,
            self.artifact_reference,
            self.evidence_identity,
            self.redacted_summary if self.status is DispatchResultStatus.PASSED else "",
            self.redacted_summary
            if self.status is not DispatchResultStatus.PASSED
            else "",
            self.diagnostic_digest,
            manifest,
            self.evidence_reference,
            coverage_gaps=self.coverage_gaps,
            library_product=manifest.library_product,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "external_task_id": self.external_task_id,
            "status": self.status.value,
            "observed_environment": self.observed_environment.to_dict(),
            "exit_status": self.exit_status,
            "artifact_reference": (
                None
                if self.artifact_reference is None
                else self.artifact_reference.to_dict()
            ),
            "evidence_identity": self.evidence_identity.to_dict(),
            "diagnostic_digest": self.diagnostic_digest,
            "manifest_identity": self.manifest_identity.to_dict(),
            "manifest_size": self.manifest_size,
            "evidence_reference": self.evidence_reference.to_dict(),
            "bundle_size": self.bundle_size,
            "redacted_summary": self.redacted_summary,
            "coverage_gaps": self.coverage_gaps,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RemoteExecutionControlResult"
    ) -> RemoteExecutionControlResult:
        names = {
            "request_identity",
            "worker_identity",
            "external_task_id",
            "status",
            "observed_environment",
            "exit_status",
            "artifact_reference",
            "evidence_identity",
            "diagnostic_digest",
            "manifest_identity",
            "manifest_size",
            "evidence_reference",
            "bundle_size",
            "redacted_summary",
        }
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(names),
            optional=frozenset({"coverage_gaps"}),
        )
        return cls(
            _identity(data["request_identity"], f"{path}.request_identity"),
            _identity(data["worker_identity"], f"{path}.worker_identity"),
            optional_string(data["external_task_id"], f"{path}.external_task_id"),
            enum_value(DispatchResultStatus, data["status"], f"{path}.status"),
            ObservedExecutionEnvironment.from_dict(
                data["observed_environment"], path=f"{path}.observed_environment"
            ),
            int_value(data["exit_status"], f"{path}.exit_status", maximum=2**31 - 1),
            _artifact_reference(
                data["artifact_reference"], f"{path}.artifact_reference"
            ),
            _identity(data["evidence_identity"], f"{path}.evidence_identity"),
            optional_string(data["diagnostic_digest"], f"{path}.diagnostic_digest"),
            _identity(data["manifest_identity"], f"{path}.manifest_identity"),
            int_value(
                data["manifest_size"],
                f"{path}.manifest_size",
                maximum=MAX_REMOTE_EVIDENCE_MANIFEST_BYTES,
            ),
            _evidence_reference(
                data["evidence_reference"], f"{path}.evidence_reference"
            ),
            int_value(
                data["bundle_size"],
                f"{path}.bundle_size",
                maximum=MAX_REMOTE_EVIDENCE_BUNDLE_BYTES,
            ),
            string_value(
                data["redacted_summary"],
                f"{path}.redacted_summary",
                nonempty=False,
                max_length=MAX_REMOTE_CONTROL_SUMMARY_BYTES,
            ),
            _coverage_gaps_payload(
                data["coverage_gaps"] if "coverage_gaps" in data else None,
                f"{path}.coverage_gaps",
            ),
        )


__all__ = [
    "DispatchResultStatus",
    "EXECUTION_ARTIFACT_REFERENCE_KIND",
    "EXECUTION_EVIDENCE_REFERENCE_KIND",
    "EXECUTION_DISPATCH_REQUEST_SCHEMA",
    "EXECUTION_DISPATCH_RESULT_SCHEMA",
    "COVERAGE_GAP_REPORT_SCHEMA",
    "REMOTE_EXECUTION_CONTROL_RESULT_SCHEMA",
    "EXECUTION_SOURCE_MATERIALIZATION_SCHEMA",
    "EXECUTION_REQUIREMENTS_SCHEMA",
    "EXECUTION_WORKER_CATALOG_SCHEMA",
    "EXECUTION_WORKER_ENVIRONMENT_SCHEMA",
    "EXECUTION_WORKER_PARAMETER_SCHEMA",
    "EXECUTION_WORKER_PARAMETER_VALUE_SCHEMA",
    "EXECUTION_WORKER_SCHEMA",
    "MAX_REMOTE_CONTROL_SUMMARY_BYTES",
    "MAX_REMOTE_EVIDENCE_BUNDLE_BYTES",
    "MAX_REMOTE_EVIDENCE_MANIFEST_BYTES",
    "ExecutionDispatchRequest",
    "ExecutionDispatchResult",
    "RemoteExecutionControlResult",
    "ExecutionSourceMaterialization",
    "ExecutionSourceMaterializationKind",
    "ExecutionRequirements",
    "ExecutionWorker",
    "ExecutionWorkerCatalog",
    "ExecutionWorkerEnvironment",
    "ExecutionWorkerKind",
    "ExecutionWorkerParameter",
    "GPU_REQUIREMENT_SCHEMA",
    "GpuRequirement",
    "LifecycleDispatchAction",
    "OBSERVED_EXECUTION_ENVIRONMENT_SCHEMA",
    "ObservedExecutionEnvironment",
    "execution_artifact_reference",
    "resolve_worker_parameters",
]
