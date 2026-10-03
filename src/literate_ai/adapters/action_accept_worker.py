"""Privately configured ACCEPT execution inside a one-shot action receiver."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_accept import (
    admit_accept_action,
    execute_accept_action,
)
from literate_ai.adapters.action_accept_preflight import hydrate_accept_proof
from literate_ai.adapters.action_build_result import (
    _remove_owned_stage,
    read_build_result,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_provider_build import read_provider_build
from literate_ai.adapters.action_source_index import hydrate_action_source
from literate_ai.adapters.configured_tool_worker import ConfiguredToolWorker
from literate_ai.adapters.exclusive_directory import directory_node


class ConfiguredAcceptWorker(ConfiguredToolWorker):
    """Privately configured ACCEPT startup and owned action execution."""

    def __init__(self, launcher, *, environment):
        super().__init__(
            launcher,
            (),
            phase="ACCEPT",
            environment=environment,
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
        handoff = admit_accept_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        execution = handoff.execution_input
        admitted = execution.build_input
        profile = self.identity

        def require_current():
            if cancelled():
                raise ActionWireError(
                    "action_accept.cancelled", "ACCEPT action was cancelled"
                )
            deadline.remaining()
            admitted.inputs.authorization.grant.require_valid(
                admitted.inputs.intent.build_request, now=datetime.now(UTC)
            )
            if self.identity != profile:
                raise ActionWireError(
                    "action_accept.profile_changed", "ACCEPT startup profile changed"
                )
            _ = self.launchers.identities
            if self._standard_observations is not None:
                self.observe_tools(require_current=deadline.remaining)

        require_current()
        if not workspace_root.is_absolute() or not cas.root.is_absolute():
            raise ActionWireError(
                "action_accept.private_path_invalid", "worker paths must be absolute"
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
            execution.accepted_providers, execution.provider_builds, strict=True
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
        raw_input = admitted.to_bytes()
        raw_result = execution.build_result.to_bytes()
        read_build_result(
            content=raw_result,
            result_identity=record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=deadline,
            cas=cas,
            blob_source=blob_source,
            require_current=require_current,
        )
        hydrate_accept_proof(
            handoff, cas=cas, blob_source=blob_source, require_current=require_current
        )
        require_current()
        if directory_node(workspace_root) != parent:
            raise ActionWireError(
                "action_accept.workspace_invalid", "worker workspace changed"
            )
        job = Path(tempfile.mkdtemp(prefix="accept-", dir=workspace_root))
        owned = directory_node(job)
        try:
            require_current()
            result = execute_accept_action(
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
