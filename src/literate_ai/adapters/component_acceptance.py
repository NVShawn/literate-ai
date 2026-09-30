"""A verifier-owned acceptance oracle a derived project can actually declare.

Generated source is an untrusted candidate until something independent says it satisfies
the Component's intent. Until now the only thing that could say so was the conformance
harness: ``tests/conformance/support/runtime_oracles.py`` dispatches through a dict of
bespoke Python builders, one per sample. That is why ``make samples`` passes and a
derived project's first ``litai build`` could not -- the oracle was test code, not a
capability the framework shipped.

This reads a portable application's case/arguments/exact-result shape, a bounded
persistent-service HTTP contract, an importable-library harness, or a lint-and-render
snapshot contract from a project-declared verifier root and supplies it to the
Standard lifecycle's independent-acceptance adapter.

An importable ``kind: library`` uses
``literate-ai/library-acceptance-oracle@1`` at
``verification/acceptance/<name>.json``. It binds the locked specification set,
public interfaces, import surface, exact verifier harness, and capability-keyed cases.
Other zero-entrypoint Components may use ``literate-ai/lint-render-acceptance@1``:
the verifier substitutes only ``{python}``, runs lint then render without a shell from
``source_root`` inside the project, and fails closed on lint failure or snapshot drift.
Visual inspection of an editor is not acceptance evidence.

Two boundaries are deliberate.

The document is resolved by Component *name* under ``verification/acceptance/`` and is
never referenced from ``component.md``. A Component's ``acceptance_contracts`` field
stays unused on this path on purpose: putting a path to the answers inside a document
the generator reads would tell the model exactly where to look.

The oracle binds the exact specification-set identity it was written against and fails
closed when that identity moves. Expected results are a hand-maintained restatement of
the specification, so silent drift is the failure mode worth designing out: a changed
specification must invalidate stale expectations rather than quietly keep passing. The
specification set is the right anchor because it always exists -- a Component need not
export a public interface, but it always has a specification.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.adapters._grpc_oracle import (
    GrpcBoundCallCase,
    GrpcReflectionProbe,
    load_grpc_declarations,
)
from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.lifecycle import LocalIndependentAcceptanceCase
from literate_ai.contracts import (
    NATIVE_CLI_ENTRYPOINT_KIND,
    PERSISTENT_SERVICE_ENTRYPOINT_KIND,
    PORTABLE_APPLICATION_ENTRYPOINT_KIND,
    WEB_APPLICATION_ENTRYPOINT_KIND,
    ComponentLock,
    ContentIdentity,
    Entrypoint,
    canonical_identity,
)
from literate_ai.contracts.executable_components.interfaces import CompatibilityPromise
from literate_ai.contracts.product_json import product_json_bytes, product_json_identity
from literate_ai.contracts.versioning import semantic_version

ORACLE_SCHEMA = "literate-ai/component-acceptance-oracle@1"
NATIVE_CLI_SCHEMA = "literate-ai/native-cli-acceptance@2"
SERVICE_SCHEMA = "literate-ai/persistent-service-acceptance@1"
LINT_RENDER_SCHEMA = "literate-ai/lint-render-acceptance@1"
BROWSER_SCHEMA = "literate-ai/browser-interaction-acceptance@1"
IPC_SURFACE_SCHEMA = "literate-ai/ipc-surface-conformance-acceptance@1"
IPC_SURFACE_NATIVE_SCHEMA = "literate-ai/ipc-surface-conformance-acceptance@2"
LIBRARY_SCHEMA = "literate-ai/library-acceptance-oracle@1"
ACCEPTANCE_DIRECTORY = "verification/acceptance"
PYTHON_PLACEHOLDER = "{python}"
_MAXIMUM_ORACLE_BYTES = 1024 * 1024
_MAXIMUM_SERVICE_CASES = 64
_MAXIMUM_SERVICE_TIMEOUT_SECONDS = 300
_MAXIMUM_SERVICE_OUTPUT_BYTES = 16 * 1024 * 1024
_MAXIMUM_ARGV = 32
_MAXIMUM_BROWSER_VIEWPORTS = 8
_MAXIMUM_BROWSER_STEPS = 64
_MAXIMUM_BROWSER_POSTCONDITIONS = 64
_MAXIMUM_BROWSER_ROLES = 64
_MAXIMUM_VIEWPORT_PIXELS = 8192
_MAXIMUM_IPC_PROTOCOL_LENGTH = 64
_MAXIMUM_IPC_IDENTITY_LENGTH = 512

_BROWSER_STEP_ACTIONS = frozenset({"click", "select", "fill", "scroll", "keyboard"})
_BROWSER_POSTCONDITION_KINDS = frozenset(
    {"text", "state", "aria", "row-identity", "count", "download"}
)
_BROWSER_REJECTION_CLASSES = frozenset(
    {"console-error", "page-error", "failed-request", "http-error"}
)

INDEPENDENT_ACCEPTANCE_EXEMPT_SCHEMA = "literate-ai/independent-acceptance-exempt@1"


class ComponentAcceptanceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def oracle_path(
    project_root: Path, component_name: str, entrypoint_name: str | None = None
) -> Path:
    for label, value in (
        ("component name", component_name),
        ("entrypoint name", entrypoint_name),
    ):
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.path_invalid",
                f"{label} must be one safe non-empty path segment",
            )
    root = project_root.joinpath(*ACCEPTANCE_DIRECTORY.split("/"))
    if entrypoint_name is None:
        return root / f"{component_name}.json"
    return root / component_name / f"{entrypoint_name}.json"


@dataclass(frozen=True, slots=True)
class ComponentAcceptanceOracleBinding:
    entrypoint_name: str
    deployment_unit: str
    entrypoint_kind: str
    oracle: object | None

    def __post_init__(self) -> None:
        for label, value in (
            ("entrypoint name", self.entrypoint_name),
            ("deployment unit", self.deployment_unit),
            ("entrypoint kind", self.entrypoint_kind),
        ):
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ComponentAcceptanceError(
                    "component_acceptance.binding_invalid",
                    f"oracle binding {label} must be a bounded non-empty string",
                )

    @property
    def identity(self) -> ContentIdentity:
        oracle_identity = getattr(self.oracle, "identity", None)
        return canonical_identity(
            {
                "schema": "literate-ai/component-acceptance-oracle-binding@1",
                "entrypoint_name": self.entrypoint_name,
                "deployment_unit": self.deployment_unit,
                "entrypoint_kind": self.entrypoint_kind,
                "oracle_identity": (
                    oracle_identity.uri
                    if isinstance(oracle_identity, ContentIdentity)
                    else None
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ComponentAcceptanceOracleBundle:
    """Verifier-owned oracle selection for every multi-entrypoint unit."""

    bindings: tuple[ComponentAcceptanceOracleBinding, ...]

    def __post_init__(self) -> None:
        if len(self.bindings) < 2 or any(
            not isinstance(item, ComponentAcceptanceOracleBinding)
            for item in self.bindings
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.bundle_invalid",
                "a multi-entrypoint oracle bundle requires at least two bindings",
            )
        units = tuple(item.deployment_unit for item in self.bindings)
        names = tuple(item.entrypoint_name for item in self.bindings)
        if units != tuple(sorted(set(units))) or len(set(names)) != len(names):
            raise ComponentAcceptanceError(
                "component_acceptance.bundle_invalid",
                "oracle bindings must use unique names and canonical unique "
                "deployment-unit order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/component-acceptance-oracle-bundle@1",
                "bindings": [item.identity.uri for item in self.bindings],
            }
        )

    def for_deployment_unit(
        self, deployment_unit: str
    ) -> ComponentAcceptanceOracleBinding:
        for item in self.bindings:
            if item.deployment_unit == deployment_unit:
                return item
        raise ComponentAcceptanceError(
            "component_acceptance.bundle_incomplete",
            f"no verifier-owned acceptance binding exists for {deployment_unit!r}",
        )


@dataclass(frozen=True, slots=True)
class DeclaredAcceptanceCase:
    case_id: str
    arguments: list[Any]
    expected_result: Any


@dataclass(frozen=True, slots=True)
class NativeCliContentExpectation:
    """Exact binary bytes or bounded UTF-8 predicates, never both."""

    exact: bytes | None
    contains: tuple[str, ...]
    not_contains: tuple[str, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "exact_identity": (
                None
                if self.exact is None
                else f"sha256:{hashlib.sha256(self.exact).hexdigest()}"
            ),
            "contains": list(self.contains),
            "not_contains": list(self.not_contains),
        }


@dataclass(frozen=True, slots=True)
class NativeCliFixture:
    path: str
    mode: int
    content: bytes

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "path": self.path,
                "mode": self.mode,
                "content_identity": (
                    f"sha256:{hashlib.sha256(self.content).hexdigest()}"
                ),
                "size_bytes": len(self.content),
            }
        )


@dataclass(frozen=True, slots=True)
class NativeCliFilesystemExpectation:
    path: str
    kind: str
    mode: int
    maximum_bytes: int
    content: NativeCliContentExpectation | None

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "path": self.path,
                "kind": self.kind,
                "mode": self.mode,
                "maximum_bytes": self.maximum_bytes,
                "content_identity": (
                    None if self.content is None else self.content.identity.uri
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class NativeCliCase:
    case_id: str
    arguments: tuple[str, ...]
    stdin: bytes | None
    expected_exit_code: int | str
    stdout: NativeCliContentExpectation
    stderr: NativeCliContentExpectation
    fixtures: tuple[NativeCliFixture, ...]
    filesystem: tuple[NativeCliFilesystemExpectation, ...]
    timeout_seconds: float

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "case_id": self.case_id,
                "arguments": list(self.arguments),
                "stdin_identity": (
                    None
                    if self.stdin is None
                    else f"sha256:{hashlib.sha256(self.stdin).hexdigest()}"
                ),
                "expected_exit_code": self.expected_exit_code,
                "stdout_identity": self.stdout.identity.uri,
                "stderr_identity": self.stderr.identity.uri,
                "fixture_identities": [item.identity.uri for item in self.fixtures],
                "filesystem_identities": [
                    item.identity.uri for item in self.filesystem
                ],
                "timeout_milliseconds": round(self.timeout_seconds * 1000),
            }
        )


@dataclass(frozen=True, slots=True)
class NativeCliAcceptance:
    """Verifier-owned native argv contract bound to current locked authority."""

    component: str
    entrypoint: Entrypoint
    entrypoint_identity: ContentIdentity
    specification_set_identity: ContentIdentity
    target_identity: ContentIdentity
    environment: tuple[tuple[str, str], ...]
    cases: tuple[NativeCliCase, ...]
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    filesystem_limit_bytes: int

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": NATIVE_CLI_SCHEMA,
                "component": self.component,
                "entrypoint_identity": self.entrypoint_identity.uri,
                "specification_set_identity": self.specification_set_identity.uri,
                "target_identity": self.target_identity.uri,
                "environment": dict(self.environment),
                "case_identities": [item.identity.uri for item in self.cases],
                "stdout_limit_bytes": self.stdout_limit_bytes,
                "stderr_limit_bytes": self.stderr_limit_bytes,
                "filesystem_limit_bytes": self.filesystem_limit_bytes,
            }
        )

    def require_current(self, component_lock: ComponentLock) -> None:
        root_node = next(
            (
                item
                for item in component_lock.nodes
                if item.revision.identity == component_lock.root_revision
            ),
            None,
        )
        if root_node is None:
            raise ComponentAcceptanceError(
                "component_acceptance.root_missing",
                "Component lock does not contain its declared root revision",
            )
        entrypoints = tuple(root_node.revision.definition.entrypoints)
        current = next(
            (item for item in entrypoints if item.name == self.entrypoint.name), None
        )
        if (
            root_node.revision.definition.coordinate.name != self.component
            or root_node.revision.specification_set_identity
            != self.specification_set_identity
            or root_node.target_flavor_selection.identity != self.target_identity
            or current is None
            or current.kind != NATIVE_CLI_ENTRYPOINT_KIND
            or canonical_identity(current.to_dict()) != self.entrypoint_identity
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                "native CLI acceptance no longer binds the current Component, "
                "entrypoint, specification set, and target",
            )


_NATIVE_CLI_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,126}")
_NATIVE_CLI_SECRET_NAME = re.compile(
    r"(?:secret|token|password|credential|private[_-]?key|api[_-]?key)", re.I
)
_NATIVE_CLI_PLACEHOLDER = re.compile(r"\{(?:work|file:([^{}]+))\}")


def _native_cli_relative_path(value: object, path: Path, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} has an invalid {label}"
        )
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a normalized relative POSIX path",
        )
    return value


def _native_cli_bytes(value: object, path: Path, label: str, maximum: int) -> bytes:
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4 + 4:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be bounded canonical base64",
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be canonical base64",
        ) from exc
    if len(decoded) > maximum or base64.b64encode(decoded).decode("ascii") != value:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} exceeds its bound or is not canonical base64",
        )
    return decoded


def _native_cli_strings(value: object, path: Path, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or any(
            not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item
            for item in value
        )
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a bounded nonempty string list",
        )
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be canonical and unique",
        )
    return result


def _native_cli_content(
    value: object, path: Path, label: str, maximum: int
) -> NativeCliContentExpectation:
    if not isinstance(value, dict) or set(value) != {
        "exact_base64",
        "utf8_contains",
        "utf8_not_contains",
    }:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} has malformed {label}",
        )
    exact_value = value["exact_base64"]
    exact = (
        None
        if exact_value is None
        else _native_cli_bytes(exact_value, path, f"{label}.exact_base64", maximum)
    )
    contains = _native_cli_strings(
        value["utf8_contains"], path, f"{label}.utf8_contains"
    )
    not_contains = _native_cli_strings(
        value["utf8_not_contains"], path, f"{label}.utf8_not_contains"
    )
    if (exact is None) == (not contains and not not_contains):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must choose exact bytes or nonempty UTF-8 predicates",
        )
    return NativeCliContentExpectation(exact, contains, not_contains)


def load_native_cli_acceptance(
    path: Path,
    component_name: str,
    entrypoint: Entrypoint,
    *,
    specification_set_identity: ContentIdentity,
    target_identity: ContentIdentity,
) -> NativeCliAcceptance:
    """Load one strict native CLI contract and bind current lock-selected authority."""

    document = _read_document(path, expected_schema=NATIVE_CLI_SCHEMA)
    required = {
        "schema",
        "component",
        "entrypoint",
        "specification_set_identity",
        "environment",
        "cases",
        "stdout_limit_bytes",
        "stderr_limit_bytes",
        "filesystem_limit_bytes",
    }
    if (
        set(document) != required
        or document["component"] != component_name
        or document["entrypoint"] != entrypoint.name
        or entrypoint.kind != NATIVE_CLI_ENTRYPOINT_KIND
        or document["specification_set_identity"] != specification_set_identity.uri
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} does not bind the exact native CLI Component authority",
        )
    stdout_limit = _positive_integer(
        document["stdout_limit_bytes"],
        path,
        "stdout_limit_bytes",
        maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
    )
    stderr_limit = _positive_integer(
        document["stderr_limit_bytes"],
        path,
        "stderr_limit_bytes",
        maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
    )
    filesystem_limit = _positive_integer(
        document["filesystem_limit_bytes"],
        path,
        "filesystem_limit_bytes",
        maximum=256 * 1024 * 1024,
    )
    raw_environment = document["environment"]
    if not isinstance(raw_environment, dict) or len(raw_environment) > 64:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} environment must be a bounded object",
        )
    environment: list[tuple[str, str]] = []
    for name, value in sorted(raw_environment.items()):
        if (
            not isinstance(name, str)
            or _NATIVE_CLI_ENVIRONMENT_NAME.fullmatch(name) is None
            or _NATIVE_CLI_SECRET_NAME.search(name) is not None
            or not isinstance(value, str)
            or len(value) > 4096
            or "\x00" in value
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid",
                f"{path} environment contains an unsafe name or value",
            )
        environment.append((name, value))
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 64:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare one to 64 native CLI cases",
        )
    cases: list[NativeCliCase] = []
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {
            "case_id",
            "arguments",
            "stdin_base64",
            "expected_exit_code",
            "stdout",
            "stderr",
            "fixtures",
            "filesystem",
            "timeout_seconds",
        }:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} has a malformed native CLI case",
            )
        case_id = raw["case_id"]
        arguments = raw["arguments"]
        if (
            not isinstance(case_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,126}", case_id) is None
            or not isinstance(arguments, list)
            or len(arguments) > 128
            or any(
                not isinstance(item, str) or len(item) > 4096 or "\x00" in item
                for item in arguments
            )
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} has an invalid case ID or argument vector",
            )
        raw_fixtures = raw["fixtures"]
        if not isinstance(raw_fixtures, list) or len(raw_fixtures) > 128:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} fixtures must be bounded",
            )
        fixtures: list[NativeCliFixture] = []
        fixture_bytes = 0
        for item in raw_fixtures:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "mode",
                "content_base64",
            }:
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    f"{path} has a malformed fixture",
                )
            content = _native_cli_bytes(
                item["content_base64"], path, "fixture.content_base64", 16 * 1024 * 1024
            )
            fixture_bytes += len(content)
            fixtures.append(
                NativeCliFixture(
                    _native_cli_relative_path(item["path"], path, "fixture path"),
                    _positive_integer(
                        item["mode"], path, "fixture.mode", maximum=0o777
                    ),
                    content,
                )
            )
        if fixture_bytes > filesystem_limit or tuple(
            item.path for item in fixtures
        ) != tuple(sorted({item.path for item in fixtures})):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} fixtures exceed the filesystem budget or are not canonical",
            )
        fixture_paths = {item.path for item in fixtures}
        for argument in arguments:
            stripped = _NATIVE_CLI_PLACEHOLDER.sub("", argument)
            if "{" in stripped or "}" in stripped:
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    f"{path} argument contains an unknown placeholder",
                )
            for match in _NATIVE_CLI_PLACEHOLDER.finditer(argument):
                fixture = match.group(1)
                if fixture is not None and fixture not in fixture_paths:
                    raise ComponentAcceptanceError(
                        "component_acceptance.case_invalid",
                        f"{path} argument references an undeclared fixture",
                    )
        raw_filesystem = raw["filesystem"]
        if not isinstance(raw_filesystem, list) or len(raw_filesystem) > 256:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} filesystem projection must be bounded",
            )
        filesystem: list[NativeCliFilesystemExpectation] = []
        for item in raw_filesystem:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "path",
                    "kind",
                    "mode",
                    "maximum_bytes",
                    "content",
                }
                or item["kind"] not in {"directory", "file"}
            ):
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    f"{path} has a malformed filesystem expectation",
                )
            content = (
                None
                if item["content"] is None
                else _native_cli_content(
                    item["content"], path, "filesystem.content", filesystem_limit
                )
            )
            maximum_bytes = item["maximum_bytes"]
            if (
                isinstance(maximum_bytes, bool)
                or not isinstance(maximum_bytes, int)
                or not 0 <= maximum_bytes <= filesystem_limit
            ):
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    f"{path} filesystem maximum_bytes is outside its bound",
                )
            if (item["kind"] == "directory") != (
                content is None and maximum_bytes == 0
            ):
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    f"{path} directory/file content declaration is inconsistent",
                )
            filesystem.append(
                NativeCliFilesystemExpectation(
                    _native_cli_relative_path(item["path"], path, "filesystem path"),
                    item["kind"],
                    _positive_integer(
                        item["mode"], path, "filesystem.mode", maximum=0o777
                    ),
                    maximum_bytes,
                    content,
                )
            )
        if tuple(item.path for item in filesystem) != tuple(
            sorted({item.path for item in filesystem})
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} filesystem projection must be canonical and unique",
            )
        expected_exit = raw["expected_exit_code"]
        if (
            not isinstance(expected_exit, int)
            or isinstance(expected_exit, bool)
            or not 0 <= expected_exit <= 255
        ) and expected_exit != "nonzero":
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} has an invalid expected exit code",
            )
        stdin_value = raw["stdin_base64"]
        stdin = (
            None
            if stdin_value is None
            else _native_cli_bytes(
                stdin_value, path, "stdin_base64", _MAXIMUM_SERVICE_OUTPUT_BYTES
            )
        )
        cases.append(
            NativeCliCase(
                case_id,
                tuple(arguments),
                stdin,
                expected_exit,
                _native_cli_content(raw["stdout"], path, "stdout", stdout_limit),
                _native_cli_content(raw["stderr"], path, "stderr", stderr_limit),
                tuple(fixtures),
                tuple(filesystem),
                _positive_number(
                    raw["timeout_seconds"],
                    path,
                    "timeout_seconds",
                    maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
                ),
            )
        )
    if tuple(item.case_id for item in cases) != tuple(
        sorted({item.case_id for item in cases})
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.case_invalid",
            f"{path} cases must be canonical and unique",
        )
    return NativeCliAcceptance(
        component_name,
        entrypoint,
        canonical_identity(entrypoint.to_dict()),
        specification_set_identity,
        target_identity,
        tuple(environment),
        tuple(cases),
        stdout_limit,
        stderr_limit,
        filesystem_limit,
    )


@dataclass(frozen=True, slots=True)
class DeclaredLibraryAcceptanceCase:
    case_id: str
    capability: str
    arguments: list[Any]
    expected_result: Any


@dataclass(frozen=True, slots=True)
class LibraryAcceptance:
    """Verifier-owned harness and cases for one exact importable library."""

    component: str
    specification_set_identity: ContentIdentity
    public_interface_identities: tuple[ContentIdentity, ...]
    import_surface_identity: ContentIdentity
    language: str
    harness_identity: ContentIdentity
    harness_content: bytes
    cases: tuple[DeclaredLibraryAcceptanceCase, ...]

    def __post_init__(self) -> None:
        for case in self.cases:
            try:
                product_json_bytes(case.arguments)
                product_json_bytes(case.expected_result)
            except ValueError as exc:
                raise ComponentAcceptanceError(
                    "component_acceptance.case_invalid",
                    "library acceptance cases must contain finite product JSON",
                ) from exc

    @property
    def identity(self) -> ContentIdentity:
        return product_json_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """The existing host-path-free identity payload, without harness bytes."""

        return {
            "schema": LIBRARY_SCHEMA,
            "component": self.component,
            "specification_set_identity": self.specification_set_identity.uri,
            "public_interface_identities": [
                item.uri for item in self.public_interface_identities
            ],
            "import_surface_identity": self.import_surface_identity.uri,
            "language": self.language,
            "harness_identity": self.harness_identity.uri,
            "cases": [
                {
                    "case_id": item.case_id,
                    "capability": item.capability,
                    "arguments": item.arguments,
                    "expected_result": item.expected_result,
                }
                for item in self.cases
            ],
        }


def load_library_acceptance(
    path: Path,
    component_name: str,
    *,
    read_bytes: Callable[[Path], bytes] | None = None,
) -> LibraryAcceptance:
    """Load one bounded library oracle and its separately identity-bound harness."""

    document = _read_document(
        path, expected_schema=LIBRARY_SCHEMA, read_bytes=read_bytes
    )
    expected_fields = {
        "schema",
        "component",
        "specification_set_identity",
        "public_interface_identities",
        "import_surface_identity",
        "language",
        "harness",
        "harness_identity",
        "cases",
    }
    if set(document) != expected_fields:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} must contain exactly the library oracle fields",
        )
    if document.get("schema") != LIBRARY_SCHEMA:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} must declare {LIBRARY_SCHEMA}",
        )
    if document.get("component") != component_name:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} names another Component",
        )
    language = document.get("language")
    if language not in {"python", "javascript", "rust", "cpp"}:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} must select python, javascript, rust, or cpp",
        )

    def parse_identity(value: object, label: str) -> ContentIdentity:
        try:
            return ContentIdentity.parse_uri(value)
        except (TypeError, ValueError) as exc:
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid",
                f"{path} has an invalid {label}",
            ) from exc

    specification = parse_identity(
        document.get("specification_set_identity"), "specification-set identity"
    )
    surface = parse_identity(
        document.get("import_surface_identity"), "import-surface identity"
    )
    raw_interfaces = document.get("public_interface_identities")
    if not isinstance(raw_interfaces, list) or not raw_interfaces:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} must bind public_interface_identities",
        )
    interfaces = tuple(
        parse_identity(item, "public-interface identity") for item in raw_interfaces
    )
    if interfaces != tuple(sorted(set(interfaces), key=lambda item: item.uri)):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} public interfaces must be canonical and unique",
        )
    harness_name = document.get("harness")
    if (
        not isinstance(harness_name, str)
        or not harness_name
        or PurePosixPath(harness_name).name != harness_name
        or "\\" in harness_name
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} must name one sibling harness file",
        )
    harness_path = path.parent / harness_name
    if harness_path.is_symlink() or not harness_path.is_file():
        raise ComponentAcceptanceError(
            "component_acceptance.harness_missing",
            f"{path} library harness is unavailable",
        )
    harness_content = (
        harness_path.read_bytes() if read_bytes is None else read_bytes(harness_path)
    )
    if not harness_content or len(harness_content) > _MAXIMUM_ORACLE_BYTES:
        raise ComponentAcceptanceError(
            "component_acceptance.harness_invalid",
            f"{path} library harness is empty or oversized",
        )
    harness_identity = parse_identity(
        document.get("harness_identity"), "harness identity"
    )
    actual_harness = ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(harness_content).hexdigest()
    )
    if harness_identity != actual_harness:
        raise ComponentAcceptanceError(
            "component_acceptance.harness_changed",
            f"{path} library harness differs from its reviewed identity",
        )
    raw_cases = document.get("cases")
    if (
        not isinstance(raw_cases, list)
        or not raw_cases
        or len(raw_cases) > _MAXIMUM_SERVICE_CASES
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare at least one library acceptance case",
        )
    cases = []
    seen = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {
            "arguments",
            "capability",
            "case_id",
            "expected_result",
        }:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} has a malformed library case",
            )
        case_id = raw["case_id"]
        capability = raw["capability"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or len(case_id) > 255
            or case_id in seen
            or not isinstance(capability, str)
            or not capability
            or len(capability) > 255
            or not isinstance(raw["arguments"], list)
            or len(raw["arguments"]) > _MAXIMUM_ARGV
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} has an invalid library case",
            )
        seen.add(case_id)
        cases.append(
            DeclaredLibraryAcceptanceCase(
                case_id, capability, list(raw["arguments"]), raw["expected_result"]
            )
        )
    if tuple(item.case_id for item in cases) != tuple(sorted(seen)):
        raise ComponentAcceptanceError(
            "component_acceptance.case_invalid",
            f"{path} library cases must use canonical case_id order",
        )
    return LibraryAcceptance(
        component_name,
        specification,
        interfaces,
        surface,
        language,
        harness_identity,
        harness_content,
        tuple(cases),
    )


@dataclass(frozen=True, slots=True)
class ServiceHttpProbe:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    expected_status: int
    expected_headers: tuple[tuple[str, str], ...]
    expected_json: Any | None
    expected_text_contains: tuple[str, ...]
    expected_sse_events: tuple[tuple[tuple[str, str], ...], ...]
    timeout_seconds: float
    response_limit_bytes: int
    expected_json_present: bool = False


@dataclass(frozen=True, slots=True)
class PersistentServiceAcceptance:
    component: str
    specification_set_identity: str
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    readiness: ServiceHttpProbe
    requests: tuple[ServiceHttpProbe, ...]
    startup_timeout_seconds: float
    process_timeout_seconds: float
    shutdown_timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int

    @property
    def component_name(self) -> str:
        return self.component

    @property
    def identity(self) -> ContentIdentity:
        return product_json_identity(
            {
                "schema": SERVICE_SCHEMA,
                "component": self.component,
                "specification_set_identity": self.specification_set_identity,
                "arguments": list(self.arguments),
                "environment": dict(self.environment),
                "readiness": _probe_identity_document(self.readiness),
                "requests": [
                    _probe_identity_document(request) for request in self.requests
                ],
                "startup_timeout_milliseconds": round(
                    self.startup_timeout_seconds * 1000
                ),
                "process_timeout_milliseconds": round(
                    self.process_timeout_seconds * 1000
                ),
                "shutdown_timeout_milliseconds": round(
                    self.shutdown_timeout_seconds * 1000
                ),
                "stdout_limit_bytes": self.stdout_limit_bytes,
                "stderr_limit_bytes": self.stderr_limit_bytes,
            }
        )

    def require_current(self, component_lock: ComponentLock) -> None:
        observed = _root_specification_identity(component_lock)
        if observed is not None and observed != self.specification_set_identity:
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                f"persistent-service acceptance for {self.component!r} was written "
                f"against {self.specification_set_identity}, but the locked Component "
                f"now specifies {observed}; review and rebind the contract",
            )


@dataclass(frozen=True, slots=True)
class IpcSurfaceConformanceAcceptance:
    """Verifier-owned, protocol-neutral conformance oracle for a served IPC surface.

    ADR-0029 introduces a single notion -- an *IPC surface*: a Component boundary
    exposing named operations to out-of-process callers under four properties. MCP,
    REST, and gRPC are *adapters* of it, not separate features. This contract is the
    framework machinery those adapters plug into; it is deliberately not shaped to
    any one protocol.

    The launch fields (``arguments``, ``environment``, and the process timeouts and
    output limits) are identical in shape to :class:`PersistentServiceAcceptance`:
    an IPC surface is served by a ``persistent-service`` deployment unit launched
    via the shipped ``--litai-serve`` path, so it reuses the same launch semantics.
    An IPC surface is therefore *not* a new entrypoint kind -- a persistent-service
    may additionally declare this oracle instead of the plain HTTP probe.

    The four ADR-0029 properties map to fields as follows:

    * **Declared schema** -- ``declared_schema_identity`` is the surface's declared
      schema identity. The served self-description and every response are checked
      against it; a mismatch fails closed.
    * **Served self-description** -- ``description_probe`` is how the verifier fetches
      the surface's own machine-readable description (an HTTP GET of, e.g.,
      ``/openapi.json`` for REST, a reflection marker for gRPC, the tool/resource
      listing for MCP). It reuses :class:`ServiceHttpProbe` so the fetch is a plain
      declared HTTP request; the ``protocol`` tag tells an adapter how to interpret
      the served bytes.
    * **Conformance acceptance** -- ``request_cases`` are declared operation calls
      whose responses must validate against the declared schema. The decision that
      turns fetched bytes + observed responses into pass/fail lives, engine-free, in
      :mod:`literate_ai.adapters.ipc_surface_acceptance`.
    * **Version + compatibility promise** -- ``surface_version`` is a semantic
      version and ``compatibility`` is ADR-0005's :class:`CompatibilityPromise`,
      reused rather than reinvented, so a surface version transition is checkable the
      same way a Component public-interface change is.

    ``protocol`` is an open adapter tag (``mcp`` / ``rest`` / ``grpc`` and any future
    adapter), intentionally a bounded nonempty string rather than an enum baked to
    those three, so a new adapter does not require touching this contract.
    """

    component: str
    specification_set_identity: str
    protocol: str
    declared_schema_identity: str
    surface_version: str
    compatibility: CompatibilityPromise
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    description_probe: ServiceHttpProbe | GrpcReflectionProbe
    request_cases: tuple[ServiceHttpProbe, ...] | tuple[GrpcBoundCallCase, ...]
    startup_timeout_seconds: float
    process_timeout_seconds: float
    shutdown_timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int

    @property
    def component_name(self) -> str:
        return self.component

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": (
                    IPC_SURFACE_NATIVE_SCHEMA
                    if isinstance(self.description_probe, GrpcReflectionProbe)
                    else IPC_SURFACE_SCHEMA
                ),
                "component": self.component,
                "specification_set_identity": self.specification_set_identity,
                "protocol": self.protocol,
                "declared_schema_identity": self.declared_schema_identity,
                "surface_version": self.surface_version,
                "compatibility": self.compatibility.to_dict(),
                "arguments": list(self.arguments),
                "environment": dict(self.environment),
                "description_probe": (
                    self.description_probe.to_document()
                    if isinstance(self.description_probe, GrpcReflectionProbe)
                    else _probe_identity_document(self.description_probe)
                ),
                "request_cases": [
                    case.to_document()
                    if isinstance(case, GrpcBoundCallCase)
                    else _probe_identity_document(case)
                    for case in self.request_cases
                ],
                "startup_timeout_milliseconds": round(
                    self.startup_timeout_seconds * 1000
                ),
                "process_timeout_milliseconds": round(
                    self.process_timeout_seconds * 1000
                ),
                "shutdown_timeout_milliseconds": round(
                    self.shutdown_timeout_seconds * 1000
                ),
                "stdout_limit_bytes": self.stdout_limit_bytes,
                "stderr_limit_bytes": self.stderr_limit_bytes,
            }
        )

    def require_current(self, component_lock: ComponentLock) -> None:
        observed = _root_specification_identity(component_lock)
        if observed is not None and observed != self.specification_set_identity:
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                f"ipc-surface conformance acceptance for {self.component!r} was "
                f"written against {self.specification_set_identity}, but the locked "
                f"Component now specifies {observed}; review and rebind the contract",
            )


@dataclass(frozen=True, slots=True)
class BrowserViewport:
    """One named viewport the contract drives, e.g. a desktop and a 390px phone."""

    label: str
    width: int
    height: int

    def to_document(self) -> dict[str, Any]:
        return {"label": self.label, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class BrowserInteractionStep:
    """One declared interaction: click, select, fill, scroll, or keyboard."""

    action: str
    selector: str
    value: str = ""

    def to_document(self) -> dict[str, Any]:
        return {"action": self.action, "selector": self.selector, "value": self.value}


@dataclass(frozen=True, slots=True)
class BrowserPostcondition:
    """One observable postcondition: text/state/aria/row-identity/count/download."""

    postcondition_id: str
    kind: str
    selector: str
    expected: str = ""
    attribute: str = ""

    def to_document(self) -> dict[str, Any]:
        return {
            "postcondition_id": self.postcondition_id,
            "kind": self.kind,
            "selector": self.selector,
            "expected": self.expected,
            "attribute": self.attribute,
        }


@dataclass(frozen=True, slots=True)
class BrowserInteractionAcceptance:
    """Verifier-owned, browser-tool-neutral acceptance for a served frontend.

    This closes the enforcement gap ADR-0028 identifies: text-only acceptance
    checks the HTTP body, so a page that renders ``NaN``, binds no working scroll
    handler, ships a no-op export button, or overflows a mobile viewport passes.
    The FRONTEND-002 ``verify-frontend-browser`` operator skill enumerates the
    same checks for manual review; this contract makes a real browser engine
    enforce them in the lifecycle.

    The declaration is intentionally not Playwright-shaped -- it names viewports,
    interaction steps, observable postconditions, required roles/landmarks, and
    rejection classes. An engine adapter behind
    :mod:`literate_ai.adapters.browser_acceptance` realizes it.
    """

    component: str
    specification_set_identity: str
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    readiness_path: str
    viewports: tuple[BrowserViewport, ...]
    steps: tuple[BrowserInteractionStep, ...]
    postconditions: tuple[BrowserPostcondition, ...]
    required_roles: tuple[tuple[str, str], ...]
    required_landmarks: tuple[str, ...]
    rejection_classes: frozenset[str]
    reject_horizontal_overflow: bool
    expected_request_count: int | None
    startup_timeout_seconds: float
    process_timeout_seconds: float
    shutdown_timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int

    @property
    def component_name(self) -> str:
        return self.component

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": BROWSER_SCHEMA,
                "component": self.component,
                "specification_set_identity": self.specification_set_identity,
                "arguments": list(self.arguments),
                "environment": dict(self.environment),
                "readiness_path": self.readiness_path,
                "viewports": [item.to_document() for item in self.viewports],
                "steps": [item.to_document() for item in self.steps],
                "postconditions": [item.to_document() for item in self.postconditions],
                "required_roles": [list(item) for item in self.required_roles],
                "required_landmarks": list(self.required_landmarks),
                "rejection_classes": sorted(self.rejection_classes),
                "reject_horizontal_overflow": self.reject_horizontal_overflow,
                "expected_request_count": self.expected_request_count,
                "startup_timeout_milliseconds": round(
                    self.startup_timeout_seconds * 1000
                ),
                "process_timeout_milliseconds": round(
                    self.process_timeout_seconds * 1000
                ),
                "shutdown_timeout_milliseconds": round(
                    self.shutdown_timeout_seconds * 1000
                ),
                "stdout_limit_bytes": self.stdout_limit_bytes,
                "stderr_limit_bytes": self.stderr_limit_bytes,
            }
        )

    def require_current(self, component_lock: ComponentLock) -> None:
        observed = _root_specification_identity(component_lock)
        if observed is not None and observed != self.specification_set_identity:
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                f"browser-interaction acceptance for {self.component!r} was written "
                f"against {self.specification_set_identity}, but the locked Component "
                f"now specifies {observed}; review and rebind the contract",
            )


def _read_document(
    path: Path,
    *,
    expected_schema: str | tuple[str, ...] = ORACLE_SCHEMA,
    read_bytes: Callable[[Path], bytes] | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_missing",
            f"no acceptance oracle at {path}; declare one before building, so "
            "generated source can be independently accepted",
        )
    if path.stat().st_size > _MAXIMUM_ORACLE_BYTES:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_too_large", f"{path} exceeds the maximum size"
        )
    try:
        content = path.read_bytes() if read_bytes is None else read_bytes(path)
        if len(content) > _MAXIMUM_ORACLE_BYTES:
            raise ComponentAcceptanceError(
                "component_acceptance.oracle_too_large",
                f"{path} exceeds the maximum size",
            )
        value = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_invalid", f"{path} is not valid JSON"
        ) from exc
    schemas = (
        (expected_schema,) if isinstance(expected_schema, str) else expected_schema
    )
    if not isinstance(value, dict) or value.get("schema") not in schemas:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_invalid",
            f"{path} must declare schema {expected_schema!r}",
        )
    return value


def _strict_keys(
    value: dict[str, Any], allowed: set[str], path: Path, label: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} has unknown fields: {', '.join(sorted(unknown))}",
        )


def _positive_number(value: Any, path: Path, label: str, *, maximum: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a positive number",
        )
    result = float(value)
    if result <= 0 or result > maximum:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be greater than zero and at most {maximum}",
        )
    return result


def _positive_integer(value: Any, path: Path, label: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a positive integer at most {maximum}",
        )
    return value


def _string_map(value: Any, path: Path, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or "\x00" in key
        or "\x00" in item
        for key, item in value.items()
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be an object of nonempty string keys "
            "and string values",
        )
    return tuple(sorted(value.items()))


def _bytes_identity(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _bounded_argv(value: Any, path: Path, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > _MAXIMUM_ARGV
        or any(
            not isinstance(item, str) or not item or "\x00" in item for item in value
        )
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a nonempty string array of at most "
            f"{_MAXIMUM_ARGV} arguments",
        )
    for item in value:
        if item == PYTHON_PLACEHOLDER:
            continue
        posix = PurePosixPath(item.replace("\\", "/"))
        if posix.is_absolute() or any(part == ".." for part in posix.parts):
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid",
                f"{path} {label} arguments must be relative POSIX paths, "
                f"bare program names, or {PYTHON_PLACEHOLDER}",
            )
    return tuple(value)


def _relative_source_root(value: Any, path: Path) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} source_root must be a relative POSIX path",
        )
    posix = PurePosixPath(value.replace("\\", "/"))
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} source_root must be a relative POSIX path",
        )
    return value.replace("\\", "/")


def _resolve_source_directory(project_root: Path, source_root: str, path: Path) -> Path:
    project = project_root.resolve()
    source = (project / source_root).resolve()
    try:
        source.relative_to(project)
    except ValueError as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} source_root must stay inside the project",
        ) from exc
    if not source.is_dir():
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} source_root must name an existing directory",
        )
    return source


def _snapshot_identity(value: Any, path: Path, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a sha256 identity",
        )
    try:
        return ContentIdentity.parse_uri(value).uri
    except (TypeError, ValueError) as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a sha256 identity",
        ) from exc


def _probe_identity_document(probe: ServiceHttpProbe) -> dict[str, Any]:
    return {
        "method": probe.method,
        "path": probe.path,
        "headers": dict(probe.headers),
        "body_identity": None
        if probe.body is None
        else f"sha256:{hashlib.sha256(probe.body).hexdigest()}",
        "expected_status": probe.expected_status,
        "expected_headers": dict(probe.expected_headers),
        "expected_json": (
            probe.expected_json if probe.expected_json_present else {"absent": True}
        ),
        "expected_text_contains": list(probe.expected_text_contains),
        "expected_sse_events": [dict(event) for event in probe.expected_sse_events],
        "timeout_milliseconds": round(probe.timeout_seconds * 1000),
        "response_limit_bytes": probe.response_limit_bytes,
    }


def _load_http_probe(value: Any, path: Path, label: str) -> ServiceHttpProbe:
    if not isinstance(value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} {label} must be an object"
        )
    _strict_keys(
        value,
        {
            "method",
            "path",
            "headers",
            "body",
            "expected_status",
            "expected_headers",
            "expected_json",
            "expected_text_contains",
            "expected_sse_events",
            "timeout_seconds",
            "response_limit_bytes",
        },
        path,
        label,
    )
    method = value.get("method", "GET")
    request_path = value.get("path")
    expected_status = value.get("expected_status", 200)
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} has invalid method",
        )
    if (
        not isinstance(request_path, str)
        or not request_path.startswith("/")
        or request_path.startswith("//")
        or "\x00" in request_path
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} path must be an origin-relative HTTP path",
        )
    if (
        isinstance(expected_status, bool)
        or not isinstance(expected_status, int)
        or not 100 <= expected_status <= 599
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} expected_status must be an HTTP status",
        )
    body_value = value.get("body")
    if body_value is not None and not isinstance(body_value, str):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} body must be a UTF-8 string",
        )
    contains = value.get("expected_text_contains", [])
    if not isinstance(contains, list) or any(
        not isinstance(item, str) or not item for item in contains
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} expected_text_contains must be a string array",
        )
    raw_events = value.get("expected_sse_events", [])
    if (
        not isinstance(raw_events, list)
        or len(raw_events) > _MAXIMUM_SERVICE_CASES
        or any(not isinstance(event, dict) for event in raw_events)
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} expected_sse_events must be a bounded array",
        )
    events = tuple(
        _string_map(event, path, f"{label} SSE event") for event in raw_events
    )
    return ServiceHttpProbe(
        method,
        request_path,
        _string_map(value.get("headers", {}), path, f"{label} headers"),
        None if body_value is None else body_value.encode("utf-8"),
        expected_status,
        _string_map(
            value.get("expected_headers", {}), path, f"{label} expected_headers"
        ),
        value.get("expected_json"),
        tuple(contains),
        events,
        _positive_number(
            value.get("timeout_seconds", 5),
            path,
            f"{label} timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_integer(
            value.get("response_limit_bytes", 1024 * 1024),
            path,
            f"{label} response_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
        "expected_json" in value,
    )


def load_persistent_service_acceptance(
    path: Path, component_name: str
) -> PersistentServiceAcceptance:
    """Load one strictly bounded verifier-owned HTTP service contract."""

    document = _read_document(path, expected_schema=SERVICE_SCHEMA)
    _strict_keys(
        document,
        {
            "schema",
            "specification_set_identity",
            "process",
            "readiness",
            "requests",
        },
        path,
        "document",
    )
    bound = document.get("specification_set_identity")
    process = document.get("process")
    requests = document.get("requests")
    if not isinstance(bound, str) or not bound:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_unbound",
            f"{path} must bind a specification_set_identity",
        )
    if not isinstance(process, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} process must be an object"
        )
    _strict_keys(
        process,
        {
            "arguments",
            "environment",
            "startup_timeout_seconds",
            "timeout_seconds",
            "shutdown_timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        },
        path,
        "process",
    )
    arguments = process.get("arguments", [])
    if not isinstance(arguments, list) or any(
        not isinstance(item, str) or "\x00" in item for item in arguments
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process arguments must be a string array",
        )
    environment = _string_map(
        process.get("environment", {}), path, "process environment"
    )
    if any(not name.isidentifier() or name.upper() != name for name, _ in environment):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process environment names must be uppercase identifiers",
        )
    if (
        not isinstance(requests, list)
        or not requests
        or len(requests) > _MAXIMUM_SERVICE_CASES
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare between one and {_MAXIMUM_SERVICE_CASES} requests",
        )
    return PersistentServiceAcceptance(
        component_name,
        bound,
        tuple(arguments),
        environment,
        _load_http_probe(document.get("readiness"), path, "readiness"),
        tuple(
            _load_http_probe(item, path, f"request {index}")
            for index, item in enumerate(requests)
        ),
        _positive_number(
            process.get("startup_timeout_seconds", 30),
            path,
            "process startup_timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("timeout_seconds", 120),
            path,
            "process timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("shutdown_timeout_seconds", 5),
            path,
            "process shutdown_timeout_seconds",
            maximum=30,
        ),
        _positive_integer(
            process.get("stdout_limit_bytes", 1024 * 1024),
            path,
            "process stdout_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
        _positive_integer(
            process.get("stderr_limit_bytes", 1024 * 1024),
            path,
            "process stderr_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
    )


def _bounded_identity(value: Any, path: Path, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value) > _MAXIMUM_IPC_IDENTITY_LENGTH
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be a nonempty string at most "
            f"{_MAXIMUM_IPC_IDENTITY_LENGTH} characters",
        )
    return value


def load_ipc_surface_conformance_acceptance(
    path: Path, component_name: str
) -> IpcSurfaceConformanceAcceptance:
    """Load one strictly bounded verifier-owned IPC-surface conformance contract.

    Mirrors :func:`load_persistent_service_acceptance`: the surface is served by a
    ``persistent-service`` deployment unit launched via the same ``--litai-serve``
    path, so the ``process`` launch fields are validated identically. Beyond the
    launch shape it binds ADR-0029's protocol-neutral properties -- an open
    ``protocol`` adapter tag, a ``description_probe`` for the served
    self-description, the ``declared_schema_identity`` the description and responses
    must match, declared ``request_cases``, a semantic ``surface_version``, and a
    reused ADR-0005 :class:`CompatibilityPromise`. All fields are strictly bounded
    and unknown keys fail closed; the document is verifier-only material never
    referenced from ``component.md`` (ADR-0005 authority boundary).
    """

    document = _read_document(
        path, expected_schema=(IPC_SURFACE_SCHEMA, IPC_SURFACE_NATIVE_SCHEMA)
    )
    _strict_keys(
        document,
        {
            "schema",
            "specification_set_identity",
            "protocol",
            "declared_schema_identity",
            "surface_version",
            "compatibility",
            "process",
            "description_probe",
            "request_cases",
        },
        path,
        "document",
    )
    bound = document.get("specification_set_identity")
    if not isinstance(bound, str) or not bound:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_unbound",
            f"{path} must bind a specification_set_identity",
        )
    protocol = document.get("protocol")
    if (
        not isinstance(protocol, str)
        or not protocol
        or "\x00" in protocol
        or len(protocol) > _MAXIMUM_IPC_PROTOCOL_LENGTH
        or protocol.strip() != protocol
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} protocol must be a nonempty adapter tag at most "
            f"{_MAXIMUM_IPC_PROTOCOL_LENGTH} characters",
        )
    declared_schema_identity = _bounded_identity(
        document.get("declared_schema_identity"), path, "declared_schema_identity"
    )
    try:
        surface_version = semantic_version(
            document.get("surface_version"), f"{path} surface_version"
        )
    except ValueError as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} surface_version must be a canonical semantic version",
        ) from exc
    try:
        compatibility = CompatibilityPromise.from_dict(
            document.get("compatibility"), path=f"{path} compatibility"
        )
    except ValueError as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} compatibility must be a valid CompatibilityPromise",
        ) from exc
    process = document.get("process")
    if not isinstance(process, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} process must be an object"
        )
    _strict_keys(
        process,
        {
            "arguments",
            "environment",
            "startup_timeout_seconds",
            "timeout_seconds",
            "shutdown_timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        },
        path,
        "process",
    )
    arguments = process.get("arguments", [])
    if not isinstance(arguments, list) or any(
        not isinstance(item, str) or "\x00" in item for item in arguments
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process arguments must be a string array",
        )
    environment = _string_map(
        process.get("environment", {}), path, "process environment"
    )
    if any(not name.isidentifier() or name.upper() != name for name, _ in environment):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process environment names must be uppercase identifiers",
        )
    request_cases = document.get("request_cases")
    if (
        not isinstance(request_cases, list)
        or not request_cases
        or len(request_cases) > _MAXIMUM_SERVICE_CASES
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare between one and {_MAXIMUM_SERVICE_CASES} "
            "request_cases",
        )
    if document["schema"] == IPC_SURFACE_NATIVE_SCHEMA:
        if protocol != "grpc":
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid",
                "native IPC @2 requires protocol grpc",
            )
        try:
            description_probe, parsed_cases = load_grpc_declarations(
                document.get("description_probe"),
                request_cases,
                declared_schema_identity,
            )
        except (ValueError, TypeError) as exc:
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid", f"{path}: {exc}"
            ) from exc
    else:
        description_probe = _load_http_probe(
            document.get("description_probe"), path, "description_probe"
        )
        parsed_cases = tuple(
            _load_http_probe(item, path, f"request_case {index}")
            for index, item in enumerate(request_cases)
        )
    return IpcSurfaceConformanceAcceptance(
        component_name,
        bound,
        protocol,
        declared_schema_identity,
        surface_version,
        compatibility,
        tuple(arguments),
        environment,
        description_probe,
        parsed_cases,
        _positive_number(
            process.get("startup_timeout_seconds", 30),
            path,
            "process startup_timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("timeout_seconds", 120),
            path,
            "process timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("shutdown_timeout_seconds", 5),
            path,
            "process shutdown_timeout_seconds",
            maximum=30,
        ),
        _positive_integer(
            process.get("stdout_limit_bytes", 1024 * 1024),
            path,
            "process stdout_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
        _positive_integer(
            process.get("stderr_limit_bytes", 1024 * 1024),
            path,
            "process stderr_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
    )


def _load_browser_viewport(value: Any, path: Path, label: str) -> BrowserViewport:
    if not isinstance(value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} {label} must be an object"
        )
    _strict_keys(value, {"label", "width", "height"}, path, label)
    name = value.get("label")
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} label must be a nonempty string",
        )
    width = _positive_integer(
        value.get("width"), path, f"{label} width", maximum=_MAXIMUM_VIEWPORT_PIXELS
    )
    height = _positive_integer(
        value.get("height"), path, f"{label} height", maximum=_MAXIMUM_VIEWPORT_PIXELS
    )
    return BrowserViewport(name, width, height)


def _load_browser_step(value: Any, path: Path, label: str) -> BrowserInteractionStep:
    if not isinstance(value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} {label} must be an object"
        )
    _strict_keys(value, {"action", "selector", "value"}, path, label)
    action = value.get("action")
    selector = value.get("selector")
    step_value = value.get("value", "")
    if action not in _BROWSER_STEP_ACTIONS:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} action must be one of "
            f"{', '.join(sorted(_BROWSER_STEP_ACTIONS))}",
        )
    if not isinstance(selector, str) or not selector or "\x00" in selector:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} selector must be a nonempty string",
        )
    if not isinstance(step_value, str) or "\x00" in step_value:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} value must be a string",
        )
    return BrowserInteractionStep(action, selector, step_value)


def _load_browser_postcondition(
    value: Any, path: Path, label: str, seen: set[str]
) -> BrowserPostcondition:
    if not isinstance(value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} {label} must be an object"
        )
    _strict_keys(
        value,
        {"postcondition_id", "kind", "selector", "expected", "attribute"},
        path,
        label,
    )
    identifier = value.get("postcondition_id")
    kind = value.get("kind")
    selector = value.get("selector")
    expected = value.get("expected", "")
    attribute = value.get("attribute", "")
    if (
        not isinstance(identifier, str)
        or not identifier
        or identifier in seen
        or "\x00" in identifier
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} postcondition_id must be a unique nonempty string",
        )
    if kind not in _BROWSER_POSTCONDITION_KINDS:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} kind must be one of "
            f"{', '.join(sorted(_BROWSER_POSTCONDITION_KINDS))}",
        )
    if not isinstance(selector, str) or not selector or "\x00" in selector:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} selector must be a nonempty string",
        )
    if not isinstance(expected, str) or "\x00" in expected:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} expected must be a string",
        )
    if not isinstance(attribute, str) or "\x00" in attribute:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} attribute must be a string",
        )
    seen.add(identifier)
    return BrowserPostcondition(identifier, kind, selector, expected, attribute)


def load_browser_interaction_acceptance(
    path: Path, component_name: str
) -> BrowserInteractionAcceptance:
    """Load one strictly bounded verifier-owned browser interaction contract.

    Mirrors :func:`load_persistent_service_acceptance`: a served frontend
    deployment unit is launched via the same ``--litai-serve`` path, but its
    acceptance is driven in a real browser instead of by text/HTTP probes. All
    fields are strictly bounded and unknown keys fail closed, so a generator
    cannot widen the contract. The document is verifier-only material and is
    never referenced from ``component.md`` (ADR-0005 authority boundary).
    """

    document = _read_document(path, expected_schema=BROWSER_SCHEMA)
    _strict_keys(
        document,
        {
            "schema",
            "specification_set_identity",
            "process",
            "readiness_path",
            "viewports",
            "steps",
            "postconditions",
            "required_roles",
            "required_landmarks",
            "rejection_classes",
            "reject_horizontal_overflow",
            "expected_request_count",
        },
        path,
        "document",
    )
    bound = document.get("specification_set_identity")
    if not isinstance(bound, str) or not bound:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_unbound",
            f"{path} must bind a specification_set_identity",
        )
    process = document.get("process")
    if not isinstance(process, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid", f"{path} process must be an object"
        )
    _strict_keys(
        process,
        {
            "arguments",
            "environment",
            "startup_timeout_seconds",
            "timeout_seconds",
            "shutdown_timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        },
        path,
        "process",
    )
    arguments = process.get("arguments", [])
    if not isinstance(arguments, list) or any(
        not isinstance(item, str) or "\x00" in item for item in arguments
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process arguments must be a string array",
        )
    environment = _string_map(
        process.get("environment", {}), path, "process environment"
    )
    if any(not name.isidentifier() or name.upper() != name for name, _ in environment):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} process environment names must be uppercase identifiers",
        )
    readiness_path = document.get("readiness_path", "/health")
    if (
        not isinstance(readiness_path, str)
        or not readiness_path.startswith("/")
        or readiness_path.startswith("//")
        or "\x00" in readiness_path
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} readiness_path must be an origin-relative HTTP path",
        )
    raw_viewports = document.get("viewports")
    if (
        not isinstance(raw_viewports, list)
        or not raw_viewports
        or len(raw_viewports) > _MAXIMUM_BROWSER_VIEWPORTS
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare between one and {_MAXIMUM_BROWSER_VIEWPORTS} "
            "viewports",
        )
    viewports = tuple(
        _load_browser_viewport(item, path, f"viewport {index}")
        for index, item in enumerate(raw_viewports)
    )
    raw_steps = document.get("steps", [])
    if not isinstance(raw_steps, list) or len(raw_steps) > _MAXIMUM_BROWSER_STEPS:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} steps must be an array of at most {_MAXIMUM_BROWSER_STEPS}",
        )
    steps = tuple(
        _load_browser_step(item, path, f"step {index}")
        for index, item in enumerate(raw_steps)
    )
    raw_postconditions = document.get("postconditions", [])
    if (
        not isinstance(raw_postconditions, list)
        or len(raw_postconditions) > _MAXIMUM_BROWSER_POSTCONDITIONS
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} postconditions must be an array of at most "
            f"{_MAXIMUM_BROWSER_POSTCONDITIONS}",
        )
    seen_postconditions: set[str] = set()
    postconditions = tuple(
        _load_browser_postcondition(
            item, path, f"postcondition {index}", seen_postconditions
        )
        for index, item in enumerate(raw_postconditions)
    )
    raw_roles = document.get("required_roles", [])
    if not isinstance(raw_roles, list) or len(raw_roles) > _MAXIMUM_BROWSER_ROLES:
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} required_roles must be a bounded array",
        )
    required_roles: list[tuple[str, str]] = []
    for item in raw_roles:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "name"}
            or not isinstance(item.get("role"), str)
            or not item["role"]
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ComponentAcceptanceError(
                "component_acceptance.contract_invalid",
                f"{path} each required role must declare a role and a name",
            )
        required_roles.append((item["role"], item["name"]))
    raw_landmarks = document.get("required_landmarks", [])
    if (
        not isinstance(raw_landmarks, list)
        or len(raw_landmarks) > _MAXIMUM_BROWSER_ROLES
        or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in raw_landmarks
        )
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} required_landmarks must be a bounded string array",
        )
    raw_rejections = document.get(
        "rejection_classes", sorted(_BROWSER_REJECTION_CLASSES)
    )
    if not isinstance(raw_rejections, list) or any(
        item not in _BROWSER_REJECTION_CLASSES for item in raw_rejections
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} rejection_classes must be a subset of "
            f"{', '.join(sorted(_BROWSER_REJECTION_CLASSES))}",
        )
    reject_overflow = document.get("reject_horizontal_overflow", True)
    if not isinstance(reject_overflow, bool):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} reject_horizontal_overflow must be a boolean",
        )
    expected_request_count = document.get("expected_request_count")
    if expected_request_count is not None and (
        isinstance(expected_request_count, bool)
        or not isinstance(expected_request_count, int)
        or expected_request_count < 0
        or expected_request_count > _MAXIMUM_SERVICE_CASES
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} expected_request_count must be a bounded non-negative integer",
        )
    return BrowserInteractionAcceptance(
        component_name,
        bound,
        tuple(arguments),
        environment,
        readiness_path,
        viewports,
        steps,
        postconditions,
        tuple(required_roles),
        tuple(raw_landmarks),
        frozenset(raw_rejections),
        reject_overflow,
        expected_request_count,
        _positive_number(
            process.get("startup_timeout_seconds", 30),
            path,
            "process startup_timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("timeout_seconds", 120),
            path,
            "process timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_number(
            process.get("shutdown_timeout_seconds", 5),
            path,
            "process shutdown_timeout_seconds",
            maximum=30,
        ),
        _positive_integer(
            process.get("stdout_limit_bytes", 1024 * 1024),
            path,
            "process stdout_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
        _positive_integer(
            process.get("stderr_limit_bytes", 1024 * 1024),
            path,
            "process stderr_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
    )


@dataclass(frozen=True, slots=True)
class LintRenderCommand:
    arguments: tuple[str, ...]
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int


@dataclass(frozen=True, slots=True)
class LintRenderAcceptance:
    """Verifier-owned lint plus exact render-snapshot contract for library/UI."""

    component: str
    specification_set_identity: str
    source_root: str
    source_directory: Path
    path: Path
    lint: LintRenderCommand
    render: LintRenderCommand
    snapshot_identity: str

    @property
    def component_name(self) -> str:
        return self.component

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "provider": "filesystem-lint-render-acceptance@1",
                "schema": LINT_RENDER_SCHEMA,
                "component": self.component,
                "specification_set_identity": self.specification_set_identity,
                "source_root": self.source_root,
                "lint_arguments": list(self.lint.arguments),
                "lint_timeout_milliseconds": round(self.lint.timeout_seconds * 1000),
                "render_arguments": list(self.render.arguments),
                "render_timeout_milliseconds": round(
                    self.render.timeout_seconds * 1000
                ),
                "snapshot_identity": self.snapshot_identity,
            }
        )

    def require_current(self, component_lock: ComponentLock) -> None:
        observed = _root_specification_identity(component_lock)
        if observed is not None and observed != self.specification_set_identity:
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                f"lint-render acceptance for {self.component!r} was written "
                f"against {self.specification_set_identity}, but the locked Component "
                f"now specifies {observed}; review and rebind the contract",
            )

    def accept(self, component_lock: ComponentLock) -> ContentIdentity:
        """Run lint, then compare render stdout to the bound snapshot identity."""

        self.require_current(component_lock)
        lint = _run_lint_render_command(
            self.lint, cwd=self.source_directory, path=self.path, label="lint"
        )
        if lint.returncode != 0:
            raise ComponentAcceptanceError(
                "component_acceptance.lint_failed",
                f"{self.path} lint exited {lint.returncode}",
            )
        rendered = _run_lint_render_command(
            self.render, cwd=self.source_directory, path=self.path, label="render"
        )
        if rendered.returncode != 0:
            raise ComponentAcceptanceError(
                "component_acceptance.render_failed",
                f"{self.path} render exited {rendered.returncode}",
            )
        stdout = rendered.stdout or b""
        if not stdout:
            raise ComponentAcceptanceError(
                "component_acceptance.render_empty",
                f"{self.path} render produced no snapshot bytes",
            )
        observed = _bytes_identity(stdout)
        if observed != self.snapshot_identity:
            raise ComponentAcceptanceError(
                "component_acceptance.render_drift",
                f"{self.path} render snapshot drifted; expected="
                f"{self.snapshot_identity}; actual={observed}",
            )
        return canonical_identity(
            {
                "schema": "literate-ai/local-lint-render-acceptance@1",
                "oracle_identity": self.identity.uri,
                "lint_status": lint.returncode,
                "lint_stdout_identity": _bytes_identity(lint.stdout or b""),
                "lint_stderr_identity": _bytes_identity(lint.stderr or b""),
                "render_status": rendered.returncode,
                "snapshot_identity": observed,
                "render_stderr_identity": _bytes_identity(rendered.stderr or b""),
            }
        )


def _load_lint_render_command(value: Any, path: Path, label: str) -> LintRenderCommand:
    if not isinstance(value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} {label} must be an object",
        )
    _strict_keys(
        value,
        {
            "arguments",
            "timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        },
        path,
        label,
    )
    return LintRenderCommand(
        _bounded_argv(value.get("arguments"), path, f"{label} arguments"),
        _positive_number(
            value.get("timeout_seconds", 30),
            path,
            f"{label} timeout_seconds",
            maximum=_MAXIMUM_SERVICE_TIMEOUT_SECONDS,
        ),
        _positive_integer(
            value.get("stdout_limit_bytes", 1024 * 1024),
            path,
            f"{label} stdout_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
        _positive_integer(
            value.get("stderr_limit_bytes", 1024 * 1024),
            path,
            f"{label} stderr_limit_bytes",
            maximum=_MAXIMUM_SERVICE_OUTPUT_BYTES,
        ),
    )


def _run_lint_render_command(
    command: LintRenderCommand,
    *,
    cwd: Path,
    path: Path,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    argv = tuple(
        sys.executable if token == PYTHON_PLACEHOLDER else token
        for token in command.arguments
    )
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "PYTHONPATH": str(cwd),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = run_with_tree_kill(
            argv,
            cwd=cwd,
            env=environment,
            timeout=command.timeout_seconds,
            text=False,
        )
    except FileNotFoundError as exc:
        raise ComponentAcceptanceError(
            f"component_acceptance.{label}_failed",
            f"{path} {label} executable was not found",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ComponentAcceptanceError(
            f"component_acceptance.{label}_timeout",
            f"{path} {label} exceeded {command.timeout_seconds} seconds",
        ) from exc
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    if (
        len(stdout) > command.stdout_limit_bytes
        or len(stderr) > command.stderr_limit_bytes
    ):
        raise ComponentAcceptanceError(
            "component_acceptance.output_too_large",
            f"{path} {label} exceeded output limits",
        )
    return completed


def load_lint_render_acceptance(
    path: Path, component_name: str, project_root: Path
) -> LintRenderAcceptance:
    """Load one strictly bounded lint-and-render snapshot contract."""

    document = _read_document(path, expected_schema=LINT_RENDER_SCHEMA)
    _strict_keys(
        document,
        {
            "schema",
            "specification_set_identity",
            "source_root",
            "lint",
            "render",
        },
        path,
        "document",
    )
    bound = document.get("specification_set_identity")
    if not isinstance(bound, str) or not bound:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_unbound",
            f"{path} must bind a specification_set_identity",
        )
    render_value = document.get("render")
    if not isinstance(render_value, dict):
        raise ComponentAcceptanceError(
            "component_acceptance.contract_invalid",
            f"{path} render must be an object",
        )
    snapshot = render_value.get("snapshot_identity")
    render_fields = dict(render_value)
    render_fields.pop("snapshot_identity", None)
    source_root = _relative_source_root(document.get("source_root", "."), path)
    return LintRenderAcceptance(
        component_name,
        bound,
        source_root,
        _resolve_source_directory(project_root, source_root, path),
        path,
        _load_lint_render_command(document.get("lint"), path, "lint"),
        _load_lint_render_command(render_fields, path, "render"),
        _snapshot_identity(snapshot, path, "render snapshot_identity"),
    )


def load_declared_cases(path: Path) -> tuple[str, tuple[DeclaredAcceptanceCase, ...]]:
    """Return the bound interface identity and the declared cases, or fail closed."""

    document = _read_document(path)
    bound = document.get("specification_set_identity")
    if not isinstance(bound, str) or not bound:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_unbound",
            f"{path} must bind an specification_set_identity",
        )
    raw = document.get("cases")
    if not isinstance(raw, list) or not raw:
        raise ComponentAcceptanceError(
            "component_acceptance.oracle_empty",
            f"{path} must declare at least one acceptance case",
        )
    cases: list[DeclaredAcceptanceCase] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid", f"{path} has a malformed case"
            )
        case_id = item.get("case_id")
        arguments = item.get("arguments")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} requires a unique nonempty case_id for every case",
            )
        if not isinstance(arguments, list):
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} case {case_id!r} must declare an arguments array",
            )
        if "expected_result" not in item:
            raise ComponentAcceptanceError(
                "component_acceptance.case_invalid",
                f"{path} case {case_id!r} must declare an expected_result",
            )
        seen.add(case_id)
        cases.append(
            DeclaredAcceptanceCase(case_id, list(arguments), item["expected_result"])
        )
    return bound, tuple(cases)


class FilesystemComponentAcceptanceOracle:
    """Supply a locked Component's declared cases to the Standard lifecycle."""

    def __init__(
        self,
        project_root: Path,
        component_name: str,
        *,
        path: Path | None = None,
    ) -> None:
        self._path = path or oracle_path(Path(project_root), component_name)
        self._component = component_name
        self._bound, self._declared = load_declared_cases(self._path)
        self._cases = tuple(
            LocalIndependentAcceptanceCase.create(
                case.case_id, case.arguments, case.expected_result
            )
            for case in self._declared
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "provider": "filesystem-component-acceptance-oracle@1",
                "component": self._component,
                "specification_set_identity": self._bound,
                "case_identities": [item.identity.uri for item in self._cases],
            }
        )

    def cases(
        self, component_lock: ComponentLock
    ) -> tuple[LocalIndependentAcceptanceCase, ...]:
        """Refuse expectations written against a different specification."""

        observed = _root_specification_identity(component_lock)
        if observed is not None and observed != self._bound:
            raise ComponentAcceptanceError(
                "component_acceptance.interface_changed",
                f"{self._path} was written against {self._bound}, but the locked "
                f"Component now specifies {observed}; review the expected results "
                "against the changed specification and rebind the oracle",
            )
        return self._cases


