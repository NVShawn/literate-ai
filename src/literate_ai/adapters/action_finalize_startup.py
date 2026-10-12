"""Private portable FINALIZE runtime construction with live file-grant authority.

Use only in the supervised one-shot child. Startup supplies reviewed contracts,
tools, oracle, profile and grant path; requests cannot select imports or grants.
"""

import os
import tempfile
import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_execution import FinalizeWorkerRuntime
from literate_ai.adapters.action_finalize_grant import FileFinalizeExecutionAuthority
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.action_finalize_profile import (
    portable_finalize_runtime_identity,
)
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts
from literate_ai.contracts import ContentIdentity

_CONTROLS = frozenset(
    {
        "litai_finalize_input_identity",
        "litai_finalize_deadline",
        "litai_finalize_cas",
        "litai_finalize_workspace",
    }
)
_CHILD_RUNTIME_LOCK = threading.Lock()


class PortableFinalizeRuntimeFactory:
    """Own disposable ports and the stage guard for one private child operation.

    Transport controls are consumed by finalize_worker.main before this factory
    runs. They are removed from the actual project-command environment while the
    runtime is active, then restored. Parallel in-process runtime use is refused.
    The deployment must protect grant storage from generated host code.
    runtime_identity may be None when startup cannot know the per-project runtime;
    the grant must then name the runtime this child actually measures.
    """

    def __init__(
        self,
        *,
        workspace_root,
        contracts,
        tool_bindings,
        oracle,
        startup,
        grant_path,
        runtime_identity,
        admission_guard,
    ):
        if (
            not isinstance(startup, WorkerToolchainRegistry)
            or not (
                runtime_identity is None
                or isinstance(runtime_identity, ContentIdentity)
            )
            or not callable(admission_guard)
        ):
            raise TypeError("FINALIZE requires measured private startup and admission")
        for path in (workspace_root, grant_path):
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or ".." in path.parts
            ):
                raise ValueError("FINALIZE private paths must be absolute")
        require_safe_directory(workspace_root)
        require_safe_directory(grant_path.parent)
        if grant_path.resolve().is_relative_to(workspace_root.resolve()):
            raise ValueError("FINALIZE grant must be outside child workspace")
        self.workspace_root = workspace_root
        self.workspace_node = directory_node(workspace_root)
        self.contracts = tuple(contracts)
        self.tool_bindings = tuple(tool_bindings)
        self.oracle, self.startup = oracle, startup
        self.grant_path, self.runtime_identity = grant_path, runtime_identity
        self.admission_guard = admission_guard
        self._active = None

    def _current(self):
        self.admission_guard()
        if directory_node(self.workspace_root) != self.workspace_node:
            raise ActionWireError(
                "action_finalize.workspace_invalid", "workspace changed"
            )

    def require_stage(self, stage, prepared):
        if self._active is None or self._active[0] is not prepared:
            raise ActionWireError(
                "action_finalize.authority_mismatch", "FINALIZE runtime is not active"
            )
        self._current()
        self._active[1].require_stage(stage, prepared)
        self._current()

    @contextmanager
    def __call__(self, prepared):
        if not isinstance(prepared, FinalizeRuntimeInputs):
            raise TypeError("FINALIZE requires verified runtime inputs")
        if not _CHILD_RUNTIME_LOCK.acquire(blocking=False):
            raise ActionWireError(
                "action_finalize.runtime_busy", "child runtime already active"
            )
        job = owned = None
        controls = {}
        try:
            self._current()
            job = Path(tempfile.mkdtemp(prefix="runtime-", dir=self.workspace_root))
            owned = directory_node(job)
            self._current()
            controls = {
                key: os.environ.pop(key)
                for key in tuple(os.environ)
                if key.casefold() in _CONTROLS
            }
            ports = LocalStandardLifecyclePorts(
                source_trees=prepared.source_trees,
                object_root=job,
                contracts=self.contracts,
                tool_bindings=self.tool_bindings,
                independent_acceptance_oracle=self.oracle,
            )
            observe = partial(
                portable_finalize_runtime_identity,
                prepared.intent,
                ports,
                startup=self.startup,
                environment=os.environ,
            )
            # Without a startup pin, the operator grant alone names the runtime.
            authority = FileFinalizeExecutionAuthority(
                grant_path=self.grant_path,
                runtime_identity=self.runtime_identity or observe(),
                observe_runtime=observe,
            )
            authority(prepared.intent)
            self._active = (prepared, authority)
            yield FinalizeWorkerRuntime(ports)
            self._current()
            authority(prepared.intent)
        finally:
            self._active = None
            try:
                if owned is not None:
                    _remove_owned_stage(job, owned)
            finally:
                os.environ.update(controls)
                _CHILD_RUNTIME_LOCK.release()
