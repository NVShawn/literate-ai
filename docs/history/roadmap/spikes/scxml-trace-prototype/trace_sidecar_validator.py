#!/usr/bin/env python3
"""Standalone prototype: structural validator for the declared-trace sidecar
format proposed in docs/history/roadmap/spikes/scxml-trace-prototype/RESULTS.md.

Scope, deliberately: this is a STATIC STRUCTURAL validator, not an SCXML
interpreter. It checks that a trace-sidecar document is well-formed *against*
a parsed .scxml chart -- every referenced state id actually exists, every
declared "simultaneously active" leaf set is a valid configuration shape
(one leaf per region of every <parallel> ancestor it implies, no leaf that
is an ancestor of another leaf in the same set), every `via_history`
reference actually names a declared <history> node, and every `event` is at
least declared somewhere in the chart. It does NOT replay the chart
step-by-step against prior state (that is full interpretation, explicitly
out of scope per the task this prototype was built for -- see RESULTS.md's
"Known limitations"). A full stateful replay checker already exists as a
*separate*, previously-prototyped spike:
docs/roadmap/0.6.0-scxml-prototype/trace_conformance.py -- this file
intentionally does not depend on it, to keep this spike standalone as
instructed.

NOT production code. Not wired into src/literate_ai/. stdlib only.

Usage:
    python3 trace_sidecar_validator.py <chart.scxml> <trace1.json> [trace2.json ...]
    python3 trace_sidecar_validator.py connection_handshake.scxml connection_handshake.trace-*.json
"""

from __future__ import annotations

import glob
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SCXML_NS = "{http://www.w3.org/2005/07/scxml}"
STATE_TAGS = {"state", "parallel", "final", "history"}


@dataclass
class Transition:
    source_id: str
    event: str | None
    targets: tuple[str, ...]


@dataclass
class Node:
    id: str
    tag: str  # state | parallel | final | history
    parent_id: str | None
    children_ids: list[str] = field(default_factory=list)
    initial: str | None = None
    transitions: list[Transition] = field(default_factory=list)


@dataclass
class Chart:
    initial: str
    nodes: dict[str, Node]

    def ancestors(self, node_id: str) -> list[str]:
        out: list[str] = []
        cur = self.nodes[node_id].parent_id
        while cur is not None:
            out.append(cur)
            cur = self.nodes[cur].parent_id
        return out

    def is_leaf(self, node_id: str) -> bool:
        node = self.nodes[node_id]
        return node.tag in {"state", "final"} and not node.children_ids

    def all_events(self) -> set[str]:
        events: set[str] = set()
        for node in self.nodes.values():
            for t in node.transitions:
                if t.event is not None:
                    events.add(t.event)
        return events


def parse_scxml(path: Path) -> Chart:
    tree = ET.parse(path)  # noqa: S314 -- trusted local fixture only
    root = tree.getroot()
    if root.tag != f"{SCXML_NS}scxml":
        raise ValueError(f"{path}: root tag {root.tag!r} is not <scxml>")
    initial = root.attrib["initial"]
    nodes: dict[str, Node] = {}

    def walk(elem: ET.Element, parent_id: str | None) -> None:
        tag = elem.tag.removeprefix(SCXML_NS)
        if tag not in STATE_TAGS:
            return
        node_id = elem.attrib["id"]
        if node_id in nodes:
            raise ValueError(f"{path}: duplicate state id {node_id!r}")
        node = Node(id=node_id, tag=tag, parent_id=parent_id, initial=elem.attrib.get("initial"))
        nodes[node_id] = node
        if parent_id is not None:
            nodes[parent_id].children_ids.append(node_id)
        for child in elem:
            child_tag = child.tag.removeprefix(SCXML_NS)
            if child_tag == "transition":
                event = child.attrib.get("event")
                target_attr = child.attrib.get("target", "")
                targets = tuple(target_attr.split()) if target_attr else ()
                node.transitions.append(Transition(node_id, event, targets))
            elif child_tag in STATE_TAGS:
                walk(child, node_id)

    for child in root:
        walk(child, None)
    return Chart(initial=initial, nodes=nodes)


@dataclass
class ValidationResult:
    trace_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_parallel_region_coverage(chart: Chart, leaves: set[str]) -> list[str]:
    """For every <parallel> node that is an ancestor of any leaf in `leaves`,
    every region (direct child of that parallel node) must be covered by
    EXACTLY ONE leaf in the set. This is the structural definition of "a
    valid simultaneously-active configuration under <parallel>": one active
    sub-state per orthogonal region, no fewer, no more.
    """
    problems: list[str] = []
    implied_parallels: set[str] = set()
    for leaf in leaves:
        for anc in chart.ancestors(leaf):
            if chart.nodes[anc].tag == "parallel":
                implied_parallels.add(anc)

    for p_id in implied_parallels:
        p = chart.nodes[p_id]
        for region_id in p.children_ids:
            covering = [
                leaf for leaf in leaves
                if leaf == region_id or region_id in chart.ancestors(leaf)
            ]
            if len(covering) == 0:
                problems.append(
                    f"parallel {p_id!r} region {region_id!r} has NO active leaf in "
                    f"the declared configuration {sorted(leaves)} -- every region of "
                    f"an active <parallel> must have exactly one active sub-state"
                )
            elif len(covering) > 1:
                problems.append(
                    f"parallel {p_id!r} region {region_id!r} has MULTIPLE active "
                    f"leaves in the declared configuration ({sorted(covering)}) -- "
                    f"a region can only have one active sub-state at a time"
                )
    return problems