def _root_specification_identity(component_lock: ComponentLock) -> str | None:
    """Read the locked root's specification-set identity, the oracle's anchor."""

    root = component_lock.root_revision
    for node in component_lock.nodes:
        if node.revision.identity != root:
            continue
        identity = getattr(node.revision, "specification_set_identity", None)
        return identity.uri if identity is not None else None
    return None


def root_component_name(component_lock: ComponentLock) -> str:
    """Return the locked root Component name used for verifier-owned lookup."""

    root = component_lock.root_revision
    for node in component_lock.nodes:
        if node.revision.identity == root:
            return node.revision.definition.coordinate.name
    raise ComponentAcceptanceError(
        "component_acceptance.root_missing",
        "Component lock does not contain its declared root revision",
    )


def root_entrypoint_kind(component_lock: ComponentLock) -> str | None:
    """Read the locked root's one declared entrypoint kind, or ``None`` if absent.

    ``None`` covers non-executable kinds. Importable libraries select their oracle
    by the verifier document's library schema rather than a process entrypoint.
    """

    root = component_lock.root_revision
    for node in component_lock.nodes:
        if node.revision.identity != root:
            continue
        entrypoints = node.revision.definition.entrypoints
        return entrypoints[0].kind if entrypoints else None
    return None


def _declared_oracle_schema(path: Path) -> str | None:
    """Read just the ``schema`` field of an oracle document, without loading it.

    A ``persistent-service`` deployment unit may declare either the plain
    persistent-service HTTP probe or, per ADR-0029, an IPC-surface conformance
    oracle -- both launch via ``--litai-serve`` from the same entrypoint kind, so
    the document's own declared schema selects which oracle to load. Returns
    ``None`` when the file is absent or unreadable, letting the caller fall back to
    its default loader (which then fails closed with its own diagnostic).
    """

    if not path.is_file() or path.stat().st_size > _MAXIMUM_ORACLE_BYTES:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if isinstance(value, dict) and isinstance(value.get("schema"), str):
        return value["schema"]
    return None


