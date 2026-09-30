"""Bounded SCXML 1.0 chart and trace-sidecar specification provider."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
)

SCXML_NAMESPACE = "http://www.w3.org/2005/07/scxml"
TRACE_SCHEMA = "litai-scxml-trace-sidecar/v1"
DEFAULT_MAXIMUM_ARTIFACT_BYTES = 2 * 1024 * 1024
DEFAULT_MAXIMUM_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_MAXIMUM_PATH_BYTES = 512
DEFAULT_MAXIMUM_STATES = 1024
DEFAULT_MAXIMUM_TRANSITIONS = 4096
DEFAULT_MAXIMUM_TRACES = 256
DEFAULT_MAXIMUM_STEPS = 4096

_UNSAFE_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_STATE_TAGS = frozenset({"state", "parallel", "final", "history"})
_SUPPORTED_TAGS = _STATE_TAGS | frozenset({"scxml", "initial", "transition"})


class ScxmlError(RuntimeError):
    """A stable, coded failure at the bounded SCXML provider boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LoadedScxml:
    specification_set: SpecificationSet
    contents: tuple[tuple[str, bytes], ...]
    context_document: tuple[str, bytes] | None = None

    def require_unchanged(self, root: Path) -> None:
        resolved = _root(root)
        for path, expected in self.contents:
            target = resolved.joinpath(*PurePosixPath(path).parts)
            if target.is_symlink() or not target.is_file():
                raise ScxmlError(
                    "scxml.artifact_missing", f"Artifact changed or disappeared: {path}"
                )
            try:
                actual = target.resolve(strict=True)
                if not actual.is_relative_to(resolved):
                    raise ScxmlError(
                        "scxml.path_escape", f"Artifact escapes root: {path}"
                    )
                with target.open("rb") as stream:
                    current = stream.read(len(expected) + 1)
            except ScxmlError:
                raise
            except OSError as exc:
                raise ScxmlError(
                    "scxml.artifact_unavailable",
                    f"Artifact could not be re-read: {path}",
                ) from exc
            if current != expected:
                raise ScxmlError(
                    "scxml.artifact_changed",
                    f"Artifact changed during operation: {path}",
                )


@dataclass(frozen=True, slots=True)
class _Transition:
    source: str
    event: str | None
    targets: tuple[str, ...]
    conditional: bool
    order: int


@dataclass(frozen=True, slots=True)
class _Node:
    node_id: str
    kind: str
    parent: str | None
    children: tuple[str, ...]
    initial: tuple[str, ...]
    history_type: str | None
    history_default: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Chart:
    nodes: dict[str, _Node]
    transitions: tuple[_Transition, ...]
    root_initial: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TraceStep:
    event: str
    expected: tuple[str, ...]
    via_history: str | None
    auto: bool


