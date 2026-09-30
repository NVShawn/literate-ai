"""Verifier-only, post-build generalization probes for sample conformance.

This module performs no filesystem I/O and creates no module-level random state.  The
sample runner must call :func:`create_post_build_probe` only after source generation and
compilation have completed, immediately before invoking the compiled artifact.  Neither
the entropy, derived invocation, nor expected result belongs in a Component, generation
recipe, model request, persisted runtime tree, or report preamble.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from literate_ai.contracts import canonical_json_bytes

RUNTIME_ENTROPY_BYTES = 32
RUNTIME_CASE_ID = "runtime-generalization"
_DOMAIN = b"literate-ai/runtime-generalization/v1\0"
_COMMON_LOG_PATTERN = re.compile(
    r'^\S+ \S+ \S+ \[[^\]]+\] "[A-Z]+ (\S+) HTTP/[\d.]+" (\d{3}) (\d+|-)$'
)


class RuntimeOracleError(ValueError):
    """A runtime probe request is outside the verifier's strict contract."""


@dataclass(frozen=True, slots=True)
class RuntimeOracleProbe:
    """One immutable in-memory invocation and its verifier-computed result.

    Canonical bytes are retained internally so callers cannot accidentally mutate the
    expected result before comparison.  The public properties return fresh JSON values.
    """

    sample_id: str
    case_id: str
    _arguments_json: bytes = field(repr=False)
    _expected_result_json: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.sample_id not in SUPPORTED_SAMPLE_IDS:
            raise RuntimeOracleError(
                f"unsupported runtime-oracle sample: {self.sample_id!r}"
            )
        if self.case_id != RUNTIME_CASE_ID:
            raise RuntimeOracleError("runtime-oracle case ID is not canonical")
        try:
            arguments = json.loads(self._arguments_json)
            expected_result = json.loads(self._expected_result_json)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeOracleError("runtime-oracle values are not JSON") from exc
        if not isinstance(arguments, list):
            raise RuntimeOracleError("runtime-oracle arguments must be a JSON array")
        if (
            canonical_json_bytes(arguments) != self._arguments_json
            or canonical_json_bytes(expected_result) != self._expected_result_json
        ):
            raise RuntimeOracleError("runtime-oracle values must be canonical JSON")

    @property
    def arguments(self) -> list[object]:
        """Return a fresh argument array for one host-artifact invocation."""

        value = json.loads(self._arguments_json)
        assert isinstance(value, list)
        return value

    @property
    def expected_result(self) -> object:
        """Return a fresh verifier result for canonical comparison after execution."""

        return json.loads(self._expected_result_json)


def _block(entropy: bytes, sample_id: str, label: str) -> bytes:
    return hashlib.sha256(
        _DOMAIN
        + sample_id.encode("ascii")
        + b"\0"
        + label.encode("ascii")
        + b"\0"
        + entropy
    ).digest()


def _hex(entropy: bytes, sample_id: str, label: str, length: int) -> str:
    return _block(entropy, sample_id, label).hex()[:length]


def _probe(
    sample_id: str, arguments: list[object], expected_result: object
) -> RuntimeOracleProbe:
    return RuntimeOracleProbe(
        sample_id,
        RUNTIME_CASE_ID,
        canonical_json_bytes(arguments),
        canonical_json_bytes(expected_result),
    )


