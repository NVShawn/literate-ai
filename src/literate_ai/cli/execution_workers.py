"""Exact execution-worker selection shared by lifecycle CLI verbs."""

from __future__ import annotations

from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from literate_ai.adapters.execution_dispatch import (
    ExecutionDispatchAdapterError,
    load_execution_worker_catalog,
)
from literate_ai.adapters.source_materialization import (
    SourceMaterializationError,
    discover_accepted_source_provider_binding,
)
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
    resolve_worker_observations_path,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ExecutionDispatchRequest,
    ExecutionWorker,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    SourceIntelligenceStage,
    canonical_identity,
    resolve_worker_parameters,
)

from .errors import CliFailure

DEFAULT_WORKER_CONFIGURATION = "workers.json"
DEFAULT_WORKER_OBSERVATIONS = "worker-observations.json"
MAX_WORKER_OBSERVATION_AGE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class SelectedExecutionWorker:
    """One exact worker and its bounded, resolved request parameters."""

    worker: ExecutionWorker
    parameters: tuple[tuple[str, str], ...]
    catalog_identity: ContentIdentity
    configuration: Path | None

    @property
    def implicit_local(self) -> bool:
        return self.configuration is None


def add_execution_worker_arguments(parser: Any) -> None:
    """Install the provider-neutral worker grammar on one lifecycle verb."""

    parser.add_argument(
        "--worker",
        help=(
            "exact execution-worker ID; omission executes locally without loading a "
            "private worker catalog"
        ),
    )
    parser.add_argument(
        "--worker-param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "schema-declared parameter for the selected worker (repeatable; unknown "
            "names and values are rejected)"
        ),
    )
    parser.add_argument(
        "--worker-config",
        help=(
            "private execution-worker catalog (default: LITAI_WORKER_CONFIG or "
            "the resolved user configuration root)"
        ),
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=int,
        default=3600,
        help="bounded lifecycle-dispatch timeout (default: 3600 seconds)",
    )
    parser.add_argument(
        "--worker-health-config",
        help=(
            "private health policy used to admit the selected worker before "
            "substantial lifecycle execution"
        ),
    )
    parser.add_argument(
        "--worker-health-poll-seconds",
        type=int,
        default=30,
        choices=range(1, 3601),
        help="active-job health polling interval when a health policy is selected",
    )


def admit_execution_worker_health(
    args: Any,
    selected: SelectedExecutionWorker,
    job_identity: ContentIdentity,
    *,
    loader=None,
    inspector=None,
    active_job=False,
) -> dict[str, Any] | None:
    """Bounded fresh preflight; inspection itself never authorizes dispatch."""

    health_configuration = getattr(args, "worker_health_config", None)
    if health_configuration is None:
        return None
    if selected.configuration is None:
        raise CliFailure(
            "execution.worker_health_catalog_required",
            "--worker-health-config requires an explicitly configured worker",
        )
    if loader is None or inspector is None:
        from literate_ai.adapters.worker_health import (
            inspect_worker_storage,
            load_worker_health_inputs,
        )

        loader = load_worker_health_inputs if loader is None else loader
        inspector = inspect_worker_storage if inspector is None else inspector
    try:
        inputs = loader(
            health_configuration,
            selected.configuration,
            worker_id=selected.worker.worker_id,
        )
        result = None
        for attempt in range(inputs.policy.maximum_retries + 1):
            result, status = inspector(
                inputs,
                job_identity=job_identity,
                attempt=attempt,
            )
            if status == 0:
                return result
            if status != 2:
                break
        assert result is not None
        assessment = result["assessment"]
        if active_job:
            return {
                **result,
                "active_job_impact": (
                    "continue-current-job; hold only new task-owned work"
                ),
            }
        raise CliFailure(
            "execution.worker_health_hold",
            f"worker {selected.worker.worker_id!r} health is "
            f"{assessment['health']}; {assessment['decision']}",
        )
    except CliFailure:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise CliFailure(
            "execution.worker_health_invalid",
            "worker health inputs or observations are invalid, unavailable, or changed",
        ) from exc