class ScxmlProvider:
    """Strict provider for one explicit SCXML chart and its explicit traces."""

    provider_id = "specification-provider:scxml@1"

    def __init__(
        self,
        *,
        maximum_artifact_bytes: int = DEFAULT_MAXIMUM_ARTIFACT_BYTES,
        maximum_total_bytes: int = DEFAULT_MAXIMUM_TOTAL_BYTES,
        maximum_path_bytes: int = DEFAULT_MAXIMUM_PATH_BYTES,
        maximum_states: int = DEFAULT_MAXIMUM_STATES,
        maximum_transitions: int = DEFAULT_MAXIMUM_TRANSITIONS,
        maximum_traces: int = DEFAULT_MAXIMUM_TRACES,
        maximum_steps: int = DEFAULT_MAXIMUM_STEPS,
    ) -> None:
        limits = (
            maximum_artifact_bytes,
            maximum_total_bytes,
            maximum_path_bytes,
            maximum_states,
            maximum_transitions,
            maximum_traces,
            maximum_steps,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in limits
        ):
            raise ValueError("SCXML provider limits must be positive integers")
        self.maximum_artifact_bytes = maximum_artifact_bytes
        self.maximum_total_bytes = maximum_total_bytes
        self.maximum_path_bytes = maximum_path_bytes
        self.maximum_states = maximum_states
        self.maximum_transitions = maximum_transitions
        self.maximum_traces = maximum_traces
        self.maximum_steps = maximum_steps

    def load(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        baseline_id: str | None = None,
        active_change_id: str | None = None,
    ) -> LoadedScxml:
        resolved = _root(root)
        declared = tuple(paths)
        chart_paths = tuple(path for path in declared if path.endswith(".scxml"))
        if len(chart_paths) != 1 or any(
            not (path.endswith(".scxml") or _is_trace_path(path)) for path in declared
        ):
            raise ScxmlError(
                "scxml.artifacts_invalid",
                "Declare exactly one .scxml chart and zero or more "
                ".trace.json sidecars",
            )
        contents = self._snapshot(resolved, declared)
        by_path = dict(contents)
        chart_path = chart_paths[0]
        chart = _parse_chart(
            by_path[chart_path],
            maximum_states=self.maximum_states,
            maximum_transitions=self.maximum_transitions,
        )
        _validate_reachability(chart)

        sidecar_paths = tuple(path for path in declared if _is_trace_path(path))
        traces: dict[str, tuple[_TraceStep, ...]] = {}
        traces_by_path: dict[str, dict[str, tuple[_TraceStep, ...]]] = {}
        total_steps = 0
        for path in sidecar_paths:
            parsed = _parse_sidecar(
                by_path[path],
                path=path,
                chart_basename=PurePosixPath(chart_path).name,
                chart=chart,
            )
            path_traces: dict[str, tuple[_TraceStep, ...]] = {}
            for trace_id, steps in parsed:
                if trace_id in traces:
                    raise ScxmlError(
                        "scxml.trace_duplicate", f"Trace ID is not unique: {trace_id}"
                    )
                traces[trace_id] = steps
                path_traces[trace_id] = steps
                total_steps += len(steps)
                if len(traces) > self.maximum_traces:
                    raise ScxmlError("scxml.trace_limit", "Too many SCXML traces")
                if total_steps > self.maximum_steps:
                    raise ScxmlError("scxml.step_limit", "Too many SCXML trace steps")
            traces_by_path[path] = path_traces
        if traces:
            _require_replay_subset(chart)
            for trace_id in sorted(traces):
                _replay(chart, trace_id, traces[trace_id])

        artifacts = tuple(
            SpecificationArtifact(uri=path, identity=_identity(content))
            for path, content in contents
        )
        requirements = tuple(
            _artifact_requirement(
                path,
                is_chart=path == chart_path,
                traces=traces_by_path.get(path, {}),
            )
            for path, _content in contents
        )
        return LoadedScxml(
            specification_set=SpecificationSet(
                provider_kind="scxml",
                provider_version="1",
                artifacts=artifacts,
                requirements=requirements,
                baseline_id=baseline_id,
                active_change_id=active_change_id,
            ),
            contents=contents,
        )

    def _snapshot(
        self, root: Path, declared: tuple[str, ...]
    ) -> tuple[tuple[str, bytes], ...]:
        if not declared:
            raise ScxmlError(
                "scxml.artifacts_empty", "No SCXML artifacts were declared"
            )
        if len(declared) != len(set(declared)):
            raise ScxmlError(
                "scxml.artifact_duplicate", "An artifact was declared twice"
            )
        result: list[tuple[str, bytes]] = []
        total = 0
        for value in declared:
            relative = _safe_relative(value, self.maximum_path_bytes)
            target = root.joinpath(*relative.parts)
            if target.is_symlink() or not target.is_file():
                raise ScxmlError(
                    "scxml.artifact_missing", f"Artifact is missing: {value}"
                )
            try:
                if not target.resolve(strict=True).is_relative_to(root):
                    raise ScxmlError(
                        "scxml.path_escape", f"Artifact escapes root: {value}"
                    )
                remaining = self.maximum_total_bytes - total
                with target.open("rb") as stream:
                    content = stream.read(
                        min(self.maximum_artifact_bytes, remaining) + 1
                    )
            except ScxmlError:
                raise
            except OSError as exc:
                raise ScxmlError(
                    "scxml.artifact_unavailable", f"Artifact could not be read: {value}"
                ) from exc
            if len(content) > self.maximum_artifact_bytes:
                raise ScxmlError(
                    "scxml.artifact_size_limit", f"Artifact is too large: {value}"
                )
            if len(content) > remaining:
                raise ScxmlError(
                    "scxml.total_size_limit", "SCXML artifacts are too large"
                )
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ScxmlError(
                    "scxml.artifact_not_utf8", f"Artifact is not UTF-8: {value}"
                ) from exc
            total += len(content)
            result.append((value, content))
        return tuple(result)