def _validate_no_ancestor_descendant_pairs(chart: Chart, leaves: set[str]) -> list[str]:
    problems = []
    for a in leaves:
        for b in leaves:
            if a != b and a in chart.ancestors(b):
                problems.append(
                    f"{a!r} is an ancestor of {b!r} -- expect_active must list only "
                    f"mutually-orthogonal leaves, not a state together with its own "
                    f"descendant"
                )
    return problems


def validate_trace(chart: Chart, trace: dict) -> ValidationResult:
    result = ValidationResult(trace_id=trace.get("id", "<unnamed>"))
    known_events = chart.all_events()

    for i, step in enumerate(trace.get("steps", [])):
        where = f"step {i} ({trace.get('id')!r})"

        event = step.get("event")
        if not event:
            result.errors.append(f"{where}: missing required 'event'")
        elif event not in known_events:
            result.warnings.append(
                f"{where}: event {event!r} is not declared on any <transition> in "
                f"the chart (checked structurally only; may still be a typo)"
            )

        expect_active = step.get("expect_active")
        if not expect_active:
            result.errors.append(f"{where}: missing required 'expect_active'")
            continue
        leaves = set(expect_active)

        unknown = leaves - set(chart.nodes)
        if unknown:
            result.errors.append(
                f"{where}: expect_active references undeclared state id(s): "
                f"{sorted(unknown)}"
            )
            continue

        non_leaf = {s for s in leaves if not chart.is_leaf(s)}
        if non_leaf:
            result.errors.append(
                f"{where}: expect_active includes non-leaf (compound/parallel) "
                f"state id(s) {sorted(non_leaf)} -- an active configuration must "
                f"list atomic <state>/<final> leaves only, not their container"
            )
            continue

        result.errors.extend(_validate_no_ancestor_descendant_pairs(chart, leaves))
        result.errors.extend(_validate_parallel_region_coverage(chart, leaves))

        expect_regions = step.get("expect_regions")
        if expect_regions:
            for region_id, region_leaf in expect_regions.items():
                if region_id not in chart.nodes:
                    result.errors.append(
                        f"{where}: expect_regions references undeclared region id "
                        f"{region_id!r}"
                    )
                    continue
                if chart.nodes[region_id].tag != "parallel" and chart.nodes[region_id].parent_id:
                    parent = chart.nodes[region_id].parent_id
                    if parent is None or chart.nodes[parent].tag != "parallel":
                        result.warnings.append(
                            f"{where}: expect_regions key {region_id!r} is not a "
                            f"direct child of a <parallel> node -- unusual but not "
                            f"rejected"
                        )
                if region_leaf not in leaves:
                    result.errors.append(
                        f"{where}: expect_regions says region {region_id!r} has "
                        f"active leaf {region_leaf!r}, but {region_leaf!r} is not "
                        f"in this step's expect_active"
                    )
                elif region_id != region_leaf and region_id not in chart.ancestors(region_leaf):
                    result.errors.append(
                        f"{where}: expect_regions says {region_leaf!r} is under "
                        f"region {region_id!r}, but the chart's structure disagrees"
                    )

        via_history = step.get("via_history")
        if via_history:
            if via_history not in chart.nodes:
                result.errors.append(
                    f"{where}: via_history references undeclared id {via_history!r}"
                )
            elif chart.nodes[via_history].tag != "history":
                result.errors.append(
                    f"{where}: via_history {via_history!r} is not a <history> node "
                    f"(it is a {chart.nodes[via_history].tag!r})"
                )

    return result


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    chart_path = Path(argv[1])
    trace_paths: list[str] = []
    for pattern in argv[2:]:
        matches = sorted(glob.glob(pattern))
        trace_paths.extend(matches if matches else [pattern])

    chart = parse_scxml(chart_path)
    print(f"Parsed {chart_path}: {len(chart.nodes)} declared state/parallel/final/history nodes")

    overall_ok = True
    for trace_path_str in trace_paths:
        trace_path = Path(trace_path_str)
        sidecar = json.loads(trace_path.read_text())
        sidecar_chart_ref = sidecar.get("scxml")
        if sidecar_chart_ref and sidecar_chart_ref != chart_path.name:
            print(
                f"  WARN: {trace_path} declares scxml={sidecar_chart_ref!r}, "
                f"validating against {chart_path.name!r} anyway"
            )
        for trace in sidecar.get("traces", []):
            result = validate_trace(chart, trace)
            status = "PASS" if result.ok else "FAIL"
            print(f"\n[{status}] {trace_path.name} :: {result.trace_id}")
            for e in result.errors:
                print(f"    ERROR: {e}")
            for w in result.warnings:
                print(f"    warn:  {w}")
            overall_ok = overall_ok and result.ok

    print()
    print("SIDECAR STRUCTURAL VALIDATION: " + ("PASS" if overall_ok else "FAIL"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
