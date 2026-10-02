"""Plan, construct, and independently verify Flavor-selected native packages."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import tempfile
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from typing import Any

from literate_ai.adapters.conan_packaging import ConanPackageAdapter, ConanToolBinding
from literate_ai.adapters.debian_packaging import (
    DebianPackageAdapter,
    DebianPackageProjection,
    DpkgDebToolBinding,
    project_debian_package,
)
from literate_ai.adapters.execution_dispatch import (
    CommandExecutionDispatcher,
    ExecutionDispatchAdapterError,
    SshExecutionDispatcher,
)
from literate_ai.adapters.lifecycle.standard_local import local_tree_identity
from literate_ai.adapters.packaging import (
    NativeMetadataArchiveAdapter,
    NativeZipPackageAdapter,
    NpmPackageAdapter,
    WheelPackageAdapter,
    lifecycle_archive_package_plan,
    materialized_package_input_bytes,
    native_archive_package_plan,
    native_metadata_archive_plan,
    validate_materialized_package_root,
)
from literate_ai.adapters.ssh_execution import SshLifecycleRequestHandler
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.cache_directories import CacheDirectoryError, resolve_cache_directories
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes
from literate_ai.contracts.executable_components.packages import (
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.execution_dispatch import (
    DispatchResultStatus,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

PACKAGE_BATCH_DECLARATION_SCHEMA = "literate-ai/package-batch-declaration@1"
PACKAGE_BATCH_EVIDENCE_SCHEMA = "literate-ai/native-package-batch@1"
_SAFE_PATH = re.compile(r"[^A-Za-z0-9._-]+")
_PROVIDER_OS = {
    "apt": "linux",
    "brew": "macos",
    "winget": "windows",
    "chocolatey": "windows",
}


def _fail(code: str, message: str):
    from .errors import CliFailure

    raise CliFailure(code, message)


def _package_declaration(
    args: argparse.Namespace,
) -> tuple[Any, Path, dict[str, Any]]:
    from .generation import plan_from_args

    try:
        project = discover_project(Path(args.project))
    except ProjectError as exc:
        _fail(exc.code, exc.message)
    if project is None:
        _fail("project.not_found", f"no {PROJECT_FILENAME} found from {args.project}")
    selected_component = (
        args.component or project.definition.repository_policy.default_component
    )
    if selected_component is None:
        _fail(
            "project.default_component_missing",
            "component is required because repository_policy.default_component is null",
        )
    component = Path(selected_component)
    specification = component if component.is_absolute() else project.root / component

    generation = plan_from_args(
        Namespace(
            specification=str(specification),
            target=args.target,
            flavor=list(args.flavor),
            flavor_root=[],
            recipe_id=None,
            model=args.model,
        )
    )
    selected = generation.get("selected_flavors")
    if not isinstance(selected, list):
        _fail(
            "package.generation_plan_invalid",
            "generation plan does not expose selected Flavor authority",
        )
    providers = tuple(
        item
        for item in selected
        if isinstance(item, dict) and item.get("axis") == "packaging"
    )
    if not providers:
        _fail(
            "package.component_untagged",
            "Component has no selected package.* Flavor; add a bounded packaging "
            "slot and select at least one package provider",
        )
    if any(
        not isinstance(item.get("coordinate"), str)
        or not str(item["coordinate"]).startswith("flavor://")
        or not isinstance(item.get("revision_identity"), str)
        for item in providers
    ):
        _fail(
            "package.provider_authority_invalid",
            "selected package provider lacks exact Flavor authority",
        )
    resolution = generation.get("resolution")
    component_record = generation.get("component")
    specifications = generation.get("specifications")
    input_closure = generation.get("input_closure")
    if not all(
        isinstance(item, dict) for item in (resolution, component_record, input_closure)
    ) or not isinstance(specifications, list):
        _fail(
            "package.generation_plan_invalid",
            "generation plan lacks exact Component or specification authority",
        )
    declaration: dict[str, Any] = {
        "schema": PACKAGE_BATCH_DECLARATION_SCHEMA,
        "component": component_record,
        "resolution": resolution,
        "specifications": specifications,
        "input_closure": input_closure,
        "providers": list(providers),
        "generation_plan_identity": canonical_identity(generation).uri,
        "execution_authorized": False,
        "publication_authorized": False,
    }
    declaration["identity"] = canonical_identity(declaration).uri
    return project, specification, declaration


def _provider_name(provider: dict[str, Any]) -> str:
    coordinate = str(provider["coordinate"])
    mapping = {
        "/package-pip": "pip",
        "/package-zip": "zip",
        "/package-conan": "conan",
        "/package-npm": "npm",
        "/package-apt": "apt",
        "/package-brew": "brew",
        "/package-winget": "winget",
        "/package-chocolatey": "chocolatey",
    }
    for suffix, name in mapping.items():
        if coordinate.endswith(suffix):
            return name
    _fail(
        "package.provider_not_implemented",
        f"native construction is not implemented for {coordinate}",
    )


def validate_package_worker_providers(selected: Any, providers: list[str]) -> None:
    """Reject a native provider that cannot run on the selected worker OS."""

    required = selected.worker.requirements.os_family
    if required is None:
        return
    for name in providers:
        expected = _PROVIDER_OS.get(name)
        if expected is not None and expected != required:
            _fail(
                "package.worker_os_mismatch",
                f"package provider {name!r} requires OS {expected!r}, but worker "
                f"{selected.worker.worker_id!r} binds {required!r}",
            )


def _execution_worker_record(selected: Any) -> dict[str, Any]:
    return {
        "worker_id": selected.worker.worker_id,
        "worker_identity": selected.worker.identity.uri,
        "catalog_identity": selected.catalog_identity.uri,
        "kind": selected.worker.kind.value,
    }


def _dispatch_remote_package_lifecycle(
    args: argparse.Namespace,
    *,
    project_root: Path,
    component: str,
    selected: Any,
) -> dict[str, Any]:
    """Run the accepted lifecycle on a remote worker without publishing an export."""

    from .execution_workers import (
        admit_execution_worker_health,
        create_execution_dispatch_request,
        dispatch_with_worker_health_poll,
    )

    dispatch_request = create_execution_dispatch_request(
        args,
        project_root=project_root,
        component=component,
        selected=selected,
        action=LifecycleDispatchAction.BUILD,
    )
    admit_execution_worker_health(args, selected, dispatch_request.identity)
    try:
        dispatcher = (
            CommandExecutionDispatcher()
            if selected.worker.kind is ExecutionWorkerKind.COMMAND
            else SshExecutionDispatcher(SshLifecycleRequestHandler())
        )
        dispatched, _health_polls = dispatch_with_worker_health_poll(
            args,
            selected,
            dispatch_request.identity,
            lambda: dispatcher.dispatch(
                selected.worker,
                dispatch_request,
                cwd=project_root,
            ),
        )
    except ExecutionDispatchAdapterError as exc:
        admit_execution_worker_health(args, selected, dispatch_request.identity)
        _fail(exc.code, exc.message)
    if dispatched.status is not DispatchResultStatus.PASSED:
        admit_execution_worker_health(args, selected, dispatch_request.identity)
        _fail(
            "execution.dispatch_failed",
            f"worker dispatch ended with {dispatched.status.value}",
        )
    return dispatched.to_dict()


def _safe_name(value: str, *, lowercase: bool = False) -> str:
    normalized = _SAFE_PATH.sub("_", value).strip("._-") or "component"
    return normalized.lower() if lowercase else normalized


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _component_specification_file(specification: Path) -> Path:
    candidate = (
        specification / "component.md" if specification.is_dir() else specification
    )
    if not candidate.is_file() or candidate.is_symlink():
        _fail(
            "package.specification_unavailable",
            "package construction requires a regular Component specification file",
        )
    return candidate


def _build_packages(
    args: argparse.Namespace,
    project: Any,
    specification: Path,
    declaration: dict[str, Any],
) -> dict[str, Any]:
    if not args.allow_host_execution:
        _fail(
            "package.host_execution_not_acknowledged",
            "package build runs the accepted build, test, and application lifecycle; "
            "rerun with --allow-host-execution",
        )
    from .execution_workers import select_execution_worker

    providers = [(_provider_name(item), item) for item in declaration["providers"]]
    selected_worker = select_execution_worker(
        args, project_root=project.root, target_profile=args.target
    )
    validate_package_worker_providers(
        selected_worker, [name for name, _provider in providers]
    )
    if any(name == "apt" for name, _provider in providers) and (
        selected_worker.worker.kind is not ExecutionWorkerKind.LOCAL
    ):
        _fail(
            "package.remote_debian_dispatch_unavailable",
            "Debian artifact construction requires serializable remote package "
            "dispatch; the selected worker only supports lifecycle dispatch",
        )
    component_record = declaration["component"]
    coordinate = str(component_record["coordinate"])
    version = str(component_record["version"])
    distribution = _safe_name(coordinate.rsplit("/", 1)[-1])
    component_specification = _component_specification_file(specification)
    captured: dict[str, Any] = {}
    component_path = (
        specification.relative_to(project.root).as_posix()
        if specification.is_relative_to(project.root)
        else str(specification)
    )
    remote_dispatch = None
    if selected_worker.worker.kind in {
        ExecutionWorkerKind.COMMAND,
        ExecutionWorkerKind.SSH,
    }:
        remote_dispatch = _dispatch_remote_package_lifecycle(
            args,
            project_root=project.root,
            component=component_path,
            selected=selected_worker,
        )

    def observe(rebuilt, adapter, prepared, directories) -> None:
        integration = rebuilt.execution.lifecycle.root_integration
        if integration is None:
            _fail(
                "package.accepted_custody_unavailable",
                "accepted lifecycle did not retain exact root package evidence",
            )
        custody = adapter.runtime.lifecycle_ports.project_package_custody(
            integration.package_plan,
            integration.package_result,
        )
        root_result = next(
            item
            for item in rebuilt.execution.lifecycle.node_results
            if item.component_revision
            == integration.package_plan.root_component_revision
        )
        if root_result.build_evidence is None:
            _fail(
                "package.resolved_sbom_unavailable",
                "accepted root Component has no typed build or resolved-SBOM evidence",
            )
        resolved_sbom = adapter.runtime.lifecycle_ports.resolved_sbom_content(
            root_result.build_evidence
        )
        source_sbom = adapter.runtime.lifecycle_ports.source_sbom_content(
            root_result.build_evidence
        )
        package_root = directories.obj_dir / "packages"
        package_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="native-package-", dir=package_root))
        try:
            input_root = staging / "input-root"
            shutil.copytree(custody.root, input_root)
            spec_destination = input_root / "specification" / "component.md"
            spec_destination.parent.mkdir(parents=True)
            spec_destination.write_bytes(component_specification.read_bytes())
            sbom_destination = input_root / "sbom" / "resolved.cdx.json"
            sbom_destination.parent.mkdir(parents=True)
            sbom_destination.write_bytes(resolved_sbom)
            provider_records: list[dict[str, Any]] = []
            plans: list[PackagePlan] = []
            adapters: list[
                tuple[str, Any, dict[str, bytes], dict[str, object] | None]
            ] = []
            for name, _provider in providers:
                extra: dict[str, bytes] = {}
                adapter_authority: dict[str, object] | None = None
                if name == "pip":
                    native_adapter = WheelPackageAdapter(distribution, version)
                    plan = native_archive_package_plan(
                        integration.package_plan,
                        packager_identity=native_adapter.packager_identity,
                        specification=component_specification.read_bytes(),
                        resolved_sbom=resolved_sbom,
                        resolved_sbom_source_identity=root_result.build_evidence.identity,
                    )
                elif name == "conan":
                    native_adapter = ConanPackageAdapter(
                        _safe_name(distribution, lowercase=True),
                        version.lower(),
                        ConanToolBinding.discover(),
                    )
                    plan = native_archive_package_plan(
                        integration.package_plan,
                        packager_identity=native_adapter.packager_identity,
                        specification=component_specification.read_bytes(),
                        resolved_sbom=resolved_sbom,
                        resolved_sbom_source_identity=root_result.build_evidence.identity,
                    )
                elif name in {"npm", "zip"}:
                    native_adapter = (
                        NativeZipPackageAdapter()
                        if name == "zip"
                        else NpmPackageAdapter(
                            _safe_name(distribution, lowercase=True).replace("_", "-"),
                            version,
                        )
                    )
                    plan = lifecycle_archive_package_plan(
                        integration.package_plan,
                        packager_identity=native_adapter.packager_identity,
                        specification=component_specification.read_bytes(),
                        source_sbom=source_sbom,
                        source_sbom_identity=(
                            root_result.build_evidence.source_sbom.bom_identity
                        ),
                        resolved_sbom=resolved_sbom,
                        resolved_sbom_source_identity=(
                            root_result.build_evidence.identity
                        ),
                    )
                    extra = {"sbom/source.cdx.json": source_sbom}
                elif name == "apt":
                    tool = DpkgDebToolBinding.discover()
                    source_plan = native_archive_package_plan(
                        integration.package_plan,
                        packager_identity=integration.package_plan.packager_identity,
                        specification=component_specification.read_bytes(),
                        resolved_sbom=resolved_sbom,
                        resolved_sbom_source_identity=root_result.build_evidence.identity,
                    )
                    architecture = (
                        selected_worker.worker.requirements.cpu_architecture
                        or platform.machine()
                    )
                    projection = project_debian_package(
                        source_plan,
                        package=_safe_name(distribution, lowercase=True).replace(
                            "_", "-"
                        ),
                        version=version,
                        target_architecture=architecture,
                        worker_architecture=architecture,
                        resolved_sbom=resolved_sbom,
                        tool_identity=tool.identity,
                    )
                    native_adapter = DebianPackageAdapter(projection, tool)
                    plan = replace(
                        source_plan,
                        packager_identity=native_adapter.packager_identity,
                    )
                    adapter_authority = {"debian_projection": projection.to_dict()}
                else:
                    native_adapter = NativeMetadataArchiveAdapter(
                        name, distribution, version
                    )
                    plan, meta_path, meta_bytes = native_metadata_archive_plan(
                        integration.package_plan,
                        packager_identity=native_adapter.packager_identity,
                        specification=component_specification.read_bytes(),
                        resolved_sbom=resolved_sbom,
                        resolved_sbom_source_identity=root_result.build_evidence.identity,
                        provider=name,
                        distribution=distribution,
                        version=version,
                    )
                    extra = {meta_path: meta_bytes}
                plans.append(plan)
                adapters.append((name, native_adapter, extra, adapter_authority))
            batch_identity = canonical_identity(
                {
                    "schema": PACKAGE_BATCH_EVIDENCE_SCHEMA,
                    "declaration_identity": declaration["identity"],
                    "accepted_lifecycle_identity": (
                        rebuilt.execution.lifecycle.identity.uri
                    ),
                    "package_plan_identities": [item.identity.uri for item in plans],
                }
            )
            for (name, native_adapter, extra, adapter_authority), plan in zip(
                adapters, plans, strict=True
            ):
                provider_root = staging / name
                provider_root.mkdir()
                materialized = input_root
                if extra:
                    materialized = provider_root / "materialized"
                    shutil.copytree(input_root, materialized)
                    for relative, body in extra.items():
                        destination = materialized.joinpath(*Path(relative).parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                if name in {"apt", "conan"}:
                    result = native_adapter.package(
                        plan,
                        materialized_root=materialized,
                        object_root=provider_root,
                    )
                else:
                    result = native_adapter.package(
                        plan, materialized_root=materialized
                    )
                content = native_adapter.read_created_blob(result.artifacts[0].blob)
                artifact_path = provider_root / result.artifacts[0].path
                artifact_path.write_bytes(content)
                provider_record = {
                    "provider": name,
                    "plan": plan.to_dict(),
                    "result": result.to_dict(),
                    "artifact": artifact_path.relative_to(staging).as_posix(),
                    "input_root": materialized.relative_to(staging).as_posix(),
                }
                if adapter_authority is not None:
                    provider_record.update(adapter_authority)
                provider_records.append(provider_record)
            evidence = {
                "schema": PACKAGE_BATCH_EVIDENCE_SCHEMA,
                "identity": batch_identity.uri,
                "declaration": declaration,
                "accepted_lifecycle_identity": rebuilt.execution.lifecycle.identity.uri,
                "input_root": "input-root",
                "providers": provider_records,
                "publication_authorized": False,
            }
            (staging / "package-batch.json").write_bytes(canonical_json_bytes(evidence))
            final = package_root / batch_identity.digest
            staged_identity = local_tree_identity(staging)
            if final.exists():
                if final.is_symlink() or local_tree_identity(final) != staged_identity:
                    _fail(
                        "package.output_collision",
                        "existing package batch differs from its exact identity",
                    )
                shutil.rmtree(staging)
            else:
                staging.rename(final)
            index_name = _safe_name(f"{coordinate}-{args.target}", lowercase=True)
            index = {
                "schema": "literate-ai/native-package-index@1",
                "batch_identity": batch_identity.uri,
                "batch": final.relative_to(package_root).as_posix(),
            }
            _write_atomic(
                package_root / "by-component" / f"{index_name}.json",
                canonical_json_bytes(index),
            )
            captured.update(
                {
                    "schema": PACKAGE_BATCH_EVIDENCE_SCHEMA,
                    "identity": batch_identity.uri,
                    "manifest": str(final / "package-batch.json"),
                    "packages": [
                        {
                            "provider": item["provider"],
                            "artifact": str(final / item["artifact"]),
                            "result_identity": canonical_identity(item["result"]).uri,
                        }
                        for item in provider_records
                    ],
                    "publication_authorized": False,
                    "execution_worker": _execution_worker_record(selected_worker),
                }
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    from .rebuild import rebuild_from_args

    rebuild_from_args(
        Namespace(
            specification=str(specification),
            project=str(project.root),
            target=args.target,
            model=args.model,
            runtime_root=None,
            candidate_receipt=None,
            update_receipt=False,
            keep_runtime=False,
            jobs=args.jobs,
            flavor=list(args.flavor),
            force_regeneration=args.force_regeneration,
            source_cache_entry=[],
            source_cache_root=[],
            allow_host_execution=True,
            execution_worker=selected_worker,
        ),
        standard_observer=observe,
    )
    if not captured:
        _fail("package.execution_incomplete", "package observer produced no evidence")
    if remote_dispatch is not None:
        captured["dispatch"] = remote_dispatch
        captured["publication_authorized"] = False
    return captured


def _verify_packages(
    args: argparse.Namespace,
    project: Any,
    declaration: dict[str, Any],
) -> dict[str, Any]:
    from .execution_workers import select_execution_worker

    selected_worker = select_execution_worker(
        args, project_root=project.root, target_profile=args.target
    )
    validate_package_worker_providers(
        selected_worker,
        [_provider_name(item) for item in declaration["providers"]],
    )
    try:
        directories = resolve_cache_directories(project.root)
    except CacheDirectoryError as exc:
        _fail("package.cache_directory_invalid", str(exc))
    coordinate = str(declaration["component"]["coordinate"])
    index_name = _safe_name(f"{coordinate}-{args.target}", lowercase=True)
    package_root = directories.obj_dir / "packages"
    index_path = package_root / "by-component" / f"{index_name}.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        batch = package_root / str(index["batch"])
        evidence = json.loads(
            (batch / "package-batch.json").read_text(encoding="utf-8")
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        _fail("package.evidence_unavailable", "native package evidence is unavailable")
    if evidence.get("declaration", {}).get("identity") != declaration["identity"]:
        _fail(
            "package.declaration_changed", "package selection changed; rebuild packages"
        )
    verified: list[dict[str, str]] = []
    for record in evidence["providers"]:
        input_root = batch / str(record.get("input_root", evidence["input_root"]))
        plan = PackagePlan.from_dict(record["plan"])
        result = PackageResult.from_dict(record["result"])
        root, materialized = validate_materialized_package_root(plan, input_root)
        artifact_path = batch / str(record["artifact"])
        content = artifact_path.read_bytes()

        def read_blob(
            reference: BlobRef,
            *,
            package_result=result,
            package_content=content,
            package_plan=plan,
            inputs=materialized,
        ) -> bytes:
            if reference == package_result.artifacts[0].blob:
                return package_content
            item = next(
                candidate
                for candidate in package_plan.inputs
                if candidate.blob == reference
            )
            return materialized_package_input_bytes(inputs[item.path])

        verify_package_result(plan, result, read_blob=read_blob)
        provider = str(record["provider"])
        component = declaration["component"]
        distribution = _safe_name(str(component["coordinate"]).rsplit("/", 1)[-1])
        version = str(component["version"])
        if provider == "pip":
            WheelPackageAdapter(distribution, version).verify_bytes(result, content)
        elif provider == "zip":
            NativeZipPackageAdapter().verify_bytes(result, content)
        elif provider == "conan":
            ConanPackageAdapter(
                _safe_name(distribution, lowercase=True),
                version.lower(),
                ConanToolBinding.discover(),
            ).verify_bytes(result, content, object_root=directories.obj_dir)
        elif provider == "npm":
            NpmPackageAdapter(
                _safe_name(distribution, lowercase=True).replace("_", "-"),
                version,
            ).verify_bytes(result, content)
        elif provider == "apt":
            projection = DebianPackageProjection.from_dict(
                record.get("debian_projection"),
                path="package evidence.debian_projection",
            )
            DebianPackageAdapter(
                projection, DpkgDebToolBinding.discover()
            ).verify_bytes(result, content)
        elif provider in {"brew", "winget", "chocolatey"}:
            NativeMetadataArchiveAdapter(provider, distribution, version).verify_bytes(
                result, content
            )
        else:
            _fail("package.provider_not_implemented", f"unknown provider {provider}")
        verified.append({"provider": provider, "artifact": str(artifact_path)})
        del root
    return {
        "schema": "literate-ai/native-package-verification@1",
        "batch_identity": evidence["identity"],
        "verified": verified,
        "publication_authorized": False,
        "execution_worker": _execution_worker_record(selected_worker),
    }


def package_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.package_command not in {"plan", "build", "verify"}:
        _fail("cli.usage", "a package command is required")
    try:
        project, specification, declaration = _package_declaration(args)
        if args.package_command == "plan":
            return declaration, 0
        if args.package_command == "build":
            return _build_packages(args, project, specification, declaration), 0
        return _verify_packages(args, project, declaration), 0
    except PackagingError as exc:
        _fail("package.execution_failed", str(exc))


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("component", nargs="?", default=None)
    parser.add_argument("--project", default=".")
    parser.add_argument("--target", default="host")
    parser.add_argument("--flavor", action="append", default=[])
    parser.add_argument(
        "--model",
        help=(
            "pipeline model inherited by source generation; narrower Component, "
            "Flavor, or skill scopes may override it"
        ),
    )


def add_package_parser(commands: argparse._SubParsersAction) -> None:
    package = commands.add_parser(
        "package",
        help="plan, build, and verify exact native packages for tagged Components",
        description=(
            "Plan one or more Flavor-selected native packages, construct them only "
            "after the full accepted lifecycle, or independently verify retained "
            "package bytes. No package command publishes to a registry."
        ),
    )
    package_commands = package.add_subparsers(
        dest="package_command", required=True, parser_class=type(package)
    )
    plan = package_commands.add_parser(
        "plan", help="create a read-only multi-provider package declaration"
    )
    _add_common_arguments(plan)
    build = package_commands.add_parser(
        "build", help="run the accepted lifecycle and construct selected packages"
    )
    _add_common_arguments(build)
    build.add_argument("--jobs", type=int, default=1)
    build.add_argument("--force-regeneration", action="store_true")
    build.add_argument("--allow-host-execution", action="store_true")
    from .execution_workers import add_execution_worker_arguments

    add_execution_worker_arguments(build)
    verify = package_commands.add_parser(
        "verify", help="independently verify the current retained package batch"
    )
    _add_common_arguments(verify)
    add_execution_worker_arguments(verify)


__all__ = [
    "PACKAGE_BATCH_DECLARATION_SCHEMA",
    "PACKAGE_BATCH_EVIDENCE_SCHEMA",
    "add_package_parser",
    "package_from_args",
    "validate_package_worker_providers",
]
