"""Shared test fixtures extracted from test_retained_provider_generation."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.retained_provider_generation import (
    read_retained_provider_generation,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import canonical_identity
from literate_ai.source_to_specification.promotion_materialization import (
    PromotionInputKind,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
    generation_input_subset_identity,
)
from tests.support import fixtures_test_cli_locked_generation as fixtures


def _inventory(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class RetainedProviderGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.component, self.flavors = fixtures._generation_fixture(self.root)
        fixtures._add_generation_dependency(self.component)
        self.lock = fixtures._write_lock(self.component, self.flavors)
        self.request = GenerationPreparationRequest(
            self.component,
            fixtures._TARGET,
            fixtures._SELECTORS,
            (self.flavors,),
        )

    def read(self, **kwargs):
        return read_retained_provider_generation(
            self.request, provider_id="codex", **kwargs
        )

    def audit_evidence(self, *, omit_prefix=None):
        inputs = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            if omit_prefix is not None and relative.startswith(omit_prefix):
                continue
            kind = (
                PromotionInputKind.REVIEWED_FLAVOR
                if relative.startswith("flavors/")
                else PromotionInputKind.FORWARD_SKILL
                if relative.startswith("skills/")
                else PromotionInputKind.COMPONENT_INTENT
                if path.name == "component.md"
                else PromotionInputKind.SPECIFICATION
            )
            inputs.append(
                SourcePromotionInput(
                    kind,
                    self.root.resolve(),
                    "provider",
                    relative,
                    relative,
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        return VerifiedSourcePromotionEvidence(
            (SourcePromotionMaterializer().audit(inputs),)
        )

    def test_closure_uses_current_locked_references_and_audited_inputs(self):
        current = self.read()
        evidence = self.audit_evidence()
        qualification = canonical_identity({"reopened-qualification": 1})
        before = _inventory(self.root)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("execution")):
            closure = current.generation_closure(
                self.root.resolve(), evidence, qualification_identity=qualification
            )
        self.assertEqual(
            closure.workflow_identity,
            current.prepared.definition.workflow_definition.identity,
        )
        self.assertEqual(
            closure.routing_policy_identity,
            current.prepared.definition.routing_policy.identity,
        )
        self.assertEqual(closure.qualification_evidence_identity, qualification)
        self.assertEqual(
            closure.skill_set_identity,
            generation_input_subset_identity(
                evidence.promotion_input_audits[0],
                kinds=frozenset({PromotionInputKind.FORWARD_SKILL}),
            ),
        )
        self.assertIsNone(evidence.generation_closure)
        self.assertEqual(before, _inventory(self.root))

    def test_closure_refuses_missing_selected_flavor_even_when_another_is_audited(self):
        current = self.read()
        evidence = self.audit_evidence(omit_prefix="flavors/lang-python/")
        with self.assertRaises(SourcePromotionError):
            current.generation_closure(
                self.root.resolve(),
                evidence,
                qualification_identity=canonical_identity(
                    {"reopened-qualification": 1}
                ),
            )

    def test_closure_rechecks_actual_audit_files(self):
        current = self.read()
        path = self.root / "reviewed-extra.md"
        path.write_text("Reviewed input.\n")
        evidence = self.audit_evidence()
        path.write_text("Changed input.\n")
        with self.assertRaises(SourcePromotionError):
            current.generation_closure(
                self.root.resolve(),
                evidence,
                qualification_identity=canonical_identity(
                    {"reopened-qualification": 1}
                ),
            )
        self.assertEqual(path.read_text(), "Changed input.\n")

    def test_offline_complete_recipes_preserve_every_input(self):
        before = _inventory(self.root)
        with (
            mock.patch.dict(os.environ, {"PATH": ""}),
            mock.patch(
                "subprocess.Popen",
                side_effect=AssertionError("must not launch a process"),
            ),
        ):
            current = self.read(pipeline_model="openai/gpt-5")
            current.require_unchanged()
        self.assertEqual(before, _inventory(self.root))
        self.assertEqual(len(current.recipes), 2)
        self.assertEqual(
            set(current.recipes), {node.revision.identity for node in self.lock.nodes}
        )
        self.assertEqual(current.execution.component_lock_identity, self.lock.identity)
        for plan in current.execution.generation_plans:
            recipe = current.recipes[plan.component_revision]
            self.assertIsNotNone(recipe.model_scope)
            self.assertEqual(
                recipe.model_scope.identity, plan.generation_key.model_identity
            )
        with self.assertRaises(TypeError):
            current.recipes[self.lock.root_revision] = None

    def test_explicit_current_model_changes_every_node_recipe(self):
        first = self.read(pipeline_model="openai/gpt-5")
        second = self.read(pipeline_model="openai/gpt-5.1")
        self.assertEqual(set(first.recipes), set(second.recipes))
        for revision in first.recipes:
            self.assertNotEqual(
                first.recipes[revision].model_scope.identity,
                second.recipes[revision].model_scope.identity,
            )
            self.assertNotEqual(first.recipes[revision], second.recipes[revision])

    def test_changed_dependency_refuses_custody_and_fresh_read(self):
        current = self.read()
        path = self.component / "dependency/specs/private.md"
        path.write_bytes(path.read_bytes() + b"\nChanged current provider behavior.\n")
        before = _inventory(self.root)
        with self.assertRaises(LockedGenerationAuthorityReaderError) as caught:
            current.require_unchanged()
        self.assertEqual(caught.exception.code, "component_lock.stale")
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            self.read()
        self.assertEqual(before, _inventory(self.root))

    def test_guard_refuses_changed_workflow_and_routing(self):
        for relative in ("workflows/host.md", "routing/default.json"):
            with self.subTest(relative=relative):
                current = self.read()
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    with self.assertRaises(LockedGenerationAuthorityReaderError):
                        current.require_unchanged()
                finally:
                    path.write_bytes(original)

    def test_invalid_selection_never_creates_output(self):
        before = _inventory(self.root)
        for model in ("", " ", 1):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.read(pipeline_model=model)
        with self.assertRaises(ValueError):
            read_retained_provider_generation(self.request, provider_id="unknown")
        self.assertEqual(before, _inventory(self.root))

    def test_input_change_during_projection_prevents_return(self):
        project = LockedComponentNodePreparationAdapter.project
        path = self.root / "routing/default.json"
        changed = path.read_bytes() + b"\n"

        def change_after_projection(adapter, snapshot, plan):
            result = project(adapter, snapshot, plan)
            path.write_bytes(changed)
            return result

        with (
            mock.patch.object(
                LockedComponentNodePreparationAdapter,
                "project",
                change_after_projection,
            ),
            self.assertRaises(LockedGenerationAuthorityReaderError),
        ):
            self.read()
        self.assertEqual(path.read_bytes(), changed)
