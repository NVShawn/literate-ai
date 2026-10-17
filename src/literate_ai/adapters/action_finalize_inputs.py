"""Disposable FINALIZE package and root-source inputs, without host execution."""

import json
from contextlib import contextmanager
from dataclasses import dataclass

from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_source import materialize_build_source
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_package import materialize_finalize_package
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.retained_package_tree import RetainedPackageTree
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.executable_components import PackageResult


@dataclass(frozen=True)
class FinalizeRuntimeInputs:
    intent: FinalizeWorkerInput
    package: PackageResult
    package_tree: RetainedPackageTree
    source_trees: LocalSourceTreeRegistry
    component_builds: tuple[BuildWorkerInput, ...]

    @property
    def root_build(self):
        return next(
            build
            for build in self.component_builds
            if build.plan.component_revision == self.intent.component_lock.root_revision
        )


@contextmanager
def materialize_finalize_inputs(
    *,
    input_record,
    input_identity,
    records,
    cas,
    workspace_root,
    deadline,
    admission_guard,
    verify_package,
    blob_source=None,
):
    """Reconstruct source suite and package without granting host execution."""
    records = dict(records)

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    with materialize_finalize_package(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        workspace_root=workspace_root,
        deadline=deadline,
        admission_guard=current,
        verify_package=verify_package,
        blob_source=blob_source,
    ) as (value, package, tree):
        builds = []
        for _, identity in value.package_input.link_results:
            current()
            linked_id = ContentIdentity.parse_uri(
                json.loads(records[identity])["input_identity"]
            )
            linked = LinkWorkerInput.admit(records[linked_id], linked_id, deadline)
            builds.append(linked.build)
        builds = tuple(
            sorted(builds, key=lambda build: build.plan.component_revision.uri)
        )
        root = next(
            build
            for build in builds
            if build.plan.component_revision == value.component_lock.root_revision
        )
        with materialize_build_source(
            plan=root.plan,
            inputs=root.inputs,
            candidate=root.candidate,
            files=root.files,
            validation_inputs=root.source_validation,
            source_generation_identity=root.source_generation_identity,
            source_custody_identity=root.source_custody_identity,
            deadline=deadline,
            cas=cas,
            workspace_root=workspace_root,
            blob_source=blob_source,
            admission_guard=current,
        ) as registry:
            inputs = FinalizeRuntimeInputs(value, package, tree, registry, builds)

            def verify():
                current()
                tree.require_unchanged()
                custody = registry.evidence(root.candidate.tree_identity)
                if custody.identity != root.source_custody_identity:
                    raise ActionWireError(
                        "action_finalize.source_changed",
                        "FINALIZE root source custody changed",
                    )
                current()

            verify()
            yield inputs
            verify()
