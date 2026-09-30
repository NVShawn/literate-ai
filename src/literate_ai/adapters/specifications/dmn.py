"""Narrow, execution-free DMN decision-table specification provider."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
)

DMN_NAMESPACES = frozenset(
    {
        "https://www.omg.org/spec/DMN/20191111/MODEL/",
        "https://www.omg.org/spec/DMN/20230324/MODEL/",
    }
)
XINCLUDE_NAMESPACE = "http://www.w3.org/2001/XInclude"
DEFAULT_MAXIMUM_ARTIFACTS = 256
DEFAULT_MAXIMUM_ARTIFACT_BYTES = 2 * 1024 * 1024
DEFAULT_MAXIMUM_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MAXIMUM_PATH_BYTES = 512

_UNSAFE_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_INTERVAL = re.compile(r"^\[([^][]+?)\.\.([^][]+?)\]$")
_INPUT_EXPRESSION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_DMN_ELEMENTS = frozenset(
    {
        "definitions",
        "decision",
        "decisionTable",
        "informationRequirement",
        "input",
        "inputData",
        "inputExpression",
        "output",
        "requiredInput",
        "rule",
        "inputEntry",
        "outputEntry",
        "text",
        "variable",
    }
)


class DmnError(RuntimeError):
    """A stable, fail-closed DMN adapter diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Rule:
    rule_id: str
    inputs: tuple[str, ...]
    intervals: tuple[tuple[Decimal, Decimal] | None, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Decision:
    decision_id: str
    name: str
    input_labels: tuple[str, ...]
    output_labels: tuple[str, ...]
    rules: tuple[_Rule, ...]


@dataclass(frozen=True, slots=True)
class LoadedDmn:
    specification_set: SpecificationSet
    contents: tuple[tuple[str, bytes], ...]
    context_document: tuple[str, bytes] | None = None

    def require_unchanged(self, root: Path) -> None:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise DmnError("dmn.root_unavailable", "DMN root is unavailable") from exc
        for path, expected in self.contents:
            target = resolved.joinpath(*PurePosixPath(path).parts)
            try:
                safe = (
                    not target.is_symlink()
                    and target.is_file()
                    and target.resolve(strict=True).is_relative_to(resolved)
                )
            except OSError:
                safe = False
            if not safe:
                raise DmnError(
                    "dmn.artifact_missing", f"Artifact changed or disappeared: {path}"
                )
            try:
                with target.open("rb") as stream:
                    current = stream.read(len(expected) + 1)
            except OSError as exc:
                raise DmnError(
                    "dmn.artifact_unavailable",
                    f"Artifact could not be re-read: {path}",
                ) from exc
            if current != expected:
                raise DmnError(
                    "dmn.artifact_changed",
                    f"Artifact changed during operation: {path}",
                )


class DmnProvider:
    """Load the deliberately narrow v1 DMN UNIQUE decision-table profile."""

    provider_id = "specification-provider:dmn@1"

    def __init__(
        self,
        *,
        maximum_artifacts: int = DEFAULT_MAXIMUM_ARTIFACTS,
        maximum_artifact_bytes: int = DEFAULT_MAXIMUM_ARTIFACT_BYTES,
        maximum_total_bytes: int = DEFAULT_MAXIMUM_TOTAL_BYTES,
        maximum_path_bytes: int = DEFAULT_MAXIMUM_PATH_BYTES,
    ) -> None:
        limits = (
            maximum_artifacts,
            maximum_artifact_bytes,
            maximum_total_bytes,
            maximum_path_bytes,
        )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1
            for item in limits
        ):
            raise ValueError("DMN snapshot limits must be positive integers")
        self.maximum_artifacts = maximum_artifacts
        self.maximum_artifact_bytes = maximum_artifact_bytes
        self.maximum_total_bytes = maximum_total_bytes
        self.maximum_path_bytes = maximum_path_bytes

    def load(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        baseline_id: str | None = None,
        active_change_id: str | None = None,
    ) -> LoadedDmn:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise DmnError("dmn.root_invalid", "DMN root is not a directory") from exc
        if not resolved.is_dir():
            raise DmnError("dmn.root_invalid", "DMN root is not a directory")

        declared_paths = tuple(paths)
        if not declared_paths:
            raise DmnError("dmn.artifacts_empty", "No DMN artifacts were declared")
        if len(declared_paths) > self.maximum_artifacts:
            raise DmnError("dmn.artifact_limit", "Too many DMN artifacts were declared")

        artifacts: list[SpecificationArtifact] = []
        requirements: list[SpecificationRequirement] = []
        contents: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        total_bytes = 0
        for declared in declared_paths:
            relative = self._safe_path(declared)
            normalized = relative.as_posix()
            if normalized in seen:
                raise DmnError(
                    "dmn.artifact_duplicate", f"Artifact declared twice: {normalized}"
                )
            seen.add(normalized)
            target = resolved.joinpath(*relative.parts)
            try:
                safe = (
                    not target.is_symlink()
                    and target.is_file()
                    and target.resolve(strict=True).is_relative_to(resolved)
                )
            except OSError:
                safe = False
            if not safe:
                raise DmnError(
                    "dmn.artifact_missing", f"Artifact is missing: {normalized}"
                )

            remaining_total = self.maximum_total_bytes - total_bytes
            read_limit = min(self.maximum_artifact_bytes, remaining_total)
            try:
                with target.open("rb") as stream:
                    content = stream.read(read_limit + 1)
            except OSError as exc:
                raise DmnError(
                    "dmn.artifact_unavailable",
                    f"DMN artifact could not be read: {normalized}",
                ) from exc
            if len(content) > self.maximum_artifact_bytes:
                raise DmnError(
                    "dmn.artifact_size_limit",
                    f"DMN artifact exceeds its size limit: {normalized}",
                )
            if len(content) > remaining_total:
                raise DmnError(
                    "dmn.total_size_limit", "DMN artifacts exceed the total size limit"
                )
            total_bytes += len(content)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DmnError(
                    "dmn.artifact_not_utf8", f"Artifact is not UTF-8: {normalized}"
                ) from exc

            decisions = _parse_document(normalized, text)
            digest = hashlib.sha256(content).hexdigest()
            artifacts.append(
                SpecificationArtifact(
                    uri=normalized,
                    identity=ContentIdentity(HashAlgorithm.SHA256, digest),
                )
            )
            requirements.append(_artifact_requirement(normalized, digest, decisions))
            contents.append((normalized, content))

        return LoadedDmn(
            specification_set=SpecificationSet(
                provider_kind="dmn",
                provider_version="1",
                artifacts=tuple(artifacts),
                requirements=tuple(requirements),
                baseline_id=baseline_id,
                active_change_id=active_change_id,
            ),
            contents=tuple(contents),
        )

    def _safe_path(self, value: str) -> PurePosixPath:
        if not isinstance(value, str):
            raise DmnError("dmn.path_invalid", f"Unsafe DMN artifact path: {value!r}")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or str(path) != value
            or "\\" in value
            or len(value.encode("utf-8")) > self.maximum_path_bytes
            or not (value.endswith(".dmn") or value.endswith(".dmn.xml"))
        ):
            raise DmnError("dmn.path_invalid", f"Unsafe DMN artifact path: {value!r}")
        return path


