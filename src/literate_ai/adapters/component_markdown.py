"""Strict ``component.md`` projection into the Component authoring contract."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts.capabilities import (
    CapabilityConstraint,
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.component_locking import (
    AuthoredProvidedCapability,
    AuthoredRepositorySourceDependency,
    ComponentAssetSelector,
    ComponentAuthoring,
    ComponentContentSelector,
)
from literate_ai.contracts.components import Entrypoint
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.identity import ComponentCoordinate, ContentIdentity
from literate_ai.contracts.library_imports import AuthoredLibraryImport
from literate_ai.contracts.provider_resolution import (
    ProviderCapabilitySet,
    ProviderResolutionDeclaration,
)
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
)

_MAXIMUM_DOCUMENT_BYTES = 256 * 1024
_MAXIMUM_DESCRIPTION_CHARACTERS = 16_384
_MAXIMUM_FRONTMATTER_LINES = 4096
_MAXIMUM_NESTING = 8
_KEY = re.compile(r"^([a-z][a-z0-9_]*):(.*)$")
_INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FIELDS = frozenset(
    {
        "namespace",
        "name",
        "version",
        "display_name",
        "profiles",
        "sample",
        "inheritable",
        "provides",
        "requires",
        "specification_provider",
        "specification_roots",
        "authoring_inputs",
        "workflow_definition",
        "routing_policy",
        "flavor_slots",
        "entrypoints",
        "acceptance_contracts",
        "source_dependencies",
        "assets",
        "provider_resolutions",
        "kind",
        "library_imports",
    }
)
_REQUIRED_FIELDS = _FIELDS - {
    "name",
    "inheritable",
    "specification_provider",
    "specification_roots",
    "assets",
    "provider_resolutions",
    "kind",
    "library_imports",
}


class ComponentMarkdownError(ValueError):
    """A component Markdown document is ambiguous, unsafe, or incomplete."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class _Line:
    indent: int
    content: str
    number: int


