"""Throwaway prototype for the 0.6.0 DMN specification_provider design spike's
follow-on prototype (see ../../history/roadmap/0.6.0-dmn-provider-design.md, "Maturity / next-action
assessment", items 1 and 2).

NOT production code. Nothing here is wired into src/literate_ai/. This exists only
to prove, against a real hand-authored .dmn file
(docs/roadmap/0.6.0-dmn-prototype/risk-category.dmn), that:

  1. A minimal DMN-XML parser can extract a decision table's structure (input/output
     column definitions, hit policy, per-rule input/output cell text) from real DMN
     1.x XML using nothing but the Python standard library (xml.etree.ElementTree) —
     no third-party DMN dependency.
  2. A UNIQUE-hit-policy overlap check (do any two rules' input conditions intersect?)
     can be implemented and run end to end against that parsed structure.

Parsing approach chosen: hand-rolled, stdlib-only (xml.etree.ElementTree), scoped to
exactly the subset of DMN 1.x this prototype needs (a single <decision>/<decisionTable>
per file, numeric-range and string-literal unary tests only, no full FEEL grammar).
See the "Parsing approach" section appended to 0.6.0-dmn-provider-design.md for the
justification versus a dependency like pyDMNrules.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

DMN_NS = "https://www.omg.org/spec/DMN/20191111/MODEL/"


def _tag(local: str) -> str:
    return f"{{{DMN_NS}}}{local}"


@dataclass(frozen=True)
class InputColumn:
    label: str
    type_ref: str


@dataclass(frozen=True)
class OutputColumn:
    label: str
    type_ref: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    input_entries: tuple[str, ...]  # raw FEEL unary-test text, one per input column
    output_entries: tuple[str, ...]  # raw FEEL literal text, one per output column


@dataclass(frozen=True)
class DecisionTable:
    decision_id: str
    decision_name: str
    hit_policy: str
    inputs: tuple[InputColumn, ...]
    outputs: tuple[OutputColumn, ...]
    rules: tuple[Rule, ...]


class DmnParseError(Exception):
    """Raised when a .dmn file does not match the minimal subset this prototype parses."""


def parse_decision_table(dmn_path: Path) -> DecisionTable:
    """Parse the single <decision>/<decisionTable> out of a .dmn file.

    Scoped deliberately narrow: exactly one <decision> containing exactly one
    <decisionTable>, per the prototype's brief. A real DmnProvider would need to
    handle multiple decisions per file; that generalization is out of scope here
    (see design doc Q3 — minimum viable adapter).
    """
    root = ET.parse(dmn_path).getroot()

    decisions = root.findall(_tag("decision"))
    if len(decisions) != 1:
        raise DmnParseError(
            f"expected exactly one <decision>, found {len(decisions)} in {dmn_path}"
        )
    decision = decisions[0]
    decision_id = decision.get("id", "")
    decision_name = decision.get("name", "")

    tables = decision.findall(_tag("decisionTable"))
    if len(tables) != 1:
        raise DmnParseError(
            f"expected exactly one <decisionTable>, found {len(tables)} in {dmn_path}"
        )
    table = tables[0]

    hit_policy = table.get("hitPolicy", "UNIQUE")

    inputs: list[InputColumn] = []
    for input_el in table.findall(_tag("input")):
        label = input_el.get("label", "")
        expr = input_el.find(_tag("inputExpression"))
        type_ref = expr.get("typeRef", "") if expr is not None else ""
        inputs.append(InputColumn(label=label, type_ref=type_ref))

    outputs: list[OutputColumn] = []
    for output_el in table.findall(_tag("output")):
        label = output_el.get("label", "")
        type_ref = output_el.get("typeRef", "")
        outputs.append(OutputColumn(label=label, type_ref=type_ref))

    if not inputs:
        raise DmnParseError(f"decision table has no <input> columns in {dmn_path}")
    if not outputs:
        raise DmnParseError(f"decision table has no <output> columns in {dmn_path}")

    rules: list[Rule] = []
    for rule_el in table.findall(_tag("rule")):
        rule_id = rule_el.get("id", "")
        in_entries = tuple(
            (e.find(_tag("text")).text or "").strip()
            for e in rule_el.findall(_tag("inputEntry"))
        )
        out_entries = tuple(
            (e.find(_tag("text")).text or "").strip()
            for e in rule_el.findall(_tag("outputEntry"))
        )
        if len(in_entries) != len(inputs):
            raise DmnParseError(
                f"rule {rule_id} has {len(in_entries)} input entries, "
                f"expected {len(inputs)}"
            )
        if len(out_entries) != len(outputs):
            raise DmnParseError(
                f"rule {rule_id} has {len(out_entries)} output entries, "
                f"expected {len(outputs)}"
            )
        rules.append(
            Rule(rule_id=rule_id, input_entries=in_entries, output_entries=out_entries)
        )

    if not rules:
        raise DmnParseError(f"decision table has no <rule> rows in {dmn_path}")

    return DecisionTable(
        decision_id=decision_id,
        decision_name=decision_name,
        hit_policy=hit_policy,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        rules=tuple(rules),
    )


# --- Minimal FEEL unary-test subset -----------------------------------------------
#
# Only what risk-category.dmn actually uses:
#   "-"                dontcare (matches anything)
#   [a..b]              closed numeric interval
#   "literal string"    string equality test
#
# This is NOT a FEEL grammar implementation (per design doc Q3, litai's DmnProvider
# should not implement one). It is exactly enough unary-test parsing to run the
# UNIQUE-hit-policy overlap check on numeric-interval and string-literal cells.


class UnsupportedFeelExpression(Exception):
    """Raised when a cell uses FEEL syntax outside this prototype's tiny subset."""


