"""Production candidate-repair adapter tests at the filesystem seam."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.candidate_repair import (
    FilesystemStandardCandidateRepairAdapter,
)
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
)
from literate_ai.contracts import StandardNodeFailurePhase
from literate_ai.contracts.executable_components import (
    CandidateFailureClassification,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeFailureEvidence,
)
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_generation_scheduling import (
    _decision,
    _names,
    _prepared_execution,
)
from tests.unit.test_standard_project_lifecycle import (
    LifecyclePorts,
    RepairableBuildPorts,
    _identity,
    _prepared_nodes,
    _service,
)


class _ImportBomMismatchBuildError(RuntimeError):
    code = "dependencies.import-bom-mismatch"


class _ImportBomMismatchBuildPorts(RepairableBuildPorts):
    def build(self, plan, provider_artifacts):
        name = self.names[plan.component_revision.uri]
        if name == "money" and self.rejected_attempts > 0:
            self.rejected_attempts -= 1
            self._record("build", name)
            raise _ImportBomMismatchBuildError(
                "generated external imports are absent from manifests and the "
                "source BOM: app"
            )
        return super().build(plan, provider_artifacts)


class FilesystemStandardCandidateRepairAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = _diamond_lock()
        self.execution, requests = _prepared_execution(self.lock)
        self.nodes = _prepared_nodes(self.execution, requests)
        self.names = _names(self.lock)
        self.revision = next(
            revision for revision, name in self.names.items() if name == "money"
        )

    def _source_output(self, node):
        return LifecyclePorts(self.execution, self.names)(node)

    def test_diagnoses_only_exact_attributable_build_or_test_rejection(self) -> None:
        node = self.nodes[self.revision]
        output = self._source_output(node)
        failure = StandardNodeFailureEvidence(
            node.plan.component_revision,
            StandardNodeFailurePhase.BUILD,
            "builder.generated-source-rejected",
            output.identity,
        )
        adapter = FilesystemStandardCandidateRepairAdapter(
            {
                self.revision: (
                    "/private/tmp/candidate/tests/app_test.cc:4:10: "
                    "fatal error: 'app.hpp' file not found; token=hunter2"
                )
            }
        )

        diagnostic = adapter.diagnose(failure, output)

        self.assertIs(
            diagnostic.classification, CandidateFailureClassification.RETRYABLE
        )
        self.assertIn("app.hpp", diagnostic.sanitized_text)
        self.assertNotIn("/private", diagnostic.sanitized_text)
        self.assertNotIn("hunter2", diagnostic.sanitized_text)
        self.assertEqual(diagnostic.failure_evidence_identity, failure.identity)

        terminal = adapter.diagnose(
            replace(
                failure,
                phase=StandardNodeFailurePhase.LIFECYCLE,
                code="lifecycle.failed",
            ),
            output,
        )
        self.assertIs(terminal.classification, CandidateFailureClassification.TERMINAL)
        self.assertIn("lifecycle", terminal.sanitized_text)

        test_failure = replace(
            failure,
            phase=StandardNodeFailurePhase.TEST,
            code="generated-test.failed",
        )
        test_diagnostic = FilesystemStandardCandidateRepairAdapter(
            {
                self.revision: (
                    "GeneratedCandidateCommandError: generated-test runner did not "
                    "emit attributable case results"
                )
            }
        ).diagnose(test_failure, output)
        self.assertIn(
            "did not emit attributable case results",
            test_diagnostic.sanitized_text,
        )

        missing_export = FilesystemStandardCandidateRepairAdapter(
            {
                self.revision: (
                    "GeneratedCandidateCommandError: generated build did not produce "
                    "the exact declared export"
                )
            }
        ).diagnose(failure, output)
        self.assertEqual(
            missing_export.sanitized_text,
            "generated build failed because it did not produce the exact declared "
            "export",
        )

        import_mismatch = replace(
            failure,
            code="dependencies.import-bom-mismatch",
        )
        mismatch_diagnostic = FilesystemStandardCandidateRepairAdapter(
            {
                self.revision: (
                    "DependencyObservationError: generated external imports are "
                    "absent from manifests and the source BOM: app"
                )
            }
        ).diagnose(import_mismatch, output)
        self.assertIs(
            mismatch_diagnostic.classification,
            CandidateFailureClassification.RETRYABLE,
        )
        self.assertEqual(
            mismatch_diagnostic.sanitized_text,
            "generated build failed because an imported module is absent from both "
            "generated source paths and declared dependency metadata",
        )
        self.assertIs(
            FilesystemStandardCandidateRepairAdapter({})
            .diagnose(
                replace(
                    import_mismatch,
                    phase=StandardNodeFailurePhase.BUILD_PLAN,
                ),
                output,
            )
            .classification,
            CandidateFailureClassification.TERMINAL,
        )

        zipapp_entrypoint = FilesystemStandardCandidateRepairAdapter(
            {
                self.revision: (
                    "TypeError: _cli() missing 1 required positional argument: 'argv'"
                )
            }
        ).diagnose(test_failure, output)
        self.assertIn(
            "zipapp entry point requires argv", zipapp_entrypoint.sanitized_text
        )
        self.assertNotIn("_cli", zipapp_entrypoint.sanitized_text)

    def test_rejects_source_output_for_another_component(self) -> None:
        node = self.nodes[self.revision]
        other = next(
            candidate
            for revision, candidate in self.nodes.items()
            if revision != self.revision
        )
        failure = StandardNodeFailureEvidence(
            node.plan.component_revision,
            StandardNodeFailurePhase.TEST,
            "generated-test.failed",
            _identity("failed-test"),
        )

        with self.assertRaisesRegex(ValueError, "another Component"):
            FilesystemStandardCandidateRepairAdapter({}).diagnose(
                failure, self._source_output(other)
            )

    def test_prepares_fresh_empty_sibling_with_new_bounded_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_workspace = root / "original"
            original_workspace.mkdir()
            node = self.nodes[self.revision]
            node = replace(
                node,
                workspace=ComponentGenerationWorkspaceDescriptor(
                    node.plan.component_revision,
                    node.plan.identity,
                    node.plan.generation_key.identity,
                    _identity("original-workspace"),
                    str(original_workspace),
                ),
            )
            output = self._source_output(node)
            failure = StandardNodeFailureEvidence(
                node.plan.component_revision,
                StandardNodeFailurePhase.TEST,
                "generated-test.failed",
                output.identity,
            )
            adapter = FilesystemStandardCandidateRepairAdapter(
                {self.revision: "generated tests disagreed with the public contract"}
            )
            diagnostic = adapter.diagnose(failure, output)

            replacement = adapter.prepare_repair(node, diagnostic, (output.identity,))

            replacement_workspace = Path(replacement.workspace.locator)
            self.assertEqual(replacement_workspace.parent, root.resolve())
            self.assertNotEqual(replacement_workspace, original_workspace)
            self.assertTrue(replacement_workspace.is_dir())
            self.assertEqual(tuple(replacement_workspace.iterdir()), ())
            self.assertNotEqual(
                replacement.request.request.identity, node.request.request.identity
            )
            self.assertNotEqual(
                replacement.workspace.allocation_identity,
                node.workspace.allocation_identity,
            )
            self.assertIn(
                diagnostic.sanitized_text.encode(), replacement.request.prompt
            )
            self.assertIn(diagnostic.identity.uri.encode(), replacement.request.prompt)
            self.assertIn(output.identity.uri.encode(), replacement.request.prompt)

            with self.assertRaisesRegex(ValueError, "exact retryable chain"):
                adapter.prepare_repair(
                    node, diagnostic, (output.identity, output.identity)
                )

    def test_production_adapter_drives_one_complete_replacement_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original_workspace = root / "original"
            original_workspace.mkdir()
            node = self.nodes[self.revision]
            prepared = dict(self.nodes)
            prepared[self.revision] = replace(
                node,
                workspace=ComponentGenerationWorkspaceDescriptor(
                    node.plan.component_revision,
                    node.plan.identity,
                    node.plan.generation_key.identity,
                    _identity("production-original-workspace"),
                    str(original_workspace),
                ),
            )
            ports = RepairableBuildPorts(
                self.execution, self.names, rejected_attempts=1
            )
            adapter = FilesystemStandardCandidateRepairAdapter(
                {
                    self.revision: (
                        "candidate/app.cc:1:10: fatal error: 'app.hpp' file not found"
                    )
                }
            )

            result = _service(ports, candidate_repair_port=adapter).execute(
                self.execution,
                component_lock=self.lock,
                invalidation=_decision(
                    self.execution,
                    self.names,
                    "money",
                    tuple(self.names.values()),
                ),
                prepared_nodes=prepared,
                max_parallelism=1,
            )

            chain = next(
                item
                for item in result.candidate_attempt_chains
                if item.component_revision.uri == self.revision
            )
            self.assertTrue(result.successful)
            self.assertEqual(len(chain.attempts), 2)
            self.assertEqual(
                tuple(item.attempt_index for item in chain.attempts), (0, 1)
            )
            self.assertNotEqual(
                chain.attempts[0].workspace_allocation_identity,
                chain.attempts[1].workspace_allocation_identity,
            )
            self.assertEqual(
                sum(event == ("generate", "money") for event in ports.events), 2
            )

    def test_import_bom_mismatch_uses_only_bounded_fresh_replacements(self) -> None:
        for rejected_attempts, expected_success, expected_attempts in (
            (1, True, 2),
            (3, False, 3),
        ):
            with self.subTest(rejected_attempts=rejected_attempts):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    original_workspace = root / "original"
                    original_workspace.mkdir()
                    node = self.nodes[self.revision]
                    prepared = dict(self.nodes)
                    prepared[self.revision] = replace(
                        node,
                        workspace=ComponentGenerationWorkspaceDescriptor(
                            node.plan.component_revision,
                            node.plan.identity,
                            node.plan.generation_key.identity,
                            _identity(f"import-mismatch-workspace-{rejected_attempts}"),
                            str(original_workspace),
                        ),
                    )
                    ports = _ImportBomMismatchBuildPorts(
                        self.execution,
                        self.names,
                        rejected_attempts=rejected_attempts,
                    )
                    adapter = FilesystemStandardCandidateRepairAdapter(
                        {
                            self.revision: (
                                "DependencyObservationError: generated external "
                                "imports are absent from manifests and the source "
                                "BOM: app"
                            )
                        }
                    )

                    result = _service(
                        ports,
                        candidate_repair_port=adapter,
                    ).execute(
                        self.execution,
                        component_lock=self.lock,
                        invalidation=_decision(
                            self.execution,
                            self.names,
                            "money",
                            tuple(self.names.values()),
                        ),
                        prepared_nodes=prepared,
                        max_parallelism=1,
                    )

                    chain = next(
                        item
                        for item in result.candidate_attempt_chains
                        if item.component_revision.uri == self.revision
                    )
                    self.assertEqual(result.successful, expected_success)
                    self.assertEqual(len(chain.attempts), expected_attempts)
                    self.assertEqual(
                        len(
                            {
                                item.workspace_allocation_identity
                                for item in chain.attempts
                            }
                        ),
                        expected_attempts,
                    )
                    self.assertEqual(
                        sum(event == ("publish", "money") for event in ports.events),
                        1 if expected_success else 0,
                    )
                    if not expected_success:
                        self.assertEqual(
                            chain.disposition.value,
                            "exhausted",
                        )
                        self.assertTrue(
                            all(
                                attempt.diagnostic is not None
                                and attempt.diagnostic.code
                                == "dependencies.import-bom-mismatch"
                                for attempt in chain.attempts
                            )
                        )


if __name__ == "__main__":
    unittest.main()
