"""Privately configured BUILD execution inside a one-shot action receiver."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build import admit_build_action, execute_build_action
from literate_ai.adapters.action_build_record import required_build_toolchains
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_provider_build import read_provider_build
from literate_ai.adapters.action_source_index import hydrate_action_source
from literate_ai.adapters.configured_tool_worker import ConfiguredToolWorker
from literate_ai.adapters.exclusive_directory import directory_node


class ConfiguredBuildWorker(ConfiguredToolWorker):
    """Privately configured BUILD startup and owned action execution."""

    def __init__(self, launcher, tool_bindings, *, environment, standard_tools=None):
        super().__init__(
            launcher,
            tool_bindings,
            phase="BUILD",
            environment=environment,
            standard_tools=standard_tools,
        )

    def execute(
        self,
        request,
        deadline,
        records,
        *,
        expected_worker_identity,
        cas,
        workspace_root,
        blob_source=None,
        cancelled=lambda: False,
    ):
        admitted = admit_build_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        selected = required_build_toolchains(admitted.inputs)

        def require_current():
            if cancelled():
                raise ActionWireError(
                    "action_build.cancelled", "BUILD action was cancelled"
                )
            deadline.remaining()
            admitted.inputs.authorization.grant.require_valid(
                admitted.inputs.intent.build_request, now=datetime.now(UTC)
            )
            _ = self.launchers.identities
            if self._standard_observations is not None:
                self.observe_tools(require_current=deadline.remaining)
            self.tools.select(selected)

        require_current()
        if not workspace_root.is_absolute() or not cas.root.is_absolute():
            raise ActionWireError(
                "action_build.private_path_invalid", "worker paths must be absolute"
            )
        require_safe_directory(workspace_root)
        parent = directory_node(workspace_root)
        hydrate_action_source(
            admitted.files,
            admitted.candidate.tree_identity,
            deadline,
            cas=cas,
            blob_source=blob_source,
            require_current=require_current,
        )
        for receipt, transfer in zip(
            admitted.accepted_providers, admitted.provider_builds, strict=True
        ):
            read_provider_build(
                transfer=transfer,
                receipt=receipt,
                generation_plan=next(
                    item
                    for item in admitted.execution_plan.generation_plans
                    if item.component_revision == receipt.component_revision
                ),
                cas=cas,
                deadline=deadline,
                require_current=require_current,
                blob_source=blob_source,
            )
        if directory_node(workspace_root) != parent:
            raise ActionWireError(
                "action_build.workspace_invalid", "worker workspace changed"
            )
        job = Path(tempfile.mkdtemp(prefix="build-", dir=workspace_root))
        owned = directory_node(job)
        try:
            require_current()
            result = execute_build_action(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker_identity,
                launcher=self.launcher,
                cwd=job,
                environment=self.environment,
                cancelled=cancelled,
                cas_root=cas.root,
                workspace_root=job,
            )
            require_current()
        finally:
            _remove_owned_stage(job, owned)
        require_current()
        return result
