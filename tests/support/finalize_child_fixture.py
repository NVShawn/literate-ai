"""Private startup for the real portable FINALIZE child integration fixture.

The authorized entrypoint uses test-issued file grants and measured runtime checks.
The command receiver composes production parent authority from a private startup
directory holding the pinned child environment and the operator grant. Commands,
oracle and input proof verification use production adapters.
"""

import os
import sys
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from literate_ai.adapters.action_finalize_execution import FinalizeWorkerRuntime
from literate_ai.adapters.action_finalize_loader import load_finalize_records
from literate_ai.adapters.action_package_result import verify_deterministic_package
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalIndependentAcceptanceCase,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.contracts import canonical_identity
from literate_ai.finalize_worker import main


class ExactOutputOracle:
    identity = canonical_identity("finalize-fixture-output-oracle")

    def cases(self, lock):
        return (
            LocalIndependentAcceptanceCase.create("known-output", [], "known-output"),
        )


@contextmanager
def runtime_factory(prepared):
    root = Path(os.environ["LITAI_FINALIZE_WORKSPACE"]) / "runtime"
    ports = LocalStandardLifecyclePorts(
        source_trees=prepared.source_trees,
        object_root=root,
        contracts=tuple(build.inputs.contract for build in prepared.component_builds),
        tool_bindings=(LocalComponentToolBinding(sys.executable),),
        independent_acceptance_oracle=ExactOutputOracle(),
    )
    try:
        yield FinalizeWorkerRuntime(ports)
    finally:
        if ports._project_packages or ports._exports_by_identity:
            raise RuntimeError("runtime registration leaked")
        root.rmdir()


def run():
    return main(
        runtime_factory=runtime_factory,
        proof_loader=partial(load_finalize_records, admission_guard=lambda: None),
        verify_package=partial(
            verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
        ),
        admission_guard=lambda: None,
        require_execution_authority=lambda stage, prepared: None,
    )


def run_authorized(grant_path, runtime_identity):
    """Use production private startup with test-issued contracts and grant."""
    from literate_ai.adapters.action_finalize_startup import (
        PortableFinalizeRuntimeFactory,
    )
    from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
    from literate_ai.contracts import ContentIdentity

    active = {}

    @contextmanager
    def authorized_runtime(prepared):
        tools = (LocalComponentToolBinding(sys.executable),)
        configured = PortableFinalizeRuntimeFactory(
            workspace_root=Path(os.environ["LITAI_FINALIZE_WORKSPACE"]),
            contracts=tuple(
                build.inputs.contract for build in prepared.component_builds
            ),
            tool_bindings=tools,
            oracle=ExactOutputOracle(),
            startup=WorkerToolchainRegistry(tools),
            grant_path=Path(grant_path),
            runtime_identity=(
                ContentIdentity.parse_uri(runtime_identity)
                if runtime_identity
                else None
            ),
            admission_guard=lambda: None,
        )
        active["runtime"] = configured
        try:
            with configured(prepared) as runtime:
                yield runtime
        finally:
            active.clear()

    return main(
        runtime_factory=authorized_runtime,
        proof_loader=partial(load_finalize_records, admission_guard=lambda: None),
        verify_package=partial(
            verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
        ),
        admission_guard=lambda: None,
        require_execution_authority=lambda stage, prepared: active[
            "runtime"
        ].require_stage(stage, prepared),
    )


def receiver(private):
    """Private fixture action receiver with real PACKAGE and FINALIZE adapters.

    The private directory holds environment.json (the exact child environment)
    and grant.json (the operator grant). Requests select neither.
    """
    import json

    from literate_ai.action_worker import main as receive
    from literate_ai.adapters.action_finalize_parent import (
        PortableFinalizeParentAuthority,
    )
    from literate_ai.adapters.action_finalize_portable import (
        verify_portable_finalize_stages,
    )
    from literate_ai.adapters.action_finalize_worker import ConfiguredFinalizeWorker
    from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
    from literate_ai.adapters.builders.python import discover_python_toolchain

    private = Path(private)
    grant_path = private / "grant.json"
    environment = json.loads((private / "environment.json").read_text("utf-8"))
    repository = Path(__file__).resolve().parents[2]
    startup = (
        f"import sys; sys.path[:0]=[{str(repository)!r},{str(repository / 'src')!r}]; "
        "from tests.support.finalize_child_fixture import run_authorized; "
        f"raise SystemExit(run_authorized({str(grant_path)!r}, ''))"
    )
    runtime = discover_python_toolchain(pinned_command=(sys.executable,))
    profile = canonical_identity("finalize-command-fixture")
    authority = PortableFinalizeParentAuthority(
        grant_path=grant_path,
        tool_bindings=(LocalComponentToolBinding(sys.executable),),
        oracle=ExactOutputOracle(),
    )
    finalizer = ConfiguredFinalizeWorker(
        LocalComponentToolBinding(
            sys.executable,
            ("-I", "-B", "-c", startup),
            authority_identity=canonical_identity(
                {"runtime": runtime.identity, "code": startup}
            ),
            _authority_guard=runtime.require_unchanged,
        ),
        environment=environment,
        runtime_identity=profile,
        observe_runtime=lambda: profile,
        require_execution_authority=authority.admit,
        require_prepared_authority=authority.require,
        plan_prepared_grant=authority.grant_request,
        verify_package=partial(
            verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
        ),
        verify_stages=lambda prepared, value, evidence, records: (
            verify_portable_finalize_stages(
                value, evidence, records, prepared=prepared, oracle=ExactOutputOracle()
            )
        ),
    )
    return receive(
        finalize_worker=finalizer,
        package_worker=ConfiguredPackageWorker(
            canonical_identity({"packager": "local-directory-package@1"}),
            DirectoryPackageAdapter,
            lambda: None,
        ),
    )
