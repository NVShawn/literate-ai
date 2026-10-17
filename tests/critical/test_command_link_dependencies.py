"""Real accepted Components cross the command LINK boundary in dependency order."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.accept_handoff import CompletedStagesAcceptHandoff
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.command_linker import CommandComponentLinker
from literate_ai.adapters.execute_handoff import CompletedBuildExecuteHandoff
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.local_accept_handoff import LocalAcceptanceLinkHandoff
from literate_ai.adapters.local_test_handoff import LocalBuildTestHandoff
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    DependencyKind,
    canonical_identity,
)
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.storage import FileSystemCAS
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)


class CommandLinkDependencyTests(unittest.TestCase):
    def test_packaging_chain_reopens_all_provider_proofs_in_real_receiver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, execution = _fixture(dependency_kind=DependencyKind.PACKAGING)
            source_cas = FileSystemCAS(root / "source-cas")
            # One deadline spans generation through FINALIZE in real receivers.
            deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
            accepted, handoffs, blobs, proof_refs = {}, {}, {}, {}
            local_ports = {}
            for number, generation in enumerate(execution.generation_plans):
                workspace = root / str(number)
                workspace.mkdir()
                base, _, candidate, _ = _python_copy_lifecycle(
                    workspace,
                    prepared=(snapshot, execution),
                    generation_plan=generation,
                )
                contract = next(iter(base.contracts.values()))
                test = contract.command(ComponentCommandPhase.TEST)
                script = (
                    "import json; print(json.dumps(dict(schema="
                    "'literate-ai/generated-test-results@1',cases=["
                    "dict(case_id=c,outcome='passed') for c in "
                    "('fixture-example','fixture-boundary','fixture-invariant')])))"
                )
                test = replace(test, argv=("{tool}", "-c", script, "{artifact_root}"))
                execute = replace(
                    contract.command(ComponentCommandPhase.EXECUTE),
                    argv=(
                        "{tool}",
                        "-c",
                        "from pathlib import Path; import json,sys; "
                        "print(json.dumps((Path(sys.argv[1])/'app').read_text().strip()))",
                        "{artifact_root}",
                        "--litai-smoke",
                    ),
                )
                contract = replace(
                    contract,
                    commands=tuple(
                        test
                        if command.phase is ComponentCommandPhase.TEST
                        else execute
                        if command.phase is ComponentCommandPhase.EXECUTE
                        else command
                        for command in contract.commands
                    ),
                )
                ports = LocalStandardLifecyclePorts(
                    source_trees=base.source_trees,
                    object_root=workspace / "verified",
                    contracts=(contract,),
                    tool_bindings=tuple(base.tool_bindings.values()),
                )
                ports.retain_evidence_with(
                    QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
                )
                source = ports.source_trees.resolve(candidate.tree_identity)
                indexer = SimpleNamespace(
                    execution_plan=execution,
                    deadline=deadline,
                    cas=source_cas,
                    _candidate=lambda *args, candidate=candidate: candidate,
                    _snapshot=lambda identity, source=source: {
                        p.relative_to(source).as_posix(): p.read_bytes()
                        for p in source.rglob("*")
                        if p.is_file()
                    },
                )
                intent = ports.create(execution, generation, candidate, (), ())
                plan = ports.finalize(
                    intent,
                    ports.authorize(
                        intent,
                        ports.index(
                            candidate.component_revision, candidate.tree_identity
                        ),
                    ),
                )
                builder = LocalBuildTestHandoff(ports, indexer, ports)
                output = builder.build(plan, ())
                for export in output.exports:
                    self.assertEqual(
                        source_cas.put_bytes(
                            ports.read_artifact_blob(export.blob),
                            media_type=export.blob.media_type,
                        ),
                        export.blob,
                    )
                tests = ports.test(plan, output.exports)
                scope = plan_standard_execution_receipts(
                    execution, plan, output.exports, ()
                )
                executed = ports.execute_scoped(plan, output.exports, scope, ())
                handoff = CompletedStagesAcceptHandoff(
                    indexer,
                    ports,
                    execution_input_for=CompletedBuildExecuteHandoff(
                        builder, indexer, ports
                    ),
                )
                local = LocalAcceptanceLinkHandoff(
                    ports, indexer, ports, handoff_for=handoff
                )
                local.retain_execution_provider_evidence(plan, scope, ())
                receipt = local.accept(plan, tests.identity, executed.identity)
                value, result = local.link_handoff(plan, receipt)
                proof_refs[plan.component_revision] = next(
                    ref
                    for ref in result.evidence_records
                    if ref.identity == receipt.identity.uri
                )
                for ref in (
                    *result.evidence_records,
                    value.execution_input.build_result.artifact_archive,
                ):
                    blobs[blob_path(ref)] = source_cas.get_bytes(ref)
                accepted[plan.component_revision] = (plan, receipt)
                handoffs[plan.component_revision] = local
                local_ports[plan.component_revision] = ports
                for export in output.exports:
                    blobs[blob_path(export.blob)] = source_cas.get_bytes(export.blob)

            self.assertGreater(len(accepted), 1)
            worker_cas = FileSystemCAS(root / "worker-cas")
            controller_cas = FileSystemCAS(root / "return-cas")
            jobs = root / "jobs"
            jobs.mkdir()
            # Private receiver startup: the operator pins the child environment
            # and later places the FINALIZE grant here. Requests select neither.
            private = root / "finalize-private"
            private.mkdir()
            (private / "environment.json").write_text(
                json.dumps(dict(os.environ)), "utf-8"
            )
            for reference in source_cas.iter_refs():
                blobs[blob_path(reference)] = source_cas.get_bytes(reference)
            with source_cas_server(blobs) as (url, reads):
                worker = ExecutionWorker(
                    "link",
                    ExecutionWorkerKind.COMMAND,
                    action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
                    environment=(
                        ExecutionWorkerEnvironment(
                            "LITAI_ACTION_WORKER_IDENTITY",
                            "LITAI_ACTION_WORKER_IDENTITY",
                            True,
                        ),
                    ),
                    command=(
                        sys.executable,
                        "-I",
                        "-c",
                        "import sys; sys.path.insert(0, "
                        f"{str(Path(__file__).resolve().parents[2])!r}); "
                        "from tests.support.finalize_child_fixture import receiver; "
                        f"raise SystemExit(receiver({str(private)!r}))",
                        "--cas",
                        str(worker_cas.root),
                        "--workspace",
                        str(jobs),
                        "--source-cas-url",
                        url,
                        "--allow-http",
                    ),
                )
                catalog = ExecutionWorkerCatalog((worker,))
                admitted = LifecycleActionWorker(
                    "link",
                    worker.identity,
                    catalog.identity,
                    canonical_identity("hardware"),
                )
                controller = CommandGenerationIndexer(
                    execution,
                    ports.source_trees,
                    lambda source: None,
                    controller_cas,
                    catalog,
                    (admitted,),
                    deadline,
                    cwd=root,
                    revalidate_worker=lambda worker: None,
                    environment=dict(
                        os.environ, LITAI_ACTION_WORKER_IDENTITY=worker.identity.uri
                    ),
                )
                pool = SimpleNamespace(
                    catalog=catalog,
                    workers=(admitted,),
                    identity=canonical_identity("admission"),
                    supports_phase=lambda worker, phase: (
                        phase in (LifecycleActionKind.LINK, LifecycleActionKind.PACKAGE)
                    ),
                )
                pool.supports_package = lambda selected, identity: (
                    selected == admitted
                    and identity
                    == canonical_identity({"packager": "local-directory-package@1"})
                )
                linker = CommandComponentLinker(
                    controller,
                    pool,
                    handoff_for=lambda plan, receipt: handoffs[
                        plan.component_revision
                    ].link_handoff(plan, receipt),
                    result_source=lambda worker, ref: worker_cas.get_bytes(ref),
                )
                dependent = next(
                    revision
                    for revision, node in linker.nodes.items()
                    if len(node.predecessor_ids) > 1
                )
                self.assertIsNone(linker.try_reserve_link(*accepted[dependent]))
                pending = set(accepted)
                while pending:
                    progress = False
                    for revision in sorted(pending, key=lambda item: item.uri):
                        reservation = linker.try_reserve_link(*accepted[revision])
                        if reservation is None:
                            continue
                        manifest = reservation.run()
                        self.assertEqual(
                            manifest.exports, accepted[revision][1].build.exports
                        )
                        pending.remove(revision)
                        progress = True
                    self.assertTrue(progress, "LINK dependency queue stalled")
                self.assertEqual(len(linker._completed), len(accepted))
                self.assertTrue(reads)
                self.assertEqual(list(jobs.iterdir()), [])

                from tests.support.standard_command_package_fixture import (
                    assert_standard_command_package,
                )

                assert_standard_command_package(
                    self,
                    snapshot,
                    execution,
                    linker,
                    accepted,
                    local_ports,
                    worker_cas,
                    source_cas,
                )

                from tests.support.package_worker_proof_fixture import (
                    assert_package_proof,
                )

                assert_package_proof(
                    self,
                    execution,
                    linker,
                    accepted,
                    controller_cas,
                    deadline,
                    source_cas,
                )

                leaf = next(
                    revision
                    for revision, node in linker.nodes.items()
                    if len(node.predecessor_ids) == 1
                )
                self.assertNotEqual(leaf, execution.root_revision)
                worker_cas.path_for(proof_refs[leaf]).write_bytes(
                    b"corrupt provider proof"
                )
                with self.assertRaises(ActionWireError):
                    linker.link(*accepted[execution.root_revision])
