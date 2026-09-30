#!/usr/bin/env python3
"""Throwaway prototype declared-trace conformance checker.

Built for docs/history/roadmap/0.6.0-scxml-provider-design.md's "Maturity /
next-action assessment" steps 2/3: prototype a candidate declared-trace
sidecar format against the media_player.scxml sample and confirm a trace
recorded in it can be mechanically replayed and checked.

Candidate format prototyped here: a SEPARATE JSON file alongside the .scxml
document (media_player.trace.json in this same directory) -- see that
file's own header comment and the design doc's updated Maturity section for
why this placement was chosen over inline-in-authoring-Markdown or an XML
processing instruction inside the .scxml document itself.

IMPORTANT finding this prototype surfaced (see design doc write-up): trace
replay needs a REAL, stateful interpreter that tracks each <history>
pseudostate's last-recorded sub-configuration across steps. The static
structural-soundness checker in scxml_structural.py deliberately does NOT
do this (its Chart.entry() always resolves <history> via its default
transition, documented there as a simplification) -- that simplification is
fine for reachability/target-resolution, which only care whether a state
CAN be reached at all, but it is NOT fine for trace conformance, which must
predict which EXACT configuration a chart in a specific run reaches. This
file therefore implements its own small stateful stepper rather than
reusing Chart.entry() directly.

NOT production code, stdlib only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scxml_structural import Chart, Transition, parse_scxml


def entry_with_history(
    chart: Chart, state_id: str, history_store: dict[str, frozenset[str]]
) -> frozenset[str]:
    node = chart.nodes[state_id]
    if node.tag == "history":
        stored = history_store.get(state_id)
        if stored is not None:
            return stored
        assert node.transitions, f"<history id={state_id!r}> has no default transition"
        leaves: set[str] = set()
        for t in node.transitions[0].targets:
            leaves |= entry_with_history(chart, t, history_store)
        return frozenset(leaves)
    if not node.children_ids:
        return frozenset({state_id})
    if node.tag == "parallel":
        leaves = set()
        for c in node.children_ids:
            leaves |= entry_with_history(chart, c, history_store)
        return frozenset(leaves)
    initial_child = node.initial or node.children_ids[0]
    return entry_with_history(chart, initial_child, history_store)


def find_enabled_transition(
    chart: Chart, config: frozenset[str], event: str
) -> Transition | None:
    """First matching transition for `event`, giving priority to the
    deepest (nearest) declaring ancestor per active leaf -- sufficient for
    this prototype's conflict-free sample chart; a real interpreter needs
    the SCXML spec's full document-order/optimal-transition-set algorithm,
    deliberately out of scope here (see "parser, not interpreter" in the
    design doc).
    """
    for leaf in config:
        chain = [leaf] + chart.ancestors(leaf)
        for node_id in chain:
            for t in chart.nodes[node_id].transitions:
                if t.event == event:
                    return t
    return None


def apply_with_history(
    chart: Chart,
    config: frozenset[str],
    transition: Transition,
    history_store: dict[str, frozenset[str]],
) -> frozenset[str]:
    source = transition.source_id
    # Record history BEFORE exiting: any <history> node whose parent is the
    # subtree being exited must capture the current sub-configuration.
    for node in chart.nodes.values():
        if node.tag == "history" and chart.is_descendant(node.parent_id, source):
            history_store[node.id] = frozenset(
                leaf for leaf in config if chart.is_descendant(leaf, node.parent_id)
            )

    remaining = {leaf for leaf in config if not chart.is_descendant(leaf, source)}
    for target in transition.targets:
        remaining |= entry_with_history(chart, target, history_store)
    return frozenset(remaining)


def run_trace(chart: Chart, trace: dict) -> list[str]:
    """Replay one declared trace; return a list of failure messages (empty == pass)."""
    failures: list[str] = []
    history_store: dict[str, frozenset[str]] = {}
    config = entry_with_history(chart, chart.initial, history_store)

    for i, step in enumerate(trace["steps"]):
        event = step["event"]
        expected = frozenset(step["expect"])
        t = find_enabled_transition(chart, config, event)
        if t is None:
            failures.append(
                f"step {i}: no enabled transition for event {event!r} from config {sorted(config)}"
            )
            break
        config = apply_with_history(chart, config, t, history_store)
        if config != expected:
            failures.append(
                f"step {i}: after event {event!r}, expected config {sorted(expected)}, "
                f"got {sorted(config)}"
            )
    return failures


def main(argv: list[str]) -> int:
    here = Path(__file__).parent
    trace_path = Path(argv[1]) if len(argv) > 1 else here / "media_player.trace.json"
    sidecar = json.loads(trace_path.read_text())
    scxml_path = here / sidecar["scxml"]
    chart = parse_scxml(str(scxml_path))

    overall_ok = True
    for trace in sidecar["traces"]:
        failures = run_trace(chart, trace)
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] trace: {trace['name']}")
        for f in failures:
            print(f"    {f}")
        overall_ok = overall_ok and not failures

    print()
    print("TRACE CONFORMANCE: " + ("PASS" if overall_ok else "FAIL"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