def resolve_component_acceptance_oracle(
    project_root: Path, specification: str, component_lock: ComponentLock
) -> (
    FilesystemComponentAcceptanceOracle
    | PersistentServiceAcceptance
    | IpcSurfaceConformanceAcceptance
    | BrowserInteractionAcceptance
    | NativeCliAcceptance
    | LibraryAcceptance
    | LintRenderAcceptance
    | ComponentAcceptanceOracleBundle
    | None
):
    """Load the verifier-owned oracle for one locked Component, or omit it.

    ``portable-application`` and ``persistent-service`` require their documents.
    A ``persistent-service`` deployment unit whose verifier document declares the
    ``ipc-surface-conformance-acceptance@1`` schema loads the ADR-0029 IPC-surface
    conformance oracle instead of the plain HTTP probe: an IPC surface is not a new
    entrypoint kind, so the document's own declared schema selects the oracle.
    A ``web-application`` deployment unit requires a browser-interaction contract
    (ADR-0028): a served frontend is launched via ``--litai-serve`` and driven in
    a real browser, since text-only acceptance false-passes observable frontend
    failures. Importable libraries load their exact harness contract; other
    zero-entrypoint Components load a lint-and-render snapshot when declared or
    remain exempt.
    """

    root_node = next(
        (
            item
            for item in component_lock.nodes
            if item.revision.identity == component_lock.root_revision
        ),
        None,
    )
    if root_node is None:
        raise ComponentAcceptanceError(
            "component_acceptance.root_missing",
            "Component lock does not contain its declared root revision",
        )
    root_entrypoints = tuple(root_node.revision.definition.entrypoints)
    if len(root_entrypoints) > 1:
        name = root_component_name(component_lock)
        bindings: list[ComponentAcceptanceOracleBinding] = []
        for entrypoint in root_entrypoints:
            path = oracle_path(project_root, name, entrypoint.name)
            if entrypoint.kind == PERSISTENT_SERVICE_ENTRYPOINT_KIND:
                oracle: object | None
                if _declared_oracle_schema(path) in (
                    IPC_SURFACE_SCHEMA,
                    IPC_SURFACE_NATIVE_SCHEMA,
                ):
                    oracle = load_ipc_surface_conformance_acceptance(path, name)
                else:
                    oracle = load_persistent_service_acceptance(path, name)
            elif entrypoint.kind == WEB_APPLICATION_ENTRYPOINT_KIND:
                oracle = load_browser_interaction_acceptance(path, name)
            elif entrypoint.kind == PORTABLE_APPLICATION_ENTRYPOINT_KIND:
                oracle = FilesystemComponentAcceptanceOracle(
                    project_root, name, path=path
                )
            elif entrypoint.kind == NATIVE_CLI_ENTRYPOINT_KIND:
                oracle = load_native_cli_acceptance(
                    path,
                    name,
                    entrypoint,
                    specification_set_identity=(
                        root_node.revision.specification_set_identity
                    ),
                    target_identity=root_node.target_flavor_selection.identity,
                )
            elif path.is_file():
                oracle = load_lint_render_acceptance(path, name, project_root)
            else:
                oracle = None
            bindings.append(
                ComponentAcceptanceOracleBinding(
                    entrypoint.name,
                    entrypoint.resolved_deployment_unit,
                    entrypoint.kind,
                    oracle,
                )
            )
        return ComponentAcceptanceOracleBundle(
            tuple(sorted(bindings, key=lambda item: item.deployment_unit))
        )
    entrypoint_kind = root_entrypoint_kind(component_lock)
    if entrypoint_kind == PERSISTENT_SERVICE_ENTRYPOINT_KIND:
        name = root_component_name(component_lock)
        path = oracle_path(project_root, name)
        if _declared_oracle_schema(path) in (
            IPC_SURFACE_SCHEMA,
            IPC_SURFACE_NATIVE_SCHEMA,
        ):
            return load_ipc_surface_conformance_acceptance(path, name)
        return load_persistent_service_acceptance(path, name)
    if entrypoint_kind == WEB_APPLICATION_ENTRYPOINT_KIND:
        name = root_component_name(component_lock)
        return load_browser_interaction_acceptance(
            oracle_path(project_root, name), name
        )
    if entrypoint_kind == PORTABLE_APPLICATION_ENTRYPOINT_KIND:
        name = Path(str(specification).rstrip("/")).name
        return FilesystemComponentAcceptanceOracle(project_root, name)
    if entrypoint_kind == NATIVE_CLI_ENTRYPOINT_KIND:
        name = root_component_name(component_lock)
        entrypoint = root_entrypoints[0]
        return load_native_cli_acceptance(
            oracle_path(project_root, name),
            name,
            entrypoint,
            specification_set_identity=root_node.revision.specification_set_identity,
            target_identity=root_node.target_flavor_selection.identity,
        )
    name = root_component_name(component_lock)
    path = oracle_path(project_root, name)
    if not path.is_file():
        return None
    if _declared_oracle_schema(path) == LIBRARY_SCHEMA:
        return load_library_acceptance(path, name)
    return load_lint_render_acceptance(path, name, project_root)


