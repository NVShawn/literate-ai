"""Deterministic MAC repository-contract projection from resolved CycloneDX."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from literate_ai.contracts import (
    CYCLONEDX_SCHEMA_URI,
    CYCLONEDX_SPEC_VERSION,
    canonical_json_bytes,
)

MAC_REPOSITORY_CONTRACT_SCHEMA = "mac.repository_contract.v1"
MAC_PROJECT_CONTRACT_PATH = ".mac/project.yaml"
MAC_PROJECT_CONTRACT_EVIDENCE_PATH = ".mac/project.contract.json"
MAC_PROJECT_CONTRACT_PROJECTION_SCHEMA = "literate-ai/mac-project-contract@1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORMS = frozenset({"darwin", "linux", "windows", "wsl2"})
_COMMAND_SCOPES = frozenset({"build", "deployment", "packaging", "test", "toolchain"})
_PATH_PROPERTIES = (
    "literate-ai:launcher-path",
    "literate-ai:macho-resolved-path",
    "literate-ai:macho-path",
    "literate-ai:elf-path",
    "literate-ai:pe-path",
)
_STRICT_VALIDATOR = JsonStrictValidator(SchemaVersion.V1_7)


class MacProjectContractError(ValueError):
    """A resolved BOM cannot be projected into an honest MAC contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def project_mac_repository_contract(
    resolved_bom: bytes,
    *,
    project: str,
    platforms: Sequence[str],
    bootstrap_command: str = "litai build",
    test_command: str = "litai test",
    bootstrap_creates: Sequence[str] = (),
    evidence_required: Sequence[str] = (
        "repo.head_sha",
        "repo.pushed",
        "repo.dirty",
        "repo.files_changed",
        "tests",
    ),
) -> tuple[bytes, bytes]:
    """Return MAC YAML plus identity sidecar from one exact post-build BOM."""

    document = _resolved_bom(resolved_bom)
    project_name = _identifier(project, "project")
    platform_values = _unique_strings(platforms, "platforms")
    if set(platform_values) - _PLATFORMS:
        raise MacProjectContractError(
            "mac-contract.platform-invalid",
            "platforms must use darwin, linux, windows, or wsl2",
        )
    commands = _required_commands(document)
    creates = _relative_paths(bootstrap_creates)
    evidence = _unique_strings(evidence_required, "evidence.required")
    bootstrap = _nonempty(bootstrap_command, "bootstrap.command")
    test = _nonempty(test_command, "test.command")
    contract = {
        "schema": MAC_REPOSITORY_CONTRACT_SCHEMA,
        "project": project_name,
        "platforms": list(platform_values),
        "toolchain": {"required_commands": list(commands)},
        "bootstrap": {"command": bootstrap, "creates": list(creates)},
        "test": {"command": test},
        "evidence": {"required": list(evidence)},
    }
    source_bom_identity, resolved_graph_identity = _bom_bindings(document)
    resolved_bom_identity = "sha256:" + hashlib.sha256(resolved_bom).hexdigest()
    yaml_bytes = _yaml(contract).encode("utf-8")
    projection = {
        "schema": MAC_PROJECT_CONTRACT_PROJECTION_SCHEMA,
        "contract_path": MAC_PROJECT_CONTRACT_PATH,
        "contract_identity": "sha256:" + hashlib.sha256(yaml_bytes).hexdigest(),
        "resolved_bom_identity": resolved_bom_identity,
        "resolved_graph_identity": resolved_graph_identity,
        "source_bom_identity": source_bom_identity,
        "required_commands": list(commands),
    }
    return yaml_bytes, canonical_json_bytes(projection)


def write_mac_repository_contract(
    resolved_bom_path: Path,
    *,
    project_root: Path,
    project: str,
    platforms: Sequence[str],
    bootstrap_command: str = "litai build",
    test_command: str = "litai test",
    bootstrap_creates: Sequence[str] = (),
) -> dict[str, Any]:
    """Write `.mac/project.yaml` and its exact BOM-binding sidecar atomically."""

    root = project_root.resolve(strict=True)
    if not root.is_dir():
        raise MacProjectContractError(
            "mac-contract.project-root-invalid", "project root must be a directory"
        )
    bom = resolved_bom_path.resolve(strict=True)
    if not bom.is_file() or bom.is_symlink():
        raise MacProjectContractError(
            "mac-contract.bom-path-invalid", "resolved BOM must be a regular file"
        )
    yaml_bytes, projection_bytes = project_mac_repository_contract(
        bom.read_bytes(),
        project=project,
        platforms=platforms,
        bootstrap_command=bootstrap_command,
        test_command=test_command,
        bootstrap_creates=bootstrap_creates,
    )
    contract_path = root / MAC_PROJECT_CONTRACT_PATH
    evidence_path = root / MAC_PROJECT_CONTRACT_EVIDENCE_PATH
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(contract_path, yaml_bytes)
    _atomic_write(evidence_path, projection_bytes)
    return json.loads(projection_bytes)