def _hello_component(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "hello-component"
    material = _block(entropy, sample_id, "case")
    token = material[:6].hex()
    name = f"Runtime {token} Probe"
    messages: list[str] = []
    for index in range(2 + material[12] % 4):
        message = _block(entropy, sample_id, f"message-{index}")
        word_count = 2 + message[0] % 4
        words = [
            f"w{index}{word}-{message[1 + 2 * word : 3 + 2 * word].hex()}"
            for word in range(word_count)
        ]
        messages.append(" ".join(words))
    argument = {"messages": messages, "name": name}
    return _probe(
        sample_id,
        [argument],
        {
            "greeting": f"Hello, {name}!",
            "message_count": len(messages),
            "recipient_id": f"runtime-{token}-probe",
            "word_count": sum(len(message.split()) for message in messages),
        },
    )


_LOAN_RISK_RULES = (
    ((18, 25), (0, 30000), "high"),
    ((18, 25), (30001, 999999), "medium"),
    ((26, 55), (0, 40000), "medium"),
    ((26, 55), (40001, 999999), "low"),
    ((56, 120), None, "low"),
)
_LOAN_RISK_BANDS = (
    ("starter", 0, 30000),
    ("established", 30001, 40000),
    ("prime", 40001, 999999),
)


def _loan_risk_result(age: int, income: int) -> dict[str, object]:
    category = next(
        (
            risk
            for ages, incomes, risk in _LOAN_RISK_RULES
            if ages[0] <= age <= ages[1]
            and (incomes is None or incomes[0] <= income <= incomes[1])
        ),
        None,
    )
    if category is None:
        raise RuntimeOracleError("loan-risk-gate inputs miss the UNIQUE table")
    band = next(
        identifier
        for identifier, lower, upper in _LOAN_RISK_BANDS
        if lower <= income <= upper
    )
    return {
        "age": age,
        "income": income,
        "risk_category": category,
        "score_band": band,
    }


def _loan_risk_gate(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "loan-risk-gate"
    material = _block(entropy, sample_id, "case")
    selector = material[0] % 3
    if selector == 0:
        age = 18 + material[1] % 8
        income = material[2] % 20000
    elif selector == 1:
        age = 26 + material[1] % 30
        income = 30001 + material[2] % 9000
    else:
        age = 56 + material[1] % 50
        income = 40001 + int.from_bytes(material[2:4], "big") % 100000
    argument = {"age": age, "income": income}
    return _probe(sample_id, [argument], _loan_risk_result(age, income))


def _playback_step(
    leaves: set[str], powered: bool, history: tuple[str, ...], event: str
) -> tuple[set[str], bool, tuple[str, ...]]:
    if not powered:
        if event == "resume":
            restored = set(history) if history else {"audible", "stopped"}
            return restored, True, history
        return leaves, powered, history
    if event == "power":
        return {"off"}, False, tuple(sorted(leaf for leaf in leaves))
    if event == "play" and "stopped" in leaves:
        return (leaves - {"stopped"}) | {"playing"}, True, history
    if event == "mute" and "audible" in leaves:
        return (leaves - {"audible"}) | {"muted"}, True, history
    return leaves, powered, history


def _playback_result(events: list[str]) -> dict[str, object]:
    leaves = {"audible", "stopped"}
    powered = True
    history: tuple[str, ...] = ()
    for event in events:
        leaves, powered, history = _playback_step(leaves, powered, history, event)
    active = ["off"] if not powered else sorted(leaves)
    return {
        "active": active,
        "event_count": len(events),
        "powered": powered,
    }


def _playback_controller(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "playback-controller"
    material = _block(entropy, sample_id, "case")
    extra_mutes = int.from_bytes(material, "big") % 10007
    events = ["mute"] + ["mute"] * extra_mutes
    argument = {"events": events}
    return _probe(sample_id, [argument], _playback_result(events))


def _generated_library(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "generated-library"
    material = _block(entropy, sample_id, "case")
    base = 1 + int.from_bytes(material[:2], "big") % 1_000
    first_gap = 2 * (material[2] % 16) + 1
    second_gap = 2 * (material[3] % 16) + 1
    third_gap = 2 * (material[4] % 16 + 1)
    ordered = [
        base,
        base + first_gap,
        base + first_gap + second_gap,
        base + first_gap + second_gap + third_gap,
    ]
    rotation = 1 + material[5] % 3
    values = ordered[rotation:] + ordered[:rotation]
    total = sum(ordered)
    return _probe(
        sample_id,
        [{"values": values}],
        {
            "count": 4,
            "maximum": ordered[-1],
            "mean": {"denominator": 4, "numerator": total},
            "median": {
                "denominator": 2,
                "numerator": ordered[1] + ordered[2],
            },
            "minimum": ordered[0],
            "total": total,
        },
    )


def _cuda_vector_transform_cpp(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "cuda-vector-transform-cpp"
    material = _block(entropy, sample_id, "case")
    values = [int(material[index]) - 128 for index in range(7)]
    multiplier = int(material[8] % 11) - 5
    if multiplier == 0:
        multiplier = 7
    bias = int(material[9]) - 128
    transformed = [value * multiplier + bias for value in values]
    return _probe(
        sample_id,
        [{"values": values, "multiplier": multiplier, "bias": bias}],
        {
            "backend": "cuda",
            "device_executed": True,
            "count": len(transformed),
            "values": transformed,
            "checksum": sum(transformed),
        },
    )


def _cuda_vector_transform_python(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "cuda-vector-transform-python"
    material = _block(entropy, sample_id, "case")
    left = [
        [int(material[row * 3 + column]) % 17 - 8 for column in range(3)]
        for row in range(2)
    ]
    right = [
        [int(material[6 + row * 4 + column]) % 17 - 8 for column in range(4)]
        for row in range(3)
    ]
    values = [
        [
            sum(left[row][inner] * right[inner][column] for inner in range(3))
            for column in range(4)
        ]
        for row in range(2)
    ]
    return _probe(
        sample_id,
        [{"left": left, "right": right}],
        {
            "backend": "cupy-cuda",
            "device_executed": True,
            "rows": 2,
            "columns": 4,
            "values": values,
            "checksum": sum(sum(row) for row in values),
        },
    )


def _service_stack(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "service-stack"
    material = _block(entropy, sample_id, "case")
    items: list[dict[str, object]] = []
    for index in range(2 + material[0] % 3):
        item = _block(entropy, sample_id, f"item-{index}")
        items.append(
            {
                "quantity": 1 + item[5] % 7,
                "sku": f"sku-{index}-{item[:5].hex()}",
                "unit_price_cents": 25 + int.from_bytes(item[6:8], "big") % 4_976,
            }
        )
    discount_basis_points = 1 + int.from_bytes(material[1:3], "big") % 5_000
    subtotal = sum(
        int(item["quantity"]) * int(item["unit_price_cents"]) for item in items
    )
    discount = (subtotal * discount_basis_points + 5_000) // 10_000
    return _probe(
        sample_id,
        [
            {
                "discount_basis_points": discount_basis_points,
                "items": items,
            }
        ],
        {
            "discount_cents": discount,
            "line_count": len(items),
            "subtotal_cents": subtotal,
            "total_cents": subtotal - discount,
            "unit_count": sum(int(item["quantity"]) for item in items),
        },
    )


def _multi_repository_component(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "multi-repository-component"
    material = _block(entropy, sample_id, "case")
    jobs: list[dict[str, str]] = []
    for index in range(2 + material[0] % 3):
        job = _block(entropy, sample_id, f"job-{index}")
        word_count = 1 + job[6] % 5
        text = " ".join(
            f"w{index}{word}-{job[7 + 2 * word : 9 + 2 * word].hex()}"
            for word in range(word_count)
        )
        jobs.append({"id": f"job-{index}-{job[:6].hex()}", "text": text})
    results = [
        {
            "characters": len(job["text"]),
            "digest": hashlib.sha256(job["text"].encode("utf-8")).hexdigest()[:12],
            "id": job["id"],
            "words": len(job["text"].split()),
        }
        for job in jobs
    ]
    return _probe(
        sample_id,
        [{"jobs": jobs}],
        {
            "completed": len(jobs),
            "results": results,
            "total_characters": sum(int(result["characters"]) for result in results),
        },
    )


def _model_routing(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "model-routing"
    material = _block(entropy, sample_id, "case")
    required = [
        f"code-{_hex(entropy, sample_id, 'capability-code', 8)}",
        f"structured-{_hex(entropy, sample_id, 'capability-structured', 8)}",
    ]
    endpoints: list[dict[str, object]] = []
    rejection_kinds = ("unavailable", "remote", "incomplete")
    rejection_count = 1 + material[0] % 4
    for index in range(rejection_count):
        kind = rejection_kinds[index % len(rejection_kinds)]
        endpoint_token = _hex(entropy, sample_id, f"endpoint-{index}", 10)
        endpoint = {
            "available": kind != "unavailable",
            "capabilities": required if kind != "incomplete" else required[:1],
            "id": f"rejected-{index}-{endpoint_token}",
            "locality": "remote" if kind == "remote" else "local",
        }
        endpoints.append(endpoint)
    selected_id = f"selected-{_hex(entropy, sample_id, 'selected', 12)}"
    endpoints.append(
        {
            "available": True,
            "capabilities": [required[1], "extra", required[0]],
            "id": selected_id,
            "locality": "local",
        }
    )
    endpoints.append(
        {
            "available": True,
            "capabilities": required,
            "id": f"trailing-{_hex(entropy, sample_id, 'trailing', 12)}",
            "locality": "local",
        }
    )
    rejected_ids = [str(endpoint["id"]) for endpoint in endpoints[:rejection_count]]
    return _probe(
        sample_id,
        [{"endpoints": endpoints, "required_capabilities": required}],
        {
            "considered": [*rejected_ids, selected_id],
            "data_egress": "none",
            "fallback_used": True,
            "rejected": rejected_ids,
            "selected": selected_id,
        },
    )


def _empty_cache_restart(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "empty-cache-restart"
    material = _block(entropy, sample_id, "case").hex()
    first = f"probe:{material[:16]}"
    second = f"probe:{material[16:32]}"
    records = [first, second, first, f"tail:{material[32:]}"]
    identities = [
        hashlib.sha256(record.encode("utf-8")).hexdigest() for record in records
    ]
    return _probe(
        sample_id,
        [{"records": records}],
        {
            "identities": identities,
            "object_count": len(set(identities)),
            "recovered": records,
            "restart_verified": True,
        },
    )


def _critical_path_result(tasks: list[dict[str, object]]) -> dict[str, object]:
    """Compute the verifier's independent deterministic CPM result."""

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
    execution_order: list[str] = []
    while ready:
        task_id = heapq.heappop(ready)
        execution_order.append(task_id)
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    earliest_start: dict[str, int] = {}
    earliest_finish: dict[str, int] = {}
    for task_id in execution_order:
        dependencies = [str(item) for item in by_id[task_id]["depends_on"]]
        start = max((earliest_finish[item] for item in dependencies), default=0)
        earliest_start[task_id] = start
        earliest_finish[task_id] = start + int(by_id[task_id]["duration"])
    project_duration = max(earliest_finish.values())

    latest_start: dict[str, int] = {}
    latest_finish: dict[str, int] = {}
    for task_id in reversed(execution_order):
        finish = min(
            (latest_start[item] for item in dependents[task_id]),
            default=project_duration,
        )
        latest_finish[task_id] = finish
        latest_start[task_id] = finish - int(by_id[task_id]["duration"])

    current = min(
        task_id
        for task_id, finish in earliest_finish.items()
        if finish == project_duration
    )
    reversed_path = [current]
    while True:
        candidates = [
            str(item)
            for item in by_id[current]["depends_on"]
            if earliest_finish[str(item)] == earliest_start[current]
        ]
        if not candidates:
            break
        current = min(candidates)
        reversed_path.append(current)

    schedule = []
    for task_id in sorted(by_id):
        dependencies = sorted(str(item) for item in by_id[task_id]["depends_on"])
        slack = latest_start[task_id] - earliest_start[task_id]
        schedule.append(
            {
                "critical": slack == 0,
                "depends_on": dependencies,
                "duration": int(by_id[task_id]["duration"]),
                "earliest_finish": earliest_finish[task_id],
                "earliest_start": earliest_start[task_id],
                "id": task_id,
                "latest_finish": latest_finish[task_id],
                "latest_start": latest_start[task_id],
                "slack": slack,
            }
        )
    return {
        "critical_path": list(reversed(reversed_path)),
        "execution_order": execution_order,
        "project_duration": project_duration,
        "schedule": schedule,
        "task_count": len(tasks),
    }


def _critical_path_scheduler(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "critical-path-scheduler"
    material = _block(entropy, sample_id, "case")
    token = material[:4].hex()
    ids = {
        name: f"{name}-{token}"
        for name in ("brief", "backend", "frontend", "audit", "join", "release")
    }
    tasks: list[dict[str, object]] = [
        {
            "id": ids["brief"],
            "duration": 1 + material[4] % 7,
            "depends_on": [],
        },
        {
            "id": ids["backend"],
            "duration": 2 + material[5] % 9,
            "depends_on": [ids["brief"]],
        },
        {
            "id": ids["frontend"],
            "duration": 2 + material[6] % 9,
            "depends_on": [ids["brief"]],
        },
        {
            "id": ids["audit"],
            "duration": 1 + material[7] % 5,
            "depends_on": [ids["backend"]],
        },
        {
            "id": ids["join"],
            "duration": 1 + material[8] % 6,
            "depends_on": [ids["frontend"], ids["backend"]],
        },
        {
            "id": ids["release"],
            "duration": 1 + material[9] % 4,
            "depends_on": [ids["audit"], ids["join"]],
        },
    ]
    tasks = [
        next(task for task in tasks if task["id"] == task_id)
        for task_id in _entropy_order(
            entropy,
            sample_id,
            "task-order",
            [str(task["id"]) for task in tasks],
        )
    ]
    return _probe(
        sample_id,
        [{"tasks": tasks}],
        _critical_path_result(tasks),
    )


def _dependency_plan_result(tasks: list[dict[str, object]]) -> dict[str, object]:
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
            prior = min(
                dependencies,
                key=lambda item: (-completion[item], item),
            )
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


def _dependency_planner(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "dependency-planner"
    material = _block(entropy, sample_id, "case")
    token = material[:4].hex()
    ids = {
        name: f"{name}-{token}"
        for name in ("ingest", "parse", "index", "audit", "publish", "notify")
    }
    tasks: list[dict[str, object]] = [
        {
            "id": ids["ingest"],
            "duration_minutes": 1 + material[4] % 8,
            "depends_on": [],
        },
        {
            "id": ids["parse"],
            "duration_minutes": 2 + material[5] % 10,
            "depends_on": [ids["ingest"]],
        },
        {
            "id": ids["index"],
            "duration_minutes": 2 + material[6] % 10,
            "depends_on": [ids["parse"]],
        },
        {
            "id": ids["audit"],
            "duration_minutes": 2 + material[7] % 10,
            "depends_on": [ids["ingest"]],
        },
        {
            "id": ids["publish"],
            "duration_minutes": 1 + material[8] % 7,
            "depends_on": [ids["index"], ids["audit"]],
        },
        {
            "id": ids["notify"],
            "duration_minutes": 1 + material[9] % 5,
            "depends_on": [ids["publish"]],
        },
    ]
    task_ids = _entropy_order(
        entropy,
        sample_id,
        "task-order",
        [str(task["id"]) for task in tasks],
    )
    tasks = [
        next(task for task in tasks if task["id"] == task_id) for task_id in task_ids
    ]
    return _probe(
        sample_id,
        [{"tasks": tasks}],
        _dependency_plan_result(tasks),
    )


def _ledger_result(argument: dict[str, object]) -> dict[str, object]:
    transactions = list(argument["transactions"])
    budgets = list(argument["category_budgets"])
    credits = sum(
        int(item["amount_cents"]) for item in transactions if item["kind"] == "credit"
    )
    debits = [item for item in transactions if item["kind"] == "debit"]
    debit_total = sum(int(item["amount_cents"]) for item in debits)
    spending = {
        str(budget["category"]): sum(
            int(item["amount_cents"])
            for item in debits
            if item["category"] == budget["category"]
        )
        for budget in budgets
    }
    limits = {str(budget["category"]): int(budget["limit_cents"]) for budget in budgets}
    category_spend = []
    for category in sorted(limits):
        spent = spending[category]
        remaining = limits[category] - spent
        status = (
            "over-budget"
            if remaining < 0
            else "at-limit"
            if remaining == 0
            else "within-budget"
        )
        category_spend.append(
            {
                "category": category,
                "limit_cents": limits[category],
                "remaining_cents": remaining,
                "spent_cents": spent,
                "status": status,
            }
        )
    largest = min(
        debits,
        key=lambda item: (-int(item["amount_cents"]), str(item["id"])),
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


def _javascript_ledger_workbench(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "javascript-ledger-workbench"
    material = _block(entropy, sample_id, "case")
    token = material[:5].hex()
    food_limit = 2_000 + int.from_bytes(material[5:7], "big") % 20_000
    travel_limit = 5_000 + int.from_bytes(material[7:9], "big") % 30_000
    tied_debit = travel_limit + 1 + material[9] % 5_000
    argument: dict[str, object] = {
        "account_id": f"runtime-{token}",
        "opening_balance_cents": 10_000 + int.from_bytes(material[10:13], "big"),
        "transactions": [
            {
                "id": f"income-{token}",
                "kind": "credit",
                "amount_cents": 50_000 + int.from_bytes(material[13:16], "big"),
                "category": "income",
            },
            {
                "id": f"z-travel-{token}",
                "kind": "debit",
                "amount_cents": tied_debit,
                "category": "travel",
            },
            {
                "id": f"a-travel-{token}",
                "kind": "debit",
                "amount_cents": tied_debit,
                "category": "travel",
            },
            {
                "id": f"food-{token}",
                "kind": "debit",
                "amount_cents": food_limit,
                "category": "food",
            },
        ],
        "category_budgets": [
            {"category": "travel", "limit_cents": travel_limit},
            {"category": "food", "limit_cents": food_limit},
        ],
    }
    return _probe(sample_id, [argument], _ledger_result(argument))


def _manifest_slug(value: str) -> str:
    result: list[str] = []
    separated = True
    for character in value.strip().lower():
        if character.isascii() and character.isalnum():
            result.append(character)
            separated = False
        elif not separated:
            result.append("-")
            separated = True
    return "".join(result).strip("-")


def _regenerative_roundtrip_result(argument: dict[str, object]) -> dict[str, object]:
    aggregated: dict[str, dict[str, int]] = {}
    for raw in argument["orders"]:
        order = dict(raw)
        sku = str(order["sku"]).strip()
        line = aggregated.setdefault(sku, {"quantity": 0, "subtotal_cents": 0})
        line["quantity"] += int(order["quantity"])
        line["subtotal_cents"] += int(order["quantity"]) * int(order["unit_cents"])
    lines = [{"sku": sku, **aggregated[sku]} for sku in sorted(aggregated)]
    gross = sum(item["subtotal_cents"] for item in lines)
    discount = gross * int(argument["discount_basis_points"]) // 10_000
    net = gross - discount
    dominant = min(lines, key=lambda item: (-item["quantity"], item["sku"]))["sku"]
    warehouse = str(argument["warehouse"]).strip()
    return {
        "warehouse": warehouse,
        "manifest_id": f"{_manifest_slug(warehouse)}-{len(lines)}-{net}",
        "dominant_sku": dominant,
        "line_count": len(lines),
        "gross_cents": gross,
        "discount_cents": discount,
        "net_cents": net,
        "lines": lines,
    }


def _regenerative_roundtrip(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "regenerative-roundtrip"
    material = _block(entropy, sample_id, "case")
    first_quantity = 1 + material[0] % 8
    second_quantity = 1 + material[1] % 8
    argument: dict[str, object] = {
        "warehouse": f" Runtime / Depot {material[2]:02x} ",
        "discount_basis_points": 1 + int.from_bytes(material[3:5], "big") % 9_999,
        "orders": [
            {
                "sku": f" R-{material[5]:02X} ",
                "quantity": first_quantity,
                "unit_cents": 1 + int.from_bytes(material[6:8], "big"),
            },
            {
                "sku": f"A-{material[8]:02X}",
                "quantity": second_quantity,
                "unit_cents": 1 + int.from_bytes(material[9:11], "big"),
            },
            {
                "sku": f"R-{material[5]:02X}",
                "quantity": 1 + material[11] % 4,
                "unit_cents": 1 + int.from_bytes(material[12:14], "big"),
            },
        ],
    }
    return _probe(
        sample_id,
        [argument],
        _regenerative_roundtrip_result(argument),
    )


def _release_analysis_result(argument: dict[str, object]) -> dict[str, object]:
    """Independently calculate the Rust backend's observable JSON boundary."""

    service_results = []
    passed = 0
    failed = 0
    for service in argument["services"]:
        risk = (
            int(service["failed_checks"]) * 25
            + int(service["critical_incidents"]) * 60
            + int(service["warning_incidents"]) * 15
            + (int(service["changed_lines"]) + 99) // 100
        )
        status = (
            "blocked"
            if int(service["failed_checks"]) > 0
            or int(service["critical_incidents"]) > 0
            else "review"
            if risk >= 20
            else "ready"
        )
        service_results.append(
            {"name": service["name"], "risk_points": risk, "status": status}
        )
        passed += int(service["passed_checks"])
        failed += int(service["failed_checks"])
    service_results.sort(
        key=lambda item: (-int(item["risk_points"]), str(item["name"]))
    )
    blocked = sum(item["status"] == "blocked" for item in service_results)
    review = sum(item["status"] == "review" for item in service_results)
    status = "blocked" if blocked else "review" if review else "ready"
    release = str(argument["release_id"])
    return {
        "release": release,
        "release_status": status,
        "services": service_results,
        "passed_checks": passed,
        "failed_checks": failed,
        "total_checks": passed + failed,
        "blocked_services": blocked,
        "review_services": review,
        "total_risk_points": sum(int(item["risk_points"]) for item in service_results),
        "pass_rate_basis_points": passed * 10_000 // (passed + failed),
        "top_risk_service": service_results[0]["name"],
    }


def _release_dashboard_from_analysis(
    analysis: dict[str, object],
) -> dict[str, object]:
    """Independently calculate the JavaScript view over an observed backend value."""

    status = str(analysis["release_status"])
    blocked = int(analysis["blocked_services"])
    review = int(analysis["review_services"])
    count = blocked if blocked else review
    release = str(analysis["release"])
    headline = (
        f"{status.upper()}: {release} ({count} service{'s' if count != 1 else ''})"
        if status != "ready"
        else f"READY: {release}"
    )
    return {
        "headline": headline,
        "release": release,
        "release_status": status,
        "services": analysis["services"],
        "summary": {
            "blocked_services": blocked,
            "failed_checks": analysis["failed_checks"],
            "pass_rate_basis_points": analysis["pass_rate_basis_points"],
            "review_services": review,
            "total_risk_points": analysis["total_risk_points"],
        },
        "top_risk_service": analysis["top_risk_service"],
    }


def _release_dashboard_result(argument: dict[str, object]) -> dict[str, object]:
    return _release_dashboard_from_analysis(_release_analysis_result(argument))


def _full_stack_rust_js(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "full-stack-rust-js"
    material = _block(entropy, sample_id, "case")
    token = material[:4].hex()
    argument: dict[str, object] = {
        "release_id": f"runtime-{token}",
        "services": [
            {
                "name": f"api-{token}",
                "passed_checks": 50 + material[4],
                "failed_checks": 1 + material[5] % 3,
                "critical_incidents": material[6] % 2,
                "warning_incidents": material[7] % 3,
                "changed_lines": 50 + int.from_bytes(material[8:10], "big") % 2_000,
            },
            {
                "name": f"web-{token}",
                "passed_checks": 50 + material[10],
                "failed_checks": 0,
                "critical_incidents": 0,
                "warning_incidents": 1 + material[11] % 3,
                "changed_lines": 500 + int.from_bytes(material[12:14], "big") % 2_000,
            },
            {
                "name": f"worker-{token}",
                "passed_checks": 50 + material[14],
                "failed_checks": 0,
                "critical_incidents": 0,
                "warning_incidents": 0,
                "changed_lines": 1 + material[15] % 99,
            },
        ],
    }
    return _probe(sample_id, [argument], _release_dashboard_result(argument))


def _publication_import(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "publication-import"
    material = _block(entropy, sample_id, "case").hex()
    files = {
        "payload/empty.txt": "",
        f"payload/{material[:16]}.txt": f"nonce={material}\n",
        f"payload/{material[16:32]}.json": ('{"nonce":"' + material[32:] + '"}\n'),
    }
    manifest = []
    for path in sorted(files):
        content = files[path].encode("utf-8")
        manifest.append(
            {
                "digest": hashlib.sha256(content).hexdigest(),
                "path": path,
                "size": len(content),
            }
        )
    return _probe(
        sample_id,
        [{"files": files}],
        {
            "file_count": len(manifest),
            "files": manifest,
            "release_digest": hashlib.sha256(
                canonical_json_bytes(manifest)
            ).hexdigest(),
        },
    )


def _security_policies(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "security-policies"
    material = _block(entropy, sample_id, "case")
    optional_privileges = (
        "devices",
        "host-filesystem",
        "network",
        "package-manager",
        "processes",
        "sandbox-escape",
        "secrets",
    )
    privilege = optional_privileges[material[0] % len(optional_privileges)]
    requested_privileges = sorted(("compiler", privilege))
    findings = [
        {
            "id": f"review-{material[1:9].hex()}",
            "severity": "high",
        },
        {
            "id": f"critical-{material[9:17].hex()}",
            "severity": "critical",
        },
    ]
    return _probe(
        sample_id,
        [
            {
                "findings": findings,
                "requested_privileges": requested_privileges,
                "yolo_acknowledged": True,
            }
        ],
        {
            "decision": "maximum-privilege",
            "maximum_severity": "critical",
            "requested_privileges": requested_privileges,
            "warning": "YOLO MAXIMUM PRIVILEGE",
        },
    )


def _entropy_order(
    entropy: bytes, sample_id: str, label: str, values: Sequence[str]
) -> list[str]:
    return sorted(
        values,
        key=lambda value: _block(entropy, sample_id, f"{label}-{value}"),
    )


def _flavor_matrix(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "flavor-matrix"
    operating_systems = ("linux", "macos", "windows")
    accelerators = ("cpu", "cuda")
    languages = ("python", "rust")
    combinations = [
        f"{operating_system}:{accelerator}:{language}"
        for operating_system in operating_systems
        for accelerator in accelerators
        for language in languages
    ]
    material = _block(entropy, sample_id, "case")
    start = material[0] % len(combinations)
    step = (1, 5, 7, 11)[material[1] % 4]
    selected = [
        combinations[(start + index * step) % len(combinations)] for index in range(4)
    ]
    targets = [
        {"accelerator": accelerator, "language": language, "os": operating_system}
        for operating_system, accelerator, language in (
            item.split(":") for item in selected
        )
    ]
    suffixes = {"linux": "", "macos": ".app", "windows": ".exe"}
    toolchains = {"python": "cpython", "rust": "cargo"}
    plans = [
        {
            "artifact": "sample" + suffixes[str(target["os"])],
            "target": (f"{target['os']}-{target['accelerator']}-{target['language']}"),
            "toolchain": toolchains[str(target["language"])],
        }
        for target in targets
    ]
    return _probe(
        sample_id,
        [{"targets": targets}],
        {"plan_count": len(plans), "plans": plans},
    )


def _linux_cgroup_budget(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "linux-cgroup-budget"
    material = _block(entropy, sample_id, "case")
    period = 10_000 + int.from_bytes(material[:2], "big")
    quota = period + int.from_bytes(material[2:4], "big")
    memory = 1_048_576 + int.from_bytes(material[4:8], "big")
    mount = f" /sys/fs/cgroup/{material[8:12].hex()} "
    return _probe(
        sample_id,
        [{"mount": mount, "cpu_max": f"{quota} {period}", "memory_max": str(memory)}],
        {
            "mount": mount.strip(),
            "cpu_quota_millicores": quota * 1000 // period,
            "memory_limit_bytes": memory,
            "constrained": True,
        },
    )


def _macos_launch_agent(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "macos-launch-agent"
    token = _hex(entropy, sample_id, "label", 12)
    label = f"dev.literate.runtime-{token}"
    program = f"/opt/literate/bin/worker-{token[:6]}"
    arguments = [program, "--identity", token, "--once"]
    run_at_load = bool(_block(entropy, sample_id, "policy")[0] & 1)
    return _probe(
        sample_id,
        [
            {
                "bundle_id": f"dev.literate.bundle-{token}",
                "label": label,
                "arguments": arguments,
                "run_at_load": run_at_load,
            }
        ],
        {
            "bundle_id": f"dev.literate.bundle-{token}",
            "label": label,
            "plist_file": f"{label}.plist",
            "program": program,
            "argument_count": len(arguments),
            "run_at_load": run_at_load,
        },
    )


def _windows_path_auditor(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "windows-path-auditor"
    token = _hex(entropy, sample_id, "path", 10)
    drive = chr(ord("A") + _block(entropy, sample_id, "drive")[0] % 26)
    paths = [
        f"{drive}:/work//{token}/artifact.bin",
        f"//server-{token[:4]}/share-{token[4:]}///logs",
        f"/rooted//{token}",
        f"relative//{token}",
    ]
    return _probe(
        sample_id,
        [{"paths": paths}],
        [
            {
                "original": paths[0],
                "normalized": f"{drive}:\\work\\{token}\\artifact.bin",
                "kind": "drive",
                "root": f"{drive}:\\",
            },
            {
                "original": paths[1],
                "normalized": f"\\\\server-{token[:4]}\\share-{token[4:]}\\logs",
                "kind": "unc",
                "root": f"\\\\server-{token[:4]}\\share-{token[4:]}",
            },
            {
                "original": paths[2],
                "normalized": f"\\rooted\\{token}",
                "kind": "rooted",
                "root": "\\",
            },
            {
                "original": paths[3],
                "normalized": f"relative\\{token}",
                "kind": "relative",
                "root": "",
            },
        ],
    )


_FRAMEWORK_VERSION = "0.2.0"
_PACKAGED_FRAMEWORK_SKILLS = frozenset(
    {
        "architecture",
        "api-surface",
        "behavior-state",
        "tests",
        "security",
        "operations",
    }
)


def _framework_readiness_result(argument: dict[str, object]) -> dict[str, object]:
    requested_version = str(
        argument.get("expected_framework_version", _FRAMEWORK_VERSION)
    )
    requested_skills = {
        str(item)
        for item in argument.get(
            "required_skill_ids", sorted(_PACKAGED_FRAMEWORK_SKILLS)
        )
    }
    available = sorted(requested_skills & _PACKAGED_FRAMEWORK_SKILLS)
    return {
        "framework_version": _FRAMEWORK_VERSION,
        "operation": "framework-compatibility-readiness",
        "packaged_skills": available,
        "public_api_ready": (
            requested_version == _FRAMEWORK_VERSION
            and requested_skills <= _PACKAGED_FRAMEWORK_SKILLS
        ),
    }


def _log_tally_result(argument: dict[str, object]) -> dict[str, object]:
    counts = {"status_2xx": 0, "status_3xx": 0, "status_4xx": 0, "status_5xx": 0}
    path_hits: dict[str, int] = {}
    parsed_lines = 0
    malformed_lines = 0
    bytes_total = 0
    access_log = argument["access_log"]
    if not isinstance(access_log, list):
        raise RuntimeOracleError("log tally access_log must be an array")
    for raw_line in access_log:
        if not isinstance(raw_line, str):
            raise RuntimeOracleError("log tally lines must be strings")
        match = _COMMON_LOG_PATTERN.match(raw_line)
        if match is None:
            malformed_lines += 1
            continue
        path, raw_status, raw_size = match.groups()
        status = int(raw_status)
        parsed_lines += 1
        counts[f"status_{status // 100}xx"] += 1
        path_hits[path] = path_hits.get(path, 0) + 1
        bytes_total += 0 if raw_size == "-" else int(raw_size)
    top_count = max(0, min(int(argument["top_count"]), 100))
    ranked_paths = sorted(path_hits.items(), key=lambda item: (-item[1], item[0]))
    return {
        "total_lines": len(access_log),
        "parsed_lines": parsed_lines,
        "malformed_lines": malformed_lines,
        **counts,
        "bytes_total": bytes_total,
        "top_paths": [
            {"path": path, "hits": hits} for path, hits in ranked_paths[:top_count]
        ],
        "report_version": "1.0",
    }


def _containerized_log_tally(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "containerized-log-tally"
    material = _block(entropy, sample_id, "case")
    line_count = 2 + material[0] % 6
    malformed_count = material[1] % 2
    top_count = 1 + material[2] % 3
    paths = [f"/p{material[3 + index] % 4}" for index in range(3)]
    statuses = [200, 301, 404, 500]
    access_log: list[str] = []
    for index in range(line_count):
        material_line = _block(entropy, sample_id, f"line-{index}")
        status = statuses[material_line[0] % len(statuses)]
        path = paths[material_line[1] % len(paths)]
        size = (material_line[2] % 5) * 10
        access_log.append(
            f"10.0.0.{index % 250} - - [22/Aug/2026:10:00:{index:02d} +0000] "
            f'"GET {path} HTTP/1.1" {status} {size}'
        )
    for index in range(malformed_count):
        access_log.append(f"malformed-{index}")
    arguments = {"access_log": access_log, "top_count": top_count}
    return _probe(
        sample_id,
        [arguments],
        _log_tally_result(arguments),
    )


_CLUSTER_HEALTHS = ("degraded", "healthy", "unhealthy")
_DASHBOARD_STATISTICS = ("cpu_utilization", "memory_utilization")


def _python_service_example_result(argument: dict[str, object]) -> dict[str, object]:
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


def _python_service_example(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "python-service-example"
    material = _block(entropy, sample_id, "case")
    cluster_count = 3 + material[0] % 3
    page_size = 1 + material[1] % 2
    clusters = []
    for index in range(cluster_count):
        chunk = _block(entropy, sample_id, f"cluster-{index}")
        clusters.append(
            {
                "cluster_id": f"rt-{chunk[:4].hex()}",
                "health": _CLUSTER_HEALTHS[chunk[4] % len(_CLUSTER_HEALTHS)],
            }
        )
    argument = {
        "clusters": clusters,
        "page_size": page_size,
        "unknown_cluster_id": (
            f"missing-{_block(entropy, sample_id, 'unknown')[:4].hex()}"
        ),
    }
    return _probe(sample_id, [argument], _python_service_example_result(argument))


def _react_dashboard_example_result(argument: dict[str, object]) -> dict[str, object]:
    dataset = list(argument["dataset"])
    selected = [str(item) for item in argument["selected"]]
    deselect = str(argument["deselect"])
    return {
        "fetch_count": 1,
        "retained_row_count": len(dataset),
        "selected": [item for item in selected if item != deselect],
    }


def _react_dashboard_example(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "react-dashboard-example"
    material = _block(entropy, sample_id, "case")
    row_count = 2 + material[0] % 3
    dataset = []
    for index in range(row_count):
        chunk = _block(entropy, sample_id, f"row-{index}")
        dataset.append(
            {
                "cluster_id": f"rt-{chunk[:4].hex()}",
                "cpu_utilization": int(chunk[4]) % 101,
                "memory_utilization": int(chunk[5]) % 101,
            }
        )
    selected = list(_DASHBOARD_STATISTICS)
    argument = {
        "dataset": dataset,
        "deselect": _DASHBOARD_STATISTICS[material[1] % 2],
        "selected": selected,
    }
    return _probe(sample_id, [argument], _react_dashboard_example_result(argument))


def _framework_readiness(entropy: bytes) -> RuntimeOracleProbe:
    sample_id = "self-hosting"
    material = _block(entropy, sample_id, "case")
    packaged_skills = tuple(sorted(_PACKAGED_FRAMEWORK_SKILLS))
    ordered = _entropy_order(entropy, sample_id, "skill", packaged_skills)
    required_skills = ordered[: 3 + material[0] % 2]
    expected_version = "0.2.0" if material[1] & 1 else "0.2.1"
    return _probe(
        sample_id,
        [
            {
                "expected_framework_version": expected_version,
                "required_skill_ids": required_skills,
            }
        ],
        _framework_readiness_result(
            {
                "expected_framework_version": expected_version,
                "required_skill_ids": required_skills,
            }
        ),
    )


_ProbeBuilder = Callable[[bytes], RuntimeOracleProbe]
_BUILDERS: dict[str, _ProbeBuilder] = {
    "containerized-log-tally": _containerized_log_tally,
    "critical-path-scheduler": _critical_path_scheduler,
    "cuda-vector-transform-cpp": _cuda_vector_transform_cpp,
    "cuda-vector-transform-python": _cuda_vector_transform_python,
    "dependency-planner": _dependency_planner,
    "empty-cache-restart": _empty_cache_restart,
    "flavor-matrix": _flavor_matrix,
    "full-stack-rust-js": _full_stack_rust_js,
    "generated-library": _generated_library,
    "hello-component": _hello_component,
    "javascript-ledger-workbench": _javascript_ledger_workbench,
    "linux-cgroup-budget": _linux_cgroup_budget,
    "loan-risk-gate": _loan_risk_gate,
    "macos-launch-agent": _macos_launch_agent,
    "model-routing": _model_routing,
    "multi-repository-component": _multi_repository_component,
    "playback-controller": _playback_controller,
    "publication-import": _publication_import,
    "python-service-example": _python_service_example,
    "react-dashboard-example": _react_dashboard_example,
    "regenerative-roundtrip": _regenerative_roundtrip,
    "security-policies": _security_policies,
    "self-hosting": _framework_readiness,
    "service-stack": _service_stack,
    "windows-path-auditor": _windows_path_auditor,
}
SUPPORTED_SAMPLE_IDS = tuple(sorted(_BUILDERS))


def create_post_build_probe(
    sample_id: str, *, entropy: bytes | None = None
) -> RuntimeOracleProbe:
    """Create one verifier-only probe after generation and compilation.

    ``entropy`` is injectable only to make verifier tests deterministic.  Live callers
    should omit it; exactly 32 bytes are then obtained from :mod:`secrets` at call time.
    The entropy is domain-separated by sample and purpose, retained only on this stack,
    and absent from the returned object.  This function performs no filesystem writes.
    """

    builder = _BUILDERS.get(sample_id)
    if builder is None:
        raise RuntimeOracleError(f"unsupported runtime-oracle sample: {sample_id!r}")
    selected_entropy = (
        secrets.token_bytes(RUNTIME_ENTROPY_BYTES) if entropy is None else entropy
    )
    if (
        type(selected_entropy) is not bytes
        or len(selected_entropy) != RUNTIME_ENTROPY_BYTES
    ):
        raise RuntimeOracleError(
            f"runtime-oracle entropy must be exactly {RUNTIME_ENTROPY_BYTES} bytes"
        )
    return builder(selected_entropy)


__all__ = [
    "RUNTIME_CASE_ID",
    "RUNTIME_ENTROPY_BYTES",
    "SUPPORTED_SAMPLE_IDS",
    "RuntimeOracleError",
    "RuntimeOracleProbe",
    "create_post_build_probe",
]
