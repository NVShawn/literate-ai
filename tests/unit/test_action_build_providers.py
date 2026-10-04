"""Worker provider paths exist only inside verified consumer custody."""

import shutil
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.action_build_providers import materialize_build_providers
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts import canonical_identity
from tests.support import fixtures_test_action_provider_build as provider_fixture


class BuildProviderMaterializationTests(unittest.TestCase):
    def setUp(self):
        fixture = provider_fixture.ProviderBuildTransferTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        old = fixture.fixture.receiver
        self.ports = LocalStandardLifecyclePorts(
            source_trees=old.source_trees,
            object_root=fixture.fixture.root / "consumer-objects",
            contracts=tuple(old.contracts.values()),
            tool_bindings=tuple(old.tool_bindings.values()),
        )
        self.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        self.admitted = SimpleNamespace(
            accepted_providers=(fixture.receipt,),
            provider_builds=(fixture.transfer,),
            execution_plan=SimpleNamespace(generation_plans=(fixture.generation_plan,)),
            inputs=SimpleNamespace(providers=fixture.receipt.build.exports),
        )
        self.guard = Mock()

    def materialize(self):
        return materialize_build_providers(
            admitted=self.admitted,
            ports=self.ports,
            cas=self.fixture.target,
            deadline=self.fixture.deadline,
            require_current=self.guard,
            blob_source=self.fixture.source.get_bytes,
        )

    def assert_clean(self):
        self.assertEqual(list(self.ports.object_root.iterdir()), [])
        for name in (
            "_artifact_paths",
            "_artifact_blob_paths",
            "_artifact_blob_bytes",
            "_exports_by_identity",
            "_build_evidence",
        ):
            self.assertFalse(getattr(self.ports, name), name)

    def test_reopen_artifact_after_controller_removal_and_cleanup(self):
        shutil.rmtree(self.fixture.archive_root)
        export = self.fixture.receipt.build.exports[0]
        with self.materialize():
            path = self.ports.artifact_path(export)
            self.assertEqual(path.read_bytes(), b"known-output\n")
            self.assertEqual(
                self.ports.read_artifact_blob(export.blob), path.read_bytes()
            )
            self.assertTrue(self.ports.retained_evidence_records())
            self.assertGreater(self.guard.call_count, 1)
        self.assertFalse(path.exists())
        self.assert_clean()

    def test_consumer_failure_removes_paths_and_registration(self):
        with (
            self.assertRaisesRegex(RuntimeError, "consumer failed"),
            self.materialize(),
        ):
            raise RuntimeError("consumer failed")
        self.assert_clean()

    def test_changed_contract_refuses_before_registration(self):
        revision = self.fixture.receipt.component_revision.uri
        contract = self.ports.contracts[revision]
        self.ports.contracts[revision] = replace(
            contract, locked_build_authority_identity=canonical_identity("changed")
        )
        with (
            self.assertRaisesRegex(LocalStandardLifecycleError, "contract differs"),
            self.materialize(),
        ):
            self.fail("changed contract admitted")
        self.assert_clean()

    def test_revocation_or_artifact_mutation_refuses_result_and_cleans(self):
        for revoke in (False, True):
            with self.subTest(revoke=revoke):
                self.guard.side_effect = None
                with self.assertRaises((ValueError, RuntimeError)), self.materialize():
                    if revoke:
                        self.guard.side_effect = RuntimeError("consumer revoked")
                    else:
                        self.ports.artifact_path(
                            self.fixture.receipt.build.exports[0]
                        ).write_bytes(b"changed")
                self.assert_clean()

    def test_consumer_build_uses_transferred_provider_after_original_removal(self):
        self._consumer_build(configured=False)

    def test_configured_receiver_builds_with_provider_in_supervised_child(self):
        self._consumer_build(configured=True)

    def _consumer_build(self, *, configured):
        from contextlib import contextmanager

        from literate_ai.adapters.action_build_execution import (
            execute_worker_build_from_cas,
        )
        from literate_ai.adapters.action_build_record import BuildWorkerInput
        from literate_ai.adapters.action_build_result import BuildWorkerResult
        from literate_ai.adapters.action_dispatch_wire import record_identity
        from literate_ai.contracts import ComponentCommandPhase
        from literate_ai.contracts.generation_cache import CachedSourceFile
        from tests.unit.standard_source_evidence_fixture import register_strict_source
        from tests.support.fixtures_test_component_node_generation_preparation import _fixture

        snapshot, execution = _fixture()
        provider = self.fixture.receipt
        generation = next(
            item
            for item in execution.generation_plans
            if item.component_revision != provider.component_revision
        )
        provider_contract = self.ports.contracts[provider.component_revision.uri]
        command = replace(
            provider_contract.command(ComponentCommandPhase.BUILD),
            argv=(
                "{tool}",
                "-c",
                "from pathlib import Path; import os,sys; "
                "Path(sys.argv[1]).write_bytes(Path(os.environ['PROVIDER']).read_bytes())",
                "{export_path}",
                "{source_root}",
                "{object_root}",
            ),
        )
        binding = next(iter(self.ports.tool_bindings.values()))
        contract = replace(
            provider_contract,
            component_revision=generation.component_revision,
            build_system_toolchain_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
            commands=tuple(
                command if item.phase is ComponentCommandPhase.BUILD else item
                for item in provider_contract.commands
            ),
        )
        self.ports.contracts[contract.component_revision.uri] = contract
        self.ports.provider_environment["app"] = ("PROVIDER", "app")
        source = self.fixture.fixture.root / "consumer-source"
        source.mkdir()
        (source / "app.py").write_text("consumer_source = True\n")
        candidate = register_strict_source(
            self.ports.source_trees,
            source,
            snapshot=snapshot,
            generation_plan=generation,
            identity_namespace="provider-consumer",
        )
        intent = self.ports.create(
            execution, generation, candidate, provider.build.exports, ()
        )
        authorization = self.ports.authorize(
            intent,
            self.ports.index(candidate.component_revision, candidate.tree_identity),
        )
        inputs = self.ports.plan_finalization_inputs(intent, authorization)
        plan = inputs.finalize()
        custody = self.ports.source_trees.evidence(candidate.tree_identity)
        record = BuildWorkerInput(
            execution.identity,
            generation.identity,
            candidate,
            plan,
            inputs,
            tuple(
                CachedSourceFile(
                    path.relative_to(source).as_posix(),
                    self.fixture.source.put_file(path),
                )
                for path in sorted(source.rglob("*"))
                if path.is_file()
            ),
            self.ports.source_trees.validation_inputs(candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            generation,
            execution,
            (provider,),
            (self.fixture.transfer,),
        )
        content = record.to_bytes()
        shutil.rmtree(source)
        shutil.rmtree(self.fixture.archive_root)
        workspace = self.fixture.fixture.root / "worker-source"
        workspace.mkdir()
        worker_root = self.fixture.fixture.root / "worker-objects"

        runtimes = []

        @contextmanager
        def runtime(admitted, registry, recorder):
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=worker_root,
                contracts=(provider_contract, contract),
                tool_bindings=tuple(self.ports.tool_bindings.values()),
                provider_environment={"app": ("PROVIDER", "app")},
                command_phases=(ComponentCommandPhase.BUILD,),
            )
            ports.retain_evidence_with(recorder)
            runtimes.append(ports)
            yield ports

        if configured:
            import json
            import os
            import sys
            from pathlib import Path

            from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
            from literate_ai.adapters.lifecycle import LocalComponentToolBinding
            from tests.support.fixtures_test_action_build_action import build_request

            code = """
import json, os, sys
from pathlib import Path
from contextlib import contextmanager
from literate_ai.build_worker import main
from literate_ai.adapters.lifecycle import (
    LocalStandardLifecyclePorts, LocalComponentToolBinding,
)
from literate_ai.contracts import ComponentCommandContract, ComponentCommandPhase

@contextmanager
def runtime(admitted, registry, recorder):
    contracts = tuple(ComponentCommandContract.from_dict(item)
        for item in json.loads(os.environ['PROVIDER_BUILD_CONTRACTS']))
    ports = LocalStandardLifecyclePorts(
        source_trees=registry, object_root=Path('objects').resolve(),
        contracts=contracts, tool_bindings=(LocalComponentToolBinding(sys.executable),),
        provider_environment={'app': ('PROVIDER', 'app')},
        command_phases=(ComponentCommandPhase.BUILD,),
    )
    ports.retain_evidence_with(recorder)
    yield ports

raise SystemExit(main(runtime_factory=runtime))
"""
            environment = dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                PROVIDER_BUILD_CONTRACTS=json.dumps(
                    [provider_contract.to_dict(), contract.to_dict()]
                ),
            )
            from literate_ai.adapters.builders.python import discover_python_toolchain

            runtime_observation = discover_python_toolchain(
                pinned_command=(sys.executable,)
            )
            launcher = LocalComponentToolBinding(
                sys.executable,
                ("-c", code),
                authority_identity=canonical_identity(
                    {
                        "runtime": runtime_observation.identity,
                        "code": code,
                    }
                ),
                _authority_guard=runtime_observation.require_unchanged,
            )
            worker = ConfiguredBuildWorker(
                launcher,
                (binding,),
                environment=environment,
            )
            request, records = build_request(record, self.fixture.deadline)
            from unittest.mock import patch

            from literate_ai.adapters.builders._process import run_bounded_process

            def checked_process(*args, **kwargs):
                completed = run_bounded_process(*args, **kwargs)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed

            with patch(
                "literate_ai.adapters.action_worker_process.run_bounded_process",
                side_effect=checked_process,
            ):
                result_bytes = worker.execute(
                    request,
                    self.fixture.deadline,
                    records,
                    expected_worker_identity=request.worker.worker_identity,
                    cas=self.fixture.target,
                    workspace_root=workspace,
                    blob_source=self.fixture.source.get_bytes,
                )
        else:
            result_bytes = execute_worker_build_from_cas(
                input_record=content,
                input_identity=record_identity(content),
                deadline=self.fixture.deadline,
                cas=self.fixture.target,
                workspace_root=workspace,
                runtime_factory=runtime,
                blob_source=self.fixture.source.get_bytes,
            )
        result = BuildWorkerResult.admit(
            result_bytes,
            record_identity(result_bytes),
            input_record=content,
            input_identity=record_identity(content),
            deadline=self.fixture.deadline,
        )
        self.assertEqual(
            result.evidence.exports[0].blob.digest,
            provider.build.exports[0].blob.digest,
        )
        if runtimes:
            self.assertEqual(
                runtimes[0].read_artifact_blob(result.evidence.exports[0].blob),
                b"known-output\n",
            )
        from literate_ai.adapters.directory_artifacts import read_directory_export

        files = read_directory_export(
            self.fixture.target.get_bytes(result.artifact_archive),
            result.artifact_archive,
            max_bytes=256 * 1024 * 1024,
            max_entries=65534,
        )
        self.assertEqual(
            {item.path: item.content for item in files}["app"], b"known-output\n"
        )
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertFalse(list(worker_root.glob("provider-*")))

    def test_directory_export_preserves_portable_blob_bytes(self):
        from literate_ai.adapters.action_dispatch_wire import record_identity
        from literate_ai.adapters.action_provider_build import capture_provider_build
        from literate_ai.contracts import ComponentCommandPhase
        from tests.support.fixtures_test_component_node_generation_preparation import _fixture

        old = self.fixture.fixture.producer
        contract = next(iter(old.contracts.values()))
        commands = []
        for command in contract.commands:
            argv = list(command.argv)
            if command.phase is ComponentCommandPhase.BUILD:
                argv[2] = (
                    "from pathlib import Path; import sys; "
                    "p=Path(sys.argv[2]); p.mkdir(); "
                    "(p/'data').write_bytes((Path(sys.argv[1])/'app.py').read_bytes())"
                )
            else:
                argv = [part.replace("/'app')", "/'app'/'data')") for part in argv]
            commands.append(replace(command, argv=tuple(argv)))
        contract = replace(contract, commands=tuple(commands))
        producer = LocalStandardLifecyclePorts(
            source_trees=old.source_trees,
            object_root=self.fixture.fixture.root / "directory-producer",
            contracts=(contract,),
            tool_bindings=tuple(old.tool_bindings.values()),
        )
        recorder = QualificationEvidenceRecorder(
            max_bytes=64 * 1024 * 1024, max_records=4096
        )
        producer.retain_evidence_with(recorder)
        candidate = old.source_trees.evidence(
            self.fixture.receipt.build.source_tree_identity
        ).candidate
        _, execution = _fixture()
        intent = producer.create(
            execution, self.fixture.generation_plan, candidate, (), ()
        )
        authorization = producer.authorize(
            intent, canonical_identity("directory-index")
        )
        plan = producer.finalize(intent, authorization)
        output = producer.build(plan, ())
        tests = producer.test(plan, output.exports)
        observed = producer.execute(plan, output.exports)
        receipt = producer.accept(plan, tests.identity, observed.identity)
        root = producer.artifact_path(output.exports[0]).parent
        transfer = capture_provider_build(
            receipt=receipt,
            artifact_root=root,
            records=recorder.entries,
            cas=self.fixture.source,
            deadline=self.fixture.deadline,
            require_current=self.guard,
            source_validation=self.fixture.validation,
            generation_plan=self.fixture.generation_plan,
        )
        shutil.rmtree(root)
        self.ports.contracts[contract.component_revision.uri] = contract
        self.admitted.accepted_providers = (receipt,)
        self.admitted.provider_builds = (transfer,)
        self.admitted.inputs.providers = output.exports
        with self.materialize():
            export = output.exports[0]
            self.assertEqual(
                (self.ports.artifact_path(export) / "data").read_bytes(),
                b"known-output\n",
            )
            blob = self.ports.read_artifact_blob(export.blob)
            self.assertEqual(record_identity(blob).uri, export.blob.identity)
        self.assert_clean()

    def test_cleanup_restores_preexisting_custody_for_shared_blob(self):
        original = self.fixture.receipt.build.exports[0]
        existing = replace(original, component_revision=canonical_identity("existing"))
        root = self.fixture.fixture.root / "existing-artifact"
        root.mkdir()
        path = root / existing.export_id
        path.write_bytes(b"known-output\n")
        self.ports._exports_by_identity[existing.identity.uri] = existing
        self.ports._artifact_paths[existing.identity.uri] = root
        self.ports._artifact_blob_paths[existing.blob.identity] = path
        with self.materialize():
            staged = self.ports.artifact_path(original)
            self.assertNotEqual(staged, path)
        self.assertFalse(staged.exists())
        self.assertEqual(self.ports.artifact_path(existing), path)
        self.assertEqual(
            self.ports.read_artifact_blob(existing.blob), path.read_bytes()
        )
        self.assertNotIn(original.identity.uri, self.ports._exports_by_identity)
        self.assertEqual(list(self.ports.object_root.iterdir()), [])
