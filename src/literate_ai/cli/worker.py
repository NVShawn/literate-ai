"""Worker inventory CLI boundary."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.execution_dispatch import (
    ExecutionDispatchAdapterError,
    load_execution_worker_catalog,
)
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
    resolve_worker_observations_path,
)
from literate_ai.adapters.user_paths import (
    UserPathError,
    prepare_user_directory,
    resolve_user_paths,
)
from literate_ai.adapters.worker_capabilities import (
    WorkerCapabilityProbeError,
    probe_worker_catalog,
    write_worker_observations,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    ExecutionDispatchRequest,
    ExecutionSourceMaterialization,
    ExecutionWorker,
    RemoteExecutionControlResult,
    WorkerHardwareObservationCatalog,
    canonical_json_bytes,
)
from literate_ai.diagnostics import verbose_diagnostics

from .errors import CLI_ERROR_MESSAGE_CHARS, CliFailure

DEFAULT_WORKER_CONFIGURATION = "workers.json"
DEFAULT_OBSERVATION_CONFIGURATION = "worker-observations.json"
# Reserve room for the stable outer CLI envelope and trailing newline.
MAX_REMOTE_CONTROL_RESULT_BYTES = 1024 * 1024 - 4096


def _existing(path: Path) -> WorkerHardwareObservationCatalog:
    if not path.exists():
        return WorkerHardwareObservationCatalog(())
    try:
        return WorkerHardwareObservationCatalog.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ContractValidationError,
    ) as exc:
        raise CliFailure(
            "worker.observation_catalog_invalid",
            "existing worker observation catalog is invalid",
        ) from exc


def worker_from_args(args: Any) -> tuple[dict[str, object], int]:
    if args.worker_command in {"provisioner", "provision"}:
        from .worker_provisioning import provisioning_from_args

        return provisioning_from_args(args)
    if args.worker_command in {"list", "show", "add", "update", "remove", "test"}:
        from .worker_registry import registry_from_args

        return registry_from_args(args)
    if args.worker_command == "health":
        from .worker_health import worker_health_from_args

        return worker_health_from_args(args)
    if args.worker_command == "cleanup":
        from .worker_health import worker_cleanup_from_args

        return worker_cleanup_from_args(args)
    if args.worker_command == "resolve-nvidia":
        return _resolve_nvidia_from_args(args)
    if args.worker_command == "wheelhouse":
        return _wheelhouse_from_args(args)
    if args.worker_command == "bootstrap":
        return _bootstrap_from_args(args)
    if args.worker_command == "execute":
        return _execute_from_args(args)
    if args.worker_command == "execute-retained":
        return _execute_retained_from_args(args)
    if args.worker_command == "acknowledge":
        return _acknowledge_from_args(args)
    if args.worker_command == "verify-model":
        return _verify_model_from_args(args)
    if args.worker_command == "align":
        return _align_from_args(args)
    if args.worker_command != "probe":
        raise CliFailure("cli.usage", "a worker command is required")
    failures: list[WorkerCapabilityProbeError] = []
    try:
        worker_path = resolve_worker_config_path(explicit=args.worker_config)
        observation_path = resolve_worker_observations_path(explicit=args.output)
        workers = load_execution_worker_catalog(worker_path)
        observed = probe_worker_catalog(
            workers,
            errors=failures,
            worker_ids=None if args.all else tuple(args.worker_id),
            timeout_seconds=args.timeout_seconds,
        )
    except (ExecutionDispatchAdapterError, UserAssetPathError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except (ContractValidationError, WorkerCapabilityProbeError) as exc:
        raise CliFailure(
            getattr(exc, "code", "worker.probe_invalid"),
            getattr(exc, "message", str(exc)),
            message_limit=8192 if isinstance(exc, WorkerCapabilityProbeError) else 512,
        ) from exc
    replacements = {
        item.worker_id: item for item in _existing(observation_path).workers
    }
    for failure in failures:
        replacements.pop(failure.worker_id, None)
    replacements.update({item.worker_id: item for item in observed.workers})
    combined = WorkerHardwareObservationCatalog(
        tuple(replacements[key] for key in sorted(replacements))
    )
    written = not args.dry_run and bool(observed.workers or observation_path.exists())
    if written:
        try:
            if args.output is None and not os.environ.get("LITAI_WORKER_OBSERVATIONS"):
                paths = resolve_user_paths()
                prepare_user_directory(Path(paths.state_root), observation_path.parent)
            write_worker_observations(observation_path, combined)
        except (OSError, UserPathError) as exc:
            raise CliFailure(
                "worker.observation_write_failed",
                "worker observations could not be written atomically",
            ) from exc
    return {
        "schema": "literate-ai/worker-probe-result@1",
        "worker_catalog_identity": workers.identity.uri,
        "observation_catalog_identity": combined.identity.uri,
        "output": str(observation_path),
        "written": written,
        "ok": not failures,
        "failures": [
            {
                "worker_id": failure.worker_id,
                "code": failure.code,
                "message": failure.message,
            }
            for failure in failures
        ],
        "probed": [item.worker_id for item in observed.workers],
        "workers": [item.to_dict() for item in combined.workers],
    }, int(bool(failures))


def _resolve_nvidia_from_args(args: Any) -> tuple[dict[str, object], int]:
    from literate_ai.adapters.worker_capabilities import load_worker_observations
    from literate_ai.application.nvidia_stack import (
        NvidiaStackError,
        resolve_nvidia_stack,
    )

    try:
        observation_path = resolve_worker_observations_path(explicit=args.observations)
        observation = load_worker_observations(observation_path).worker(args.worker_id)
        compatibility = _read_contract(
            args.compatibility, label="NVIDIA compatibility authority"
        )
        selection = resolve_nvidia_stack(
            observation,
            compatibility,
            python_abi=args.python_abi,
            toolkit_pin=args.toolkit,
            package_pins=tuple(args.package),
        )
        return selection.to_dict(), 0
    except (
        ContractValidationError,
        UserAssetPathError,
        WorkerCapabilityProbeError,
    ) as exc:
        raise CliFailure(
            getattr(exc, "code", "nvidia_stack.observation_invalid"),
            getattr(exc, "message", str(exc)),
        ) from exc
    except NvidiaStackError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _align_from_args(args: Any) -> tuple[dict[str, object], int]:
    """Inventory and optionally align workers with every declared expectation."""

    import os

    from literate_ai.adapters.live_test_selection import try_resolve_live_test_selection
    from literate_ai.adapters.models.coding_cli import CodingCliError
    from literate_ai.adapters.worker_alignment import (
        TEMPLATE_FILE,
        UserAlignment,
        WorkerAligner,
        WorkerAlignmentError,
        WorkerTemplate,
    )
    from literate_ai.projects import ProjectError, discover_project

    try:
        project = discover_project(Path.cwd())
        catalog = load_execution_worker_catalog(
            resolve_worker_config_path(explicit=args.worker_config)
        )
        selected = (
            tuple(catalog.worker(item) for item in args.worker_id)
            if args.worker_id
            else catalog.workers
        )
        workers = tuple(
            worker
            for worker in selected
            if worker.endpoint is not None and worker.workspace is not None
        )
        default_template = project.root / TEMPLATE_FILE
        # A project that declares no worker prerequisites still checks the user's
        # own expectations; an explicitly named template must exist.
        template = (
            WorkerTemplate.load(Path(args.template))
            if args.template
            else WorkerTemplate.load(default_template)
            if default_template.exists()
            else WorkerTemplate({})
        )
        alignment = UserAlignment.load(
            Path(args.alignment)
            if args.alignment
            else Path(resolve_user_paths(environment=os.environ).worker_alignment)
        )
        selection = try_resolve_live_test_selection(
            project_root=project.root, ignore_environment_pins=True
        )
    except WorkerAlignmentError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except (
        ProjectError,
        UserPathError,
        UserAssetPathError,
        ExecutionDispatchAdapterError,
        CodingCliError,
        ValueError,
    ) as exc:
        raise CliFailure(
            getattr(exc, "code", "worker_alignment.unavailable"),
            getattr(exc, "message", str(exc)),
        ) from exc
    aligner = WorkerAligner(
        template,
        alignment,
        coding_cli=None if selection is None else selection.coding_cli,
        model=None if selection is None else selection.model,
        cwd=project.root,
    )
    report = aligner.align(workers, apply=args.apply, check_model=not args.skip_model)
    return report, 0 if report["aligned"] else 1


def _verify_model_from_args(args: Any) -> tuple[dict[str, object], int]:
    """Resolve the live-qualification selection and prove its model resolves.

    The durable model pin in project-scoped user configuration (or ``--model`` /
    ``LITAI_LIVE_MODEL``) is an unvalidated string until it is actually used. Run
    this before trusting a config: it fails closed with ``coding_cli.model_unavailable``
    if the pinned model does not resolve for the pinned coding CLI, naming the
    model and the real underlying cause.
    """

    import os

    from literate_ai.adapters.live_test_selection import (
        resolve_live_test_selection,
        verify_live_model_resolves,
    )
    from literate_ai.adapters.models.coding_cli import CodingCliError

    try:
        selection = resolve_live_test_selection(
            coding_cli=getattr(args, "coding_cli", None),
            model=getattr(args, "model", None),
            environment=os.environ,
        )
        verify_live_model_resolves(selection, environment=os.environ)
    except CodingCliError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return {
        "schema": "literate-ai/worker-verify-model-result@1",
        "coding_cli": selection.coding_cli,
        "model": selection.model,
        "coding_cli_provenance": selection.coding_cli_provenance,
        "model_provenance": selection.model_provenance,
        "resolves": True,
    }, 0


def _bootstrap_from_args(args: Any) -> tuple[dict[str, object], int]:
    from literate_ai.remote_worker_bootstrap import (
        WorkerBootstrapError,
        bootstrap_worker_distribution,
        bootstrap_worker_wheelhouse,
    )

    try:
        installed: list[dict[str, object]] = []
        if args.wheelhouse is not None:
            if args.manifest is None or args.closure_identity is None:
                raise WorkerBootstrapError(
                    "worker.bootstrap_manifest_required",
                    "wheelhouse bootstrap requires manifest and closure identity",
                )
            leftover_capability = (
                getattr(args, "capability_root", None),
                getattr(args, "capability_manifest", None),
                getattr(args, "capability_identity", None),
            )
            if any(value is not None for value in leftover_capability):
                raise WorkerBootstrapError(
                    "worker.bootstrap_capability_unsupported",
                    "worker bootstrap does not accept companion capability arguments",
                )
            launcher, installed = bootstrap_worker_wheelhouse(
                Path(args.wheelhouse),
                Path(args.manifest),
                expected_closure_identity=args.closure_identity,
                expected_distribution_identity=args.distribution_identity,
                install_root=Path(args.install_root),
                replace_existing=args.replace_existing,
            )
        else:
            if args.wheel is None or args.wheel_sha256 is None:
                raise WorkerBootstrapError(
                    "worker.bootstrap_digest_invalid",
                    "single-wheel bootstrap requires wheel and wheel digest",
                )
            launcher = bootstrap_worker_distribution(
                Path(args.wheel),
                expected_wheel_sha256=args.wheel_sha256,
                expected_distribution_identity=args.distribution_identity,
                install_root=Path(args.install_root),
                replace_existing=args.replace_existing,
            )
    except WorkerBootstrapError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    result: dict[str, object] = {
        "schema": (
            "literate-ai/worker-bootstrap-result@2"
            if args.wheelhouse is not None
            else "literate-ai/worker-bootstrap-result@1"
        ),
        "distribution_identity": args.distribution_identity,
        "launcher": str(launcher),
    }
    if args.wheelhouse is not None:
        result["dependency_closure_identity"] = args.closure_identity
        result["installed_distributions"] = installed
        result["capabilities"] = []
    return result, 0


def _wheelhouse_from_args(args: Any) -> tuple[dict[str, object], int]:
    from literate_ai.remote_worker_bootstrap import (
        WorkerBootstrapError,
        export_worker_wheelhouse,
    )

    if args.wheelhouse_command != "export":
        raise CliFailure("cli.usage", "a worker wheelhouse command is required")
    try:
        manifest, identity = export_worker_wheelhouse(
            Path(args.framework_wheel),
            Path(args.output),
            framework_distribution_identity=args.distribution_identity,
            platform=args.platform,
            python_version=args.python_version,
            implementation=args.implementation,
            abi=args.abi,
        )
    except WorkerBootstrapError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return {
        "schema": "literate-ai/worker-wheelhouse-export-result@1",
        "manifest": str(manifest),
        "dependency_closure_identity": identity,
        "distribution_identity": args.distribution_identity,
    }, 0


def _read_contract(path: str, *, label: str) -> object:
    selected = Path(path).expanduser()
    try:
        if selected.is_symlink() or not selected.is_file():
            raise OSError
        if selected.stat().st_size > 1024 * 1024:
            raise CliFailure(
                "execution.remote_contract_oversized",
                f"{label} exceeds the one MiB limit",
            )
        return json.loads(selected.read_text(encoding="utf-8"))
    except CliFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliFailure(
            "execution.remote_contract_invalid", f"{label} is unavailable or invalid"
        ) from exc


def _execute_from_args(args: Any) -> tuple[dict[str, object], int]:
    from literate_ai.adapters.remote_execution import (
        RemoteExecutionError,
        RemoteLifecycleCustody,
        materialize_and_execute,
        remote_control_summary,
    )
    from literate_ai.cli.rebuild import rebuild_from_args

    def rebuild_remote_lifecycle(
        *,
        component_path: str,
        project_root: Path,
        custody: RemoteLifecycleCustody,
        flavor_selectors: tuple[str, ...],
        target_profile: str,
        model_selector: str | None,
        accepted_source_only: bool,
        accepted_source_provider_id: str | None,
        accepted_source_provider_identity: ContentIdentity | None,
        jobs: int,
    ) -> dict[str, object]:
        return rebuild_from_args(
            Namespace(
                specification=component_path,
                project=str(project_root),
                runtime_root=str(custody.runtime_root),
                candidate_receipt=str(custody.candidate_receipt),
                update_receipt=False,
                keep_runtime=True,
                jobs=jobs,
                flavor=list(flavor_selectors),
                target=target_profile,
                execution_worker=None,
                force_regeneration=False,
                model=model_selector,
                source_cache_entry=None,
                source_cache_root=[],
                allow_host_execution=True,
                from_accepted_source=accepted_source_only,
                accepted_source_provider_id=accepted_source_provider_id,
                accepted_source_provider_identity=(accepted_source_provider_identity),
            ),
            cache_directories=custody.cache_directories,
        )

    try:
        worker = ExecutionWorker.from_dict(
            _read_contract(args.worker_file, label="worker declaration")
        )
        request = ExecutionDispatchRequest.from_dict(
            _read_contract(args.request, label="dispatch request")
        )
        materialization = ExecutionSourceMaterialization.from_dict(
            _read_contract(args.materialization, label="source materialization")
        )
        with verbose_diagnostics(request.verbose):
            result = materialize_and_execute(
                worker,
                request,
                materialization,
                archive=Path(args.archive),
                workspace=Path(args.workspace),
                cas_root=Path(args.cas_root),
                rebuild=rebuild_remote_lifecycle,
                accepted_source_cache_archive=(
                    Path(args.accepted_source_cache)
                    if getattr(args, "accepted_source_cache", None)
                    else None
                ),
                evidence_output=(
                    Path(args.evidence_output)
                    if getattr(args, "evidence_output", None)
                    else None
                ),
                cleanup_ticket=(
                    Path(args.cleanup_ticket)
                    if getattr(args, "cleanup_ticket", None)
                    else None
                ),
            )
    except ContractValidationError as exc:
        raise CliFailure(
            "execution.remote_contract_invalid",
            "worker execution input violates its versioned contract: "
            f"{exc.path}: {exc.message}",
        ) from exc
    except RemoteExecutionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if getattr(args, "evidence_output", None) is None:
        return result.to_dict(), 0 if result.status.value == "passed" else 1
    if result.evidence_manifest is None or result.evidence_reference is None:
        raise CliFailure(
            "execution.remote_evidence_custody_invalid",
            "worker execution omitted its transferable evidence custody",
        )
    evidence_path = Path(args.evidence_output)
    try:
        bundle_size = evidence_path.stat().st_size
    except OSError as exc:
        raise CliFailure(
            "execution.remote_evidence_custody_invalid",
            "worker evidence bundle is unavailable after execution",
        ) from exc
    control = RemoteExecutionControlResult.from_dispatch_result(
        result,
        manifest_size=len(canonical_json_bytes(result.evidence_manifest.to_dict())),
        bundle_size=bundle_size,
        redacted_summary=remote_control_summary(result),
    )
    control_value = control.to_dict()
    if len(canonical_json_bytes(control_value)) > MAX_REMOTE_CONTROL_RESULT_BYTES:
        raise CliFailure(
            "execution.remote_control_result_oversized",
            "worker control result exceeds the fixed one MiB limit",
        )
    return control_value, 0 if result.status.value == "passed" else 1


def _execute_retained_from_args(args: Any) -> tuple[dict[str, object], int]:
    """Run the internal POSIX retained-harness receiver."""

    from literate_ai.adapters.retained_harness_remote import (
        RetainedHarnessRemoteError,
        execute_retained_harness_receiver,
        retained_remote_public_error_message,
    )

    try:
        result = execute_retained_harness_receiver(
            worker_file=Path(args.worker_file),
            request_file=Path(args.request),
            inventory_file=Path(args.inventory),
            archive=Path(args.archive),
            runtime_archive=Path(args.runtime_archive),
            workspace=Path(args.workspace),
        )
    except RetainedHarnessRemoteError as exc:
        raise CliFailure(
            exc.code,
            retained_remote_public_error_message(exc.message),
            message_limit=CLI_ERROR_MESSAGE_CHARS * 64,
        ) from exc
    return result.to_dict(), 0


def _acknowledge_from_args(args: Any) -> tuple[dict[str, object], int]:
    from literate_ai.adapters.remote_execution import (
        RemoteExecutionError,
        acknowledge_remote_evidence_cleanup,
    )

    try:
        manifest_identity = ContentIdentity.parse_uri(args.manifest_identity)
        bundle_identity = ContentIdentity.parse_uri(args.bundle_identity)
        acknowledgement = acknowledge_remote_evidence_cleanup(
            Path(args.cleanup_ticket),
            manifest_identity=manifest_identity,
            bundle_identity=bundle_identity,
            acknowledgement_root=Path(args.acknowledgement_root),
        )
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "execution.remote_cleanup_identity_invalid",
            "cleanup acknowledgement identities must be exact SHA-256 identities",
        ) from exc
    except RemoteExecutionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return {
        "schema": "literate-ai/remote-evidence-cleanup-result@1",
        "manifest_identity": manifest_identity.uri,
        "bundle_identity": bundle_identity.uri,
        "acknowledgement_identity": acknowledgement.uri,
        "status": "cleaned",
    }, 0


__all__ = ["worker_from_args"]