def _parse_document(path: str, text: str) -> tuple[_Decision, ...]:
    if _UNSAFE_XML.search(text):
        raise DmnError(
            "dmn.xml_unsafe", f"DTD and ENTITY declarations are forbidden: {path}"
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DmnError("dmn.xml_invalid", f"Invalid DMN XML: {path}") from exc
    for element in root.iter():
        namespace, local = _split_tag(element.tag)
        if namespace == XINCLUDE_NAMESPACE and local in {"include", "fallback"}:
            raise DmnError("dmn.xml_unsafe", f"XInclude is forbidden: {path}")

    namespace, local = _split_tag(root.tag)
    if local != "definitions" or namespace not in DMN_NAMESPACES:
        raise DmnError(
            "dmn.namespace_unsupported", f"Unsupported DMN namespace: {path}"
        )
    expression_language = root.get("expressionLanguage")
    if expression_language not in {
        None,
        "https://www.omg.org/spec/DMN/20191111/FEEL/",
        "https://www.omg.org/spec/DMN/20230324/FEEL/",
    }:
        raise DmnError(
            "dmn.expression_language_unsupported",
            f"Narrow DMN v1 requires FEEL expressions: {path}",
        )
    for element in root.iter():
        element_namespace, element_local = _split_tag(element.tag)
        if element_namespace != namespace or element_local not in _DMN_ELEMENTS:
            raise DmnError(
                "dmn.element_unsupported",
                f"Narrow DMN v1 does not support <{element_local}>: {path}",
            )

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    _validate_dmn_structure(root, path)

    decision_elements = root.findall(tag("decision"))
    if not decision_elements:
        raise DmnError("dmn.decisions_empty", f"DMN artifact has no decisions: {path}")

    decisions: list[_Decision] = []
    decision_ids: set[str] = set()
    rule_ids: set[str] = set()
    for decision_element in decision_elements:
        decision_id = _required_id(decision_element, "decision", path)
        if decision_id in decision_ids:
            raise DmnError(
                "dmn.decision_id_duplicate",
                f"Duplicate decision ID {decision_id!r}: {path}",
            )
        decision_ids.add(decision_id)
        tables = decision_element.findall(tag("decisionTable"))
        if len(tables) != 1:
            raise DmnError(
                "dmn.decision_table_count",
                f"Decision {decision_id!r} must contain exactly one "
                f"decisionTable: {path}",
            )
        decisions.append(
            _parse_table(
                path,
                tag,
                decision_id,
                decision_element.get("name", "").strip() or decision_id,
                tables[0],
                rule_ids,
            )
        )
    return tuple(decisions)


def _validate_dmn_structure(root: ET.Element, path: str) -> None:
    allowed_children = {
        "definitions": {"inputData", "decision"},
        "inputData": {"variable"},
        "decision": {"variable", "informationRequirement", "decisionTable"},
        "informationRequirement": {"requiredInput"},
        "decisionTable": {"input", "output", "rule"},
        "input": {"inputExpression"},
        "inputExpression": {"text"},
        "rule": {"inputEntry", "outputEntry"},
        "inputEntry": {"text"},
        "outputEntry": {"text"},
        "output": set(),
        "requiredInput": set(),
        "text": set(),
        "variable": set(),
    }
    for parent in root.iter():
        _namespace, parent_local = _split_tag(parent.tag)
        if "expressionLanguage" in parent.attrib and parent is not root:
            raise DmnError(
                "dmn.expression_language_unsupported",
                f"Narrow DMN v1 does not permit expression-language overrides: {path}",
            )
        allowed_attributes = {
            "definitions": {"id", "name", "namespace", "expressionLanguage"},
            "inputData": {"id", "name"},
            "variable": {"id", "name", "typeRef"},
            "decision": {"id", "name"},
            "informationRequirement": set(),
            "requiredInput": {"href"},
            "decisionTable": {"id", "hitPolicy"},
            "input": {"id", "label"},
            "inputExpression": {"id", "typeRef"},
            "output": {"id", "label", "name", "typeRef"},
            "rule": {"id"},
            "inputEntry": {"id"},
            "outputEntry": {"id"},
            "text": set(),
        }[parent_local]
        unknown_attributes = set(parent.attrib) - allowed_attributes
        if unknown_attributes:
            raise DmnError(
                "dmn.attribute_unsupported",
                f"Narrow DMN v1 does not support {parent_local} attribute(s): "
                + ", ".join(sorted(unknown_attributes)),
            )
        for child in parent:
            _child_namespace, child_local = _split_tag(child.tag)
            if child_local not in allowed_children[parent_local]:
                raise DmnError(
                    "dmn.structure_unsupported",
                    f"Narrow DMN v1 does not permit <{child_local}> under "
                    f"<{parent_local}>: {path}",
                )


def _parse_table(
    path: str,
    tag: Callable[[str], str],
    decision_id: str,
    decision_name: str,
    table: ET.Element,
    rule_ids: set[str],
) -> _Decision:
    if table.get("hitPolicy", "UNIQUE") != "UNIQUE":
        raise DmnError(
            "dmn.hit_policy_unsupported",
            f"Decision {decision_id!r} must use UNIQUE hit policy: {path}",
        )

    input_labels: list[str] = []
    for index, input_element in enumerate(table.findall(tag("input")), 1):
        expressions = input_element.findall(tag("inputExpression"))
        if len(expressions) != 1 or expressions[0].get("typeRef") != "number":
            raise DmnError(
                "dmn.input_type_unsupported",
                f"Decision {decision_id!r} input {index} must have "
                f"typeRef='number': {path}",
            )
        expression_text = expressions[0].findall(tag("text"))
        if (
            len(expression_text) != 1
            or expression_text[0].text is None
            or not expression_text[0].text.strip()
        ):
            raise DmnError(
                "dmn.input_expression_invalid",
                f"Decision {decision_id!r} input {index} requires expression text: "
                f"{path}",
            )
        expression = expression_text[0].text.strip()
        if _INPUT_EXPRESSION.fullmatch(expression) is None:
            raise DmnError(
                "dmn.input_expression_unsupported",
                f"Decision {decision_id!r} input {index} has unsupported "
                f"expression: {path}",
            )
        input_labels.append(expression)
    if not input_labels:
        raise DmnError(
            "dmn.inputs_empty", f"Decision {decision_id!r} has no inputs: {path}"
        )

    output_labels: list[str] = []
    output_types: list[str] = []
    for index, output_element in enumerate(table.findall(tag("output")), 1):
        output_type = output_element.get("typeRef", "")
        if output_type not in {"string", "number", "boolean"}:
            raise DmnError(
                "dmn.output_type_unsupported",
                f"Decision {decision_id!r} output {index} has unsupported type: {path}",
            )
        output_types.append(output_type)
        output_labels.append(
            output_element.get("label", "").strip()
            or output_element.get("name", "").strip()
            or f"output {index}"
        )
    if not output_labels:
        raise DmnError(
            "dmn.outputs_empty", f"Decision {decision_id!r} has no outputs: {path}"
        )

    rules: list[_Rule] = []
    for rule_element in table.findall(tag("rule")):
        rule_id = _required_id(rule_element, "rule", path)
        if rule_id in rule_ids:
            raise DmnError(
                "dmn.rule_id_duplicate", f"Duplicate rule ID {rule_id!r}: {path}"
            )
        rule_ids.add(rule_id)
        inputs = tuple(
            _entry_text(entry, tag, rule_id, "input", path)
            for entry in rule_element.findall(tag("inputEntry"))
        )
        outputs = tuple(
            _entry_text(entry, tag, rule_id, "output", path)
            for entry in rule_element.findall(tag("outputEntry"))
        )
        if len(inputs) != len(input_labels) or len(outputs) != len(output_labels):
            raise DmnError(
                "dmn.rule_arity",
                f"Rule {rule_id!r} does not match decision-table arity: {path}",
            )
        intervals = tuple(_parse_interval(value, rule_id, path) for value in inputs)
        for value, output_type in zip(outputs, output_types, strict=True):
            _validate_output(value, output_type, rule_id, path)
        rules.append(_Rule(rule_id, inputs, intervals, outputs))
    if not rules:
        raise DmnError(
            "dmn.rules_empty", f"Decision {decision_id!r} has no rules: {path}"
        )

    for index, first in enumerate(rules):
        for second in rules[index + 1 :]:
            if all(
                _overlap(left, right)
                for left, right in zip(first.intervals, second.intervals, strict=True)
            ):
                raise DmnError(
                    "dmn.unique_overlap",
                    f"UNIQUE rules {first.rule_id!r} and {second.rule_id!r} overlap "
                    f"in decision {decision_id!r}: {path}",
                )
    return _Decision(
        decision_id,
        decision_name,
        tuple(input_labels),
        tuple(output_labels),
        tuple(rules),
    )


def _entry_text(
    entry: ET.Element,
    tag: Callable[[str], str],
    rule_id: str,
    kind: str,
    path: str,
) -> str:
    texts = entry.findall(tag("text"))
    if len(texts) != 1 or texts[0].text is None or not texts[0].text.strip():
        raise DmnError(
            "dmn.rule_entry_invalid",
            f"Rule {rule_id!r} has an invalid {kind} entry: {path}",
        )
    return texts[0].text.strip()


def _parse_interval(
    value: str, rule_id: str, path: str
) -> tuple[Decimal, Decimal] | None:
    if value == "-":
        return None
    match = _INTERVAL.fullmatch(value)
    if match is None:
        raise DmnError(
            "dmn.feel_unsupported",
            f"Rule {rule_id!r} uses unsupported FEEL input {value!r}: {path}",
        )
    try:
        lower = Decimal(match.group(1).strip())
        upper = Decimal(match.group(2).strip())
    except InvalidOperation as exc:
        raise DmnError(
            "dmn.feel_unsupported",
            f"Rule {rule_id!r} uses unsupported FEEL input {value!r}: {path}",
        ) from exc
    if not lower.is_finite() or not upper.is_finite() or lower > upper:
        raise DmnError(
            "dmn.feel_interval_invalid",
            f"Rule {rule_id!r} has an invalid numeric interval {value!r}: {path}",
        )
    return lower, upper


def _validate_output(value: str, output_type: str, rule_id: str, path: str) -> None:
    valid = False
    if output_type == "string":
        try:
            valid = isinstance(json.loads(value), str)
        except (json.JSONDecodeError, UnicodeDecodeError):
            valid = False
    elif output_type == "boolean":
        valid = value in {"true", "false"}
    else:
        try:
            valid = Decimal(value).is_finite()
        except InvalidOperation:
            valid = False
    if not valid:
        raise DmnError(
            "dmn.output_literal_invalid",
            f"Rule {rule_id!r} has invalid {output_type} output {value!r}: {path}",
        )


def _overlap(
    first: tuple[Decimal, Decimal] | None,
    second: tuple[Decimal, Decimal] | None,
) -> bool:
    if first is None or second is None:
        return True
    return first[0] <= second[1] and second[0] <= first[1]


def _required_id(element: ET.Element, kind: str, path: str) -> str:
    value = element.get("id", "").strip()
    if not value:
        raise DmnError("dmn.id_missing", f"DMN {kind} requires a non-empty ID: {path}")
    return value


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _artifact_requirement(
    path: str, digest: str, decisions: tuple[_Decision, ...]
) -> SpecificationRequirement:
    artifact_key = hashlib.sha256(f"{path}\0{digest}".encode()).hexdigest()
    prefix = f"dmn.{artifact_key}"
    scenarios: list[SpecificationScenario] = []
    for decision in decisions:
        for rule in decision.rules:
            inputs = ", ".join(
                f"{label} matches {value}"
                for label, value in zip(decision.input_labels, rule.inputs, strict=True)
            )
            outputs = ", ".join(
                f"{label} = {value}"
                for label, value in zip(
                    decision.output_labels, rule.outputs, strict=True
                )
            )
            scenarios.append(
                SpecificationScenario(
                    scenario_id=(
                        f"{prefix}."
                        + hashlib.sha256(
                            f"{decision.decision_id}\0{rule.rule_id}".encode()
                        ).hexdigest()
                    ),
                    title=(f"{decision.name} rule {rule.rule_id}"),
                    given=(f"the UNIQUE decision {decision.decision_id} is evaluated",),
                    when=(inputs,),
                    then=(outputs,),
                )
            )
    return SpecificationRequirement(
        requirement_id=prefix,
        title=f"DMN artifact {path}",
        statement=(
            f"The DMN artifact {path} SHALL implement its validated UNIQUE "
            "decision-table rules exactly."
        ),
        scenarios=tuple(scenarios),
    )


__all__ = ["DMN_NAMESPACES", "DmnError", "DmnProvider", "LoadedDmn"]