def _parse_numeric_interval(text: str) -> tuple[float, float] | None:
    if text == "-":
        return None  # dontcare: caller treats this as "always overlaps"
    if text.startswith("[") and text.endswith("]") and ".." in text:
        inner = text[1:-1]
        lo_str, hi_str = inner.split("..", 1)
        return (float(lo_str), float(hi_str))
    raise UnsupportedFeelExpression(f"cannot parse numeric unary test: {text!r}")


def _intervals_overlap(
    a: tuple[float, float] | None, b: tuple[float, float] | None
) -> bool:
    if a is None or b is None:
        return True  # dontcare overlaps everything
    a_lo, a_hi = a
    b_lo, b_hi = b
    return a_lo <= b_hi and b_lo <= a_hi


@dataclass(frozen=True)
class OverlapFinding:
    rule_a: str
    rule_b: str
    detail: str


def check_unique_hit_policy(table: DecisionTable) -> list[OverlapFinding]:
    """Confirm no two rules' input conditions overlap (DMN UNIQUE hit policy).

    Only meaningful for numeric-interval / dontcare input columns, which is exactly
    what risk-category.dmn's Age/Income columns are. A real DmnProvider would need
    the fuller FEEL unary-test grammar (string lists, comparisons, negation, etc.)
    to validate arbitrary DMN tables; scoping to numeric intervals + dontcare is
    sufficient to prove the overlap-check *mechanism* end to end, which is this
    prototype's brief (design doc item 1).
    """
    if table.hit_policy != "UNIQUE":
        raise ValueError(
            f"check_unique_hit_policy only applies to UNIQUE hit policy, "
            f"got {table.hit_policy!r}"
        )

    findings: list[OverlapFinding] = []
    rules = table.rules
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            rule_a, rule_b = rules[i], rules[j]
            per_column_overlap = []
            for col_index in range(len(table.inputs)):
                a_text = rule_a.input_entries[col_index]
                b_text = rule_b.input_entries[col_index]
                a_interval = _parse_numeric_interval(a_text)
                b_interval = _parse_numeric_interval(b_text)
                per_column_overlap.append(_intervals_overlap(a_interval, b_interval))
            if all(per_column_overlap):
                findings.append(
                    OverlapFinding(
                        rule_a=rule_a.rule_id,
                        rule_b=rule_b.rule_id,
                        detail=(
                            f"{rule_a.rule_id} inputs={rule_a.input_entries} overlaps "
                            f"{rule_b.rule_id} inputs={rule_b.input_entries} on every "
                            f"input column"
                        ),
                    )
                )
    return findings


def main(argv: list[str]) -> int:
    dmn_path = Path(argv[1] if len(argv) > 1 else "risk-category.dmn")
    table = parse_decision_table(dmn_path)

    print(f"Decision: {table.decision_name!r} (id={table.decision_id})")
    print(f"Hit policy: {table.hit_policy}")
    print(f"Inputs: {[c.label for c in table.inputs]}")
    print(f"Outputs: {[c.label for c in table.outputs]}")
    print(f"Rule count: {len(table.rules)}")
    for rule in table.rules:
        print(f"  {rule.rule_id}: IN={rule.input_entries} -> OUT={rule.output_entries}")

    findings = check_unique_hit_policy(table)
    if findings:
        print(
            f"\nUNIQUE hit policy VIOLATED — {len(findings)} overlapping rule pair(s):"
        )
        for f in findings:
            print(f"  - {f.detail}")
        return 1

    print("\nUNIQUE hit policy check: PASSED (no overlapping rule input conditions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