def parse_component_markdown(
    path: Path,
    text: str,
    *,
    project_root: Path,
) -> ComponentAuthoring:
    """Parse one strict component document without invoking a general YAML loader."""

    if not isinstance(text, str):
        raise TypeError("component Markdown text must be a string")
    if len(text.encode("utf-8")) > _MAXIMUM_DOCUMENT_BYTES:
        _error("document_limit", "component.md exceeds the maximum byte size")
    component_name = _derived_component_name(path, project_root=project_root)
    frontmatter, prose = _split_document(text)
    raw = _parse_yaml_subset(frontmatter)
    unknown = set(raw) - _FIELDS
    if unknown:
        _error("frontmatter_key_unknown", f"unknown key {sorted(unknown)[0]!r}")
    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        _error("frontmatter_required", f"missing {', '.join(sorted(missing))}")
    explicit_name = raw.get("name")
    if explicit_name is not None and _string(explicit_name, "name") != component_name:
        _error("coordinate_mismatch", "name must match the component.md directory")
    description = prose.strip()
    if not description:
        _error("description_missing", "Markdown prose must describe the Component")
    if len(description) > _MAXIMUM_DESCRIPTION_CHARACTERS:
        _error(
            "description_limit",
            "component.md prose exceeds 16384 characters; move genuinely distinct "
            "named boundaries into explicit specification roots",
        )

    provides = tuple(
        sorted(
            (
                _provided(item, index)
                for index, item in enumerate(_list(raw["provides"], "provides"))
            ),
            key=lambda item: item.name,
        )
    )
    requirements = tuple(
        sorted(
            (
                _requirement(item, index)
                for index, item in enumerate(_list(raw["requires"], "requires"))
            ),
            key=lambda item: item.requirement_id,
        )
    )
    authoring_inputs = tuple(
        sorted(
            (
                _selector(item, f"authoring_inputs[{index}]")
                for index, item in enumerate(
                    _list(raw["authoring_inputs"], "authoring_inputs")
                )
            ),
            key=lambda item: (item.kind, item.uri),
        )
    )
    flavor_slots = tuple(
        sorted(
            (
                _flavor_slot(item, index)
                for index, item in enumerate(_list(raw["flavor_slots"], "flavor_slots"))
            ),
            key=lambda item: item.slot_id,
        )
    )
    entrypoints = tuple(
        sorted(
            (
                _entrypoint(item, index)
                for index, item in enumerate(_list(raw["entrypoints"], "entrypoints"))
            ),
            key=lambda item: item.name,
        )
    )
    acceptance = tuple(
        sorted(
            (
                _selector(
                    item,
                    f"acceptance_contracts[{index}]",
                    fixed_kind="acceptance-contract",
                )
                for index, item in enumerate(
                    _list(raw["acceptance_contracts"], "acceptance_contracts")
                )
            ),
            key=lambda item: (item.kind, item.uri),
        )
    )
    dependencies = tuple(
        sorted(
            (
                _source_dependency(item, index)
                for index, item in enumerate(
                    _list(raw["source_dependencies"], "source_dependencies")
                )
            ),
            key=lambda item: item.dependency_id,
        )
    )
    assets = tuple(
        sorted(
            (
                _asset(item, index)
                for index, item in enumerate(_list(raw.get("assets", []), "assets"))
            ),
            key=lambda item: item.asset_id,
        )
    )
    provider_resolutions = tuple(
        sorted(
            (
                _provider_resolution(item, index)
                for index, item in enumerate(
                    _list(
                        raw.get("provider_resolutions", []),
                        "provider_resolutions",
                    )
                )
            ),
            key=lambda item: item.resolution_id,
        )
    )
    return ComponentAuthoring(
        coordinate=ComponentCoordinate(
            _string(raw["namespace"], "namespace"), component_name
        ),
        version=_string(raw["version"], "version"),
        display_name=_string(raw["display_name"], "display_name"),
        description=description,
        profiles=tuple(sorted(_string_list(raw["profiles"], "profiles"))),
        sample=_boolean(raw["sample"], "sample"),
        inheritable=_boolean(raw.get("inheritable", True), "inheritable"),
        kind=_string(raw["kind"], "kind") if "kind" in raw else "",
        provides=provides,
        requires=requirements,
        specification_provider=_string(
            raw.get("specification_provider", "literate-markdown"),
            "specification_provider",
        ),
        specification_roots=_string_list(
            raw.get("specification_roots", ["component.md"]),
            "specification_roots",
        ),
        authoring_inputs=authoring_inputs,
        workflow_definition=_selector(
            raw["workflow_definition"],
            "workflow_definition",
            fixed_kind="workflow",
        ),
        routing_policy=_selector(
            raw["routing_policy"], "routing_policy", fixed_kind="routing-policy"
        ),
        flavor_slots=flavor_slots,
        entrypoints=entrypoints,
        acceptance_contracts=acceptance,
        source_dependencies=dependencies,
        assets=assets,
        provider_resolutions=provider_resolutions,
        library_imports=tuple(
            sorted(
                (
                    AuthoredLibraryImport.from_dict(item)
                    for item in _list(raw.get("library_imports", []), "library_imports")
                ),
                key=lambda item: (item.language, item.capability),
            )
        ),
    )