def _resolved_bom(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MacProjectContractError(
            "mac-contract.bom-invalid", "resolved BOM is not JSON"
        ) from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != content:
        raise MacProjectContractError(
            "mac-contract.bom-noncanonical",
            "resolved BOM must be canonical minified JSON",
        )
    if (
        document.get("bomFormat") != "CycloneDX"
        or document.get("$schema") != CYCLONEDX_SCHEMA_URI
        or document.get("specVersion") != CYCLONEDX_SPEC_VERSION
        or document.get("version") != 1
    ):
        raise MacProjectContractError(
            "mac-contract.bom-profile-invalid",
            f"resolved BOM must be CycloneDX {CYCLONEDX_SPEC_VERSION}",
        )
    try:
        schema_errors = tuple(
            _STRICT_VALIDATOR.validate_str(content.decode("utf-8"), all_errors=True)
            or ()
        )
    except Exception as exc:
        raise MacProjectContractError(
            "mac-contract.bom-schema-invalid",
            "resolved BOM could not be checked against CycloneDX 1.7",
        ) from exc
    if schema_errors:
        raise MacProjectContractError(
            "mac-contract.bom-schema-invalid",
            "resolved BOM does not conform to the strict CycloneDX 1.7 schema",
        )
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("lifecycles") != [
        {"phase": "post-build"}
    ]:
        raise MacProjectContractError(
            "mac-contract.bom-lifecycle-invalid",
            "MAC projection requires a post-build CycloneDX BOM",
        )
    components = document.get("components")
    if not isinstance(components, list):
        raise MacProjectContractError(
            "mac-contract.bom-components-invalid", "resolved BOM components are missing"
        )
    return document


def _properties(component: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    raw = component.get("properties", [])
    if not isinstance(raw, list):
        raise MacProjectContractError(
            "mac-contract.bom-properties-invalid", "component properties must be a list"
        )
    for item in raw:
        if not isinstance(item, dict):
            raise MacProjectContractError(
                "mac-contract.bom-properties-invalid", "component property is invalid"
            )
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise MacProjectContractError(
                "mac-contract.bom-properties-invalid", "component property is invalid"
            )
        result.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in result.items()}


def _required_commands(document: Mapping[str, Any]) -> tuple[str, ...]:
    commands = {"git", "litai"}
    for raw in document["components"]:
        if not isinstance(raw, dict):
            raise MacProjectContractError(
                "mac-contract.bom-components-invalid", "component entry is invalid"
            )
        properties = _properties(raw)
        scopes = set(properties.get("literate-ai:dependency-scope", ()))
        if not scopes & _COMMAND_SCOPES:
            continue
        paths = tuple(
            value
            for property_name in _PATH_PROPERTIES
            for value in properties.get(property_name, ())
        )
        if not paths:
            continue
        for path in paths:
            name = PurePath(path.replace("\\", "/")).name
            if name.lower().endswith(".exe"):
                name = name[:-4]
            if name and _IDENTIFIER.fullmatch(name):
                commands.add(name)
    return tuple(sorted(commands))


def _bom_bindings(document: Mapping[str, Any]) -> tuple[str, str]:
    metadata = document.get("metadata")
    assert isinstance(metadata, dict)
    properties = _properties(metadata)
    bindings = []
    for name in (
        "literate-ai:source-bom-identity",
        "literate-ai:resolved-graph-identity",
    ):
        values = properties.get(name, ())
        if len(values) != 1 or not _SHA256.fullmatch(values[0]):
            raise MacProjectContractError(
                "mac-contract.bom-binding-incomplete",
                f"resolved BOM requires exactly one valid {name}",
            )
        bindings.append(values[0])
    return bindings[0], bindings[1]


def _identifier(value: str, field: str) -> str:
    result = _nonempty(value, field)
    if not _IDENTIFIER.fullmatch(result):
        raise MacProjectContractError(
            "mac-contract.identifier-invalid", f"{field} is not a portable identifier"
        )
    return result


def _nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MacProjectContractError(
            "mac-contract.value-invalid", f"{field} must be a non-empty string"
        )
    return value.strip()


def _unique_strings(values: Sequence[str], field: str) -> tuple[str, ...]:
    result = tuple(_nonempty(value, field) for value in values)
    if not result or len(set(result)) != len(result):
        raise MacProjectContractError(
            "mac-contract.list-invalid", f"{field} must be a non-empty unique list"
        )
    return result


def _relative_paths(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_nonempty(value, "bootstrap.creates") for value in values)
    for value in result:
        path = PurePath(value)
        if path.is_absolute() or ".." in path.parts:
            raise MacProjectContractError(
                "mac-contract.path-invalid",
                "bootstrap.creates entries must stay inside the project",
            )
    return result


def _yaml(contract: Mapping[str, Any]) -> str:
    lines = [
        f"schema: {contract['schema']}",
        f"project: {contract['project']}",
        "platforms:",
        *(f"  - {item}" for item in contract["platforms"]),
        "toolchain:",
        "  required_commands:",
        *(f"    - {item}" for item in contract["toolchain"]["required_commands"]),
        "bootstrap:",
        "  command: " + json.dumps(contract["bootstrap"]["command"]),
        "  creates:",
        *(f"    - {json.dumps(item)}" for item in contract["bootstrap"]["creates"]),
        "test:",
        "  command: " + json.dumps(contract["test"]["command"]),
        "evidence:",
        "  required:",
        *(f"    - {item}" for item in contract["evidence"]["required"]),
    ]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "MAC_PROJECT_CONTRACT_EVIDENCE_PATH",
    "MAC_PROJECT_CONTRACT_PATH",
    "MAC_PROJECT_CONTRACT_PROJECTION_SCHEMA",
    "MAC_REPOSITORY_CONTRACT_SCHEMA",
    "MacProjectContractError",
    "project_mac_repository_contract",
    "write_mac_repository_contract",
]
