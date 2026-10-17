"""Controller-owned portable FINALIZE verification with disposable input custody."""

import tempfile
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_finalize_inputs import materialize_finalize_inputs
from literate_ai.adapters.action_finalize_portable import (
    verify_portable_finalize_stages,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.contracts import ContentIdentity


class PortableFinalizeVerification:
    def __init__(self, *, oracle, workspace_root, require_configuration):
        if not isinstance(
            getattr(oracle, "identity", None), ContentIdentity
        ) or not all(
            callable(item)
            for item in (getattr(oracle, "cases", None), require_configuration)
        ):
            raise TypeError(
                "portable FINALIZE requires a private oracle and configuration guard"
            )
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            raise ValueError("FINALIZE verification requires an absolute workspace")
        self.oracle = oracle
        self.workspace_root = workspace_root
        self.require_configuration = require_configuration

    @contextmanager
    def __call__(self, **inputs):
        admission = inputs["admission_guard"]

        def current():
            self.require_configuration()
            admission()

        current()
        require_safe_directory(self.workspace_root)
        parent = directory_node(self.workspace_root)
        job = Path(tempfile.mkdtemp(prefix="finalize-proof-", dir=self.workspace_root))
        owned = directory_node(job)
        try:
            current()
            if directory_node(self.workspace_root) != parent:
                raise ValueError("FINALIZE verification workspace changed")
            with materialize_finalize_inputs(
                **(inputs | {"admission_guard": current}), workspace_root=job
            ) as prepared:
                verify = partial(
                    verify_portable_finalize_stages,
                    prepared=prepared,
                    oracle=self.oracle,
                )

                def verify_current(value, evidence, records):
                    current()
                    verify(value, evidence, records)
                    current()

                yield verify_current
                current()
        finally:
            _remove_owned_stage(job, owned)