def render_component_markdown(
    authoring: ComponentAuthoring,
    path: Path,
    *,
    project_root: Path,
) -> str:
    """Render canonical constrained frontmatter and unchanged Markdown prose."""

    if not isinstance(authoring, ComponentAuthoring):
        raise TypeError("component Markdown rendering requires ComponentAuthoring")
    derived_name = _derived_component_name(path, project_root=project_root)
    if authoring.coordinate.name != derived_name:
        _error(
            "coordinate_mismatch",
            "Component coordinate name must match the component.md directory",
        )
    description = authoring.description
    if not description or description != description.strip() or "\r" in description:
        _error(
            "description_invalid",
            "description must be normalized, non-empty Markdown without outer "
            "whitespace",
        )

    lines = [
        "---",
        f"namespace: {_yaml_string(authoring.coordinate.namespace)}",
        f"version: {_yaml_string(authoring.version)}",
        f"display_name: {_yaml_string(authoring.display_name)}",
    ]
    if authoring.kind:
        lines.append(f"kind: {_yaml_string(authoring.kind)}")
    _append_scalar_list(lines, "profiles", authoring.profiles)
    lines.append(f"sample: {'true' if authoring.sample else 'false'}")
    if not authoring.inheritable:
        lines.append("inheritable: false")
    _append_mapping_list(lines, "provides", authoring.provides, _render_provided)
    _append_mapping_list(lines, "requires", authoring.requires, _render_requirement)
    if not (
        authoring.specification_provider == "literate-markdown"
        and authoring.specification_roots == ("component.md",)
    ):
        lines.append(
            "specification_provider: " + _yaml_string(authoring.specification_provider)
        )
        _append_scalar_list(lines, "specification_roots", authoring.specification_roots)
    _append_mapping_list(
        lines,
        "authoring_inputs",
        authoring.authoring_inputs,
        lambda item: _render_selector(item, include_kind=True),
    )
    _append_nested_mapping(
        lines,
        "workflow_definition",
        _render_selector(authoring.workflow_definition, include_kind=False),
    )
    _append_nested_mapping(
        lines,
        "routing_policy",
        _render_selector(authoring.routing_policy, include_kind=False),
    )
    _append_mapping_list(
        lines, "flavor_slots", authoring.flavor_slots, _render_flavor_slot
    )
    _append_mapping_list(
        lines, "entrypoints", authoring.entrypoints, _render_entrypoint
    )
    _append_mapping_list(
        lines,
        "acceptance_contracts",
        authoring.acceptance_contracts,
        lambda item: _render_selector(item, include_kind=False),
    )
    _append_mapping_list(
        lines,
        "source_dependencies",
        authoring.source_dependencies,
        _render_source_dependency,
    )
    if authoring.assets:
        _append_mapping_list(lines, "assets", authoring.assets, _render_asset)
    if authoring.provider_resolutions:
        _append_mapping_list(
            lines,
            "provider_resolutions",
            authoring.provider_resolutions,
            _render_provider_resolution,
        )
    if authoring.library_imports:
        _append_mapping_list(
            lines, "library_imports", authoring.library_imports, _render_library_import
        )
    return "\n".join((*lines, "---", description, ""))


def _append_scalar_list(lines: list[str], key: str, values: tuple[str, ...]) -> None:
    if not values:
        lines.append(f"{key}: []")
        return
    lines.append(f"{key}:")
    lines.extend(f"  - {_yaml_string(item, sequence_item=True)}" for item in values)


def _append_mapping_list(
    lines: list[str],
    key: str,
    values: tuple[Any, ...],
    render: Callable[[Any], list[str]],
) -> None:
    if not values:
        lines.append(f"{key}: []")
        return
    lines.append(f"{key}:")
    for item in values:
        rendered = render(item)
        if not rendered:
            raise AssertionError(
                "component Markdown mapping renderer returned no fields"
            )
        lines.append(f"  - {rendered[0]}")
        lines.extend(f"    {line}" for line in rendered[1:])


def _append_nested_mapping(lines: list[str], key: str, values: list[str]) -> None:
    lines.append(f"{key}:")
    lines.extend(f"  {line}" for line in values)


def _render_library_import(item: AuthoredLibraryImport) -> list[str]:
    lines = [
        f"{key}: {_yaml_string(getattr(item, key))}"
        for key in ("language", "package", "capability", "module")
    ]
    _append_scalar_list(lines, "symbols", item.symbols)
    return lines


def _render_provided(item: AuthoredProvidedCapability) -> list[str]:
    lines = [
        f"name: {_yaml_string(item.name)}",
        f"version: {_yaml_string(item.version)}",
    ]
    if item.interface is None:
        lines.append("interface: null")
    else:
        lines.append("interface:")
        lines.extend(
            f"  {line}" for line in _render_selector(item.interface, include_kind=False)
        )
    return lines


def _render_requirement(item: CapabilityRequirement) -> list[str]:
    lines = [
        f"requirement_id: {_yaml_string(item.requirement_id)}",
        f"capability: {_yaml_string(item.capability)}",
        f"version_range: {_yaml_string(item.version_range)}",
        f"dependency_kind: {_yaml_string(item.dependency_kind.value)}",
        f"optional: {'true' if item.optional else 'false'}",
    ]
    if not item.constraints:
        lines.append("constraints: []")
        return lines
    lines.append("constraints:")
    for constraint in item.constraints:
        rendered = _render_constraint(constraint)
        lines.append(f"  - {rendered[0]}")
        lines.extend(f"    {line}" for line in rendered[1:])
    return lines


