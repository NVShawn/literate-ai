"""Qualify both entrypoints across the real supervised EXECUTE boundary."""

import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_build_result import (
    BuildWorkerResult,
    capture_build_result,
    import_build_result,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result import import_execute_result
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.build_handoff import capture_build_input
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceRecorder,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_execute_action import make_execute_request

_CHILD = """
import os,json,sys
from pathlib import Path
from contextlib import contextmanager
from literate_ai.execute_worker import main
from literate_ai.adapters.lifecycle import (
    LocalStandardLifecyclePorts, LocalComponentToolBinding,
)
from literate_ai.contracts import ComponentCommandContract, ComponentCommandPhase
@contextmanager
def runtime(admitted,registry,recorder):
    ports = LocalStandardLifecyclePorts(
        source_trees=registry,
        object_root=Path(os.environ['LITAI_EXECUTE_WORKSPACE'])/'objects',
        contracts=(ComponentCommandContract.from_dict(json.loads(os.environ['MULTI_CONTRACT'])),),
        tool_bindings=(LocalComponentToolBinding(sys.executable),),
        command_phases=(ComponentCommandPhase.EXECUTE,),
    )
    ports.retain_evidence_with(recorder)
    yield ports
raise SystemExit(main(runtime_factory=runtime))
"""


def assert_multi_execute_worker(case, ports, execution, plan, output):
    root = ports.object_root.parent.resolve() / "worker-proof"
    root.mkdir()
    source_cas = FileSystemCAS(root / "source-cas")
    deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
    source = ports.source_trees.resolve(plan.request.source_tree_identity)
    indexer = SimpleNamespace(
        execution_plan=execution,
        deadline=deadline,
        cas=source_cas,
        _candidate=lambda *args: (
            ports.source_trees.evidence(plan.request.source_tree_identity).candidate
        ),
        _snapshot=lambda identity: {
            p.relative_to(source).as_posix(): p.read_bytes()
            for p in source.rglob("*")
            if p.is_file()
        },
    )
    build = capture_build_input(indexer, ports, plan, (), ())
    build_content = build.to_bytes()
    result_content = capture_build_result(
        input_record=build_content,
        input_identity=record_identity(build_content),
        deadline=deadline,
        ports=ports,
        output=output,
        records=ports.retained_evidence_records(),
        cas=source_cas,
    )
    result = BuildWorkerResult.admit(
        result_content,
        record_identity(result_content),
        input_record=build_content,
        input_identity=record_identity(build_content),
        deadline=deadline,
    )
    scope = plan_standard_execution_receipts(execution, plan, output.exports, ())
    value = ExecuteWorkerInput(build, result, scope)
    request, records = make_execute_request(value, deadline)
    runtime = discover_python_toolchain(pinned_command=(sys.executable,))
    launcher = LocalComponentToolBinding(
        sys.executable,
        ("-c", _CHILD),
        authority_identity=canonical_identity(
            {"runtime": runtime.identity, "code": _CHILD}
        ),
        _authority_guard=runtime.require_unchanged,
    )
    worker = ConfiguredExecuteWorker(
        launcher,
        tuple(ports.tool_bindings.values()),
        environment=dict(os.environ)
        | {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "MULTI_CONTRACT": json.dumps(build.inputs.contract.to_dict()),
        },
    )
    jobs = root / "jobs"
    jobs.mkdir()
    worker_cas = FileSystemCAS(root / "worker-cas")
    content = worker.execute(
        request,
        deadline,
        records,
        expected_worker_identity=request.worker.worker_identity,
        cas=worker_cas,
        workspace_root=jobs,
        blob_source=source_cas.get_bytes,
    )
    case.assertEqual(list(jobs.iterdir()), [])
    controller = LocalStandardLifecyclePorts(
        source_trees=ports.source_trees,
        object_root=root / "controller",
        contracts=(build.inputs.contract,),
        tool_bindings=(),
        command_phases=(),
    )
    controller.retain_evidence_with(
        QualificationEvidenceRecorder(
            max_bytes=64 * 1024 * 1024,
            max_records=4096,
        )
    )
    controller.accept_build_intent(
        execution,
        build.generation_plan,
        build.candidate,
        (),
        (),
        build.inputs.intent,
    )
    controller.accept_finalized_plan(
        build.inputs.intent, build.inputs.authorization, plan
    )
    controller_cas = FileSystemCAS(root / "controller-cas")
    import_build_result(
        content=result_content,
        result_identity=record_identity(result_content),
        input_record=build_content,
        input_identity=record_identity(build_content),
        deadline=deadline,
        ports=controller,
        cas=controller_cas,
        retain_record=controller.retain_evidence_record,
        blob_source=source_cas.get_bytes,
    )
    returned = ExecuteWorkerResult.admit(
        content,
        record_identity(content),
        input_record=value.to_bytes(),
        input_identity=record_identity(value.to_bytes()),
        deadline=deadline,
    )
    for unit in returned.evidence.entrypoint_evidence:
        changed = replace(
            returned,
            evidence_records=tuple(
                ref
                for ref in returned.evidence_records
                if ref.identity != unit.stdout_identity.uri
            ),
        ).to_bytes()
        with case.subTest(missing_stdout=unit.deployment_unit):
            with case.assertRaises((ActionWireError, QualificationCaptureError)):
                import_execute_result(
                    content=changed,
                    result_identity=record_identity(changed),
                    input_record=value.to_bytes(),
                    input_identity=record_identity(value.to_bytes()),
                    deadline=deadline,
                    ports=controller,
                    cas=controller_cas,
                    admission_guard=deadline.remaining,
                    blob_source=worker_cas.get_bytes,
                )
            case.assertEqual(controller._execution_evidence, {})
            case.assertEqual(controller.execution_stdout, {})
    with patch.object(
        controller, "_run_locked", side_effect=AssertionError("controller command")
    ):
        evidence = import_execute_result(
            content=content,
            result_identity=record_identity(content),
            input_record=value.to_bytes(),
            input_identity=record_identity(value.to_bytes()),
            deadline=deadline,
            ports=controller,
            cas=controller_cas,
            admission_guard=deadline.remaining,
            blob_source=worker_cas.get_bytes,
        )
    case.assertEqual(len(evidence.entrypoint_evidence), 2)
    case.assertEqual(
        {unit.deployment_unit for unit in evidence.entrypoint_evidence},
        {"primary", "worker"},
    )
    case.assertEqual(evidence.execution_authority.input_scope, scope)
    case.assertEqual(controller.tool_bindings, {})
    case.assertEqual(controller._execution_evidence[evidence.identity.uri], evidence)
    return controller.execution_stdout[plan.component_revision.uri]