__all__ = [
    "ACCEPTANCE_DIRECTORY",
    "BROWSER_SCHEMA",
    "INDEPENDENT_ACCEPTANCE_EXEMPT_SCHEMA",
    "IPC_SURFACE_SCHEMA",
    "IPC_SURFACE_NATIVE_SCHEMA",
    "LIBRARY_SCHEMA",
    "LINT_RENDER_SCHEMA",
    "NATIVE_CLI_SCHEMA",
    "ORACLE_SCHEMA",
    "PORTABLE_APPLICATION_ENTRYPOINT_KIND",
    "PYTHON_PLACEHOLDER",
    "SERVICE_SCHEMA",
    "WEB_APPLICATION_ENTRYPOINT_KIND",
    "BrowserInteractionAcceptance",
    "BrowserInteractionStep",
    "BrowserPostcondition",
    "BrowserViewport",
    "ComponentAcceptanceError",
    "ComponentAcceptanceOracleBinding",
    "ComponentAcceptanceOracleBundle",
    "DeclaredAcceptanceCase",
    "DeclaredLibraryAcceptanceCase",
    "FilesystemComponentAcceptanceOracle",
    "IpcSurfaceConformanceAcceptance",
    "LintRenderAcceptance",
    "LintRenderCommand",
    "NativeCliAcceptance",
    "NativeCliCase",
    "NativeCliContentExpectation",
    "NativeCliFilesystemExpectation",
    "NativeCliFixture",
    "LibraryAcceptance",
    "PersistentServiceAcceptance",
    "ServiceHttpProbe",
    "load_browser_interaction_acceptance",
    "load_declared_cases",
    "load_ipc_surface_conformance_acceptance",
    "load_lint_render_acceptance",
    "load_library_acceptance",
    "load_native_cli_acceptance",
    "load_persistent_service_acceptance",
    "oracle_path",
    "resolve_component_acceptance_oracle",
    "root_component_name",
    "root_entrypoint_kind",
]