def _render_constraint(item: CapabilityConstraint) -> list[str]:
    lines = [
        f"key: {_yaml_string(item.key)}",
        f"operator: {_yaml_string(item.operator)}",
    ]
    if item.values:
        lines.append("values:")
        lines.extend(
            f"  - {_yaml_string(value, sequence_item=True)}" for value in item.values
        )
    else:  # CapabilityConstraint already forbids this; keep rendering total.
        lines.append("values: []")
    return lines


def _render_selector(
    item: ComponentContentSelector, *, include_kind: bool
) -> list[str]:
    lines = []
    if include_kind:
        lines.append(f"kind: {_yaml_string(item.kind)}")
    lines.append(f"uri: {_yaml_string(item.uri)}")
    if item.pin is not None:
        lines.append(f"pin: {_yaml_string(item.pin.uri)}")
    return lines


def _render_flavor_slot(item: FlavorSlot) -> list[str]:
    lines = [
        f"slot_id: {_yaml_string(item.slot_id)}",
        f"axis: {_yaml_string(item.axis.value)}",
        f"cardinality: {_yaml_string(item.cardinality.value)}",
        f"capability_contract: {_yaml_string(item.capability_contract)}",
    ]
    if item.minimum is not None:
        lines.append(f"minimum: {item.minimum}")
    if item.maximum is not None:
        lines.append(f"maximum: {item.maximum}")
    return lines


def _render_entrypoint(item: Entrypoint) -> list[str]:
    rendered = [
        f"name: {_yaml_string(item.name)}",
        f"kind: {_yaml_string(item.kind)}",
        f"path: {_yaml_string(item.path)}",
    ]
    if item.deployment_unit is not None:
        rendered.append(f"deployment_unit: {_yaml_string(item.deployment_unit)}")
    return rendered


def _render_source_dependency(item: AuthoredRepositorySourceDependency) -> list[str]:
    lines = [
        f"dependency_id: {_yaml_string(item.dependency_id)}",
        f"repository_url: {_yaml_string(item.repository_url)}",
        "revision_selector:",
        f"  kind: {_yaml_string(item.revision_selector.kind.value)}",
    ]
    if item.revision_selector.value is not None:
        lines.append(f"  value: {_yaml_string(item.revision_selector.value)}")
    lines.extend(
        (
            f"dependency_kind: {_yaml_string(item.dependency_kind.value)}",
            f"optional: {'true' if item.optional else 'false'}",
        )
    )
    if item.integration_contract is None:
        lines.append("integration_contract: null")
    else:
        lines.append("integration_contract:")
        lines.extend(
            f"  {line}"
            for line in _render_selector(item.integration_contract, include_kind=False)
        )
    return lines


