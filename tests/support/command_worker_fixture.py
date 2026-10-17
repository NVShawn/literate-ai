"""Private accepted-worker launcher shared by lifecycle regressions."""

_ACCEPT_CHILD = """
import os, shutil
from pathlib import Path
from contextlib import contextmanager
from literate_ai.accept_worker import main
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
@contextmanager
def factory(build, registry, recorder):
    ports = LocalStandardLifecyclePorts(
        source_trees=registry,
        object_root=Path(os.environ['LITAI_ACCEPT_WORKSPACE'])/'objects',
        contracts=(build.inputs.contract,), tool_bindings=(), command_phases=(),
    )
    ports.retain_evidence_with(recorder)
    try: yield ports
    finally: shutil.rmtree(ports.object_root)
raise SystemExit(main(runtime_factory=factory))
"""
