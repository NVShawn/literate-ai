#!/usr/bin/env python3
"""Throwaway prototype parser + structural-soundness checker for SCXML.

Built for docs/history/roadmap/0.6.0-scxml-provider-design.md's "Maturity /
next-action assessment" step 1: validate whether the two structural-soundness
checks the design proposes (transition-target resolution, reachability from
`initial`) are as mechanical as the design assumed once a chart actually has
<parallel> and <history> in it.

NOT production code. Not wired into any specification_provider. stdlib only
(xml.etree.ElementTree), matching the design's minimal-adapter philosophy for
this prototype step. A real SCXMLProvider still needs the XXE-hardening
decision the design flags separately (defusedxml vs. a hardened stdlib parse
path) -- this prototype parses a trusted, hand-authored local fixture only,
so that hardening is deliberately skipped here.

Usage:
    python3 scxml_structural.py media_player.scxml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SCXML_NS = "{http://www.w3.org/2005/07/scxml}"

STATE_TAGS = {"state", "parallel", "final", "history"}


@dataclass
class Transition:
    source_id: str
    event: str | None  # None == eventless/default transition (used by <history>)
    targets: tuple[str, ...]


@dataclass
class Node:
    id: str
    tag: str  # state | parallel | final | history
    parent_id: str | None
    children_ids: list[str] = field(default_factory=list)
    initial: str | None = None  # explicit initial="" attribute, state/parallel only
    transitions: list[Transition] = field(default_factory=list)


@dataclass
class Chart:
    initial: str
    nodes: dict[str, Node]

    def ancestors(self, node_id: str) -> list[str]:
        """Return node_id's ancestor chain, nearest first, not including node_id itself."""
        out = []
        cur = self.nodes[node_id].parent_id
        while cur is not None:
            out.append(cur)
            cur = self.nodes[cur].parent_id
        return out

    def is_descendant(self, candidate_id: str, ancestor_id: str) -> bool:
        if candidate_id == ancestor_id:
            return True
        return ancestor_id in self.ancestors(candidate_id)

    def entry(self, state_id: str) -> frozenset[str]:
        """Atomic (leaf) states active when state_id is entered "fresh" (no
        stored history) -- i.e. SCXML's default-entry algorithm: compound
        states descend into their initial child, parallel states descend
        into ALL children simultaneously, atomic/final states are the base
        case.
        """
        node = self.nodes[state_id]
        if node.tag == "history":
            # A <history> pseudostate is never itself "entered" at rest --
            # resolve via its own default (eventless) transition, same as a
            # real SCXML processor does the first time a history is visited
            # with nothing recorded yet. This prototype does not model
            # persisted history values across multiple visits (see write-up).
            assert node.transitions, (
                f"<history id={state_id!r}> has no default transition"
            )
            leaves: set[str] = set()
            for t in node.transitions[0].targets:
                leaves |= self.entry(t)
            return frozenset(leaves)
        if not node.children_ids:
            return frozenset({state_id})
        if node.tag == "parallel":
            leaves = set()
            for c in node.children_ids:
                leaves |= self.entry(c)
            return frozenset(leaves)
        # compound <state>: descend into initial child
        initial_child = node.initial or node.children_ids[0]
        return self.entry(initial_child)

    def enabled_transitions(self, config: frozenset[str]) -> list[Transition]:
        """Transitions reachable from an active configuration, including
        those declared on ANCESTOR states -- SCXML transitions declared on a
        compound/parallel ancestor are inherited by every descendant
        configuration ("event bubbling"). Deduplicated by declaring node id
        so a transition shared by multiple active leaves under the same
        ancestor isn't double-counted.
        """
        seen_source_ids: set[str] = set()
        out: list[Transition] = []
        for leaf in config:
            chain = [leaf] + self.ancestors(leaf)
            for node_id in chain:
                if node_id in seen_source_ids:
                    continue
                seen_source_ids.add(node_id)
                out.extend(self.nodes[node_id].transitions)
        return out

    def apply(self, config: frozenset[str], transition: Transition) -> frozenset[str]:
        """New configuration after firing `transition` from `config`."""
        source = transition.source_id
        remaining = {leaf for leaf in config if not self.is_descendant(leaf, source)}
        for target in transition.targets:
            remaining |= self.entry(target)
        return frozenset(remaining)