def _root(root: Path) -> Path:
    if root.is_symlink():
        raise ScxmlError("scxml.root_invalid", "SCXML root must be a regular directory")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ScxmlError("scxml.root_invalid", "SCXML root is unavailable") from exc
    if not resolved.is_dir():
        raise ScxmlError("scxml.root_invalid", "SCXML root must be a regular directory")
    return resolved


def _safe_relative(value: str, maximum: int) -> PurePosixPath:
    if not isinstance(value, str):
        raise ScxmlError("scxml.path_invalid", f"Unsafe artifact path: {value!r}")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
        or "\\" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ScxmlError("scxml.path_invalid", f"Unsafe artifact path: {value!r}")
    return path


def _is_trace_path(path: str) -> bool:
    return path.endswith(".json") and ".trace" in PurePosixPath(path).name


def _identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _parse_chart(
    content: bytes, *, maximum_states: int, maximum_transitions: int
) -> _Chart:
    if _UNSAFE_XML.search(content):
        raise ScxmlError(
            "scxml.xml_unsafe", "DTD and ENTITY declarations are forbidden"
        )
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ScxmlError(
            "scxml.xml_invalid", "SCXML chart is not well-formed XML"
        ) from exc
    prefix = f"{{{SCXML_NAMESPACE}}}"
    if root.tag != prefix + "scxml":
        raise ScxmlError(
            "scxml.namespace_invalid", "Chart must use the W3C SCXML namespace"
        )
    if root.get("version") != "1.0":
        raise ScxmlError(
            "scxml.version_invalid", "Chart must declare SCXML version 1.0"
        )
    for element in root.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith(prefix):
            raise ScxmlError(
                "scxml.xml_foreign", "Foreign XML and XInclude are forbidden"
            )
        if _local(element) not in _SUPPORTED_TAGS:
            raise ScxmlError(
                "scxml.element_unsupported",
                f"Narrow SCXML v1 does not support <{_local(element)}>",
            )
        local = _local(element)
        allowed_attributes = {
            "scxml": {"version", "initial", "name"},
            "state": {"id", "initial"},
            "parallel": {"id"},
            "final": {"id"},
            "history": {"id", "type"},
            "initial": set(),
            "transition": {"event", "target", "cond", "type"},
        }[local]
        unknown_attributes = set(element.attrib) - allowed_attributes
        if unknown_attributes:
            raise ScxmlError(
                "scxml.attribute_unsupported",
                f"Narrow SCXML v1 does not support {local} attribute(s): "
                + ", ".join(sorted(unknown_attributes)),
            )
        if local == "transition" and element.get("type") not in {None, "external"}:
            raise ScxmlError(
                "scxml.transition_type_unsupported",
                "Narrow SCXML v1 supports only external transitions",
            )
    _validate_scxml_structure(root)

    elements: dict[str, ET.Element] = {}
    parents: dict[str, str | None] = {}

    def collect(container: ET.Element, parent: str | None) -> None:
        for child in container:
            kind = _local(child)
            if kind not in _STATE_TAGS:
                continue
            node_id = child.get("id")
            if (
                not node_id
                or node_id.strip() != node_id
                or any(character.isspace() for character in node_id)
            ):
                raise ScxmlError(
                    "scxml.id_invalid", f"{kind} requires a nonempty XML ID"
                )
            if node_id in elements:
                raise ScxmlError(
                    "scxml.id_duplicate", f"State ID is not unique: {node_id}"
                )
            elements[node_id] = child
            parents[node_id] = parent
            if len(elements) > maximum_states:
                raise ScxmlError("scxml.state_limit", "SCXML chart has too many states")
            if kind != "history":
                collect(child, node_id)

    collect(root, None)
    if not elements:
        raise ScxmlError("scxml.states_empty", "SCXML chart contains no states")

    def direct_children(container: ET.Element) -> tuple[str, ...]:
        return tuple(
            child.get("id", "")
            for child in container
            if _local(child) in {"state", "parallel", "final"}
        )

    def initial(container: ET.Element, owner: str) -> tuple[str, ...]:
        attribute = container.get("initial")
        explicit = [child for child in container if _local(child) == "initial"]
        if attribute is not None and explicit:
            raise ScxmlError(
                "scxml.initial_ambiguous", f"Initial state is ambiguous: {owner}"
            )
        if len(explicit) > 1:
            raise ScxmlError(
                "scxml.initial_ambiguous", f"Multiple initial elements: {owner}"
            )
        if attribute is not None:
            targets = tuple(attribute.split())
        elif explicit:
            transitions = [
                child for child in explicit[0] if _local(child) == "transition"
            ]
            if (
                len(transitions) != 1
                or transitions[0].get("cond") is not None
                or transitions[0].get("event") is not None
            ):
                raise ScxmlError(
                    "scxml.initial_invalid", f"Invalid initial transition: {owner}"
                )
            targets = tuple((transitions[0].get("target") or "").split())
        else:
            targets = ()
        children = direct_children(container)
        if children and _local(container) != "parallel" and not targets:
            raise ScxmlError(
                "scxml.initial_missing", f"Compound state requires initial: {owner}"
            )
        if any(target not in children for target in targets):
            raise ScxmlError(
                "scxml.initial_target_invalid",
                f"Initial target is not a child: {owner}",
            )
        if len(targets) > 1:
            raise ScxmlError(
                "scxml.initial_invalid",
                f"Narrow compound initial must have one target: {owner}",
            )
        return targets

    nodes: dict[str, _Node] = {}
    for node_id, element in elements.items():
        kind = _local(element)
        children = direct_children(element)
        history_default: tuple[str, ...] = ()
        history_type: str | None = None
        node_initial: tuple[str, ...] = ()
        if kind == "history":
            history_type = element.get("type", "shallow")
            transitions = [child for child in element if _local(child) == "transition"]
            if len(transitions) != 1:
                raise ScxmlError(
                    "scxml.history_invalid",
                    f"History requires exactly one default transition: {node_id}",
                )
            history_default = tuple((transitions[0].get("target") or "").split())
        elif kind in {"state", "parallel"}:
            node_initial = initial(element, node_id)
        nodes[node_id] = _Node(
            node_id,
            kind,
            parents[node_id],
            children,
            node_initial,
            history_type,
            history_default,
        )
    root_initial = initial(root, "<scxml>")

    transitions: list[_Transition] = []
    for source, element in elements.items():
        if _local(element) == "history":
            continue
        for child in element:
            if _local(child) != "transition":
                continue
            targets = tuple((child.get("target") or "").split())
            if not targets:
                raise ScxmlError(
                    "scxml.transition_target_missing",
                    f"Transition from {source} has no target",
                )
            transitions.append(
                _Transition(
                    source,
                    child.get("event"),
                    targets,
                    child.get("cond") is not None,
                    len(transitions),
                )
            )
            if len(transitions) > maximum_transitions:
                raise ScxmlError(
                    "scxml.transition_limit", "SCXML chart has too many transitions"
                )
    all_targets = [target for item in transitions for target in item.targets]
    all_targets.extend(
        target for node in nodes.values() for target in node.history_default
    )
    unknown = next((target for target in all_targets if target not in nodes), None)
    if unknown is not None:
        raise ScxmlError(
            "scxml.transition_target_invalid", f"Unknown transition target: {unknown}"
        )
    for transition in transitions:
        _validate_target_configuration(nodes, transition.targets)
    for node in nodes.values():
        if node.kind == "history" and node.parent is None:
            raise ScxmlError(
                "scxml.history_invalid", f"History requires a parent: {node.node_id}"
            )
        if any(
            target != node.parent and not _descendant(nodes, target, node.parent)
            for target in node.history_default
        ):
            raise ScxmlError(
                "scxml.history_target_invalid",
                f"History default escapes parent: {node.node_id}",
            )
    return _Chart(nodes, tuple(transitions), root_initial)


