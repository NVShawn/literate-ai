"""FINALIZE composition for the reference Standard receiver.

The verifier-owned oracle is an operator-provisioned copy named by private
config; requests never carry it. Grants come only from the configured grant
file, checked by the receiver parent before staging and after measuring the
portable runtime, and again by the child at every stage. Every Component build
the child will run must pass the receiver's contract policy first.
"""

import os
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from literate_ai.adapters.action_finalize_loader import load_finalize_records
from literate_ai.adapters.action_finalize_parent import (
    PortableFinalizeParentAuthority,
)
from literate_ai.adapters.action_finalize_portable import (
    verify_portable_finalize_stages,
)
from literate_ai.adapters.action_finalize_startup import (
    PortableFinalizeRuntimeFactory,
)
from literate_ai.adapters.action_finalize_worker import ConfiguredFinalizeWorker
from literate_ai.adapters.action_package_result import verify_deterministic_package
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.component_acceptance import (
    FilesystemComponentAcceptanceOracle,
)
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.adapters.standard_receiver_runtime import (
    require_portable_starter_build,
)
from literate_ai.contracts import canonical_identity

_VERIFY_PACKAGE = partial(
    verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
)


def receiver_oracle(config):
    settings = config.finalize
    return FilesystemComponentAcceptanceOracle(
        settings.oracle_path.parent,
        settings.oracle_component,
        path=settings.oracle_path,
    )


def finalize_profile(config, code_identity):
    """The private profile an operator pins in controller configuration."""
    return canonical_identity(
        {
            "schema": "literate-ai/standard-receiver-finalize@1",
            "config": config.identity.uri,
            "code": code_identity.uri,
        }
    )


def configured_finalize_worker(config, launcher, code_identity, tools):
    profile = finalize_profile(config, code_identity)
    oracle = receiver_oracle(config)
    authority = PortableFinalizeParentAuthority(
        grant_path=config.finalize.grant_path,
        tool_bindings=(tools.python_binding,),
        oracle=oracle,
    )

    def observe_runtime():
        config.require_unchanged()
        return profile

    return ConfiguredFinalizeWorker(
        launcher,
        environment=dict(config.child_environment),
        runtime_identity=profile,
        observe_runtime=observe_runtime,
        require_execution_authority=authority.admit,
        require_prepared_authority=authority.require,
        plan_prepared_grant=authority.grant_request,
        verify_package=_VERIFY_PACKAGE,
        verify_stages=lambda prepared, value, evidence, records: (
            verify_portable_finalize_stages(
                value, evidence, records, prepared=prepared, oracle=oracle
            )
        ),
    )


def run_finalize_child(config, tools):
    from literate_ai import finalize_worker

    oracle = receiver_oracle(config)
    active = {}

    @contextmanager
    def runtime(prepared):
        for build in prepared.component_builds:
            require_portable_starter_build(build, tools)
        configured = PortableFinalizeRuntimeFactory(
            workspace_root=Path(os.environ["LITAI_FINALIZE_WORKSPACE"]),
            contracts=tuple(
                build.inputs.contract for build in prepared.component_builds
            ),
            tool_bindings=(tools.python_binding,),
            oracle=oracle,
            startup=WorkerToolchainRegistry((tools.python_binding,)),
            grant_path=config.finalize.grant_path,
            runtime_identity=None,
            admission_guard=config.require_unchanged,
        )
        active["runtime"] = configured
        try:
            with configured(prepared) as opened:
                yield opened
        finally:
            active.clear()

    return finalize_worker.main(
        [],
        runtime_factory=runtime,
        proof_loader=partial(
            load_finalize_records, admission_guard=config.require_unchanged
        ),
        verify_package=_VERIFY_PACKAGE,
        admission_guard=config.require_unchanged,
        require_execution_authority=lambda stage, prepared: active[
            "runtime"
        ].require_stage(stage, prepared),
    )
