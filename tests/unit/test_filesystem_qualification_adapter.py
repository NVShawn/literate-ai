"""Filesystem qualification allocates fresh Standard and parity custody."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.qualification import (
    FilesystemQualificationParityVerifier,
    FilesystemQualificationWorkspaceAllocator,
    FilesystemStandardQualificationLifecyclePort,
    _qualification_acceptance_oracle,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceRecorder,
)
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationCase,
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.inventory import inventory_source
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleRequest,
)
from tests.unit.test_qualification_lifecycle_runner import (
    _lifecycle,
    _receipt,
    identity,
)
from tests.unit.test_standard_rebuild_adapter import (
    FilesystemStandardRebuildAdapterTests as _RebuildFixture,
)


class FilesystemQualificationAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_lifecycle_port_recomposes_two_cache_empty_standard_runtimes(self) -> None:
        fixture = _RebuildFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.project.definition.flavor_roots = ()
        fixture.snapshot.authority.lock.nodes = (
            SimpleNamespace(
                revision=SimpleNamespace(
                    identity=fixture.invalidation.changed_component
                )
            ),
        )
        workspaces = FilesystemQualificationWorkspaceAllocator(self.root)
        profile = LocalQualificationProfile(
            "standard-oracle@1",
            ("build",),
            (("test",),),
            ("source",),
            ("generated",),
            (LocalQualificationCase("known", ({"value": 1},), {"value": 2}),),
            ("api.value",),
        )
        port = FilesystemStandardQualificationLifecyclePort(
            project=fixture.project,
            prepared=fixture.prepared,
            binding=fixture.binding,
            invalidation=fixture.invalidation,
            workspaces=workspaces,
            profile=profile,
            pipeline_model="fixture/selected-model",
        )
        assembled_roots: list[Path] = []

        def assemble(**kwargs):
            root = kwargs["object_root"].parent
            assembled_roots.append(root)
            lifecycle = _lifecycle(root.name)
            root_revision = lifecycle.node_results[0].component_revision
            fixture.snapshot.authority.lock.root_revision = root_revision
            tree = lifecycle.node_results[0].source_output.candidate.tree_identity
            generated = root / "retained-generated-tree"
            generated.mkdir(parents=True)
            adapter = mock.Mock()
            adapter.runtime.source_trees.resolve.return_value = generated
            adapter.rebuild.return_value = SimpleNamespace(
                execution=SimpleNamespace(lifecycle=lifecycle),
                receipt=_receipt(lifecycle),
                lifecycle_request_identity=identity(f"request:{root.name}"),
                lifecycle_invocation_identity=identity(f"invocation:{root.name}"),
            )
            adapter._tree = tree
            return adapter

        with mock.patch(
            "literate_ai.adapters.qualification."
            "assemble_filesystem_standard_rebuild_adapter",
            side_effect=assemble,
        ) as factory:
            for label in ("run-a", "run-b"):
                run = identity(label)
                allocation = workspaces.allocate(run)
                port.execute(
                    QualificationLifecycleRequest(
                        run,
                        identity("target"),
                        fixture.lock_identity,
                        identity("specification"),
                        identity("source"),
                        identity("audit"),
                        identity("promotion-tree"),
                        allocation,
                    )
                )

        self.assertEqual(factory.call_count, 2)
        self.assertEqual(len(set(assembled_roots)), 2)
        for call in factory.call_args_list:
            self.assertEqual(call.kwargs["pipeline_model"], "fixture/selected-model")
            roots = {
                call.kwargs[name].parent
                for name in (
                    "object_root",
                    "generated_source_cache_root",
                    "source_cache_root",
                    "candidate_cas_root",
                    "accepted_cas_root",
                    "checkpoint_root",
                )
            }
            self.assertEqual(len(roots), 1)
            oracle = call.kwargs["independent_acceptance_oracle"]
            self.assertIs(oracle, port.acceptance_oracle)
            acceptance_cases = oracle.cases(
                fixture.prepared.locked_authority_snapshot.authority.lock
            )
            self.assertEqual([item.case_id for item in acceptance_cases], ["known"])
            self.assertEqual(
                acceptance_cases[0].expected_result_document, b'{"value":2}'
            )

    def test_current_suite_refusal_prevents_retained_product_context(self):
        self.assert_capture_refusal_prevents_context("suite")

    def test_source_capture_refusal_prevents_retained_product_context(self):
        self.assert_capture_refusal_prevents_context("source")

    def assert_capture_refusal_prevents_context(self, stage):
        fixture = _RebuildFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.project.definition.flavor_roots = ()
        fixture.snapshot.authority.lock.nodes = (
            SimpleNamespace(
                revision=SimpleNamespace(
                    identity=fixture.invalidation.changed_component
                )
            ),
        )
        workspaces = FilesystemQualificationWorkspaceAllocator(self.root)
        profile = LocalQualificationProfile(
            "standard-oracle@1",
            ("build",),
            (("test",),),
            ("source",),
            ("generated",),
            (LocalQualificationCase("known", ({"value": 1},), {"value": 2}),),
            ("api.value",),
        )
        port = FilesystemStandardQualificationLifecyclePort(
            project=fixture.project,
            prepared=fixture.prepared,
            binding=fixture.binding,
            invalidation=fixture.invalidation,
            workspaces=workspaces,
            profile=profile,
            retain_library_products=True,
        )
        port.evidence_recorder = QualificationEvidenceRecorder(
            max_bytes=100_000, max_records=100
        )
        run = identity("refused-suite")
        allocation = workspaces.allocate(run)
        lifecycle = _lifecycle("refused-suite")
        node = lifecycle.node_results[0]
        fixture.snapshot.authority.lock.root_revision = node.component_revision
        plan = SimpleNamespace(component_revision=node.component_revision)
        adapter = mock.Mock()
        adapter.runtime.source_trees.resolve.return_value = self.root
        adapter.rebuild.return_value = SimpleNamespace(
            execution=SimpleNamespace(
                lifecycle=lifecycle,
                planned=SimpleNamespace(
                    execution_plan=SimpleNamespace(
                        identity=lifecycle.execution_plan_identity,
                        generation_plans=(plan,),
                    )
                ),
            ),
            receipt=_receipt(lifecycle),
            lifecycle_request_identity=identity("request"),
            lifecycle_invocation_identity=identity("invocation"),
        )
        project = adapter.runtime.node_preparation.project
        project.return_value = SimpleNamespace(recipe=mock.sentinel.current_recipe)
        with (
            mock.patch(
                "literate_ai.adapters.qualification.assemble_filesystem_standard_rebuild_adapter",
                return_value=adapter,
            ),
            mock.patch("literate_ai.adapters.qualification.capture_lifecycle_records"),
            mock.patch(
                "literate_ai.adapters.qualification.verify_qualification_generated_suite",
                side_effect=(
                    QualificationCaptureError("qualification.capture.suite-invalid")
                    if stage == "suite"
                    else None
                ),
            ) as verify,
            mock.patch(
                "literate_ai.adapters.qualification.FileSystemCAS",
                return_value=mock.sentinel.candidate_cas,
            ) as cas,
            mock.patch(
                "literate_ai.adapters.qualification.capture_qualification_source_records",
                side_effect=QualificationCaptureError(
                    "qualification.capture.source-invalid"
                ),
            ) as capture,
            self.assertRaisesRegex(QualificationCaptureError, stage + "-invalid"),
        ):
            port.execute(
                QualificationLifecycleRequest(
                    run,
                    identity("target"),
                    fixture.lock_identity,
                    identity("specification"),
                    identity("source"),
                    identity("audit"),
                    identity("promotion-tree"),
                    allocation,
                )
            )
        project.assert_called_once_with(fixture.snapshot, plan)
        adapter.runtime.application.lifecycle.indexer.retain_evidence_with.assert_called_once_with(
            port.evidence_recorder
        )
        self.assertIs(verify.call_args.kwargs["recipe"], mock.sentinel.current_recipe)
        self.assertIs(verify.call_args.kwargs["tests"], node.generated_test_evidence)
        self.assertEqual(
            verify.call_args.kwargs["component_lock_identity"], fixture.lock_identity
        )
        self.assertEqual(port.product_sources, {})
        if stage == "suite":
            capture.assert_not_called()
            cas.assert_not_called()
        else:
            capture.assert_called_once_with(
                port.evidence_recorder,
                candidate=node.source_output.candidate,
                cas=mock.sentinel.candidate_cas,
            )
            self.assertFalse(cas.call_args.kwargs["create"])

    def test_library_qualification_uses_verifier_owned_library_oracle(self) -> None:
        profile = LocalQualificationProfile(
            "library-qualification@1",
            ("build",),
            (("test",),),
            ("source",),
            ("generated",),
            (LocalQualificationCase("known", ("input",), {"value": 2}),),
            ("api.value",),
        )
        oracle = LibraryAcceptance(
            "library",
            identity("specification"),
            (identity("interface"),),
            identity("surface"),
            "rust",
            identity("harness"),
            b"fn main() {}\n",
            (
                DeclaredLibraryAcceptanceCase(
                    "known", "api.value", ["input"], {"value": 2}
                ),
            ),
        )
        lock = SimpleNamespace(identity=identity("lock"))
        prepared = SimpleNamespace(
            locked_authority_snapshot=SimpleNamespace(
                authority=SimpleNamespace(
                    root_authoring=SimpleNamespace(resolved_kind="library"),
                    lock=lock,
                )
            )
        )
        project = SimpleNamespace(root=self.root)

        with (
            mock.patch(
                "literate_ai.adapters.qualification.root_component_name",
                return_value="library",
            ),
            mock.patch(
                "literate_ai.adapters.qualification.load_library_acceptance",
                return_value=oracle,
            ) as loader,
        ):
            selected = _qualification_acceptance_oracle(project, prepared, profile)

        self.assertIs(selected, oracle)
        loader.assert_called_once_with(
            self.root / "verification" / "acceptance" / "library.json", "library"
        )

    def test_library_qualification_rejects_profile_oracle_case_drift(self) -> None:
        profile = LocalQualificationProfile(
            "library-qualification@1",
            ("build",),
            (("test",),),
            ("source",),
            ("generated",),
            (LocalQualificationCase("known", ("input",), {"value": 2}),),
            ("api.value",),
        )
        oracle = LibraryAcceptance(
            "library",
            identity("specification"),
            (identity("interface"),),
            identity("surface"),
            "rust",
            identity("harness"),
            b"fn main() {}\n",
            (
                DeclaredLibraryAcceptanceCase(
                    "known", "api.value", ["other"], {"value": 2}
                ),
            ),
        )
        prepared = SimpleNamespace(
            locked_authority_snapshot=SimpleNamespace(
                authority=SimpleNamespace(
                    root_authoring=SimpleNamespace(resolved_kind="library"),
                    lock=SimpleNamespace(identity=identity("lock")),
                )
            )
        )

        with (
            mock.patch(
                "literate_ai.adapters.qualification.root_component_name",
                return_value="library",
            ),
            mock.patch(
                "literate_ai.adapters.qualification.load_library_acceptance",
                return_value=oracle,
            ),
            self.assertRaisesRegex(RuntimeError, "profile differs"),
        ):
            _qualification_acceptance_oracle(
                SimpleNamespace(root=self.root), prepared, profile
            )

    def test_library_case_binding_preserves_nested_boolean_and_integer_types(
        self,
    ) -> None:
        prepared = SimpleNamespace(
            locked_authority_snapshot=SimpleNamespace(
                authority=SimpleNamespace(
                    root_authoring=SimpleNamespace(resolved_kind="library"),
                    lock=SimpleNamespace(identity=identity("lock")),
                )
            )
        )
        for profile_arguments, oracle_arguments, profile_result, oracle_result in (
            ((True,), [1], {"value": 2}, {"value": 2}),
            ((1,), [True], {"value": 2}, {"value": 2}),
            (("input",), ["input"], True, 1),
            (("input",), ["input"], {"nested": [False]}, {"nested": [0]}),
            (({"nested": [True]},), [{"nested": [1]}], {"value": 2}, {"value": 2}),
        ):
            with self.subTest(arguments=profile_arguments, result=profile_result):
                profile = LocalQualificationProfile(
                    "library-qualification@1",
                    ("build",),
                    (("test",),),
                    ("source",),
                    ("generated",),
                    (
                        LocalQualificationCase(
                            "known", profile_arguments, profile_result
                        ),
                    ),
                    ("api.value",),
                )
                oracle = LibraryAcceptance(
                    "library",
                    identity("specification"),
                    (identity("interface"),),
                    identity("surface"),
                    "rust",
                    identity("harness"),
                    b"fn main() {}\n",
                    (
                        DeclaredLibraryAcceptanceCase(
                            "known", "api.value", oracle_arguments, oracle_result
                        ),
                    ),
                )
                with (
                    mock.patch(
                        "literate_ai.adapters.qualification.root_component_name",
                        return_value="library",
                    ),
                    mock.patch(
                        "literate_ai.adapters.qualification.load_library_acceptance",
                        return_value=oracle,
                    ),
                    self.assertRaisesRegex(RuntimeError, "profile differs"),
                ):
                    _qualification_acceptance_oracle(
                        SimpleNamespace(root=self.root), prepared, profile
                    )

    def test_live_parity_preserves_json_types_and_refuses_ambiguous_results(self):
        source = self.root / "baseline"
        generated = self.root / "generated"
        native = generated / "source"
        source.mkdir()
        native.mkdir(parents=True)
        run, tree = identity("strict-parity-run"), identity("strict-generated-tree")
        lifecycle = SimpleNamespace(
            generated_roots={run.uri: ((tree, generated),)},
            root_generated_roots={run.uri: generated},
            run_roots={run.uri: self.root},
        )
        profile = LocalQualificationProfile(
            "strict-json-parity@1",
            ("build",),
            (("test",),),
            (sys.executable, "app.py"),
            (sys.executable, "app.py"),
            (LocalQualificationCase("known", (), None),),
            ("api.result",),
        )
        for baseline, candidate, passed in (
            (b"true", b"1", False),
            (b"false", b"0", False),
            (b'{"items":[true]}', b'{"items":[1]}', False),
            (b"1", b"1.0", False),
            (b'{"value":0,"value":1}', b'{"value":1}', False),
            (b'{"value":1}', b'{"value":0,"value":1}', False),
            (b"NaN", b"NaN", False),
            (b"Infinity", b"Infinity", False),
            (b"1e9999", b"1e9999", False),
            (b"1.25", b"1.250", True),
            (b"1e2", b"100.0", True),
            (b'{"value":[1.25]}', b'{ "value": [1.250] }', True),
            (b"18446744073709551616", b"18446744073709551616", True),
            (b"null", b"null", True),
            (b'{"a":1,"b":true}', b'{ "b": true, "a": 1 }', True),
        ):
            for directory, output in ((source, baseline), (native, candidate)):
                (directory / "app.py").write_text(
                    "import sys\nsys.stdout.buffer.write(" + repr(output) + ")\n",
                    encoding="utf-8",
                )
            source_identity = type(run).parse_uri(inventory_source(source).identity)
            verifier = FilesystemQualificationParityVerifier(
                source_root=source,
                source_snapshot_identity=source_identity,
                profile=profile,
                lifecycle=lifecycle,
            )
            with self.subTest(baseline=baseline, candidate=candidate):
                evidence = verifier.verify(
                    run_identity=run,
                    source_snapshot_identity=source_identity,
                    generated_tree_identities=(tree,),
                    case_map=verifier.case_map,
                )
                self.assertEqual(evidence.cases[0].passed, passed)

    def test_parity_cases_are_content_pinned_and_observed_independently(self) -> None:
        source = self.root / "baseline"
        generated = self.root / "generated"
        generated_source = generated / "source"
        source.mkdir()
        generated_source.mkdir(parents=True)
        application = (
            "import json,sys\n"
            "value=json.loads(sys.argv[1])[0]\n"
            "print(json.dumps({'value':value['value']*2},sort_keys=True))\n"
        )
        (source / "app.py").write_text(application, encoding="utf-8")
        (generated_source / "app.py").write_text(application, encoding="utf-8")
        source_identity = identity("unused")
        source_identity = type(source_identity).parse_uri(
            inventory_source(source).identity
        )
        run = identity("parity-run")
        tree = identity("generated-tree")
        lifecycle = SimpleNamespace(
            generated_roots={run.uri: ((tree, generated),)},
            root_generated_roots={run.uri: generated},
            run_roots={run.uri: self.root},
        )
        profile = LocalQualificationProfile(
            "filesystem-parity@1",
            (sys.executable, "-m", "compileall", "-q", "."),
            ((sys.executable, "-m", "compileall", "-q", "."),),
            (sys.executable, "app.py"),
            (sys.executable, "app.py"),
            (
                LocalQualificationCase("two", ({"value": 2},), {"value": 4}),
                LocalQualificationCase("nine", ({"value": 9},), {"value": 18}),
            ),
            ("api.double",),
        )
        verifier = FilesystemQualificationParityVerifier(
            source_root=source,
            source_snapshot_identity=source_identity,
            profile=profile,
            lifecycle=lifecycle,
        )

        recorder = QualificationEvidenceRecorder(max_bytes=100_000, max_records=100)
        lifecycle.evidence_recorder = recorder
        evidence = verifier.verify(
            run_identity=run,
            source_snapshot_identity=source_identity,
            generated_tree_identities=(tree,),
            case_map=verifier.case_map,
        )
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
        )
        from literate_ai.source_to_specification.inventory import (
            source_inventory_from_dict,
        )

        # The inventory is metadata, and stays readable after baseline deletion.
        (source / "app.py").unlink()
        source.rmdir()
        retained = QualificationEvidenceReader(
            recorder.entries, max_bytes=100_000, max_records=100
        )
        inventory = source_inventory_from_dict(retained.read_json(source_identity))
        self.assertEqual(inventory.identity, source_identity.uri)
        self.assertEqual(tuple(entry.path for entry in inventory.entries), ("app.py",))
        self.assertNotIn(application.encode(), dict(recorder.entries).values())

        self.assertEqual(len(evidence.cases), 2)
        self.assertTrue(all(case.passed for case in evidence.cases))
        self.assertEqual(
            {binding.case_identity for binding in verifier.case_map.cases},
            {case.case_identity for case in evidence.cases},
        )


if __name__ == "__main__":
    unittest.main()
