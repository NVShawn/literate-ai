"""Startup-owned GENERATE receiver with private, live model authority."""

import tempfile
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_generate import (
    admit_generate_action,
    execute_generate_action,
)
from literate_ai.adapters.action_retained_source import transfer_retained_input
from literate_ai.adapters.configured_tool_worker import ConfiguredToolWorker
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.storage.cas import BlobNotFoundError


class ConfiguredGenerateWorker(ConfiguredToolWorker):
    """Private admission receives the exact input; requests cannot supply policy."""

    def __init__(
        self,
        launcher,
        *,
        environment,
        authority_identity,
        admission_guard,
        tool_bindings=(),
    ):
        if not isinstance(authority_identity, ContentIdentity) or not callable(
            admission_guard
        ):
            raise TypeError("GENERATE requires identified private admission authority")
        super().__init__(
            launcher, tool_bindings, phase="GENERATE", environment=environment
        )
        self._authority_identity = authority_identity
        self._admission_guard = admission_guard

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/configured-generate-authority@1",
                "profile": super().identity.uri,
                "authority": self._authority_identity.uri,
            }
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
        value = admit_generate_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        profile = self.identity

        def current():
            if cancelled():
                raise ActionWireError(
                    "action_generate.cancelled", "GENERATE action was cancelled"
                )
            deadline.remaining()
            if self.identity != profile:
                raise ActionWireError(
                    "action_generate.profile_changed",
                    "GENERATE startup profile changed",
                )
            self._admission_guard(value)
            deadline.remaining()

        current()
        root = Path(workspace_root)
        if not root.is_absolute() or not cas.root.is_absolute():
            raise ActionWireError(
                "action_generate.private_path_invalid", "worker paths must be absolute"
            )
        require_safe_directory(root)
        if root != root.resolve(strict=True):
            raise ActionWireError(
                "action_generate.workspace_invalid", "worker root must be canonical"
            )
        parent = directory_node(root)
        try:
            prompt = cas.get_bytes(value.prompt)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            prompt = blob_source(value.prompt)
            current()
            value.read_prompt(prompt, deadline)
            if (
                cas.put_bytes(prompt, media_type=value.prompt.media_type)
                != value.prompt
            ):
                raise ActionWireError(
                    "action_generate.prompt_invalid", "prompt transport changed custody"
                ) from None
        value.read_prompt(prompt, deadline)
        if value.retained is not None:
            transfer_retained_input(
                value.retained,
                cas=cas,
                deadline=deadline,
                admission_guard=current,
                blob_source=blob_source,
            )
        current()
        if directory_node(root) != parent:
            raise ActionWireError(
                "action_generate.workspace_changed", "worker root changed"
            )
        job = Path(tempfile.mkdtemp(prefix="generate-", dir=root))
        owned = directory_node(job)
        try:
            current()
            result = execute_generate_action(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker_identity,
                admission_guard=current,
                launcher=self.launcher,
                cwd=job,
                environment=self.environment,
                cancelled=cancelled,
                cas_root=cas.root,
                workspace_root=job,
            )
            current()
        finally:
            try:
                same_parent = directory_node(root) == parent
            except (OSError, ValueError):
                same_parent = False
            if same_parent:
                _remove_owned_stage(job, owned)
        current()
        return result
