"""Opt-in retained unit planning and portable, unadmitted archive inspection."""

from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.component_lock_application import (
    current_project_component_locks,
)
from literate_ai.adapters.freebsd_composed_tree_packaging import (
    inspect_freebsd_composed_package_set,
)
from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.adapters.retained_project_unit_packages import (
    plan_retained_unit_packages,
)
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
    retained_unit_metadata_reference,
)
from literate_ai.adapters.retained_unit_results import read_retained_unit_results
from literate_ai.adapters.retained_unit_specifications import (
    bind_retained_unit_specifications,
)
from literate_ai.adapters.retained_unit_tool_observations import (
    parse_retained_unit_tool_observations,
    retained_unit_tool_observations,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.cache_directories import (
    CacheDirectoryError,
    ensure_cache_directory,
    select_cache_directories,
)
from literate_ai.contracts import (
    ContentIdentity,
    StandardProjectLifecycleDriver,
    canonical_identity,
)
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.projects import discover_project
from literate_ai.storage.cas import FileSystemCAS, StorageError

from .errors import CliFailure
from .execution_workers import add_execution_worker_arguments
from .retained_project import _document, current_retained_project_authority

INSPECTION_SCHEMA = "literate-ai/retained-unit-package-inspection@1"


def _project_context(args):
    from .rebuild import _resolve_standard_binding

    project = discover_project(Path(args.project))
    if project is None:
        raise ValueError("retained.unit.project-required")
    driver = project.definition.lifecycle_driver
    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise ValueError("retained.unit.standard-lifecycle-required")
    _, document = _document(project.root / args.policy, maximum_bytes=65536)
    policy = RetainedUnitPackagePolicy.from_dict(document)
    _, observations = _document(project.root / args.tools, maximum_bytes=65536)
    pkg_tool, tar_tool = parse_retained_unit_tool_observations(observations)
    binding = _resolve_standard_binding(driver)
    authority = current_retained_project_authority(project, binding, args)
    return project, binding, policy, observations, pkg_tool, tar_tool, authority


def _read_context(args):
    project, _binding, policy, observations, pkg_tool, tar_tool, authority = (
        _project_context(args)
    )
    manifest, _worker, planning = authority()
    store = FileSystemCAS(Path(args.custody_root), create=False)
    receipt = retained_unit_metadata_reference(
        store,
        ContentIdentity.parse_uri(args.receipt),
        maximum_bytes=min(64 * 1024**2, policy.max_provider_metadata_bytes),
    )
    evidence = retained_unit_metadata_reference(
        store,
        ContentIdentity.parse_uri(args.planning_evidence),
        maximum_bytes=min(8 * 1024**2, policy.max_provider_metadata_bytes),
    )
    inputs = read_retained_unit_planning_inputs(
        project.root,
        manifest,
        planning.plan,
        current_project_component_locks(project),
        receipt,
        store,
        planning_evidence_reference=evidence,
        maximum_metadata_bytes=policy.max_provider_metadata_bytes,
        maximum_metadata_memory_bytes=policy.max_provider_memory_bytes,
    )
    projection = plan_retained_unit_packages(
        inputs,
        pkg_tool=pkg_tool,
        tar_tool=tar_tool,
        specification_context=bind_retained_unit_specifications(
            project,
            inputs.source,
            inputs.source_manifest,
            inputs.component_locks,
            maximum_units=policy.max_units,
            maximum_memory_bytes=policy.max_provider_memory_bytes,
        ),
        **policy.owner_arguments(),
    )
    return project, inputs, projection, policy, observations, authority


def retained_unit_package_from_args(args):
    try:
        if args.retained_unit_command in {
            "execution-plan",
            "execute",
            "verify-plan",
            "verify",
        }:
            from .retained_unit_execution import execute_retained_unit_package_from_args

            return execute_retained_unit_package_from_args(args)
        if args.retained_unit_command not in {"observe-tools", "plan", "inspect"}:
            raise ValueError("retained.unit.public-operation-required")
        if args.retained_unit_command == "observe-tools":
            return retained_unit_tool_observations(
                FreeBsdPkgToolBinding.discover(), NativeTarToolBinding.discover()
            ), 0
        project, inputs, projection, policy, observations, authority = _read_context(
            args
        )
        result = {
            "schema": INSPECTION_SCHEMA,
            "operation": args.retained_unit_command,
            "authority_mode": "retained-project",
            "admitted": False,
            "native_verified": False,
            "release_authorized": False,
            "retained_receipt_identity": inputs.receipt.identity.uri,
            "policy_identity": policy.identity.uri,
            "native_tool_observations_identity": canonical_identity(observations).uri,
            "set_plan_identity": projection.set_plan.identity.uri,
            "set_plan": projection.set_plan.to_dict(),
            "tree_index": projection.graph.tree.index.to_dict(),
            "graph_index": projection.graph.index.to_dict(),
        }
        if args.unit is not None:
            selected = [
                value
                for value in projection.graph.partition.definitions
                if value.unit_name == args.unit
            ]
            if len(selected) != 1:
                raise ValueError("retained.unit.selected-unit-name-required")
            result["unit_plan"] = projection.unit_plan(selected[0].identity).to_dict()
        if args.retained_unit_command == "inspect":
            reference = retained_unit_metadata_reference(
                inputs.store,
                ContentIdentity.parse_uri(args.result),
                maximum_bytes=policy.max_index_bytes,
            )
            index, results = read_retained_unit_results(
                projection,
                reference,
                inputs.store,
                max_descriptor_bytes=policy.max_descriptor_bytes,
                max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
                max_index_bytes=policy.max_index_bytes,
                max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
            )
            directories = select_cache_directories(project.root, obj_dir=args.obj_dir)
            if any(
                directories.obj_dir == project.root / name
                or directories.obj_dir.is_relative_to(project.root / name)
                for name in inputs.source_manifest.roots
            ):
                raise ValueError("retained.unit.inspection-staging-overlaps-inputs")
            work = (
                ensure_cache_directory(
                    directories.obj_dir,
                    kind="object",
                    project_root=project.root,
                    required_subdirectories=(Path("retained-unit-inspection"),),
                )
                / "retained-unit-inspection"
            )
            inspect_freebsd_composed_package_set(
                projection, results, store=inputs.store, object_root=work
            )
            result["result_index"] = reference.to_dict()
            result["archive_bytes"] = index.archive_bytes
            result["archive_bytes_verified"] = True
        if authority()[2].plan != inputs.plan:
            raise ValueError("retained.unit.inspection-authority-changed")
        return result, 0
    except CliFailure:
        raise
    except ActionWireError as exc:
        raise CliFailure(exc.code, str(exc)) from exc
    except (
        ValueError,
        TypeError,
        OSError,
        PackagingError,
        StorageError,
        CacheDirectoryError,
    ) as exc:
        raise CliFailure("retained_unit_package.invalid", str(exc)) from exc


def add_retained_unit_package_parser(commands):
    root = commands.add_parser(
        "retained-units", help="opt-in retained unit plans and archive inspection"
    )
    actions = root.add_subparsers(
        dest="retained_unit_command", required=True, parser_class=type(root)
    )
    actions.add_parser(
        "observe-tools", help="probe installed native FreeBSD pkg and libarchive tools"
    )
    for operation in (
        "plan",
        "inspect",
        "execution-plan",
        "execute",
        "verify-plan",
        "verify",
    ):
        parser = actions.add_parser(
            operation,
            help=(
                {
                    "plan": "read-only exact planning from historical assertions",
                    "inspect": "inspect archive bytes; grants no admission",
                    "execution-plan": "review current lifecycle and package intent",
                    "execute": "run authorized lifecycle and native unit packaging",
                    "verify-plan": "review original archive verification intent",
                    "verify": "verify original archives under fresh admission",
                }[operation]
            ),
        )
        parser.add_argument("--project", default=".")
        parser.add_argument("--target", default="host")
        parser.add_argument("--retained-project", required=True)
        parser.add_argument("--retained-project-profile", required=True)
        if operation in {"plan", "inspect"}:
            parser.add_argument("--custody-root", required=True)
            parser.add_argument(
                "--receipt", required=True, help="retained receipt identity"
            )
            parser.add_argument(
                "--planning-evidence",
                required=True,
                help="opt-in live-captured BOM observation joins for this receipt",
            )
        parser.add_argument(
            "--policy", required=True, help="explicit package policy JSON"
        )
        parser.add_argument(
            "--tools", required=True, help="native tool observations JSON"
        )
        if operation in {"plan", "inspect"}:
            parser.add_argument("--unit", help="include one selected unit's exact plan")
        if operation == "inspect":
            parser.add_argument(
                "--result", required=True, help="set-result index identity"
            )
            parser.add_argument(
                "--obj-dir", help="owned temporary inspection staging root"
            )
        if operation in {"verify-plan", "verify"}:
            parser.add_argument("--custody-root", required=True)
            parser.add_argument(
                "--result", required=True, help="original set-result index identity"
            )
            parser.add_argument(
                "--verification-timeout-seconds", type=int, default=3600
            )
        if operation in {"execute", "verify"}:
            parser.add_argument("--authorize-retained-project", required=True)
            parser.add_argument("--authorize-unit-packages", required=True)
            parser.add_argument("--allow-host-execution", action="store_true")
            parser.add_argument("--build-dir")
            parser.add_argument("--obj-dir")
        add_execution_worker_arguments(parser)
        parser.set_defaults(
            specification=".",
            force_regeneration=False,
            retained_project_plan=True,
            authorize_retained_project=None,
            obj_dir=None,
        )
