"""Live admission, command receiver and supervised generation with separate CASes."""

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tests.support.fixtures_test_action_generate_source as fixture_module
from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.adapters.command_generator import CommandSourceGenerator
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.registered_generation import register_source_generator
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.contracts.worker_capabilities import (
    NvidiaProbeStatus,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
)
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server

_CHILD = """
import os
from contextlib import contextmanager
from pathlib import Path
from literate_ai.generate_worker import main
from literate_ai.adapters.action_generate_execution import GenerationWorkerRuntime
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_cached_coding_cli_source_generation_runner import (
    CachedCodingCliSourceGenerationRunnerTests,
)
@contextmanager
def runtime(binding):
    f = CachedCodingCliSourceGenerationRunnerTests()
    f.setUp()
    try:
        runner = CachedCodingCliSourceGenerationRunner(
            f.generator, cas=FileSystemCAS(Path(os.environ['LITAI_GENERATE_CAS'])),
            invocation_provider=lambda _: f.invocation, workspace_binding=binding,
        )
        runner.retained_source = binding.retained_source
        if binding.retained_source is not None:
            def forbidden(*args, **kwargs):
                raise AssertionError('retained worker must not call model')
            f.generator.generate = forbidden
        yield GenerationWorkerRuntime(f.node.definition, f.node.recipe, runner)
    finally: f.tearDown()
raise SystemExit(main(runtime_factory=runtime, admission_guard=lambda: None))
"""


class CommandGenerateWorkerTests(unittest.TestCase):
    def test_live_worker_generates_imports_and_indexes_source(self):
        self.exercise(retained_mode=False)

    def test_live_worker_transfers_retained_source_imports_and_indexes(self):
        self.exercise(retained_mode=True)

    def exercise(self, *, retained_mode):
        f = fixture_module.GenerateSourceTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        root = f.fixture.fixture.root
        value = f.fixture.value
        retained = None
        transferred = {
            blob_path(value.prompt): f.fixture.fixture.cas.get_bytes(value.prompt)
        }
        if retained_mode:
            retained, _ = f.fixture.fixture._retained_input()
            for _, content in retained.files:
                ref = f.fixture.fixture.cas.put_bytes(content)
                transferred[blob_path(ref)] = content
        worker_cas = FileSystemCAS(root / "worker-cas")
        candidate_cas = FileSystemCAS(root / "candidate-cas")
        jobs = root / "jobs"
        jobs.mkdir()
        # Provision locked private authority, but leave prompt transfer to HTTP.
        for reference in f.fixture.fixture.cas.iter_refs():
            if blob_path(reference) not in transferred:
                worker_cas.put_bytes(
                    f.fixture.fixture.cas.get_bytes(reference),
                    media_type=reference.media_type,
                )
        receiver = root / "receiver.py"
        receiver.write_text(f"""
import os,sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_generate_worker import ConfiguredGenerateWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
code = {_CHILD!r}
tool = discover_python_toolchain(pinned_command=(sys.executable,))
launcher = LocalComponentToolBinding(sys.executable, ('-c', code),
    authority_identity=canonical_identity({{'runtime':tool.identity,'code':code}}),
    _authority_guard=tool.require_unchanged)
worker = ConfiguredGenerateWorker(launcher, environment=dict(os.environ),
    authority_identity=canonical_identity('private-fixture-runtime'),
    admission_guard=lambda value: None)
raise SystemExit(main(generate_worker=worker))
""")
        with source_cas_server(transferred) as (url, reads):
            worker = ExecutionWorker(
                "generate",
                ExecutionWorkerKind.COMMAND,
                action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
                command=(
                    sys.executable,
                    "-I",
                    str(receiver),
                    "--cas",
                    str(worker_cas.root),
                    "--workspace",
                    str(jobs),
                    "--source-cas-url",
                    url,
                    "--allow-http",
                ),
                environment=tuple(
                    ExecutionWorkerEnvironment(name, name, True)
                    for name in ("LITAI_ACTION_WORKER_IDENTITY", "PYTHONPATH")
                ),
            )
            catalog = ExecutionWorkerCatalog((worker,))
            hardware = WorkerHardwareObservation(
                worker.worker_id,
                datetime.now(UTC).isoformat(),
                "macos",
                "macOS",
                "15.0",
                "arm64",
                8,
                8,
                16384,
                (),
                NvidiaProbeStatus.NOT_APPLICABLE,
            )
            repository = Path(__file__).resolve().parents[2]
            pool = CommandActionWorkerPool(
                lambda: catalog,
                lambda: WorkerHardwareObservationCatalog((hardware,)),
                lambda selected: canonical_identity({"health": selected.identity.uri}),
                ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=10)),
                phase=LifecycleActionKind.INDEX,
                source_handoff="http-cas",
                target_profile="host",
                cwd=root,
                environment=dict(
                    os.environ,
                    LITAI_ACTION_WORKER_IDENTITY=worker.identity.uri,
                    PYTHONPATH=os.pathsep.join(
                        (str(repository), str(repository / "src"))
                    ),
                ),
                maximum_hardware_age=timedelta(minutes=15),
            )
            indexer = CommandGenerationIndexer.from_admission(
                value.execution_plan,
                f.registry,
                lambda source: f.registry.evidence(source).candidate,
                f.cas,
                pool,
            )
            returned = []

            def fetch(selected, reference):
                self.assertEqual(selected, worker)
                returned.append(reference)
                return worker_cas.get_bytes(reference)

            generator = CommandSourceGenerator(
                indexer,
                pool,
                cache_key_provider=f.fixture.fixture.runner.planned_cache_key,
                result_source=fetch,
                candidate_cas=candidate_cas,
                retained_source=retained,
                retained_source_authorization=retained.identity.uri
                if retained
                else None,
            )
            wrapper = register_source_generator(generator, f.registry)
            reservation = wrapper.try_reserve_generate(f.prepared)
            self.assertIsNotNone(reservation)
            output = reservation.run()
            self.assertEqual(f.registry.resolve(output.candidate.tree_identity), f.root)
            self.assertEqual(
                output.candidate.workspace_allocation_identity,
                value.workspace_allocation_identity,
            )
            self.assertEqual(
                generator.cache_key_for_candidate(output.candidate),
                generator.planned_cache_key(f.prepared),
            )
            self.assertTrue(returned)
            self.assertIn((blob_path(value.prompt), None), reads)
            if retained is not None:
                self.assertEqual(
                    output.provenance.retained_source_identity, retained.identity
                )
                self.assertEqual(output.candidate.tree_identity, retained.tree_identity)
                for path in transferred:
                    self.assertIn((path, None), reads)
                f.fixture.fixture.generator.generate.assert_not_called()
            self.assertEqual(list(jobs.iterdir()), [])
            # INDEX uses the same live pool and source registry after generation.
            index_identity = indexer.index(
                f.prepared.plan.component_revision, output.candidate.tree_identity
            )
            self.assertIsNotNone(index_identity)
            self.assertEqual(list(jobs.iterdir()), [])