def _validate_scxml_structure(root: ET.Element) -> None:
    allowed_children = {
        "scxml": {"initial", "state", "parallel", "final"},
        "state": {"initial", "transition", "state", "parallel", "final", "history"},
        "parallel": {"transition", "state", "parallel", "final", "history"},
        "final": set(),
        "history": {"transition"},
        "initial": {"transition"},
        "transition": set(),
    }
    for parent in root.iter():
        parent_local = _local(parent)
        for child in parent:
            child_local = _local(child)
            if child_local not in allowed_children[parent_local]:
                raise ScxmlError(
                    "scxml.structure_unsupported",
                    f"Narrow SCXML v1 does not permit <{child_local}> under "
                    f"<{parent_local}>",
                )


def _descendant(nodes: dict[str, _Node], node_id: str, ancestor: str | None) -> bool:
    current: str | None = node_id
    while current is not None:
        if current == ancestor:
            return True
        current = nodes[current].parent
    return False


def _validate_target_configuration(
    nodes: dict[str, _Node], targets: tuple[str, ...]
) -> None:
    if len(targets) < 2:
        return
    for index, first in enumerate(targets):
        for second in targets[index + 1 :]:
            if _descendant(nodes, first, second) or _descendant(nodes, second, first):
                raise ScxmlError(
                    "scxml.transition_targets_invalid",
                    "Transition targets cannot contain ancestors and descendants",
                )
            if not _parallel_lineage(nodes, first).intersection(
                _parallel_lineage(nodes, second)
            ):
                raise ScxmlError(
                    "scxml.transition_targets_invalid",
                    "Multiple transition targets require orthogonal parallel regions",
                )


