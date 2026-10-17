"""Private FINALIZE dispatch, owned staging, supervision and verified return."""

import tempfile
from functools import partial
from pathlib import Path
from types import MappingProxyType

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize import admit_finalize_action
from literate_ai.adapters.action_finalize_grant_request import (
    encode_finalize_grant_request,
)
from literate_ai.adapters.action_finalize_inputs import materialize_finalize_inputs
from literate_ai.adapters.action_finalize_parent import portable_child_environment
from literate_ai.adapters.action_finalize_process import run_finalize_worker_process
from literate_ai.adapters.action_finalize_result import import_finalize_result
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.contracts import ContentIdentity, canonical_identity


class ConfiguredFinalizeWorker:
    """Private startup supplies current grants, runtime profile and semantic verifier.

    verify_stages receives prepared inputs followed by intent, evidence and records.
    The launcher must configure the child with the same private runtime/grant policy.
    require_execution_authority checks the exact intent before inputs are staged.
    The optional require_prepared_authority receives staged inputs, an owned
    measurement directory and the exact child environment, and is checked on every
    later poll; PortableFinalizeParentAuthority.require fits it. The optional
    plan_prepared_grant (PortableFinalizeParentAuthority.grant_request) enables
    describe_grant, which reports the exact request an operator must authorize.
    This adapter alone neither advertises capability nor selects a worker.
    """

    def __init__(
        self,
        launcher,
        *,
        environment,
        runtime_identity,
        observe_runtime,
        require_execution_authority,
        verify_package,
        verify_stages,
        require_prepared_authority=None,
        plan_prepared_grant=None,
    ):
        if any(
            item is not None and not callable(item)
            for item in (require_prepared_authority, plan_prepared_grant)
        ):
            raise TypeError("FINALIZE prepared authority must be callable")
        if not isinstance(runtime_identity, ContentIdentity) or not all(
            callable(item)
            for item in (
                observe_runtime,
                require_execution_authority,
                verify_package,
                verify_stages,
            )
        ):
            raise TypeError(
                "FINALIZE requires private profile, authority and verifiers"
            )
        self.launcher = launcher
        self.startup = WorkerToolchainRegistry((launcher,))
        private_environment = dict(environment)
        if any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in private_environment.items()
        ):
            raise TypeError("private environment requires text pairs")
        # Discovery and execution use different receiver protocols. This marker
        # is not runtime configuration and must not reach the supervised child.
        self.environment = MappingProxyType(
            {
                key: value
                for key, value in private_environment.items()
                if key.casefold() != "litai_dispatch_protocol"
            }
        )
        self.runtime_identity = runtime_identity
        self.observe_runtime = observe_runtime
        self.require_execution_authority = require_execution_authority
        self.verify_package = verify_package
        self.verify_stages = verify_stages
        self.require_prepared_authority = require_prepared_authority
        self.plan_prepared_grant = plan_prepared_grant

    @property
    def identity(self):
        if self.observe_runtime() != self.runtime_identity:
            raise ActionWireError(
                "action_finalize.profile_changed", "private runtime changed"
            )
        return canonical_identity(
            {
                "schema": "literate-ai/configured-finalize-worker@1",
                "startup": self.startup.identity.uri,
                "runtime": self.runtime_identity.uri,
                "environment": dict(self.environment),
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
        admission_guard,
        blob_source=None,
        cancelled=lambda: False,
    ):
        if not callable(admission_guard) or not callable(cancelled):
            raise TypeError(
                "FINALIZE requires live receiver admission and cancellation"
            )
        records = dict(records)
        value = admit_finalize_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        profile = self.identity

        def current():
            deadline.remaining()
            if cancelled():
                raise ActionWireError(
                    "action_finalize.cancelled", "FINALIZE was cancelled"
                )
            admission_guard()
            if self.identity != profile:
                raise ActionWireError(
                    "action_finalize.profile_changed", "private startup changed"
                )
            self.require_execution_authority(value)
            deadline.remaining()

        current()
        if not workspace_root.is_absolute() or not cas.root.is_absolute():
            raise ActionWireError(
                "action_finalize.workspace_invalid", "absolute paths required"
            )
        require_safe_directory(workspace_root)
        parent = directory_node(workspace_root)
        input_id = request.input_record_identities[0]
        proof = {
            identity: raw
            for identity, raw in records.items()
            if identity not in {input_id, request.action.payload_identity}
        }
        job = Path(tempfile.mkdtemp(prefix="finalize-", dir=workspace_root))
        owned = directory_node(job)
        try:
            current()
            if directory_node(workspace_root) != parent:
                raise ActionWireError(
                    "action_finalize.workspace_invalid", "workspace changed"
                )
            preflight, child = job / "proof", job / "run"
            preflight.mkdir()
            child.mkdir()
            args = dict(
                input_record=records[input_id],
                input_identity=input_id,
                records=proof,
                cas=cas,
                deadline=deadline,
                admission_guard=current,
                verify_package=self.verify_package,
                blob_source=blob_source,
            )
            with materialize_finalize_inputs(
                **args, workspace_root=preflight
            ) as prepared:
                child_environment = portable_child_environment(
                    self.environment, self.launcher.environment
                )

                def staged():
                    current()
                    if self.require_prepared_authority is not None:
                        self.require_prepared_authority(
                            prepared, job / "authority", child_environment
                        )

                for identity, raw in proof.items():
                    staged()
                    if cas.put_bytes(raw).identity != identity.uri:
                        raise ActionWireError(
                            "action_finalize.record_invalid", "CAS custody differs"
                        )
                staged()

                def authorize(intent):
                    if intent != value:
                        raise ActionWireError(
                            "action_finalize.authority_mismatch", "child intent differs"
                        )
                    staged()

                result = run_finalize_worker_process(
                    launcher=self.launcher,
                    input_record=records[input_id],
                    input_identity=input_id,
                    deadline=deadline,
                    cwd=child,
                    environment=self.environment,
                    require_execution_authority=authorize,
                    cancelled=cancelled,
                    cas_root=cas.root,
                    workspace_root=child,
                )
                staged()
                import_finalize_result(
                    content=result,
                    result_identity=record_identity(result),
                    verify_stages=partial(self.verify_stages, prepared),
                    **args,
                )
                current()
        finally:
            _remove_owned_stage(job, owned)
        current()
        return result

    def describe_grant(
        self,
        request,
        deadline,
        records,
        *,
        expected_worker_identity,
        cas,
        workspace_root,
        admission_guard,
        blob_source=None,
    ):
        """Stage exact inputs and return the measured grant request; run nothing.

        No grant is checked because none exists yet; the operator signs what this
        returns. Admission, profile and deadline still bind every step, and the
        staged inputs and measurement directory are removed before returning.
        """
        if self.plan_prepared_grant is None:
            raise ActionWireError(
                "action_finalize.grant_plan_unavailable",
                "FINALIZE grant planning is not configured",
            )
        if not callable(admission_guard):
            raise TypeError("FINALIZE requires live receiver admission")
        records = dict(records)
        value = admit_finalize_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        profile = self.identity

        def current():
            deadline.remaining()
            admission_guard()
            if self.identity != profile:
                raise ActionWireError(
                    "action_finalize.profile_changed", "private startup changed"
                )
            deadline.remaining()

        current()
        if not workspace_root.is_absolute() or not cas.root.is_absolute():
            raise ActionWireError(
                "action_finalize.workspace_invalid", "absolute paths required"
            )
        require_safe_directory(workspace_root)
        input_id = request.input_record_identities[0]
        proof = {
            identity: raw
            for identity, raw in records.items()
            if identity not in {input_id, request.action.payload_identity}
        }
        job = Path(tempfile.mkdtemp(prefix="finalize-plan-", dir=workspace_root))
        owned = directory_node(job)
        try:
            preflight = job / "proof"
            preflight.mkdir()
            with materialize_finalize_inputs(
                input_record=records[input_id],
                input_identity=input_id,
                records=proof,
                cas=cas,
                deadline=deadline,
                admission_guard=current,
                verify_package=self.verify_package,
                blob_source=blob_source,
                workspace_root=preflight,
            ) as prepared:
                current()
                planned = self.plan_prepared_grant(
                    prepared,
                    job / "authority",
                    portable_child_environment(
                        self.environment, self.launcher.environment
                    ),
                )
                current()
        finally:
            _remove_owned_stage(job, owned)
        return encode_finalize_grant_request(value, planned)
