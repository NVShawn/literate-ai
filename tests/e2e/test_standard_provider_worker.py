"""Standard worker composition builds a real accepted artifact dependency chain."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_provider_build import capture_provider_build
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.standard_provider_receipts import (
    select_build_provider_receipts,
)
from literate_ai.contracts import ComponentCommandPhase, canonical_identity
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_build_action import build_request
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_project_factory import _command_contracts
from tests.support.standard_source_evidence_fixture import register_strict_source

_CHILD = """
import json, os, sys
from contextlib import contextmanager
from pathlib import Path
from literate_ai.build_worker import main
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.standard_project import (
    project_standard_toolchain_closure, assemble_standard_lifecycle_ports,
)
from literate_ai.contracts import (
    ComponentCommandContract, ComponentCommandPhase, canonical_identity,
)

@contextmanager
def runtime(admitted, registry, recorder):
    binding = LocalComponentToolBinding(sys.executable)
    custody = registry.evidence(admitted.candidate.tree_identity)
    root_ref = custody.managed_graph.root_ref
    closure = project_standard_toolchain_closure(
        admitted.execution_plan,
        contracts=tuple(ComponentCommandContract.from_dict(item)
            for item in json.loads(os.environ['CHAIN_CONTRACTS'])),
        tool_bindings=(binding,),
        provider_environment={key:tuple(value) for key,value in
            json.loads(os.environ['CHAIN_PROVIDERS']).items()},
        dependency_observation=HostDependencyObservation(
            ({'type':'application','name':'fixture-python','version':sys.version.split()[0],
              'bom-ref':binding.toolchain_identity.uri,
              'properties':[
                  {'name':'literate-ai:dependency-kind','value':'toolchain'},
                  {'name':'literate-ai:dependency-scope','value':'build'},
              ]},),
            ((root_ref,binding.toolchain_identity.uri),),
        ),
        observer_identity=canonical_identity({'observer':'chain-worker'}),
    )
    composition = assemble_standard_lifecycle_ports(
        source_trees=registry, object_root=Path('objects').resolve(),
        toolchain_closure=closure, command_phases=(ComponentCommandPhase.BUILD,),
    )
    composition.ports.retain_evidence_with(recorder)
    yield composition.ports

