"""Summarize matched trial evidence; never launch models or certify their results."""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path

SCHEMA = "literate-ai/research-trials@1"
_TEXT = (
    "task",
    "variant",
    "model",
    "environment",
    "specification_identity",
    "oracle_identity",
)
_COUNTS = ("trial", "elapsed_ms", "tokens", "cost_microusd", "human_interventions")


def summarize(value: object, *, baseline: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "trials"}
        or value["schema"] != SCHEMA
    ):
        raise ValueError("invalid research trial document")
    rows = value["trials"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("no measured trials; an empty experiment is not a pass")
    groups: dict[str, dict[tuple[str, int], dict]] = {}
    required = {*_TEXT, *_COUNTS, "passed"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("trial has missing or unknown fields")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in _TEXT):
            raise ValueError("trial metadata must be nonempty strings")
        if any(type(row[key]) is not int or row[key] < 0 for key in _COUNTS):
            raise ValueError("trial measurements must be nonnegative integers")
        if type(row["passed"]) is not bool:
            raise ValueError("trial outcome must be a boolean")
        group = groups.setdefault(row["variant"], {})
        key = (row["task"], row["trial"])
        if key in group:
            raise ValueError("duplicate task/trial/variant")
        group[key] = row
    if baseline not in groups or len(groups) < 2:
        raise ValueError("comparison requires the named baseline and another variant")
    control = groups[baseline]
    for group in groups.values():
        if group.keys() != control.keys():
            raise ValueError("variants have missing or unmatched task/trial records")
        for key, row in group.items():
            if any(row[field] != control[key][field] for field in _TEXT[2:]):
                raise ValueError(
                    "paired trials differ in model, environment or authority"
                )
    tasks = sorted({key[0] for key in control})

    def task_rate(group: dict, task: str) -> float:
        return statistics.mean(
            int(row["passed"]) for (name, _), row in group.items() if name == task
        )

    comparisons = []
    for variant, group in sorted(groups.items()):
        rates = [task_rate(group, task) for task in tasks]
        differences = [
            rate - task_rate(control, task)
            for task, rate in zip(tasks, rates, strict=True)
        ]
        interval = None
        if len(tasks) > 1:
            # Resample independent tasks, not correlated repetitions of one task.
            generator = random.Random(0)
            samples = sorted(
                statistics.mean(generator.choices(differences, k=len(tasks)))
                for _ in range(2000)
            )
            interval = [samples[49], samples[1949]]
        comparisons.append(
            {
                "variant": variant,
                "task_count": len(tasks),
                "trial_count": len(group),
                "passed_trials": sum(row["passed"] for row in group.values()),
                "task_weighted_pass_rate": statistics.mean(rates),
                "paired_task_pass_rate_difference": statistics.mean(differences),
                "paired_task_bootstrap_95_interval": interval,
                **{
                    f"total_{field}": sum(row[field] for row in group.values())
                    for field in _COUNTS
                    if field != "trial"
                },
            }
        )
    return {
        "schema": "literate-ai/research-summary@1",
        "baseline": baseline,
        "evidence_kind": "reported-measurements-not-authenticated-acceptance",
        "uncertainty_unit": "task",
        "small_sample": len(tasks) < 20,
        "comparisons": comparisons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", type=Path)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    try:
        result = summarize(
            json.loads(args.trials.read_text(encoding="utf-8")), baseline=args.baseline
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