def _parallel_lineage(nodes: dict[str, _Node], node_id: str) -> set[str]:
    result: set[str] = set()
    current = nodes[node_id].parent
    while current is not None:
        if nodes[current].kind == "parallel":
            result.add(current)
        current = nodes[current].parent
    return result


def _entry(chart: _Chart, target: str) -> set[str]:
    node = chart.nodes[target]
    if node.kind == "history":
        entered = {target}
        for default in node.history_default:
            entered.update(_entry(chart, default))
        return entered
    entered = {target}
    if node.kind == "parallel":
        for child in node.children:
            entered.update(_entry(chart, child))
    elif node.children:
        entered.update(_entry(chart, node.initial[0]))
    return entered


def _validate_reachability(chart: _Chart) -> None:
    reachable: set[str] = set()

    def mark(target: str) -> set[str]:
        entered = _entry(chart, target)
        for node_id in tuple(entered):
            parent = chart.nodes[node_id].parent
            while parent is not None:
                entered.add(parent)
                parent = chart.nodes[parent].parent
        return entered

    for target in chart.root_initial:
        reachable.update(mark(target))
    changed = True
    while changed:
        changed = False
        for transition in chart.transitions:
            if transition.source not in reachable:
                continue
            for target in transition.targets:
                entered = mark(target)
                if not entered.issubset(reachable):
                    reachable.update(entered)
                    changed = True
        for node in chart.nodes.values():
            if (
                node.kind == "history"
                and node.parent in reachable
                and node.history_default
            ):
                if node.node_id not in reachable:
                    reachable.add(node.node_id)
                    changed = True
    missing = sorted(set(chart.nodes) - reachable)
    if missing:
        raise ScxmlError(
            "scxml.state_unreachable", f"Unreachable state IDs: {', '.join(missing)}"
        )