raise SystemExit(main(runtime_factory=runtime))
"""


class StandardProviderWorkerTests(unittest.TestCase):
    def test_standard_factory_builds_root_from_transferred_two_level_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            controller = root / "controller"
            controller.mkdir()
            snapshot, execution = _fixture(dependency_kind=DependencyKind.BUILD)
            names = {
                node.revision.identity: node.revision.coordinate.name
                for node in snapshot.authority.lock.nodes
            }
            generations = {
                names[item.component_revision]: item
                for item in execution.generation_plans
            }
            initial, bindings = _command_contracts(execution)
            by_name = {names[item.component_revision]: item for item in initial}
            dependencies = {
                "storage": (),
                "service": ("storage",),
                "application": ("service",),
            }
            providers = {
                by_name[name].artifact_export.export_id: (
                    "INPUT_" + name.upper(),
                    by_name[name].artifact_export.export_id,
                )
                for name in ("storage", "service")
            }
            contracts = []
            for name, contract in by_name.items():
                dependency = dependencies[name]
                expression = (
                    "Path(os.environ['INPUT_" + dependency[0].upper() + "'])"
                    if dependency
                    else "Path(sys.argv[1])/'app.py'"
                )
                build = (
                    "from pathlib import Path; import os,sys; p="
                    + expression
                    + "; Path(sys.argv[2]).write_bytes(p.read_bytes())"
                )
                test = (
                    "import json; print(json.dumps(dict(schema="
                    "'literate-ai/generated-test-results@1',cases=["
                    "dict(case_id=c,outcome='passed') for c in "
                    "('fixture-example','fixture-boundary','fixture-invariant')])))"
                )
                run = (
                    "from pathlib import Path; import sys; print((Path(sys.argv[1])/"
                    + repr(contract.artifact_export.export_id)
                    + ").read_text().strip())"
                )
                commands = tuple(
                    replace(
                        command,
                        argv=(
                            (
                                "{tool}",
                                "-c",
                                build,
                                "{source_root}",
                                "{export_path}",
                                "{object_root}",
                                "{provider_artifacts}",
                            )
                            if command.phase is ComponentCommandPhase.BUILD
                            else (
                                "{tool}",
                                "-c",
                                test
                                if command.phase is ComponentCommandPhase.TEST
                                else run,
                                "{artifact_root}",
                            )
                        ),
                    )
                    for command in contract.commands
                )
                contracts.append(replace(contract, commands=commands))
            contracts = tuple(contracts)
            by_name = {names[item.component_revision]: item for item in contracts}
            registry = LocalSourceTreeRegistry()
            candidates = {}
            for name, generation in generations.items():
                source = controller / name
                source.mkdir()
                (source / "app.py").write_bytes(b"known-output\n")
                candidates[name] = register_strict_source(
                    registry,
                    source,
                    snapshot=snapshot,
                    generation_plan=generation,
                    identity_namespace="chain-" + name,
                )
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=controller / "objects",
                contracts=contracts,
                tool_bindings=bindings,
                provider_environment=providers,
            )
            recorder = QualificationEvidenceRecorder(
                max_bytes=64 * 1024 * 1024, max_records=4096
            )
            ports.retain_evidence_with(recorder)
            outputs, receipts = {}, []
            for name in ("storage", "service", "application"):
                direct = tuple(
                    export
                    for provider in dependencies[name]
                    for export in outputs[provider].exports
                )
                candidate = candidates[name]
                intent = ports.create(
                    execution, generations[name], candidate, direct, ()
                )
                auth = ports.authorize(
                    intent,
                    ports.index(candidate.component_revision, candidate.tree_identity),
                )
                inputs = ports.plan_finalization_inputs(intent, auth)
                plan = ports.finalize(intent, auth)
                if name == "application":
                    break
                output = ports.build(plan, direct)
                tests = ports.test(plan, output.exports)
                observed = ports.execute(plan, output.exports)
                receipts.append(ports.accept(plan, tests.identity, observed.identity))
                outputs[name] = output
            selected = select_build_provider_receipts(direct, tuple(receipts))
            self.assertEqual(len(selected), 2)
            source_cas, worker_cas = (
                FileSystemCAS(root / "source-cas"),
                FileSystemCAS(root / "worker-cas"),
            )
            deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
            transfers = tuple(
                capture_provider_build(
                    receipt=receipt,
                    artifact_root=ports.artifact_path(receipt.build.exports[0]).parent,
                    records=recorder.entries,
                    cas=source_cas,
                    deadline=deadline,
                    require_current=lambda: ports.build_execution_inputs(plan),
                    source_validation=registry.validation_inputs(
                        receipt.build.source_tree_identity
                    ),
                    generation_plan=generations[names[receipt.component_revision]],
                )
                for receipt in selected
            )
            source = registry.resolve(candidate.tree_identity)
            custody = registry.evidence(candidate.tree_identity)
            value = BuildWorkerInput(
                execution.identity,
                generations["application"].identity,
                candidate,
                plan,
                inputs,
                tuple(
                    CachedSourceFile(
                        path.relative_to(source).as_posix(), source_cas.put_file(path)
                    )
                    for path in sorted(source.rglob("*"))
                    if path.is_file()
                ),
                registry.validation_inputs(candidate.tree_identity),
                custody.source_generation_identity,
                custody.identity,
                generations["application"],
                execution,
                selected,
                transfers,
            )
            content = value.to_bytes()
            shutil.rmtree(controller)
            workspace = root / "worker"
            workspace.mkdir()
            runtime = discover_python_toolchain(pinned_command=(sys.executable,))
            launcher = LocalComponentToolBinding(
                sys.executable,
                ("-c", _CHILD),
                authority_identity=canonical_identity(
                    {"runtime": runtime.identity, "code": _CHILD}
                ),
                _authority_guard=runtime.require_unchanged,
            )
            worker = ConfiguredBuildWorker(
                launcher,
                bindings,
                environment=dict(
                    os.environ,
                    PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                    CHAIN_CONTRACTS=json.dumps([item.to_dict() for item in contracts]),
                    CHAIN_PROVIDERS=json.dumps(providers),
                ),
            )
            request, records = build_request(value, deadline)

            def checked(*args, **kwargs):
                result = run_bounded_process(*args, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            with patch(
                "literate_ai.adapters.action_worker_process.run_bounded_process",
                side_effect=checked,
            ):
                result_bytes = worker.execute(
                    request,
                    deadline,
                    records,
                    expected_worker_identity=request.worker.worker_identity,
                    cas=worker_cas,
                    workspace_root=workspace,
                    blob_source=source_cas.get_bytes,
                )
            result = BuildWorkerResult.admit(
                result_bytes,
                record_identity(result_bytes),
                input_record=content,
                input_identity=record_identity(content),
                deadline=deadline,
            )
            files = read_directory_export(
                worker_cas.get_bytes(result.artifact_archive),
                result.artifact_archive,
                max_bytes=256 * 1024 * 1024,
                max_entries=65534,
            )
            self.assertEqual(
                {item.path: item.content for item in files}[
                    by_name["application"].artifact_export.export_id
                ],
                b"known-output\n",
            )
            self.assertEqual(list(workspace.iterdir()), [])
