"""Private worker-health input custody and storage-only inspection."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from literate_ai.adapters.worker_cleanup import investigate_worker_cleanup
from literate_ai.adapters.worker_pressure import observe_worker_pressure
from literate_ai.adapters.worker_storage import (
    WorkerStorageBindings,
    WorkerStorageCommand,
    _unique_object,
    observe_worker_storage,
)
from literate_ai.application.worker_capacity import (
    CapacityDecision,
    CapacityHealth,
    assess_worker_capacity,
)
from literate_ai.application.worker_cleanup import (
    CleanupInvestigation,
    CleanupRoot,
    CleanupScanPolicy,
)
from literate_ai.application.worker_pressure import (
    PressurePolicy,
    assess_worker_pressure,
)
from literate_ai.contracts import (
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
    WorkerCapacityPolicy,
)
from literate_ai.contracts._validation import (
    bool_value,
    contract_fields,
    fields,
    int_value,
    list_value,
    string_value,
)
from literate_ai.contracts.worker_capacity import capacity_alias
from literate_ai.projects import PinnedInputClosure

CONFIGURATION_SCHEMA = "literate-ai/private-worker-health@1"
_CAPACITY_FIELDS = frozenset(
    {
        "roles",
        "write_heavy",
        "maximum_age_ms",
        "probe_timeout_ms",
        "maximum_retries",
        "warning_headroom_bytes",
    }
)
_PRESSURE_FIELDS = frozenset(
    {
        "sustained_samples",
        "sample_interval_ms",
        "cpu_warning_basis_points",
        "minimum_available_memory_bytes",
        "maximum_paging_bytes_per_second",
        "minimum_gpu_free_memory_bytes",
        "require_cpu",
        "require_memory",
        "require_gpu",
    }
)


@dataclass(frozen=True)
class WorkerHealthInputs:
    bindings: WorkerStorageBindings
    policy: WorkerCapacityPolicy
    pressure: PressurePolicy | None
    cleanup: CleanupScanPolicy | None
    custody: PinnedInputClosure


def load_worker_health_inputs(configuration, catalog, *, worker_id):
    custody = PinnedInputClosure(
        maximum_files=2,
        maximum_file_bytes=1024 * 1024,
        maximum_total_bytes=2 * 1024 * 1024,
    )

    def read(path, label, maximum):
        path = Path(path).expanduser().absolute()
        raw = custody.pin(path, boundary=Path(path.anchor), label=label)
        if len(raw) > maximum:
            raise ValueError("worker.health_configuration_oversized")
        return json.loads(raw, object_pairs_hook=_unique_object)

    workers = ExecutionWorkerCatalog.from_dict(read(catalog, "workers", 1024 * 1024))
    worker = workers.worker(worker_id)
    data = contract_fields(
        read(configuration, "health", 64 * 1024),
        path="WorkerHealthConfiguration",
        schema_uri=CONFIGURATION_SCHEMA,
        required=frozenset(
            {
                "worker_id",
                "os_family",
                "paths",
                "python_executable",
                "health_command",
                "capacity",
            }
        ),
        optional=frozenset({"cleanup", "pressure"}),
    )
    if data["worker_id"] != worker_id:
        raise ValueError("worker.health_worker_mismatch")
    command = data["health_command"]
    if command is not None:
        command = contract_fields(
            command,
            path="WorkerStorageCommand",
            schema_uri="literate-ai/private-worker-storage-command@1",
            required=frozenset({"command", "environment"}),
        )
        command = WorkerStorageCommand(
            tuple(list_value(command["command"], "WorkerStorageCommand.command")),
            tuple(
                ExecutionWorkerEnvironment.from_dict(value)
                for value in list_value(
                    command["environment"], "WorkerStorageCommand.environment"
                )
            ),
        )
    paths = list_value(data["paths"], "WorkerHealthConfiguration.paths")
    bindings = WorkerStorageBindings(
        worker,
        data["os_family"],
        tuple(
            tuple(list_value(value, "WorkerHealthConfiguration.path"))
            for value in paths
        ),
        data["python_executable"],
        command,
    )
    capacity = fields(
        data["capacity"],
        path="WorkerHealthConfiguration.capacity",
        required=_CAPACITY_FIELDS,
    )
    policy = WorkerCapacityPolicy.from_dict(
        {
            **capacity,
            "schema": WorkerCapacityPolicy.SCHEMA,
            "storage_bindings_identity": bindings.identity.to_dict(),
        }
    )
    if tuple(role.role for role in policy.roles) != tuple(
        role for role, _ in bindings.paths
    ):
        raise ValueError("worker.health_roles_mismatch")
    pressure = None
    if "pressure" in data:
        pressure_data = fields(
            data["pressure"],
            path="WorkerPressureConfiguration",
            required=_PRESSURE_FIELDS,
        )
        pressure = PressurePolicy(
            int_value(
                pressure_data["sustained_samples"],
                "WorkerPressureConfiguration.sustained_samples",
                minimum=2,
                maximum=32,
            ),
            int_value(
                pressure_data["cpu_warning_basis_points"],
                "WorkerPressureConfiguration.cpu_warning_basis_points",
                maximum=10000,
            ),
            int_value(
                pressure_data["minimum_available_memory_bytes"],
                "WorkerPressureConfiguration.minimum_available_memory_bytes",
            ),
            int_value(
                pressure_data["maximum_paging_bytes_per_second"],
                "WorkerPressureConfiguration.maximum_paging_bytes_per_second",
            ),
            int_value(
                pressure_data["minimum_gpu_free_memory_bytes"],
                "WorkerPressureConfiguration.minimum_gpu_free_memory_bytes",
            ),
            int_value(
                pressure_data["sample_interval_ms"],
                "WorkerPressureConfiguration.sample_interval_ms",
                maximum=5000,
            ),
            bool_value(
                pressure_data["require_cpu"],
                "WorkerPressureConfiguration.require_cpu",
            ),
            bool_value(
                pressure_data["require_memory"],
                "WorkerPressureConfiguration.require_memory",
            ),
            bool_value(
                pressure_data["require_gpu"],
                "WorkerPressureConfiguration.require_gpu",
            ),
        )
    cleanup = None
    if "cleanup" in data:
        cleanup_data = fields(
            data["cleanup"],
            path="WorkerCleanupConfiguration",
            required=frozenset(
                {
                    "roots",
                    "deadline_ms",
                    "maximum_entries",
                    "maximum_depth",
                    "minimum_candidate_bytes",
                }
            ),
        )
        pure_path = (
            PureWindowsPath if bindings.os_family == "windows" else PurePosixPath
        )
        role_roots = tuple(pure_path(path) for _role, path in paths)
        roots = []
        for index, value in enumerate(
            list_value(cleanup_data["roots"], "WorkerCleanupConfiguration.roots")
        ):
            item = fields(
                value,
                path=f"WorkerCleanupConfiguration.roots[{index}]",
                required=frozenset(
                    {
                        "alias",
                        "path",
                        "ownership",
                        "recovery",
                        "active_markers",
                        "inactive_markers",
                        "cleanup_command",
                    }
                ),
            )
            alias = capacity_alias(
                item["alias"], f"WorkerCleanupConfiguration.roots[{index}].alias"
            )
            root_raw = string_value(
                item["path"],
                f"WorkerCleanupConfiguration.roots[{index}].path",
            )
            root_pure = pure_path(root_raw)
            if not any(
                root_pure == role_root or root_pure.is_relative_to(role_root)
                for role_root in role_roots
            ):
                raise ValueError("worker.cleanup_root_outside_storage_binding")
            root_path = (
                Path(root_raw) if worker.kind is ExecutionWorkerKind.LOCAL else root_raw
            )
            ownership = string_value(
                item["ownership"],
                f"WorkerCleanupConfiguration.roots[{index}].ownership",
                max_length=32,
            )
            if ownership != "task-owned":
                raise ValueError("worker.cleanup_root_not_task_owned")
            markers = tuple(
                string_value(
                    marker,
                    f"WorkerCleanupConfiguration.roots[{index}].active_markers",
                    max_length=128,
                )
                for marker in list_value(
                    item["active_markers"],
                    f"WorkerCleanupConfiguration.roots[{index}].active_markers",
                )
            )
            if len(markers) > 32 or markers != tuple(sorted(set(markers))):
                raise ValueError("worker.cleanup_active_markers_invalid")
            inactive_markers = tuple(
                string_value(
                    marker,
                    f"WorkerCleanupConfiguration.roots[{index}].inactive_markers",
                    max_length=128,
                )
                for marker in list_value(
                    item["inactive_markers"],
                    f"WorkerCleanupConfiguration.roots[{index}].inactive_markers",
                )
            )
            if (
                len(inactive_markers) > 32
                or inactive_markers != tuple(sorted(set(inactive_markers)))
                or set(inactive_markers) & set(markers)
            ):
                raise ValueError("worker.cleanup_inactive_markers_invalid")
            cleanup_command = tuple(
                string_value(
                    argument,
                    f"WorkerCleanupConfiguration.roots[{index}].cleanup_command",
                    max_length=4096,
                )
                for argument in list_value(
                    item["cleanup_command"],
                    f"WorkerCleanupConfiguration.roots[{index}].cleanup_command",
                )
            )
            if (
                not 1 <= len(cleanup_command) <= 128
                or sum(argument == "{target}" for argument in cleanup_command) != 1
            ):
                raise ValueError("worker.cleanup_command_invalid")
            roots.append(
                CleanupRoot(
                    alias,
                    root_path,
                    ownership,
                    string_value(
                        item["recovery"],
                        f"WorkerCleanupConfiguration.roots[{index}].recovery",
                        max_length=256,
                    ),
                    markers,
                    inactive_markers,
                    cleanup_command,
                )
            )
        aliases = [root.alias for root in roots]
        if not 1 <= len(roots) <= 16 or aliases != sorted(set(aliases)):
            raise ValueError("worker.cleanup_roots_invalid")
        cleanup = CleanupScanPolicy(
            tuple(roots),
            int_value(
                cleanup_data["deadline_ms"],
                "WorkerCleanupConfiguration.deadline_ms",
                minimum=1,
                maximum=60000,
            ),
            int_value(
                cleanup_data["maximum_entries"],
                "WorkerCleanupConfiguration.maximum_entries",
                minimum=1,
                maximum=100000,
            ),
            int_value(
                cleanup_data["maximum_depth"],
                "WorkerCleanupConfiguration.maximum_depth",
                maximum=16,
            ),
            int_value(
                cleanup_data["minimum_candidate_bytes"],
                "WorkerCleanupConfiguration.minimum_candidate_bytes",
            ),
        )
    custody.require_unchanged()
    return WorkerHealthInputs(bindings, policy, pressure, cleanup, custody)


def inspect_worker_storage(
    inputs,
    *,
    job_identity=None,
    observer=observe_worker_storage,
    clock_ms=None,
    alert_state_path=None,
    cleanup_investigator=investigate_worker_cleanup,
    pressure_observer=observe_worker_pressure,
    attempt=0,
):
    """One bounded observation; repeated inspection never authorizes a dispatch."""
    inputs.custody.require_unchanged()
    observation = observer(inputs.bindings, inputs.policy, job_identity=job_identity)
    inputs.custody.require_unchanged()
    now = int(time.time() * 1000) if clock_ms is None else clock_ms()
    assessment = assess_worker_capacity(
        inputs.policy,
        observation,
        worker_id=inputs.bindings.worker.worker_id,
        job_identity=job_identity,
        now_ms=now,
        attempt=attempt,
    )
    alerts = []
    for finding in assessment.findings:
        if finding.health is CapacityHealth.HEALTHY:
            continue
        if finding.health is CapacityHealth.CRITICAL:
            action = (
                "Hold new allocations; inspect configured storage and remeasure. "
                "Cleanup requires exact-target authorization."
            )
        elif finding.health is CapacityHealth.UNKNOWN:
            action = (
                "Check probe support, access and connectivity; collect a fresh "
                "observation before relying on this metric."
            )
        else:
            action = (
                "Review the remaining reserve and remeasure "
                "before additional allocation."
            )
        alerts.append(
            {
                **finding.to_dict(),
                "worker_id": assessment.worker_id,
                "impact": assessment.decision.value,
                "next_action": action,
            }
        )
    pressure_findings = ()
    pressure_hold = False
    pressure_status = "not-configured"
    if inputs.pressure is not None:
        pressure_samples = pressure_observer(inputs.bindings, inputs.pressure)
        pressure_findings = assess_worker_pressure(inputs.pressure, pressure_samples)
        pressure_status = "observed"
        pressure_hold = any(
            finding.health is CapacityHealth.CRITICAL
            or finding.health is CapacityHealth.UNKNOWN
            and finding.required
            for finding in pressure_findings
        )
        for finding in pressure_findings:
            if finding.health is CapacityHealth.HEALTHY:
                continue
            alerts.append(
                {
                    **finding.to_dict(),
                    "roles": ["worker"],
                    "worker_id": assessment.worker_id,
                    "impact": "hold" if pressure_hold else "proceed",
                    "next_action": (
                        "Hold new work and collect a fresh bounded pressure window."
                        if pressure_hold
                        else "Continue polling; reduce only current-task concurrency."
                    ),
                }
            )
    history = {}
    if alert_state_path is not None:
        from literate_ai.adapters.worker_alerts import record_worker_alerts

        events, metadata = record_worker_alerts(
            alert_state_path,
            assessment,
            observation,
            additional_findings=pressure_findings,
            guard=inputs.custody.require_unchanged,
        )
        history = {"events": events, "alert_history": metadata}
    disk_incident = any(
        finding.health in {CapacityHealth.WARNING, CapacityHealth.CRITICAL}
        and finding.resource in {"bytes", "inodes", "quota", "quota-inodes"}
        for finding in assessment.findings
    )
    if disk_incident:
        cleanup_policy = inputs.cleanup
        if cleanup_policy is not None and any(
            finding.health in {CapacityHealth.WARNING, CapacityHealth.CRITICAL}
            for finding in pressure_findings
        ):
            cleanup_policy = replace(
                cleanup_policy,
                deadline_ms=max(1, cleanup_policy.deadline_ms // 2),
                maximum_depth=min(1, cleanup_policy.maximum_depth),
            )
        investigation = (
            CleanupInvestigation("not-configured", 0, 0, ())
            if cleanup_policy is None
            else cleanup_investigator(inputs.bindings, cleanup_policy)
        )
    else:
        investigation = CleanupInvestigation("not-required", 0, 0, ())
    inputs.custody.require_unchanged()
    return {
        **history,
        "schema": "literate-ai/worker-storage-health-result@1",
        "scope": "storage",
        "observation": observation.to_dict(),
        "assessment": assessment.to_dict(),
        "alerts": alerts,
        "pressure": {
            "status": pressure_status,
            "findings": [item.to_dict() for item in pressure_findings],
        },
        "cleanup_investigation": investigation.to_dict(),
    }, {
        CapacityDecision.PROCEED: 0,
        CapacityDecision.HOLD: 1,
        CapacityDecision.RETRY_AFTER_RECHECK: 2,
    }[assessment.decision] if not pressure_hold else 1