def _parse_sidecar(
    content: bytes, *, path: str, chart_basename: str, chart: _Chart
) -> tuple[tuple[str, tuple[_TraceStep, ...]], ...]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScxmlError(
            "scxml.trace_json_invalid", f"Trace sidecar is invalid JSON: {path}"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"$schema", "scxml", "traces"}:
        raise ScxmlError(
            "scxml.trace_schema_invalid", f"Trace sidecar has invalid fields: {path}"
        )
    if value["$schema"] != TRACE_SCHEMA:
        raise ScxmlError(
            "scxml.trace_schema_invalid", f"Unsupported trace schema: {path}"
        )
    if value["scxml"] != chart_basename:
        raise ScxmlError(
            "scxml.trace_chart_mismatch", f"Trace sidecar names the wrong chart: {path}"
        )
    raw_traces = value["traces"]
    if not isinstance(raw_traces, list) or not raw_traces:
        raise ScxmlError("scxml.traces_empty", f"Trace sidecar requires traces: {path}")
    result = []
    seen: set[str] = set()
    for raw_trace in raw_traces:
        if (
            not isinstance(raw_trace, dict)
            or not {"id", "steps"} <= set(raw_trace)
            or set(raw_trace) - {"id", "description", "steps"}
        ):
            raise ScxmlError("scxml.trace_invalid", f"Trace has invalid fields: {path}")
        if "description" in raw_trace and not isinstance(raw_trace["description"], str):
            raise ScxmlError(
                "scxml.trace_invalid", f"Trace description is invalid: {path}"
            )
        trace_id = raw_trace["id"]
        if (
            not isinstance(trace_id, str)
            or not trace_id
            or trace_id.strip() != trace_id
        ):
            raise ScxmlError(
                "scxml.trace_invalid", f"Trace requires a nonempty ID: {path}"
            )
        if trace_id in seen:
            raise ScxmlError(
                "scxml.trace_duplicate", f"Trace ID is not unique: {trace_id}"
            )
        seen.add(trace_id)
        raw_steps = raw_trace["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ScxmlError("scxml.steps_empty", f"Trace requires steps: {trace_id}")
        steps = []
        for raw_step in raw_steps:
            allowed = {
                "event",
                "expect_active",
                "auto",
                "expect_regions",
                "via_history",
                "note",
            }
            if (
                not isinstance(raw_step, dict)
                or not {"event", "expect_active"} <= set(raw_step)
                or set(raw_step) - allowed
            ):
                raise ScxmlError(
                    "scxml.step_invalid", f"Trace step has invalid fields: {trace_id}"
                )
            event = raw_step["event"]
            expected = raw_step["expect_active"]
            if (
                not isinstance(event, str)
                or not event
                or event.strip() != event
                or any(character.isspace() for character in event)
                or "*" in event
            ):
                raise ScxmlError(
                    "scxml.event_invalid", f"Trace event must be literal: {trace_id}"
                )
            if (
                not isinstance(expected, list)
                or not expected
                or any(not isinstance(item, str) or not item for item in expected)
                or len(expected) != len(set(expected))
            ):
                raise ScxmlError(
                    "scxml.expect_active_invalid",
                    f"expect_active is invalid: {trace_id}",
                )
            for state_id in expected:
                node = chart.nodes.get(state_id)
                if node is None or node.kind not in {"state", "final"} or node.children:
                    raise ScxmlError(
                        "scxml.expect_active_reference_invalid",
                        f"expect_active references a non-leaf ID: {state_id}",
                    )
            auto = raw_step.get("auto", False)
            if not isinstance(auto, bool):
                raise ScxmlError(
                    "scxml.step_invalid", f"Trace auto flag is invalid: {trace_id}"
                )
            note = raw_step.get("note")
            if note is not None and not isinstance(note, str):
                raise ScxmlError(
                    "scxml.step_invalid", f"Trace note is invalid: {trace_id}"
                )
            via_history = raw_step.get("via_history")
            if via_history is not None and (
                not isinstance(via_history, str)
                or via_history not in chart.nodes
                or chart.nodes[via_history].kind != "history"
            ):
                raise ScxmlError(
                    "scxml.trace_history_invalid", f"via_history is invalid: {trace_id}"
                )
            regions = raw_step.get("expect_regions", {})
            if not isinstance(regions, dict) or any(
                not isinstance(region, str)
                or not isinstance(leaf, str)
                or region not in chart.nodes
                or leaf not in expected
                or not _descendant(chart.nodes, leaf, region)
                for region, leaf in regions.items()
            ):
                raise ScxmlError(
                    "scxml.trace_regions_invalid",
                    f"expect_regions is invalid: {trace_id}",
                )
            _validate_parallel_configuration(chart, set(expected), trace_id)
            steps.append(_TraceStep(event, tuple(expected), via_history, auto))
        result.append((trace_id, tuple(steps)))
    return tuple(result)


def _require_replay_subset(chart: _Chart) -> None:
    for transition in chart.transitions:
        if transition.conditional:
            raise ScxmlError(
                "scxml.replay_cond_unsupported", "Trace replay does not support cond"
            )
        if transition.event is None or not transition.event.strip():
            raise ScxmlError(
                "scxml.replay_eventless_unsupported",
                "Trace replay does not support eventless transitions",
            )
        events = transition.event.split()
        if len(events) != 1 or "*" in events[0]:
            raise ScxmlError(
                "scxml.replay_event_unsupported", "Trace replay requires literal events"
            )
    invalid_history = next(
        (
            node.node_id
            for node in chart.nodes.values()
            if node.kind == "history" and node.history_type not in {"shallow", "deep"}
        ),
        None,
    )
    if invalid_history is not None:
        raise ScxmlError(
            "scxml.replay_history_unsupported",
            f"History {invalid_history!r} has an unsupported type",
        )


def _initial_leaves(
    chart: _Chart, target: str, history: dict[str, tuple[str, ...]]
) -> set[str]:
    node = chart.nodes[target]
    if node.kind == "history":
        restored = history.get(target, node.history_default)
        if not restored:
            raise ScxmlError(
                "scxml.history_default_missing",
                f"History has no saved/default state: {target}",
            )
        leaves: set[str] = set()
        for child in restored:
            leaves.update(_initial_leaves(chart, child, history))
        return leaves
    if node.kind == "parallel":
        leaves = set()
        for child in node.children:
            leaves.update(_initial_leaves(chart, child, history))
        return leaves
    if node.children:
        return _initial_leaves(chart, node.initial[0], history)
    return {target}


def _replay(chart: _Chart, trace_id: str, steps: tuple[_TraceStep, ...]) -> None:
    history: dict[str, tuple[str, ...]] = {}
    active: set[str] = set()
    for target in chart.root_initial:
        active.update(_initial_leaves(chart, target, history))
    by_source: dict[str, list[_Transition]] = {}
    for transition in chart.transitions:
        by_source.setdefault(transition.source, []).append(transition)
    for index, step in enumerate(steps, 1):
        event = step.event
        expected = step.expected
        if step.auto != event.startswith("done.state."):
            raise ScxmlError(
                "scxml.trace_auto_invalid",
                f"Trace {trace_id!r} step {index} has inconsistent auto-event intent",
            )
        if step.auto:
            completed = event.removeprefix("done.state.")
            node = chart.nodes.get(completed)
            if node is None or not _state_complete(chart, completed, active):
                raise ScxmlError(
                    "scxml.trace_auto_unavailable",
                    f"Trace {trace_id!r} step {index} claims unavailable auto event "
                    f"{event!r}",
                )
        selected: list[_Transition] = []
        for leaf in active:
            current: str | None = leaf
            while current is not None:
                matches = [
                    item for item in by_source.get(current, ()) if item.event == event
                ]
                if len(matches) > 1:
                    raise ScxmlError(
                        "scxml.replay_nondeterministic",
                        f"Ambiguous event {event!r} at {current}",
                    )
                if matches:
                    if matches[0] not in selected:
                        selected.append(matches[0])
                    break
                current = chart.nodes[current].parent
        selected = [
            item
            for item in selected
            if not any(
                item.source != other.source
                and _descendant(chart.nodes, other.source, item.source)
                for other in selected
            )
        ]
        selected = _resolve_transition_conflicts(
            chart, sorted(selected, key=lambda item: item.order), before=active
        )
        if not selected:
            raise ScxmlError(
                "scxml.trace_transition_missing",
                f"Trace {trace_id!r} step {index} has no enabled transition for "
                f"event {event!r}",
            )
        before = set(active)
        used_history: set[str] = set()
        for transition in selected:
            domain = _transition_domain(chart, transition)
            exiting = (
                set(before)
                if domain is None
                else {leaf for leaf in before if _descendant(chart.nodes, leaf, domain)}
            )
            for history_node in chart.nodes.values():
                if history_node.kind != "history" or history_node.parent is None:
                    continue
                parent = history_node.parent
                if not any(_descendant(chart.nodes, leaf, parent) for leaf in exiting):
                    continue
                saved = set()
                for leaf in before:
                    if not _descendant(chart.nodes, leaf, parent):
                        continue
                    if history_node.history_type == "deep":
                        saved.add(leaf)
                        continue
                    current = leaf
                    while chart.nodes[current].parent != parent:
                        parent_id = chart.nodes[current].parent
                        if parent_id is None:
                            break
                        current = parent_id
                    saved.add(current)
                history[history_node.node_id] = tuple(sorted(saved))
            active.difference_update(exiting)
        for transition in selected:
            for target in transition.targets:
                if chart.nodes[target].kind == "history":
                    used_history.add(target)
                active.update(_initial_leaves(chart, target, history))
        if step.via_history is not None and step.via_history not in used_history:
            raise ScxmlError(
                "scxml.trace_history_mismatch",
                f"Trace {trace_id!r} step {index} did not use history "
                f"{step.via_history!r}",
            )
        if active != set(expected):
            raise ScxmlError(
                "scxml.trace_mismatch",
                f"Trace {trace_id!r} step {index} expected "
                f"{list(expected)!r}, got {sorted(active)!r}",
            )


def _transition_domain(chart: _Chart, transition: _Transition) -> str | None:
    source_lineage = [transition.source]
    current = chart.nodes[transition.source].parent
    while current is not None:
        source_lineage.append(current)
        current = chart.nodes[current].parent
    target_lineages = []
    for target in transition.targets:
        lineage = {target}
        current = chart.nodes[target].parent
        while current is not None:
            lineage.add(current)
            current = chart.nodes[current].parent
        target_lineages.append(lineage)
    return next(
        (
            candidate
            for candidate in source_lineage
            if all(candidate in lineage for lineage in target_lineages)
        ),
        None,
    )


def _resolve_transition_conflicts(
    chart: _Chart, transitions: list[_Transition], *, before: set[str]
) -> list[_Transition]:
    retained: list[_Transition] = []
    occupied: set[str] = set()
    for candidate in transitions:
        domain = _transition_domain(chart, candidate)
        exit_set = (
            set(before)
            if domain is None
            else {leaf for leaf in before if _descendant(chart.nodes, leaf, domain)}
        )
        if occupied.intersection(exit_set):
            continue
        retained.append(candidate)
        occupied.update(exit_set)
    return retained


def _state_complete(chart: _Chart, state_id: str, active: set[str]) -> bool:
    node = chart.nodes[state_id]
    if node.kind == "parallel":
        return all(
            any(
                chart.nodes[leaf].kind == "final"
                and _descendant(chart.nodes, leaf, region)
                for leaf in active
            )
            for region in node.children
        )
    return any(
        chart.nodes[leaf].kind == "final" and chart.nodes[leaf].parent == state_id
        for leaf in active
    )


def _validate_parallel_configuration(
    chart: _Chart, leaves: set[str], trace_id: str
) -> None:
    implied = {
        node_id
        for node_id, node in chart.nodes.items()
        if node.kind == "parallel"
        and any(_descendant(chart.nodes, leaf, node_id) for leaf in leaves)
    }
    for parallel in implied:
        for region in chart.nodes[parallel].children:
            covering = {
                leaf
                for leaf in leaves
                if leaf == region or _descendant(chart.nodes, leaf, region)
            }
            if len(covering) != 1:
                raise ScxmlError(
                    "scxml.trace_configuration_invalid",
                    f"Trace {trace_id!r} must select exactly one leaf for "
                    f"parallel region {region!r}",
                )


def _artifact_requirement(
    path: str,
    *,
    is_chart: bool,
    traces: dict[str, tuple[_TraceStep, ...]],
) -> SpecificationRequirement:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    requirement_id = f"scxml-artifact-{digest}"
    if is_chart:
        scenarios = (
            SpecificationScenario(
                scenario_id=f"{requirement_id}-valid",
                title="Chart is structurally valid",
                given=(f"the exact SCXML artifact {path} is loaded",),
                when=("the chart structure is validated",),
                then=("all state references are unique, resolved, and reachable",),
            ),
        )
        statement = (
            "The implementation SHALL conform to the exact validated SCXML chart "
            f"artifact {path}."
        )
    else:
        scenarios = tuple(
            SpecificationScenario(
                scenario_id=f"{requirement_id}-trace-{hashlib.sha256(trace_id.encode('utf-8')).hexdigest()[:16]}",
                title=f"Trace {trace_id}",
                given=(f"the exact trace sidecar {path} is loaded",),
                when=(f"trace {trace_id} is replayed",),
                then=("every expected active-state configuration is observed",),
            )
            for trace_id in sorted(traces)
        )
        if not scenarios:
            # A sidecar is required to contain traces, so this is defensive only.
            raise ScxmlError(
                "scxml.traces_empty", f"Trace sidecar requires traces: {path}"
            )
        statement = (
            "The implementation SHALL satisfy the exact replay traces in artifact "
            f"{path}."
        )
    return SpecificationRequirement(requirement_id, path, statement, scenarios)


SCXMLProvider = ScxmlProvider


__all__ = ["LoadedScxml", "SCXMLProvider", "ScxmlError", "ScxmlProvider"]
