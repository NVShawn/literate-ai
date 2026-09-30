"""Verifier-only runtime probes exercise every sample with post-build entropy."""

from __future__ import annotations

import hashlib
import heapq
import json
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts import canonical_json_bytes
from tests.conformance.support.runtime_oracles import (
    RUNTIME_CASE_ID,
    RUNTIME_ENTROPY_BYTES,
    SUPPORTED_SAMPLE_IDS,
    RuntimeOracleError,
    _release_analysis_result,
    _release_dashboard_from_analysis,
    create_post_build_probe,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"
FIRST_ENTROPY = bytes(range(RUNTIME_ENTROPY_BYTES))
SECOND_ENTROPY = bytes(reversed(range(RUNTIME_ENTROPY_BYTES)))
# The durable split-service portfolio has a verifier-owned, stateful four-process
# flow instead of the single-entrypoint post-build probe exercised by this module.
# Keep that separate boundary explicit while still requiring every sample harness to
# be owned by exactly one of the two verifier catalogs.
CUSTOM_PORTFOLIO_SAMPLE_IDS = frozenset({"durable-split-service"})


def _argument(probe) -> dict[str, object]:
    arguments = probe.arguments
    if len(arguments) != 1 or not isinstance(arguments[0], dict):
        raise AssertionError("runtime probe must pass one object argument")
    return arguments[0]


def _hello_expected(argument: dict[str, object]) -> object:
    name = str(argument["name"])
    messages = list(argument["messages"])
    return {
        "greeting": f"Hello, {name}!",
        "message_count": len(messages),
        "recipient_id": name.casefold().replace(" ", "-"),
        "word_count": sum(len(str(message).split()) for message in messages),
    }


def _loan_risk_expected(argument: dict[str, object]) -> object:
    age = int(argument["age"])
    income = int(argument["income"])
    rules = (
        ((18, 25), (0, 30000), "high"),
        ((18, 25), (30001, 999999), "medium"),
        ((26, 55), (0, 40000), "medium"),
        ((26, 55), (40001, 999999), "low"),
        ((56, 120), None, "low"),
    )
    bands = (
        ("starter", 0, 30000),
        ("established", 30001, 40000),
        ("prime", 40001, 999999),
    )
    category = next(
        risk
        for ages, incomes, risk in rules
        if ages[0] <= age <= ages[1]
        and (incomes is None or incomes[0] <= income <= incomes[1])
    )
    band = next(
        identifier for identifier, lower, upper in bands if lower <= income <= upper
    )
    return {
        "age": age,
        "income": income,
        "risk_category": category,
        "score_band": band,
    }


def _playback_expected(argument: dict[str, object]) -> object:
    events = [str(item) for item in argument["events"]]
    leaves = {"audible", "stopped"}
    powered = True
    history: tuple[str, ...] = ()
    for event in events:
        if not powered:
            if event == "resume":
                leaves = set(history) if history else {"audible", "stopped"}
                powered = True
            continue
        if event == "power":
            history = tuple(sorted(leaves))
            leaves = {"off"}
            powered = False
            continue
        if event == "play" and "stopped" in leaves:
            leaves = (leaves - {"stopped"}) | {"playing"}
        elif event == "mute" and "audible" in leaves:
            leaves = (leaves - {"audible"}) | {"muted"}
    return {
        "active": ["off"] if not powered else sorted(leaves),
        "event_count": len(events),
        "powered": powered,
    }


def _tally_expected(argument: dict[str, object]) -> object:
    import re

    counts = {"status_2xx": 0, "status_3xx": 0, "status_4xx": 0, "status_5xx": 0}
    hits: dict[str, int] = {}
    parsed = malformed = bytes_total = 0
    pattern = re.compile(
        r'^\S+ \S+ \S+ \[[^\]]+\] "[A-Z]+ (\S+) HTTP/[\d.]+" (\d{3}) (\d+|-)$'
    )
    for line in argument["access_log"]:
        match = pattern.match(line)
        if match is None:
            malformed += 1
            continue
        path, status, size = match.group(1), int(match.group(2)), match.group(3)
        parsed += 1
        counts[f"status_{status // 100}xx"] += 1
        hits[path] = hits.get(path, 0) + 1
        bytes_total += 0 if size == "-" else int(size)
    top_count = max(0, min(int(argument["top_count"]), 100))
    ranked = sorted(hits.items(), key=lambda item: (-item[1], item[0]))
    return {
        "total_lines": len(argument["access_log"]),
        "parsed_lines": parsed,
        "malformed_lines": malformed,
        **counts,
        "bytes_total": bytes_total,
        "top_paths": [{"path": p, "hits": h} for p, h in ranked[:top_count]],
        "report_version": "1.0",
    }


def _statistics_expected(argument: dict[str, object]) -> object:
    values = sorted(int(item) for item in argument["values"])
    return {
        "count": len(values),
        "maximum": values[-1],
        "mean": {"denominator": len(values), "numerator": sum(values)},
        "median": {"denominator": 2, "numerator": values[1] + values[2]},
        "minimum": values[0],
        "total": sum(values),
    }


def _cuda_vector_expected(argument: dict[str, object]) -> object:
    values = [int(item) for item in argument["values"]]
    multiplier = int(argument["multiplier"])
    bias = int(argument["bias"])
    transformed = [value * multiplier + bias for value in values]
    return {
        "backend": "cuda",
        "device_executed": True,
        "count": len(transformed),
        "values": transformed,
        "checksum": sum(transformed),
    }


def _cuda_matrix_expected(argument: dict[str, object]) -> object:
    left = argument["left"]
    right = argument["right"]
    values = [
        [
            sum(
                int(left[row][inner]) * int(right[inner][column])
                for inner in range(len(right))
            )
            for column in range(len(right[0]))
        ]
        for row in range(len(left))
    ]
    return {
        "backend": "cupy-cuda",
        "device_executed": True,
        "rows": len(values),
        "columns": len(values[0]),
        "values": values,
        "checksum": sum(sum(row) for row in values),
    }


def _invoice_expected(argument: dict[str, object]) -> object:
    items = list(argument["items"])
    subtotal = sum(
        int(item["quantity"]) * int(item["unit_price_cents"]) for item in items
    )
    basis_points = int(argument["discount_basis_points"])
    discount = (subtotal * basis_points + 5_000) // 10_000
    return {
        "discount_cents": discount,
        "line_count": len(items),
        "subtotal_cents": subtotal,
        "total_cents": subtotal - discount,
        "unit_count": sum(int(item["quantity"]) for item in items),
    }


def _jobs_expected(argument: dict[str, object]) -> object:
    jobs = list(argument["jobs"])
    results = []
    for job in jobs:
        text = str(job["text"])
        results.append(
            {
                "characters": len(text),
                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
                "id": job["id"],
                "words": len(text.split()),
            }
        )
    return {
        "completed": len(jobs),
        "results": results,
        "total_characters": sum(item["characters"] for item in results),
    }


def _routing_expected(argument: dict[str, object]) -> object:
    required = set(argument["required_capabilities"])
    considered: list[str] = []
    rejected: list[str] = []
    selected: str | None = None
    for endpoint in argument["endpoints"]:
        endpoint_id = str(endpoint["id"])
        considered.append(endpoint_id)
        compatible = (
            endpoint["available"] is True
            and endpoint["locality"] == "local"
            and required.issubset(endpoint["capabilities"])
        )
        if compatible:
            selected = endpoint_id
            break
        rejected.append(endpoint_id)
    if selected is None:
        raise AssertionError("runtime routing probe must include a compatible endpoint")
    return {
        "considered": considered,
        "data_egress": "none",
        "fallback_used": len(considered) > 1,
        "rejected": rejected,
        "selected": selected,
    }


def _cache_expected(argument: dict[str, object]) -> object:
    records = [str(item) for item in argument["records"]]
    identities = [
        hashlib.sha256(record.encode("utf-8")).hexdigest() for record in records
    ]
    return {
        "identities": identities,
        "object_count": len(set(identities)),
        "recovered": records,
        "restart_verified": True,
    }


def _scheduler_expected(argument: dict[str, object]) -> object:
    tasks = list(argument["tasks"])
    by_id = {str(task["id"]): task for task in tasks}
    dependents = {task_id: [] for task_id in by_id}
    indegree = {task_id: 0 for task_id in by_id}
    for task_id, task in by_id.items():
        dependencies = [str(item) for item in task["depends_on"]]
        indegree[task_id] = len(dependencies)
        for dependency in dependencies:
            dependents[dependency].append(task_id)
    ready = [task_id for task_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        task_id = heapq.heappop(ready)
        order.append(task_id)
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    starts: dict[str, int] = {}
    finishes: dict[str, int] = {}
    for task_id in order:
        dependencies = [str(item) for item in by_id[task_id]["depends_on"]]
        starts[task_id] = max((finishes[item] for item in dependencies), default=0)
        finishes[task_id] = starts[task_id] + int(by_id[task_id]["duration"])
    duration = max(finishes.values())
    latest_starts: dict[str, int] = {}
    latest_finishes: dict[str, int] = {}
    for task_id in reversed(order):
        latest_finishes[task_id] = min(
            (latest_starts[item] for item in dependents[task_id]), default=duration
        )
        latest_starts[task_id] = latest_finishes[task_id] - int(
            by_id[task_id]["duration"]
        )
    current = min(task_id for task_id, finish in finishes.items() if finish == duration)
    reversed_path = [current]
    while True:
        candidates = [
            str(item)
            for item in by_id[current]["depends_on"]
            if finishes[str(item)] == starts[current]
        ]
        if not candidates:
            break
        current = min(candidates)
        reversed_path.append(current)
    schedule = []
    for task_id in sorted(by_id):
        slack = latest_starts[task_id] - starts[task_id]
        schedule.append(
            {
                "critical": slack == 0,
                "depends_on": sorted(
                    str(item) for item in by_id[task_id]["depends_on"]
                ),
                "duration": int(by_id[task_id]["duration"]),
                "earliest_finish": finishes[task_id],
                "earliest_start": starts[task_id],
                "id": task_id,
                "latest_finish": latest_finishes[task_id],
                "latest_start": latest_starts[task_id],
                "slack": slack,
            }
        )
    return {
        "critical_path": list(reversed(reversed_path)),
        "execution_order": order,
        "project_duration": duration,
        "schedule": schedule,
        "task_count": len(tasks),
    }


def _dependency_expected(argument: dict[str, object]) -> object:
    tasks = list(argument["tasks"])
    by_id = {str(task["id"]): task for task in tasks}
    dependents = {task_id: [] for task_id in by_id}
    indegree = {task_id: 0 for task_id in by_id}
    for task_id, task in by_id.items():
        dependencies = [str(item) for item in task["depends_on"]]
        indegree[task_id] = len(dependencies)
        for dependency in dependencies:
            dependents[dependency].append(task_id)
    ready = [task_id for task_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    completion: dict[str, int] = {}
    predecessor: dict[str, str | None] = {}
    while ready:
        task_id = heapq.heappop(ready)
        order.append(task_id)
        dependencies = [str(item) for item in by_id[task_id]["depends_on"]]
        if dependencies:
            prior = min(dependencies, key=lambda item: (-completion[item], item))
            predecessor[task_id] = prior
            start = completion[prior]
        else:
            predecessor[task_id] = None
            start = 0
        completion[task_id] = start + int(by_id[task_id]["duration_minutes"])
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    finish = max(completion.values())
    current = min(task_id for task_id, value in completion.items() if value == finish)
    reversed_path = [current]
    while predecessor[current] is not None:
        current = str(predecessor[current])
        reversed_path.append(current)
    return {
        "critical_path": list(reversed(reversed_path)),
        "edge_count": sum(len(task["depends_on"]) for task in tasks),
        "minimum_completion_minutes": finish,
        "task_count": len(tasks),
        "topological_order": order,
    }


def _ledger_expected(argument: dict[str, object]) -> object:
    transactions = list(argument["transactions"])
    budgets = list(argument["category_budgets"])
    credits = sum(
        int(item["amount_cents"]) for item in transactions if item["kind"] == "credit"
    )
    debits = [item for item in transactions if item["kind"] == "debit"]
    debit_total = sum(int(item["amount_cents"]) for item in debits)
    category_spend = []
    for budget in sorted(budgets, key=lambda item: str(item["category"])):
        category = str(budget["category"])
        limit = int(budget["limit_cents"])
        spent = sum(
            int(item["amount_cents"]) for item in debits if item["category"] == category
        )
        remaining = limit - spent
        category_spend.append(
            {
                "category": category,
                "limit_cents": limit,
                "remaining_cents": remaining,
                "spent_cents": spent,
                "status": (
                    "over-budget"
                    if remaining < 0
                    else "at-limit"
                    if remaining == 0
                    else "within-budget"
                ),
            }
        )
    largest = min(
        debits, key=lambda item: (-int(item["amount_cents"]), str(item["id"]))
    )
    return {
        "account_id": argument["account_id"],
        "category_spend": category_spend,
        "closing_balance_cents": int(argument["opening_balance_cents"])
        + credits
        - debit_total,
        "credit_cents": credits,
        "debit_cents": debit_total,
        "largest_debit_id": largest["id"],
        "over_budget_categories": [
            item["category"]
            for item in category_spend
            if item["status"] == "over-budget"
        ],
        "transaction_count": len(transactions),
    }


def _dashboard_expected(argument: dict[str, object]) -> object:
    services = []
    passed = 0
    failed = 0
    for item in argument["services"]:
        risk = (
            int(item["failed_checks"]) * 25
            + int(item["critical_incidents"]) * 60
            + int(item["warning_incidents"]) * 15
            + (int(item["changed_lines"]) + 99) // 100
        )
        status = (
            "blocked"
            if int(item["failed_checks"]) or int(item["critical_incidents"])
            else "review"
            if risk >= 20
            else "ready"
        )
        services.append({"name": item["name"], "risk_points": risk, "status": status})
        passed += int(item["passed_checks"])
        failed += int(item["failed_checks"])
    services.sort(key=lambda item: (-int(item["risk_points"]), str(item["name"])))
    blocked = sum(item["status"] == "blocked" for item in services)
    review = sum(item["status"] == "review" for item in services)
    release_status = "blocked" if blocked else "review" if review else "ready"
    release = str(argument["release_id"])
    count = blocked if blocked else review
    headline = (
        f"{release_status.upper()}: {release} ({count} service"
        f"{'s' if count != 1 else ''})"
        if release_status != "ready"
        else f"READY: {release}"
    )
    return {
        "headline": headline,
        "release": release,
        "release_status": release_status,
        "services": services,
        "summary": {
            "blocked_services": blocked,
            "failed_checks": failed,
            "pass_rate_basis_points": passed * 10_000 // (passed + failed),
            "review_services": review,
            "total_risk_points": sum(int(item["risk_points"]) for item in services),
        },
        "top_risk_service": services[0]["name"],
    }


def _publication_expected(argument: dict[str, object]) -> object:
    files = dict(argument["files"])
    manifest = []
    for path in sorted(files):
        content = str(files[path]).encode("utf-8")
        manifest.append(
            {
                "digest": hashlib.sha256(content).hexdigest(),
                "path": path,
                "size": len(content),
            }
        )
    return {
        "file_count": len(manifest),
        "files": manifest,
        "release_digest": hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
    }


def _security_expected(argument: dict[str, object]) -> object:
    order = ("informational", "low", "medium", "high", "critical")
    maximum = max(
        (str(item["severity"]) for item in argument["findings"]),
        key=order.index,
    )
    acknowledged = argument["yolo_acknowledged"] is True
    decision = (
        "maximum-privilege"
        if acknowledged
        else "blocked"
        if maximum == "critical"
        else "constrained"
    )
    return {
        "decision": decision,
        "maximum_severity": maximum,
        "requested_privileges": argument["requested_privileges"],
        "warning": "YOLO MAXIMUM PRIVILEGE" if acknowledged else None,
    }


def _matrix_expected(argument: dict[str, object]) -> object:
    suffixes = {"linux": "", "macos": ".app", "windows": ".exe"}
    toolchains = {"python": "cpython", "rust": "cargo"}
    plans = [
        {
            "artifact": "sample" + suffixes[target["os"]],
            "target": f"{target['os']}-{target['accelerator']}-{target['language']}",
            "toolchain": toolchains[target["language"]],
        }
        for target in argument["targets"]
    ]
    return {"plan_count": len(plans), "plans": plans}


def _self_hosting_expected(argument: dict[str, object]) -> object:
    requested = list(argument["required_skill_ids"])
    return {
        "framework_version": "0.2.0",
        "operation": "framework-compatibility-readiness",
        "packaged_skills": sorted(requested),
        "public_api_ready": argument["expected_framework_version"] == "0.2.0",
    }


def _roundtrip_expected(argument: dict[str, object]) -> object:
    aggregated: dict[str, dict[str, int]] = {}
    for order in argument["orders"]:
        sku = str(order["sku"]).strip()
        line = aggregated.setdefault(sku, {"quantity": 0, "subtotal_cents": 0})
        line["quantity"] += int(order["quantity"])
        line["subtotal_cents"] += int(order["quantity"]) * int(order["unit_cents"])
    lines = [{"sku": sku, **aggregated[sku]} for sku in sorted(aggregated)]
    gross = sum(item["subtotal_cents"] for item in lines)
    discount = gross * int(argument["discount_basis_points"]) // 10_000
    warehouse = str(argument["warehouse"]).strip()
    slug_parts = []
    current = []
    for character in warehouse.lower():
        if character.isascii() and character.isalnum():
            current.append(character)
        elif current:
            slug_parts.append("".join(current))
            current = []
    if current:
        slug_parts.append("".join(current))
    net = gross - discount
    return {
        "warehouse": warehouse,
        "manifest_id": f"{'-'.join(slug_parts)}-{len(lines)}-{net}",
        "dominant_sku": min(lines, key=lambda item: (-item["quantity"], item["sku"]))[
            "sku"
        ],
        "line_count": len(lines),
        "gross_cents": gross,
        "discount_cents": discount,
        "net_cents": net,
        "lines": lines,
    }


def _cgroup_expected(argument: dict[str, object]) -> object:
    quota_text, period_text = str(argument["cpu_max"]).split()
    memory_text = str(argument["memory_max"])
    return {
        "mount": str(argument["mount"]).strip(),
        "cpu_quota_millicores": (
            -1 if quota_text == "max" else int(quota_text) * 1000 // int(period_text)
        ),
        "memory_limit_bytes": -1 if memory_text == "max" else int(memory_text),
        "constrained": quota_text != "max" or memory_text != "max",
    }


def _launch_agent_expected(argument: dict[str, object]) -> object:
    arguments = list(argument["arguments"])
    label = str(argument["label"])
    return {
        "bundle_id": argument["bundle_id"],
        "label": label,
        "plist_file": f"{label}.plist",
        "program": arguments[0],
        "argument_count": len(arguments),
        "run_at_load": argument["run_at_load"],
    }


def _python_service_expected(argument: dict[str, object]) -> object:
    clusters = list(argument["clusters"])
    page_size = int(argument["page_size"])
    unknown = str(argument["unknown_cluster_id"])
    known = {str(item["cluster_id"]) for item in clusters}
    page = clusters[:page_size]
    has_more = len(clusters) > page_size
    return {
        "has_more": has_more,
        "item_count": len(page),
        "next_cursor": str(page[-1]["cluster_id"]) if has_more and page else None,
        "unknown_cluster_status": 404 if unknown not in known else 200,
    }


def _react_dashboard_expected(argument: dict[str, object]) -> object:
    dataset = list(argument["dataset"])
    selected = [str(item) for item in argument["selected"]]
    deselect = str(argument["deselect"])
    return {
        "fetch_count": 1,
        "retained_row_count": len(dataset),
        "selected": [item for item in selected if item != deselect],
    }


def _windows_paths_expected(argument: dict[str, object]) -> object:
    records = []
    for original_value in argument["paths"]:
        original = str(original_value)
        converted = original.replace("/", "\\")
        unc = converted.startswith("\\\\")
        rooted = not unc and converted.startswith("\\")
        prefix = "\\\\" if unc else ("\\" if rooted else "")
        body = converted[2:] if unc else (converted[1:] if rooted else converted)
        normalized = prefix + "\\".join(part for part in body.split("\\") if part)
        if len(normalized) >= 3 and normalized[1:3] == ":\\":
            kind, root = "drive", normalized[:3]
        elif unc:
            parts = normalized[2:].split("\\")
            kind, root = "unc", "\\\\" + "\\".join(parts[:2])
        elif normalized.startswith("\\"):
            kind, root = "rooted", "\\"
        else:
            kind, root = "relative", ""
        records.append(
            {"original": original, "normalized": normalized, "kind": kind, "root": root}
        )
    return records


EXPECTED_FROM_ARGUMENTS = {
    "containerized-log-tally": _tally_expected,
    "critical-path-scheduler": _scheduler_expected,
    "cuda-vector-transform-cpp": _cuda_vector_expected,
    "cuda-vector-transform-python": _cuda_matrix_expected,
    "dependency-planner": _dependency_expected,
    "empty-cache-restart": _cache_expected,
    "flavor-matrix": _matrix_expected,
    "full-stack-rust-js": _dashboard_expected,
    "generated-library": _statistics_expected,
    "hello-component": _hello_expected,
    "javascript-ledger-workbench": _ledger_expected,
    "linux-cgroup-budget": _cgroup_expected,
    "loan-risk-gate": _loan_risk_expected,
    "macos-launch-agent": _launch_agent_expected,
    "model-routing": _routing_expected,
    "multi-repository-component": _jobs_expected,
    "playback-controller": _playback_expected,
    "publication-import": _publication_expected,
    "python-service-example": _python_service_expected,
    "react-dashboard-example": _react_dashboard_expected,
    "regenerative-roundtrip": _roundtrip_expected,
    "security-policies": _security_expected,
    "self-hosting": _self_hosting_expected,
    "service-stack": _invoice_expected,
    "windows-path-auditor": _windows_paths_expected,
}


class RuntimeOracleTests(unittest.TestCase):
    def test_full_stack_oracle_exposes_both_process_boundaries(self) -> None:
        probe = create_post_build_probe("full-stack-rust-js", entropy=FIRST_ENTROPY)
        argument = _argument(probe)
        backend_result = _release_analysis_result(argument)

        self.assertEqual(
            set(backend_result),
            {
                "blocked_services",
                "failed_checks",
                "pass_rate_basis_points",
                "passed_checks",
                "release",
                "release_status",
                "review_services",
                "services",
                "top_risk_service",
                "total_checks",
                "total_risk_points",
            },
        )
        self.assertEqual(
            _release_dashboard_from_analysis(backend_result),
            probe.expected_result,
        )

    def test_exact_sample_catalog_and_deterministic_semantics(self) -> None:
        self.assertEqual(tuple(sorted(EXPECTED_FROM_ARGUMENTS)), SUPPORTED_SAMPLE_IDS)
        self.assertEqual(
            set(SUPPORTED_SAMPLE_IDS) | CUSTOM_PORTFOLIO_SAMPLE_IDS,
            {
                path.name
                for path in (SAMPLES / "_harness").iterdir()
                if path.is_dir() and (path / "sample.json").is_file()
            },
        )
        for sample_id, expected_from_arguments in EXPECTED_FROM_ARGUMENTS.items():
            with self.subTest(sample=sample_id):
                first = create_post_build_probe(sample_id, entropy=FIRST_ENTROPY)
                repeated = create_post_build_probe(sample_id, entropy=FIRST_ENTROPY)
                second = create_post_build_probe(sample_id, entropy=SECOND_ENTROPY)
                self.assertEqual(first, repeated)
                self.assertEqual(first.case_id, RUNTIME_CASE_ID)
                self.assertEqual(
                    first.expected_result,
                    expected_from_arguments(_argument(first)),
                )
                self.assertNotEqual(first.arguments, second.arguments)
                self.assertNotEqual(first.expected_result, second.expected_result)
                canonical_json_bytes(first.arguments)
                canonical_json_bytes(first.expected_result)

    def test_probe_is_materially_distinct_from_every_persisted_case(self) -> None:
        for sample_id in SUPPORTED_SAMPLE_IDS:
            with self.subTest(sample=sample_id):
                probe = create_post_build_probe(sample_id, entropy=FIRST_ENTROPY)
                acceptance = SAMPLES / "_harness" / sample_id / "acceptance"
                interface = json.loads(
                    (acceptance / "execution.json").read_text(encoding="utf-8")
                )
                oracle = json.loads(
                    (acceptance / "oracle.json").read_text(encoding="utf-8")
                )
                self.assertNotIn(
                    probe.arguments,
                    [item["arguments"] for item in interface["invocations"]],
                )
                self.assertNotIn(
                    probe.expected_result,
                    [item["expected_result"] for item in oracle["oracle_results"]],
                )

    def test_live_default_draws_secret_entropy_only_when_called(self) -> None:
        with mock.patch(
            "tests.conformance.support.runtime_oracles.secrets.token_bytes",
            return_value=FIRST_ENTROPY,
        ) as token_bytes:
            live = create_post_build_probe("hello-component")
        explicit = create_post_build_probe("hello-component", entropy=FIRST_ENTROPY)
        self.assertEqual(live, explicit)
        token_bytes.assert_called_once_with(RUNTIME_ENTROPY_BYTES)
        self.assertFalse(hasattr(live, "entropy"))
        self.assertFalse(hasattr(live, "identity"))
        self.assertNotIn("expected_result", repr(live))
        self.assertNotIn("arguments_json", repr(live))

    def test_returned_values_are_fresh_and_cannot_mutate_the_oracle(self) -> None:
        probe = create_post_build_probe("hello-component", entropy=FIRST_ENTROPY)
        arguments = probe.arguments
        expected_result = probe.expected_result
        arguments.clear()
        assert isinstance(expected_result, dict)
        expected_result.clear()
        self.assertTrue(probe.arguments)
        self.assertTrue(probe.expected_result)

    def test_unknown_sample_and_noncanonical_entropy_fail_strictly(self) -> None:
        with mock.patch(
            "tests.conformance.support.runtime_oracles.secrets.token_bytes"
        ) as token_bytes:
            with self.assertRaisesRegex(RuntimeOracleError, "unsupported.*unknown"):
                create_post_build_probe("unknown")
        token_bytes.assert_not_called()
        invalid = (b"", b"short", b"x" * 31, b"x" * 33, bytearray(32), "x" * 32)
        for entropy in invalid:
            with self.subTest(entropy_type=type(entropy).__name__, length=len(entropy)):
                with self.assertRaisesRegex(RuntimeOracleError, "exactly 32 bytes"):
                    create_post_build_probe("hello-component", entropy=entropy)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
