"""Actual configured command BUILD from a controller with no execution tools."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_hardware import (
    HARDWARE_PROBE_TIMEOUT_SECONDS,
    probe_command_hardware,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.command_builder import CommandComponentBuilder
from literate_ai.adapters.command_executor import CommandComponentExecutor
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.command_tester import CommandComponentTester
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.execute_handoff import CompletedBuildExecuteHandoff
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.adapters.standard_project import (
    LocalObservedToolchainAuthority,
    assemble_standard_lifecycle_ports,
    project_standard_toolchain_closure,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import ComponentCommandPhase, canonical_identity
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_project_factory import _command_contracts
from tests.support.fixtures_test_standard_provider_worker import _CHILD
from tests.unit.action_deadline import ACTION_TEST_DEADLINE
from tests.unit.standard_source_evidence_fixture import register_strict_source

_RECEIVER = """
import os, sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_test_worker import ConfiguredTestWorker
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
runtime = discover_python_toolchain(pinned_command=(sys.executable,))
code = os.environ['BUILD_CHILD']
launcher = LocalComponentToolBinding(
    sys.executable, ('-c', code),
    authority_identity=canonical_identity({'runtime':runtime.identity,'code':code}),
    _authority_guard=runtime.require_unchanged,
)
worker = ConfiguredBuildWorker(
    launcher, (LocalComponentToolBinding(sys.executable),),
    environment=dict(os.environ),
)
test_code = os.environ['TEST_CHILD']
test_launcher = LocalComponentToolBinding(
    sys.executable, ('-c', test_code),
    authority_identity=canonical_identity({'runtime':runtime.identity,'code':test_code}),
    _authority_guard=runtime.require_unchanged,
)
test_worker = ConfiguredTestWorker(
    test_launcher, (LocalComponentToolBinding(sys.executable),),
    environment=dict(os.environ),
)
execute_code = os.environ['EXECUTE_CHILD']
execute_launcher = LocalComponentToolBinding(
    sys.executable, ('-c', execute_code),
    authority_identity=canonical_identity({'runtime':runtime.identity,'code':execute_code}),
    _authority_guard=runtime.require_unchanged,
)
execute_worker = ConfiguredExecuteWorker(
    execute_launcher, (LocalComponentToolBinding(sys.executable),),
    environment=dict(os.environ),
)
raise SystemExit(main(
    build_worker=worker, test_worker=test_worker, execute_worker=execute_worker,
))
"""


class CommandBuildWorkerTests(unittest.TestCase):
    def test_data_only_controller_dispatches_real_build_test_and_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, execution = _fixture()
            generation = execution.generation_plans[0]
            initial, bindings = _command_contracts(execution)
            build = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[3]).write_bytes((Path(sys.argv[1])/'app.py').read_bytes())"
            )
            test = (
                "from pathlib import Path; import sys,json; "
                "assert any(p.read_bytes()==b'built-on-worker\\n' for p in "
                "Path(sys.argv[1]).iterdir() if p.is_file()); "
                "print(json.dumps(dict(schema='literate-ai/generated-test-results@1',"
                "cases=[dict(case_id='fixture-'+name,outcome='passed') for name in "
                "('example','boundary','invariant')])))"
            )
            execute = (
                "from pathlib import Path; import sys; "
                "assert any(p.read_bytes()==b'built-on-worker\\n' for p in "
                "Path(sys.argv[1]).iterdir() if p.is_file()); "
                "print('executed-on-worker')"
            )
            contracts = tuple(
                replace(
                    contract,
                    commands=tuple(
                        replace(
                            command, argv=(*command.argv[:2], build, *command.argv[3:])
                        )
                        if command.phase is ComponentCommandPhase.BUILD
                        else replace(
                            command, argv=(*command.argv[:2], test, *command.argv[3:])
                        )
                        if command.phase is ComponentCommandPhase.TEST
                        else replace(
                            command,
                            argv=(*command.argv[:2], execute, *command.argv[3:]),
                        )
                        if command.phase is ComponentCommandPhase.EXECUTE
                        else command
                        for command in contract.commands
                    ),
                )
                for contract in initial
            )
            registry = LocalSourceTreeRegistry()
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_bytes(b"built-on-worker\n")
            candidate = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation,
                identity_namespace="command-custody",
            )
            cas = FileSystemCAS(root / "cas")
            workspace = root / "jobs"
            workspace.mkdir()
            receiver = root / "receiver.py"
            receiver.write_text(_RECEIVER, encoding="utf-8")
            worker = ExecutionWorker(
                "builder",
                ExecutionWorkerKind.COMMAND,
                action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
                command=(
                    sys.executable,
                    str(receiver),
                    "--cas",
                    str(cas.root),
                    "--workspace",
                    str(workspace),
                ),
                environment=tuple(
                    ExecutionWorkerEnvironment(name, name, True)
                    for name in (
                        "BUILD_CHILD",
                        "CHAIN_CONTRACTS",
                        "CHAIN_PROVIDERS",
                        "EXECUTE_CHILD",
                        "LITAI_ACTION_WORKER_IDENTITY",
                        "PYTHONPATH",
                        "TEST_CHILD",
                    )
                ),
            )
            catalog = ExecutionWorkerCatalog((worker,))
            environment = dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                BUILD_CHILD=_CHILD,
                EXECUTE_CHILD=_CHILD.replace(
                    "literate_ai.build_worker", "literate_ai.execute_worker"
                ).replace(
                    "ComponentCommandPhase.BUILD", "ComponentCommandPhase.EXECUTE"
                ),
                TEST_CHILD=_CHILD.replace(
                    "literate_ai.build_worker", "literate_ai.test_worker"
                ).replace("ComponentCommandPhase.BUILD", "ComponentCommandPhase.TEST"),
                CHAIN_CONTRACTS=json.dumps([item.to_dict() for item in contracts]),
                CHAIN_PROVIDERS="{}",
                LITAI_ACTION_WORKER_IDENTITY=worker.identity.uri,
            )
            hardware = probe_command_hardware(
                worker,
                timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
                cwd=root,
                environment=environment,
            )
            admission = CommandActionWorkerPool(
                lambda: catalog,
                lambda: WorkerHardwareObservationCatalog((hardware,)),
                lambda selected: canonical_identity({"healthy": selected.identity.uri}),
                ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE),
                phase=LifecycleActionKind.INDEX,
                source_handoff="filesystem-cas",
                target_profile="host",
                maximum_hardware_age=ACTION_TEST_DEADLINE,
                cwd=root,
                environment=environment,
            )
            admitted = admission.workers[0]
            capability = admission.capabilities[0]
            required = tuple(binding.toolchain_identity for binding in bindings)
            self.assertEqual(capability.build_toolchains, required)
            self.assertTrue(admission.supports_build(admitted, required))

            def require_worker_tools():
                admission.revalidate(admitted)
                self.assertTrue(admission.supports_build(admitted, required))

            root_ref = registry.evidence(candidate.tree_identity).managed_graph.root_ref
            closure = project_standard_toolchain_closure(
                execution,
                contracts=contracts,
                tool_bindings=(),
                command_phases=(),
                toolchain_authorities=tuple(
                    LocalObservedToolchainAuthority(identity, require_worker_tools)
                    for identity in capability.build_toolchains
                ),
                dependency_observation=HostDependencyObservation(
                    tuple(
                        {
                            "type": "application",
                            "name": "observed-worker-python",
                            "version": ".".join(map(str, capability.python_version)),
                            "bom-ref": identity.uri,
                        }
                        for identity in capability.build_toolchains
                    ),
                    tuple(
                        (root_ref, identity.uri)
                        for identity in capability.build_toolchains
                    ),
                ),
                observer_identity=capability.identity,
            )
            ports = assemble_standard_lifecycle_ports(
                source_trees=registry,
                object_root=root / "controller",
                toolchain_closure=closure,
                command_phases=(),
            ).ports
            ports.retain_evidence_with(
                QualificationEvidenceRecorder(
                    max_bytes=64 * 1024 * 1024, max_records=4096
                )
            )
            intent = ports.create(execution, generation, candidate, (), ())
            plan = ports.finalize(
                intent,
                ports.authorize(
                    intent,
                    ports.index(candidate.component_revision, candidate.tree_identity),
                ),
            )
            indexer = CommandGenerationIndexer.from_admission(
                execution,
                registry,
                lambda tree: registry.evidence(tree).candidate,
                cas,
                admission,
            )
            builder = CommandComponentBuilder(indexer, admission, ports)
            tester = CommandComponentTester(
                indexer, admission, ports, handoff_for=builder.test_handoff
            )

            executor = CommandComponentExecutor(
                indexer,
                admission,
                ports,
                handoff_for=CompletedBuildExecuteHandoff(builder, indexer, ports),
            )

            def checked(*args, **kwargs):
                result = run_bounded_process(*args, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            with (
                patch.object(
                    LocalComponentToolBinding,
                    "require_unchanged",
                    side_effect=AssertionError(
                        "controller inspected a local build tool"
                    ),
                ),
                patch(
                    "literate_ai.adapters.action_command_dispatch.run_bounded_process",
                    side_effect=checked,
                ),
            ):
                output = builder.build(plan, ())
                test_result = tester.test(plan, output.exports)
                self.assertEqual(test_result.passed_count, 3)
                scope = plan_standard_execution_receipts(
                    execution, plan, output.exports, ()
                )
                executor.retain_execution_provider_evidence(plan, scope, ())
                execution_result = executor.execute_scoped(
                    plan, output.exports, scope, ()
                )
                self.assertEqual(
                    execution_result.execution_authority.input_scope, scope
                )
                self.assertEqual(
                    ports.execution_stdout[plan.component_revision.uri],
                    "executed-on-worker",
                )
                self.assertEqual(
                    ports._execution_evidence[execution_result.identity.uri],
                    execution_result,
                )
            self.assertEqual(ports.tool_bindings, {})
            self.assertFalse(ports.locked_command_authority_is_current())
            self.assertEqual(
                ports.artifact_path(output.exports[0]).read_bytes(),
                b"built-on-worker\n",
            )
            self.assertEqual(list(workspace.iterdir()), [])
            self.assertTrue(ports.retained_evidence_records())
            for operation in (ports.build, ports.test, ports.execute):
                with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
                    operation(plan, ())
            slot = indexer.slots.try_reserve(lambda selected, slot: None)
            self.assertIsNotNone(slot)
            slot.release()
            # A new private child profile must invalidate both projected authority
            # and subsequent dispatch, even though its interpreter is unchanged.
            admission.environment["BUILD_CHILD"] += "\n# changed private runtime\n"
            with self.assertRaisesRegex(ValueError, "observed toolchain changed"):
                closure.require_unchanged()
            with self.assertRaises(ActionWireError) as refused:
                builder.build(plan, ())
            self.assertEqual(refused.exception.code, "action_admission.runtime_changed")
            self.assertEqual(list(workspace.iterdir()), [])
            self.assertEqual(
                ports.artifact_path(output.exports[0]).read_bytes(),
                b"built-on-worker\n",
            )
            slot = indexer.slots.try_reserve(lambda selected, slot: None)
            self.assertIsNotNone(slot)
            slot.release()
