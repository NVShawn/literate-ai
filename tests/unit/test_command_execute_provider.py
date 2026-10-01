"""Runtime-only providers cross a live command/child boundary and return proof."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_build_providers import materialize_provider_artifacts
from literate_ai.adapters.action_build_result import import_build_result
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_test_record import TestWorkerInput
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.command_executor import CommandComponentExecutor
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.execute_handoff import CompletedBuildExecuteHandoff
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.storage import FileSystemCAS
from tests.unit import test_action_execute_providers as provider_fixture
from tests.unit import test_standard_local_command_adapter as local_fixture
from tests.unit.test_standard_project_factory import _command_contracts
from tests.unit.test_standard_provider_worker import _CHILD

_RECEIVER = """
import os, sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
runtime = discover_python_toolchain(pinned_command=(sys.executable,))
code = os.environ['EXECUTE_CHILD']
launcher = LocalComponentToolBinding(
    sys.executable, ('-c', code),
    authority_identity=canonical_identity({'runtime': runtime.identity, 'code': code}),
    _authority_guard=runtime.require_unchanged,
)
worker = ConfiguredExecuteWorker(
    launcher, (LocalComponentToolBinding(sys.executable),),
    environment=dict(os.environ),
)
raise SystemExit(main(execute_worker=worker))
"""


class CommandExecuteProviderTests(unittest.TestCase):
    def test_live_child_returns_runtime_provider_proof_to_command_free_controller(self):
        f = provider_fixture.ExecuteProviderTests()
        self.addCleanup(f.doCleanups)
        f.preserve_sources = True
        contract_type = local_fixture.ComponentCommandContract
        runtime_identity = LocalComponentToolBinding(sys.executable).toolchain_identity

        def observed_contract(**kwargs):
            return contract_type(
                **(
                    kwargs
                    | {
                        "build_system_toolchain_identity": runtime_identity,
                        "language_runtime_identity": runtime_identity,
                    }
                )
            )

        with patch.object(
            local_fixture, "ComponentCommandContract", side_effect=observed_contract
        ):
            f.setUp()
        value, build = f.value, f.value.build_input
        root = f.fixture.fixture.root
        ports = LocalStandardLifecyclePorts(
            source_trees=f.consumer.source_trees,
            object_root=root / "return-controller",
            contracts=f.contracts,
            tool_bindings=(),
            command_phases=(),
        )
        ports.retain_evidence_with(
            QualificationEvidenceRecorder(
                max_bytes=64 * 1024 * 1024,
                max_records=4096,
            )
        )
        ports.accept_build_intent(
            build.execution_plan,
            build.generation_plan,
            build.candidate,
            build.inputs.providers,
            build.inputs.package_artifacts,
            build.inputs.intent,
        )
        ports.accept_finalized_plan(
            build.inputs.intent, build.inputs.authorization, build.plan
        )
        cas = FileSystemCAS(root / "controller-cas")
        raw_input, raw_result = build.to_bytes(), value.build_result.to_bytes()
        import_build_result(
            content=raw_result,
            result_identity=record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=f.fixture.deadline,
            ports=ports,
            cas=cas,
            retain_record=ports.retain_evidence_record,
            blob_source=f.fixture.source.get_bytes,
        )
        jobs = root / "live-jobs"
        jobs.mkdir()
        receiver = root / "receiver.py"
        receiver.write_text(_RECEIVER, encoding="utf-8")
        worker = ExecutionWorker(
            "runtime",
            ExecutionWorkerKind.COMMAND,
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
            environment=tuple(
                ExecutionWorkerEnvironment(name, name, True)
                for name in (
                    "CHAIN_CONTRACTS",
                    "CHAIN_PROVIDERS",
                    "EXECUTE_CHILD",
                    "LITAI_ACTION_WORKER_IDENTITY",
                    "PYTHONPATH",
                )
            ),
            command=(
                sys.executable,
                str(receiver),
                "--cas",
                str(cas.root),
                "--workspace",
                str(jobs),
            ),
        )
        catalog = ExecutionWorkerCatalog((worker,))
        admitted = LifecycleActionWorker(
            worker.worker_id,
            worker.identity,
            catalog.identity,
            canonical_identity("fixture hardware admission"),
        )
        defaults, _ = _command_contracts(build.execution_plan)
        contracts = {item.component_revision: item for item in defaults}
        contracts.update({item.component_revision: item for item in f.contracts})
        environment = dict(os.environ) | {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "EXECUTE_CHILD": _CHILD.replace(
                "literate_ai.build_worker", "literate_ai.execute_worker"
            ).replace("ComponentCommandPhase.BUILD", "ComponentCommandPhase.EXECUTE"),
            "CHAIN_CONTRACTS": json.dumps(
                [item.to_dict() for item in contracts.values()]
            ),
            "CHAIN_PROVIDERS": json.dumps({"app": ["PROVIDER", "app"]}),
            "LITAI_ACTION_WORKER_IDENTITY": worker.identity.uri,
        }
        indexer = CommandGenerationIndexer(
            build.execution_plan,
            ports.source_trees,
            lambda source: ports.source_trees.evidence(source).candidate,
            cas,
            catalog,
            (admitted,),
            f.fixture.deadline,
            cwd=root,
            environment=environment,
            revalidate_worker=lambda selected: self.assertEqual(selected, admitted),
        )
        # Hardware admission is a fixture; dispatch, receiver, child, custody,
        # provider bytes and process proof all use the real implementations.
        admission = SimpleNamespace(
            catalog=catalog,
            workers=(admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda selected, phase: phase is LifecycleActionKind.EXECUTE,
            supports_execute=lambda selected, tools: set(tools).issubset(
                binding.toolchain_identity for binding in f.bindings
            ),
        )
        builder = SimpleNamespace(
            test_handoff=lambda *args: TestWorkerInput(build, value.build_result)
        )
        executor = CommandComponentExecutor(
            indexer,
            admission,
            ports,
            handoff_for=CompletedBuildExecuteHandoff(builder, indexer, ports),
            result_source=lambda selected, reference: cas.get_bytes(reference),
        )
        executor.retain_execution_provider_evidence(
            build.plan, value.scope, value.accepted_providers
        )

        def checked(*args, **kwargs):
            result = run_bounded_process(*args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(
                json.loads(result.stdout).get("failure_code"),
                (result.stdout, result.stderr),
            )
            return result

        with (
            patch(
                "literate_ai.adapters.action_command_dispatch.run_bounded_process",
                side_effect=checked,
            ),
            materialize_provider_artifacts(
                receipts=value.accepted_providers,
                transfers=value.provider_builds,
                execution_plan=build.execution_plan,
                providers=value.provider_artifacts,
                ports=ports,
                cas=cas,
                deadline=f.fixture.deadline,
                require_current=f.fixture.deadline.remaining,
                blob_source=f.fixture.source.get_bytes,
            ),
            patch.object(
                ports, "_run_locked", side_effect=AssertionError("local command")
            ),
        ):
            result = executor.execute_scoped(
                build.plan,
                value.build_result.evidence.exports,
                value.scope,
                value.provider_artifacts,
            )
            self.assertEqual(result.execution_authority.input_scope, value.scope)
            self.assertEqual(
                result.provider_artifact_identities,
                value.scope.provider_artifact_identities,
            )
            self.assertEqual(
                ports.execution_stdout[build.plan.component_revision.uri],
                "known-output",
            )
            self.assertEqual(ports._execution_evidence[result.identity.uri], result)
        self.assertEqual(build.inputs.providers, ())
        self.assertEqual(ports.tool_bindings, {})
        self.assertEqual(list(jobs.iterdir()), [])
        for artifact in value.provider_artifacts:
            self.assertNotIn(artifact.identity.uri, ports._artifact_paths)
        reservation = indexer.slots.try_reserve(lambda *args: None)
        self.assertIsNotNone(reservation)
        reservation.release()