def dispatch_with_worker_health_poll(
    args: Any,
    selected: SelectedExecutionWorker,
    job_identity: ContentIdentity,
    dispatch,
    *,
    admission=admit_execution_worker_health,
):
    """Poll explicit health policy while preserving the active job's ownership."""

    if getattr(args, "worker_health_config", None) is None:
        return dispatch(), ()
    interval = getattr(args, "worker_health_poll_seconds", 30)
    polls = []
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="litai-dispatch") as pool:
        future = pool.submit(dispatch)
        while True:
            try:
                result = future.result(timeout=interval)
                return result, tuple(polls)
            except TimeoutError:
                if future.done():
                    return future.result(), tuple(polls)
                try:
                    observed = admission(
                        args,
                        selected,
                        job_identity,
                        active_job=True,
                    )
                except CliFailure as exc:
                    observed = {
                        "schema": "literate-ai/worker-health-poll-error@1",
                        "worker_id": selected.worker.worker_id,
                        "code": exc.code,
                        "active_job_impact": (
                            "continue-current-job; hold only new task-owned work"
                        ),
                    }
                if observed is not None:
                    polls.append(observed)


def _parameter_pairs(values: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise CliFailure(
                "execution.worker_parameter_invalid",
                f"worker parameter {index} must be NAME=VALUE",
            )
        name, separator, selected = value.partition("=")
        if not separator or not name or not selected:
            raise CliFailure(
                "execution.worker_parameter_invalid",
                f"worker parameter {index} must be a nonempty NAME=VALUE pair",
            )
        pairs.append((name, selected))
    return tuple(sorted(pairs))


def _configuration(args: Any, project_root: Path) -> Path:
    return resolve_worker_config_path(
        explicit=getattr(args, "worker_config", None),
        project_root=project_root,
    )


def select_execution_worker(
    args: Any,
    *,
    project_root: Path,
    target_profile: str,
) -> SelectedExecutionWorker:
    """Resolve one exact worker without search, ranking, or provisioning policy."""

    worker_id = getattr(args, "worker", None)
    supplied = tuple(getattr(args, "worker_param", ()) or ())
    if worker_id is None:
        if supplied:
            raise CliFailure(
                "execution.worker_required",
                "--worker-param requires one explicit --worker ID",
            )
        worker = ExecutionWorker(
            "local", ExecutionWorkerKind.LOCAL, target_profile=target_profile
        )
        return SelectedExecutionWorker(
            worker,
            (),
            canonical_identity(
                {
                    "schema": "literate-ai/implicit-local-worker-catalog@1",
                    "worker": worker.to_dict(),
                }
            ),
            None,
        )

    try:
        configuration = _configuration(args, project_root)
        catalog = load_execution_worker_catalog(configuration)
        worker = catalog.worker(worker_id)
        parameters = resolve_worker_parameters(worker, _parameter_pairs(supplied))
    except (ExecutionDispatchAdapterError, UserAssetPathError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except ContractValidationError as exc:
        raise CliFailure("execution.worker_selection_invalid", str(exc)) from exc
    if worker.target_profile != target_profile:
        raise CliFailure(
            "execution.worker_target_profile_mismatch",
            f"worker {worker.worker_id!r} binds target profile "
            f"{worker.target_profile!r}, not requested {target_profile!r}",
        )
    return SelectedExecutionWorker(
        worker,
        parameters,
        catalog.identity,
        configuration,
    )


def validate_worker_platform_flavors(
    selected: SelectedExecutionWorker, flavors: tuple[Any, ...]
) -> None:
    """Reject a concrete locked OS Flavor that contradicts the exact worker."""

    platform_values = tuple(
        flavor.value
        for flavor in flavors
        if getattr(flavor, "axis", None) == "platform.os"
    )
    if len(platform_values) != 1:
        raise CliFailure(
            "execution.worker_platform_flavor_invalid",
            "the locked lifecycle must select exactly one platform.os Flavor",
        )
    required = selected.worker.requirements.os_family
    platform = platform_values[0]
    if required is not None and platform not in {"host", required}:
        raise CliFailure(
            "execution.worker_platform_flavor_mismatch",
            f"worker {selected.worker.worker_id!r} requires OS {required!r}, but the "
            f"locked platform Flavor is {platform!r}",
        )


def validate_worker_accelerator_flavors(
    selected: SelectedExecutionWorker,
    flavors: tuple[Any, ...],
    *,
    project_root: Path,
) -> None:
    """Reject an ineligible CUDA worker before any generation or dispatch."""

    accelerators = tuple(
        flavor.value
        for flavor in flavors
        if getattr(flavor, "axis", None) == "accelerator"
    )
    if "nvidia-cuda" not in accelerators:
        return
    from literate_ai.adapters.worker_capabilities import (
        WorkerCapabilityProbeError,
        load_worker_observations,
        probe_worker_capabilities,
    )
    from literate_ai.contracts import NvidiaProbeStatus

    try:
        observed = (
            probe_worker_capabilities(selected.worker)
            if selected.implicit_local
            else load_worker_observations(
                resolve_worker_observations_path(project_root=project_root)
            ).worker(selected.worker.worker_id)
        )
    except (
        ContractValidationError,
        UserAssetPathError,
        WorkerCapabilityProbeError,
    ) as exc:
        raise CliFailure(
            getattr(exc, "code", "execution.worker_observation_invalid"),
            getattr(exc, "message", str(exc)),
        ) from exc
    observed_time = datetime.fromisoformat(observed.observed_at.replace("Z", "+00:00"))
    if datetime.now(UTC) - observed_time > MAX_WORKER_OBSERVATION_AGE:
        raise CliFailure(
            "execution.worker_observation_stale",
            f"worker {observed.worker_id!r} observation is older than 24 hours; "
            "run `litai worker probe`",
        )
    if observed.os_family not in {"linux", "windows"}:
        raise CliFailure(
            "execution.worker_accelerator_ineligible",
            "NVIDIA CUDA requires a Linux or Windows worker",
        )
    if observed.nvidia_status is not NvidiaProbeStatus.OK:
        raise CliFailure(
            "execution.worker_accelerator_ineligible",
            f"worker {observed.worker_id!r} NVIDIA state is "
            f"{observed.nvidia_status.value!r}, not healthy",
        )
    requirements = selected.worker.requirements
    if not requirements.gpu.constrained:
        from literate_ai.contracts import ExecutionRequirements, GpuRequirement

        requirements = ExecutionRequirements(
            requirements.os_family,
            requirements.os_version,
            requirements.cpu_architecture,
            requirements.minimum_cpu_cores,
            requirements.minimum_memory_mib,
            GpuRequirement(vendor="nvidia", capabilities=("cuda",)),
        )
    if not observed.satisfies(requirements):
        raise CliFailure(
            "execution.worker_accelerator_requirements_unsatisfied",
            f"worker {observed.worker_id!r} does not satisfy the declared CUDA "
            "device requirements",
        )


def create_execution_dispatch_request(
    args: Any,
    *,
    project_root: Path,
    component: str,
    selected: SelectedExecutionWorker,
    action: LifecycleDispatchAction,
    artifact_reference: ContentReference | None = None,
    application_arguments: tuple[str, ...] = (),
    entrypoint: str | None = None,
) -> ExecutionDispatchRequest:
    """Bind one worker action to the exact current project/generation authority."""

    from literate_ai.project_source_index import (
        ProjectSourceIntelligenceError,
        require_lifecycle_project_index,
    )
    from literate_ai.projects import ProjectError, discover_project

    from .generation import plan_from_args

    component_root = (project_root / component).resolve()
    target_profile = getattr(args, "target", "host")
    try:
        project = discover_project(project_root)
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure("project.not_found", "execution dispatch requires a project")
    effective_flavors = project.flavor_selectors_for(
        component_root, tuple(getattr(args, "flavor", ()) or ())
    )
    plan = plan_from_args(
        Namespace(
            specification=str(component_root),
            recipe_id=None,
            target=target_profile,
            flavor=list(getattr(args, "flavor", ()) or ()),
            flavor_root=[],
            model=getattr(args, "model", None),
        )
    )
    try:
        index = require_lifecycle_project_index(
            project.root,
            project.definition.source_intelligence,
            stage=SourceIntelligenceStage.SOURCE_GENERATION,
        )
        project_report = plan["project"]
        component_report = plan["component"]
        resolution = plan["resolution"]
        input_closure = plan["input_closure"]
        selected_flavors = plan["selected_flavors"]
        model_scopes = plan["model_scopes"]
        toolchain_constraints = plan["toolchain_constraints"]
        if not all(
            isinstance(item, dict)
            for item in (
                project_report,
                component_report,
                resolution,
                input_closure,
                index,
            )
        ):
            raise TypeError
        required_os = selected.worker.requirements.os_family
        platform_values = tuple(
            item.get("value")
            for item in selected_flavors
            if isinstance(item, dict) and item.get("axis") == "platform.os"
        )
        if len(platform_values) != 1:
            raise CliFailure(
                "execution.worker_platform_flavor_invalid",
                "the locked lifecycle must select exactly one platform.os Flavor",
            )
        if required_os is not None and platform_values[0] not in {"host", required_os}:
            raise CliFailure(
                "execution.worker_platform_flavor_mismatch",
                f"worker {selected.worker.worker_id!r} requires OS {required_os!r}, "
                f"but the locked platform Flavor is {platform_values[0]!r}",
            )

        @dataclass(frozen=True, slots=True)
        class _Flavor:
            axis: str
            value: str

        validate_worker_accelerator_flavors(
            selected,
            tuple(
                _Flavor(str(item.get("axis")), str(item.get("value")))
                for item in selected_flavors
                if isinstance(item, dict)
            ),
            project_root=project_root,
        )
        accepted_source_only = bool(getattr(args, "from_accepted_source", False))
        accepted_source_provider_binding = (
            discover_accepted_source_provider_binding(project.root)
            if accepted_source_only
            else None
        )
        return ExecutionDispatchRequest(
            action=action,
            component=str(component_report["coordinate"]),
            component_path=component_root.relative_to(project.root).as_posix(),
            target_profile=target_profile,
            flavor_selectors=effective_flavors,
            worker_identity=selected.worker.identity,
            requirements=selected.worker.requirements,
            parameters=selected.parameters,
            arguments=application_arguments,
            project_identity=ContentIdentity.parse_uri(str(project_report["identity"])),
            source_identity=ContentIdentity.parse_uri(str(input_closure["identity"])),
            specification_identity=ContentIdentity.parse_uri(
                str(resolution["root_revision_identity"])
            ),
            flavor_identity=canonical_identity(
                {
                    "schema": "literate-ai/selected-flavor-authority@1",
                    "target_profile": target_profile,
                    "selected_flavors": selected_flavors,
                }
            ),
            toolchain_identity=canonical_identity(
                {
                    "schema": "literate-ai/toolchain-requirement-authority@1",
                    "constraints": toolchain_constraints,
                }
            ),
            source_index_identity=ContentIdentity.parse_uri(
                str(index["database_identity"])
            ),
            model_scope_identity=canonical_identity(
                {
                    "schema": "literate-ai/model-scope-set@1",
                    "bindings": model_scopes,
                }
            ),
            artifact_reference=artifact_reference,
            timeout_seconds=getattr(args, "worker_timeout_seconds", 3600),
            jobs=getattr(args, "jobs", 1),
            model_selector=getattr(args, "model", None),
            verbose=bool(getattr(args, "verbose", False)),
            accepted_source_only=accepted_source_only,
            accepted_source_provider_id=(
                None
                if accepted_source_provider_binding is None
                else accepted_source_provider_binding[0]
            ),
            accepted_source_provider_identity=(
                None
                if accepted_source_provider_binding is None
                else accepted_source_provider_binding[1]
            ),
            entrypoint=entrypoint,
        )
    except CliFailure:
        raise
    except ProjectSourceIntelligenceError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except SourceMaterializationError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise CliFailure(
            "execution.dispatch_authority_invalid",
            "the current project plan cannot form an exact execution request",
        ) from exc


__all__ = [
    "DEFAULT_WORKER_CONFIGURATION",
    "SelectedExecutionWorker",
    "admit_execution_worker_health",
    "add_execution_worker_arguments",
    "create_execution_dispatch_request",
    "dispatch_with_worker_health_poll",
    "select_execution_worker",
    "validate_worker_platform_flavors",
]
