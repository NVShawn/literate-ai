"""Scope verified FINALIZE package custody into privately constructed local ports."""

from contextlib import contextmanager

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.lifecycle.standard_local import (
    LocalProjectPackageCustody,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_python import (
    StandardPythonDependencyObserver,
)
from literate_ai.contracts.executable_components import PackageFileKind


@contextmanager
def bind_finalize_runtime(prepared, ports, *, admission_guard, python_observer=None):
    """Register exact package/source custody temporarily; grant no host execution."""
    if (
        not isinstance(prepared, FinalizeRuntimeInputs)
        or not isinstance(ports, LocalStandardLifecyclePorts)
        or not callable(admission_guard)
    ):
        raise TypeError("FINALIZE requires prepared inputs and private Standard ports")
    value, root = prepared.intent, prepared.root_build
    plan, package = value.package_input.plan, prepared.package
    graph = value.package_input.artifact_graph

    def current():
        admission_guard()
        prepared.package_tree.require_unchanged()
        if (
            ports.source_trees is not prepared.source_trees
            or any(
                ports.contracts.get(build.plan.component_revision.uri)
                != build.inputs.contract
                for build in prepared.component_builds
            )
            or ports.source_trees.evidence(root.candidate.tree_identity).identity
            != root.source_custody_identity
        ):
            raise ActionWireError(
                "action_finalize.runtime_mismatch", "private runtime differs"
            )

    current()
    exports = {
        export.identity.uri: export
        for manifest in graph.manifests
        for export in manifest.exports
    }
    paths = {
        item.source_identity.uri: prepared.package_tree.root / item.path
        for item in plan.inputs
        if item.kind is PackageFileKind.ARTIFACT
    }
    sdk_resources = sdk_scope = None
    if ports._native_sdk_inputs is not None:
        from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
        from literate_ai.adapters.native_sdk_package_scope import (
            NativeSdkPackageExecutionScope,
        )

        sdk_resources = NativeSdkPackageResources(
            ports._native_sdk_inputs,
            component_lock_identity=value.component_lock.identity,
            root_revision=value.component_lock.root_revision,
            target_identity=plan.target_identity,
            consumer_revisions=tuple(
                sorted(
                    {exports[key].component_revision for key in paths},
                    key=lambda item: item.uri,
                )
            ),
        )
        if not set(sdk_resources.inputs) <= set(plan.inputs) or not set(
            sdk_resources.runtime_requirements
        ) <= set(plan.runtime_requirements):
            raise ActionWireError(
                "action_finalize.sdk_mismatch", "private SDK package differs"
            )
        sdk_resources.verify_materialized(prepared.package_tree.root)
        if any(
            binding.build.selection.component_revision
            != value.component_lock.root_revision
            for binding in sdk_resources.bindings
        ):
            link = next(
                item
                for item in graph.link_plans
                if item.identity == plan.link_plan_identity
            )
            sdk_scope = NativeSdkPackageExecutionScope(
                value.component_lock,
                graph,
                link,
                tuple(
                    build.plan
                    for build in prepared.component_builds
                    if build.plan.component_revision
                    in {exports[key].component_revision for key in paths}
                ),
                tuple(
                    build.inputs.contract
                    for build in prepared.component_builds
                    if build.plan.component_revision
                    in {exports[key].component_revision for key in paths}
                ),
            )
            sdk_scope.select_bindings(
                sdk_resources.bindings, value.component_lock.root_revision
            )
    elif any(
        build.plan.materialization.native_sdk_input_identities
        for build in prepared.component_builds
    ):
        raise ActionWireError(
            "action_finalize.sdk_missing", "private SDK runtime is required"
        )
    if sdk_resources is None and any(
        item.role.startswith("native-sdk") for item in plan.inputs
    ):
        raise ActionWireError(
            "action_finalize.sdk_missing", "private SDK runtime is required"
        )
    if root.plan.component_revision.uri not in ports.python_targets and any(
        item.role == "python-runtime-resource" for item in plan.inputs
    ):
        raise ActionWireError(
            "action_finalize.python_missing", "private Python target is required"
        )
    python_tree = None
    if root.plan.component_revision.uri in ports.python_targets:
        if not isinstance(python_observer, StandardPythonDependencyObserver):
            raise ActionWireError(
                "action_finalize.python_missing", "private Python observer is required"
            )
        binding = python_observer.binding
        artifact_root = paths[plan.root_artifact_identity.uri].parent
        if (
            python_observer.artifact_root != artifact_root
            or binding.build_plan_identity != root.plan.identity
            or binding.component_revision != root.plan.component_revision
            or binding.source_tree_identity != root.candidate.tree_identity
            or binding.authorization_identity
            != root.plan.request.authorization_identity
            or binding.command_contract_identity != root.inputs.contract.identity
            or binding.python_toolchain_identity
            != root.inputs.contract.language_runtime_identity
            or any(
                item.source_identity != python_observer.expected_identity
                for item in plan.inputs
                if item.role == "python-runtime-resource"
            )
        ):
            raise ActionWireError(
                "action_finalize.python_mismatch", "private Python custody differs"
            )
        python_observer()
        python_tree = local_tree_identity(artifact_root)
    elif python_observer is not None:
        raise ActionWireError(
            "action_finalize.python_mismatch", "unexpected Python observer"
        )
    source = prepared.source_trees.evidence(root.candidate.tree_identity)
    custody = LocalProjectPackageCustody(
        plan,
        package,
        prepared.package_tree.root,
        paths,
        root.plan,
        source.generated_test_suite,
        local_tree_identity(prepared.package_tree.root),
        sdk_resources,
        sdk_scope,
        python_observer,
        python_tree,
    )
    bindings = {
        "_exports_by_identity": exports,
        "_planned_exports": {
            root.plan.component_revision.uri: exports[plan.root_artifact_identity.uri]
        },
        "_plans_by_revision": {
            build.plan.component_revision.uri: build.plan
            for build in prepared.component_builds
        },
        "_intent_artifacts": {
            build.inputs.intent.identity.uri: build.inputs.providers
            for build in prepared.component_builds
        },
        "_project_packages": {package.identity.uri: custody},
    }
    for name, entries in bindings.items():
        if set(getattr(ports, name)) & set(entries):
            raise ActionWireError(
                "action_finalize.runtime_occupied",
                "runtime custody is already registered",
            )
    current()
    try:
        for name, entries in bindings.items():
            getattr(ports, name).update(entries)
        current()
        yield ports
        current()
        ports.project_package_custody(plan, package)
    finally:
        for name, entries in bindings.items():
            for key in entries:
                getattr(ports, name).pop(key, None)