def _yaml_string(value: str, *, sequence_item: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("component Markdown scalars must be non-empty strings")
    ambiguous = (
        value != value.strip()
        or any(ord(character) < 32 for character in value)
        or "#" in value
        or ": " in value
        or (sequence_item and _KEY.fullmatch(value) is not None)
        or value.startswith(
            ("-", "?", ":", "[", "{", "&", "*", "!", "|", ">", "%", "@", "`", '"', "'")
        )
        or value in {"true", "false", "null", "~", ".nan", ".inf", "-.inf"}
        or _INTEGER.fullmatch(value) is not None
    )
    return json.dumps(value, ensure_ascii=False) if ambiguous else value


def _derived_component_name(path: Path, *, project_root: Path) -> str:
    root = Path(project_root).resolve(strict=True)
    supplied = Path(path)
    candidate = supplied if supplied.is_absolute() else root / supplied
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        _error("path_escape", "component.md must be beneath the project root")
    if candidate.name != "component.md":
        _error(
            "path_invalid", "Component authoring document must be named component.md"
        )
    name = candidate.parent.name
    if not name:
        _error("coordinate_invalid", "component.md has no path-derived Component name")
    return name


def _split_document(text: str) -> tuple[list[str], str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != "---":
        _error("frontmatter_missing", "component.md must begin with ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        _error("frontmatter_unclosed", "component.md frontmatter is not closed")
    frontmatter = lines[1:end]
    if len(frontmatter) > _MAXIMUM_FRONTMATTER_LINES:
        _error("frontmatter_limit", "component.md frontmatter has too many lines")
    return frontmatter, "\n".join(lines[end + 1 :])


def _parse_yaml_subset(lines: list[str]) -> dict[str, Any]:
    tokens: list[_Line] = []
    for number, raw in enumerate(lines, start=2):
        if "\t" in raw:
            _error("yaml_feature", f"tabs are forbidden at line {number}")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            _error(
                "yaml_indentation", f"indentation must use two spaces at line {number}"
            )
        tokens.append(_Line(indent, raw[indent:], number))
    if not tokens:
        _error("frontmatter_required", "component.md frontmatter is empty")
    value, index = _parse_block(tokens, 0, tokens[0].indent, depth=0)
    if index != len(tokens) or not isinstance(value, dict) or tokens[0].indent != 0:
        _error("yaml_structure", "frontmatter must be one top-level mapping")
    return value


def _parse_block(
    tokens: list[_Line], index: int, indent: int, *, depth: int
) -> tuple[Any, int]:
    if depth > _MAXIMUM_NESTING:
        _error("yaml_nesting", "frontmatter nesting is too deep")
    if tokens[index].content == "-" or tokens[index].content.startswith("- "):
        return _parse_sequence(tokens, index, indent, depth=depth)
    return _parse_mapping(tokens, index, indent, depth=depth)


def _parse_mapping(
    tokens: list[_Line], index: int, indent: int, *, depth: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(tokens) and tokens[index].indent == indent:
        token = tokens[index]
        if token.content == "-" or token.content.startswith("- "):
            break
        key, raw = _mapping_line(token)
        if key == "<<":
            _error(
                "yaml_feature", f"YAML merge keys are forbidden at line {token.number}"
            )
        if key in result:
            _error("yaml_duplicate", f"duplicate key {key!r} at line {token.number}")
        index += 1
        if raw:
            result[key] = _scalar(raw, token.number)
            continue
        if index >= len(tokens) or tokens[index].indent <= indent:
            result[key] = None
            continue
        if tokens[index].indent != indent + 2:
            _error(
                "yaml_indentation",
                f"unexpected indentation at line {tokens[index].number}",
            )
        result[key], index = _parse_block(tokens, index, indent + 2, depth=depth + 1)
    return result, index


def _parse_sequence(
    tokens: list[_Line], index: int, indent: int, *, depth: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(tokens) and tokens[index].indent == indent:
        token = tokens[index]
        if token.content == "-":
            raw = ""
        elif token.content.startswith("- "):
            raw = token.content[2:].strip()
        else:
            break
        index += 1
        if not raw:
            if index >= len(tokens) or tokens[index].indent != indent + 2:
                _error("yaml_structure", f"empty list item at line {token.number}")
            item, index = _parse_block(tokens, index, indent + 2, depth=depth + 1)
        elif _KEY.fullmatch(raw):
            key, first = _mapping_line(_Line(indent + 2, raw, token.number))
            item = {}
            if first:
                item[key] = _scalar(first, token.number)
            elif index < len(tokens) and tokens[index].indent > indent + 2:
                if tokens[index].indent != indent + 4:
                    _error(
                        "yaml_indentation",
                        f"unexpected indentation at line {tokens[index].number}",
                    )
                item[key], index = _parse_block(
                    tokens, index, indent + 4, depth=depth + 2
                )
            else:
                item[key] = None
            if index < len(tokens) and tokens[index].indent == indent + 2:
                rest, index = _parse_mapping(tokens, index, indent + 2, depth=depth + 1)
                overlap = set(item) & set(rest)
                if overlap:
                    _error("yaml_duplicate", f"duplicate key {sorted(overlap)[0]!r}")
                item.update(rest)
        else:
            item = _scalar(raw, token.number)
            if index < len(tokens) and tokens[index].indent > indent:
                _error(
                    "yaml_structure",
                    "scalar list item cannot have children at line "
                    f"{tokens[index].number}",
                )
        result.append(item)
        if len(result) > 256:
            _error("yaml_collection_limit", "frontmatter list exceeds 256 items")
    return result, index


def _mapping_line(token: _Line) -> tuple[str, str]:
    match = _KEY.fullmatch(token.content)
    if match is None:
        _error("yaml_structure", f"invalid mapping at line {token.number}")
    key, raw = match.groups()
    if key.startswith("_"):
        _error("yaml_feature", f"reserved key at line {token.number}")
    return key, raw.strip()


def _scalar(raw: str, line: int) -> Any:
    value = raw.strip()
    if not value:
        _error("yaml_scalar", f"empty scalar at line {line}")
    if value.startswith(("&", "*", "!", "|", ">", "%", "?")):
        _error(
            "yaml_feature",
            f"YAML aliases, tags, and directives are forbidden at line {line}",
        )
    if value.startswith(("[", "{")):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ComponentMarkdownError(
                "yaml_feature",
                f"flow values must use strict JSON syntax at line {line}",
            ) from exc
        return decoded
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ComponentMarkdownError(
                "yaml_scalar", f"invalid quoted string at line {line}"
            ) from exc
        if not isinstance(decoded, str):
            _error("yaml_scalar", f"quoted scalar must be a string at line {line}")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            _error("yaml_scalar", f"invalid single-quoted string at line {line}")
        return value[1:-1].replace("''", "'")
    if " #" in value:
        _error("yaml_feature", f"inline comments are forbidden at line {line}")
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value in {"~", ".nan", ".inf", "-.inf"}:
        _error("yaml_feature", f"implicit YAML values are forbidden at line {line}")
    if _INTEGER.fullmatch(value):
        return int(value)
    return value


def _provided(value: Any, index: int) -> AuthoredProvidedCapability:
    path = f"provides[{index}]"
    data = _record(value, path, required={"name", "version"}, optional={"interface"})
    interface = data.get("interface")
    return AuthoredProvidedCapability(
        _string(data["name"], f"{path}.name"),
        _string(data["version"], f"{path}.version"),
        (
            None
            if interface is None
            else _selector(
                interface, f"{path}.interface", fixed_kind="public-interface-contract"
            )
        ),
    )


def _requirement(value: Any, index: int) -> CapabilityRequirement:
    path = f"requires[{index}]"
    data = _record(
        value,
        path,
        required={"requirement_id", "capability", "version_range", "dependency_kind"},
        optional={"optional", "constraints"},
    )
    constraints = tuple(
        _constraint(item, f"{path}.constraints[{constraint_index}]")
        for constraint_index, item in enumerate(
            _list(data.get("constraints", []), f"{path}.constraints")
        )
    )
    try:
        dependency_kind = DependencyKind(
            _string(data["dependency_kind"], f"{path}.dependency_kind")
        )
    except ValueError:
        _error("value_invalid", f"{path}.dependency_kind is unsupported")
    return CapabilityRequirement(
        _string(data["requirement_id"], f"{path}.requirement_id"),
        _string(data["capability"], f"{path}.capability"),
        _string(data["version_range"], f"{path}.version_range"),
        dependency_kind,
        _boolean(data.get("optional", False), f"{path}.optional"),
        constraints,
    )


def _constraint(value: Any, path: str) -> CapabilityConstraint:
    data = _record(value, path, required={"key", "operator", "values"})
    return CapabilityConstraint(
        _string(data["key"], f"{path}.key"),
        _string(data["operator"], f"{path}.operator"),
        _string_list(data["values"], f"{path}.values"),
    )


def _selector(
    value: Any, path: str, *, fixed_kind: str | None = None
) -> ComponentContentSelector:
    if isinstance(value, str):
        if fixed_kind is None:
            _error("value_invalid", f"{path} must declare kind and uri")
        return ComponentContentSelector(fixed_kind, value, None)
    required = {"uri"} if fixed_kind is not None else {"kind", "uri"}
    data = _record(value, path, required=required, optional={"kind", "pin"})
    kind = fixed_kind or _string(data["kind"], f"{path}.kind")
    explicit_kind = data.get("kind")
    if explicit_kind is not None and _string(explicit_kind, f"{path}.kind") != kind:
        _error("value_invalid", f"{path}.kind must be {kind!r}")
    pin = data.get("pin")
    if pin is not None:
        try:
            parsed_pin = ContentIdentity.parse_uri(_string(pin, f"{path}.pin"))
        except (TypeError, ValueError) as exc:
            raise ComponentMarkdownError(
                "value_invalid", f"{path}.pin must be a content identity URI"
            ) from exc
    else:
        parsed_pin = None
    return ComponentContentSelector(
        kind, _string(data["uri"], f"{path}.uri"), parsed_pin
    )


def _flavor_slot(value: Any, index: int) -> FlavorSlot:
    path = f"flavor_slots[{index}]"
    data = _record(
        value,
        path,
        required={"slot_id", "axis", "cardinality", "capability_contract"},
        optional={"minimum", "maximum"},
    )
    try:
        axis = FlavorAxis(_string(data["axis"], f"{path}.axis"))
        cardinality = FlavorCardinality(
            _string(data["cardinality"], f"{path}.cardinality")
        )
    except ValueError:
        _error("value_invalid", f"{path} has an unsupported axis or cardinality")
    return FlavorSlot(
        _string(data["slot_id"], f"{path}.slot_id"),
        axis,
        cardinality,
        _string(data["capability_contract"], f"{path}.capability_contract"),
        _optional_integer(data.get("minimum"), f"{path}.minimum"),
        _optional_integer(data.get("maximum"), f"{path}.maximum"),
    )


def _entrypoint(value: Any, index: int) -> Entrypoint:
    path = f"entrypoints[{index}]"
    data = _record(
        value,
        path,
        required={"name", "kind", "path"},
        optional={"deployment_unit"},
    )
    return Entrypoint(
        _string(data["name"], f"{path}.name"),
        _string(data["kind"], f"{path}.kind"),
        _string(data["path"], f"{path}.path"),
        (
            None
            if data.get("deployment_unit") is None
            else _string(data["deployment_unit"], f"{path}.deployment_unit")
        ),
    )


def _source_dependency(value: Any, index: int) -> AuthoredRepositorySourceDependency:
    path = f"source_dependencies[{index}]"
    data = _record(
        value,
        path,
        required={
            "dependency_id",
            "repository_url",
            "revision_selector",
            "dependency_kind",
        },
        optional={"optional", "integration_contract"},
    )
    selector_data = _record(
        data["revision_selector"],
        f"{path}.revision_selector",
        required={"kind"},
        optional={"value"},
    )
    try:
        revision_kind = RepositoryRevisionKind(
            _string(selector_data["kind"], f"{path}.revision_selector.kind")
        )
        dependency_kind = DependencyKind(
            _string(data["dependency_kind"], f"{path}.dependency_kind")
        )
    except ValueError:
        _error("value_invalid", f"{path} has an unsupported dependency selector")
    selector_value = selector_data.get("value")
    integration = data.get("integration_contract")
    return AuthoredRepositorySourceDependency(
        _string(data["dependency_id"], f"{path}.dependency_id"),
        _string(data["repository_url"], f"{path}.repository_url"),
        RepositoryRevisionSelector(
            revision_kind,
            None
            if selector_value is None
            else _string(selector_value, f"{path}.revision_selector.value"),
        ),
        dependency_kind,
        _boolean(data.get("optional", False), f"{path}.optional"),
        (
            None
            if integration is None
            else _selector(
                integration,
                f"{path}.integration_contract",
                fixed_kind="integration-contract",
            )
        ),
    )


def _asset(value: Any, index: int) -> ComponentAssetSelector:
    path = f"assets[{index}]"
    data = _record(
        value,
        path,
        required={"asset_id", "source", "path", "role"},
        optional={"media_type", "pin"},
    )
    pin = data.get("pin")
    try:
        parsed_pin = (
            None
            if pin is None
            else ContentIdentity.parse_uri(_string(pin, f"{path}.pin"))
        )
        return ComponentAssetSelector(
            asset_id=_string(data["asset_id"], f"{path}.asset_id"),
            source=_string(data["source"], f"{path}.source"),
            path=_string(data["path"], f"{path}.path"),
            role=_string(data["role"], f"{path}.role"),
            media_type=_string(
                data.get("media_type", "application/octet-stream"),
                f"{path}.media_type",
            ),
            pin=parsed_pin,
        )
    except (TypeError, ValueError) as exc:
        raise ComponentMarkdownError("value_invalid", f"{path} is invalid") from exc


def _provider_resolution(value: Any, index: int) -> ProviderResolutionDeclaration:
    path = f"provider_resolutions[{index}]"
    data = _record(
        value,
        path,
        required={
            "resolution_id",
            "preferred_provider",
            "required_capabilities",
            "fallback_order",
            "capability_sets",
        },
    )
    capability_sets = tuple(
        sorted(
            (
                _provider_capability_set(item, f"{path}.capability_sets[{item_index}]")
                for item_index, item in enumerate(
                    _list(data["capability_sets"], f"{path}.capability_sets")
                )
            ),
            key=lambda item: item.provider_id,
        )
    )
    return ProviderResolutionDeclaration(
        resolution_id=_string(data["resolution_id"], f"{path}.resolution_id"),
        preferred_provider=_string(
            data["preferred_provider"], f"{path}.preferred_provider"
        ),
        required_capabilities=tuple(
            sorted(
                _string_list(
                    data["required_capabilities"], f"{path}.required_capabilities"
                )
            )
        ),
        fallback_order=_string_list(data["fallback_order"], f"{path}.fallback_order"),
        capability_sets=capability_sets,
    )


def _provider_capability_set(value: Any, path: str) -> ProviderCapabilitySet:
    data = _record(value, path, required={"provider_id", "capabilities"})
    return ProviderCapabilitySet(
        provider_id=_string(data["provider_id"], f"{path}.provider_id"),
        capabilities=tuple(
            sorted(_string_list(data["capabilities"], f"{path}.capabilities"))
        ),
    )


def _render_asset(item: ComponentAssetSelector) -> list[str]:
    lines = [
        f"asset_id: {_yaml_string(item.asset_id)}",
        f"source: {_yaml_string(item.source)}",
        f"path: {_yaml_string(item.path)}",
        f"role: {_yaml_string(item.role)}",
    ]
    if item.media_type != "application/octet-stream":
        lines.append(f"media_type: {_yaml_string(item.media_type)}")
    if item.pin is not None:
        lines.append(f"pin: {_yaml_string(item.pin.uri)}")
    return lines


def _render_provider_resolution(item: ProviderResolutionDeclaration) -> list[str]:
    lines = [
        f"resolution_id: {_yaml_string(item.resolution_id)}",
        f"preferred_provider: {_yaml_string(item.preferred_provider)}",
        "required_capabilities:",
        *(
            f"  - {_yaml_string(value, sequence_item=True)}"
            for value in item.required_capabilities
        ),
        "fallback_order:",
    ]
    if item.fallback_order:
        lines.extend(
            f"  - {_yaml_string(value, sequence_item=True)}"
            for value in item.fallback_order
        )
    else:
        lines[-1] = "fallback_order: []"
    lines.append("capability_sets:")
    for capability_set in item.capability_sets:
        lines.append(f"  - provider_id: {_yaml_string(capability_set.provider_id)}")
        lines.append("    capabilities:")
        lines.extend(
            f"      - {_yaml_string(value, sequence_item=True)}"
            for value in capability_set.capabilities
        )
    return lines


def _record(
    value: Any,
    path: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _error("value_invalid", f"{path} must be a mapping")
    allowed = required | (optional or set())
    unknown = set(value) - allowed
    missing = required - value.keys()
    if unknown:
        _error(
            "frontmatter_key_unknown", f"{path} has unknown key {sorted(unknown)[0]!r}"
        )
    if missing:
        _error("frontmatter_required", f"{path} is missing {sorted(missing)[0]!r}")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _error("value_invalid", f"{path} must be a list")
    if len(value) > 256:
        _error("yaml_collection_limit", f"{path} exceeds 256 items")
    return value


def _string_list(value: Any, path: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{path}[{index}]")
        for index, item in enumerate(_list(value, path))
    )


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _error("value_invalid", f"{path} must be a non-empty string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _error("value_invalid", f"{path} must be true or false")
    return value


def _optional_integer(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        _error("value_invalid", f"{path} must be an integer or null")
    return value


def _error(code: str, message: str) -> None:
    raise ComponentMarkdownError(f"component_markdown.{code}", message)


__all__ = [
    "ComponentMarkdownError",
    "parse_component_markdown",
    "render_component_markdown",
]