def parse_scxml(path: str) -> Chart:
    tree = ET.parse(path)  # noqa: S314 -- trusted local fixture only, see module docstring
    root = tree.getroot()
    assert root.tag == f"{SCXML_NS}scxml", (
        f"not an SCXML document: root tag {root.tag!r}"
    )
    initial = root.attrib["initial"]

    nodes: dict[str, Node] = {}

    def walk(elem: ET.Element, parent_id: str | None) -> None:
        tag = elem.tag.removeprefix(SCXML_NS)
        if tag not in STATE_TAGS:
            return
        node_id = elem.attrib["id"]
        if node_id in nodes:
            raise ValueError(f"duplicate state id: {node_id!r}")
        node = Node(
            id=node_id, tag=tag, parent_id=parent_id, initial=elem.attrib.get("initial")
        )
        nodes[node_id] = node
        if parent_id is not None:
            nodes[parent_id].children_ids.append(node_id)

        for child in elem:
            child_tag = child.tag.removeprefix(SCXML_NS)
            if child_tag == "transition":
                event = child.attrib.get("event")  # None => eventless/default
                target_attr = child.attrib.get("target", "")
                targets = tuple(target_attr.split()) if target_attr else ()
                node.transitions.append(
                    Transition(source_id=node_id, event=event, targets=targets)
                )
            elif child_tag in STATE_TAGS:
                walk(child, node_id)
            # onentry/onexit/datamodel/etc: structurally irrelevant to these
            # two checks, deliberately not modeled by this prototype.

    for child in root:
        walk(child, None)

    return Chart(initial=initial, nodes=nodes)


def check_dangling_transition_targets(chart: Chart) -> list[str]:
    """Every <transition target="..."> must resolve to a declared state id."""
    problems = []
    for node in chart.nodes.values():
        for t in node.transitions:
            for target in t.targets:
                if target not in chart.nodes:
                    problems.append(
                        f"transition on {node.id!r} (event={t.event!r}) targets "
                        f"undeclared state {target!r}"
                    )
    return problems


def check_reachability(chart: Chart) -> tuple[list[str], frozenset[str]]:
    """Every declared state/parallel/final/history id must be reachable from
    `initial` -- either as an active leaf in some visited configuration, or
    as an ancestor of one (a compound/parallel container is "reachable" if
    any descendant leaf is), or as the resolvable default-target of a
    reachable <history> pseudostate.
    """
    start = chart.entry(chart.initial)
    seen_configs: set[frozenset[str]] = {start}
    frontier = [start]
    reachable_leaves: set[str] = set(start)

    while frontier:
        config = frontier.pop()
        for t in chart.enabled_transitions(config):
            # A transition with a dangling target must not be walked here --
            # it is reported by check_dangling_transition_targets() instead.
            # Discovered empirically: reachability BFS and target-resolution
            # are NOT independent checks -- a naive reachability walk that
            # doesn't first filter out unresolved targets crashes instead of
            # degrading gracefully (see write-up in the design doc).
            if any(target not in chart.nodes for target in t.targets):
                continue
            new_config = chart.apply(config, t)
            reachable_leaves |= set(new_config)
            if new_config not in seen_configs:
                seen_configs.add(new_config)
                frontier.append(new_config)

    reachable_ids: set[str] = set()
    for leaf in reachable_leaves:
        reachable_ids.add(leaf)
        reachable_ids.update(chart.ancestors(leaf))

    # A <history> pseudostate counts as reachable if the state that
    # declares it is reachable (you can only land on a history node by
    # transitioning into its owning compound state).
    for node in chart.nodes.values():
        if node.tag == "history" and node.parent_id in reachable_ids:
            reachable_ids.add(node.id)

    unreachable = sorted(set(chart.nodes) - reachable_ids)
    return unreachable, frozenset(reachable_ids)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "media_player.scxml"
    chart = parse_scxml(path)

    print(
        f"Parsed {path}: {len(chart.nodes)} declared state/parallel/final/history nodes"
    )
    for node_id, node in chart.nodes.items():
        print(f"  - {node.tag:8s} {node_id:12s} parent={node.parent_id}")

    dangling = check_dangling_transition_targets(chart)
    unreachable, reachable_ids = check_reachability(chart)

    print()
    print(f"Dangling transition targets: {len(dangling)}")
    for p in dangling:
        print(f"  FAIL: {p}")

    print(f"Unreachable declared states: {len(unreachable)}")
    for u in unreachable:
        print(f"  FAIL: {u!r} is not reachable from initial={chart.initial!r}")

    ok = not dangling and not unreachable
    print()
    print("STRUCTURAL SOUNDNESS: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
