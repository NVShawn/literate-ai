"""Retained source never acquires a model transport or invents executable proof."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
)
from literate_ai.adapters.models.coding_cli import (
    CodingCliError,
    CodingCliSourceGenerator,
)
from literate_ai.adapters.retained_source import (
    RetainedSourceError,
    RetainedSourceInput,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardSourceGenerationAdapter,
)
from literate_ai.contracts import ModelScopeBinding, canonical_identity
from literate_ai.contracts.model_scopes import RETAINED_SOURCE_PROVIDER


class RetainedSourceNoAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("pass\n")
        self.retained = RetainedSourceInput.capture(
            self.source,
            component_lock_identity=canonical_identity("lock"),
            project_authority_identity=canonical_identity("project"),
            target="host",
        )

    def adapter(self, **kwargs):
        return FilesystemStandardSourceGenerationAdapter.from_environment(
            project_root=self.root,
            cache_root=self.root / "cache",
            cas_root=self.root / "cas",
            **kwargs,
        )

    def test_assembly_never_discovers_an_agent_and_cannot_generate(self):
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=AssertionError("agent discovery forbidden"),
        ):
            adapter = self.adapter(retained_source=self.retained)
        selection = adapter.generator.selection
        self.assertEqual(selection.name, RETAINED_SOURCE_PROVIDER)
        self.assertEqual(selection.to_dict()["generation_provider"], None)
        self.assertNotIn("executable", selection.to_dict())
        with self.assertRaises(CodingCliError) as raised:
            adapter.generator.generate(None, output_root=self.root / "output")
        self.assertEqual(raised.exception.code, "retained_source.generation_forbidden")
        self.assertFalse((self.root / "output").exists())

    def test_ordinary_generation_still_requires_its_provider(self):
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=CodingCliError("coding_cli.unavailable", "no provider"),
        ):
            with self.assertRaises(CodingCliError) as raised:
                self.adapter()
        self.assertEqual(raised.exception.code, "coding_cli.unavailable")

    def test_model_or_accepted_cache_cannot_replace_retained_origin(self):
        for changes in (
            {"pipeline_model": "some-model"},
            {
                "accepted_source_provider_id": "codex",
                "accepted_source_provider_identity": canonical_identity(
                    "cache-provider"
                ),
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.adapter(retained_source=self.retained, **changes)

    def test_selection_reopens_exact_inputs_before_metadata_use(self):
        generator = CodingCliSourceGenerator.for_retained_source(self.retained)
        (self.source / "main.py").write_text("changed\n")
        with self.assertRaises(RetainedSourceError):
            generator.selection.require_unchanged()
        with self.assertRaises(RetainedSourceError):
            CodingCliSourceGenerator.for_retained_source(self.retained)

    def test_no_model_scope_roundtrips_and_rejects_inherited_or_selected_models(self):
        scope = ModelScopeBinding.for_retained_source(canonical_identity("component"))
        self.assertIsNone(scope.explicit_model)
        self.assertEqual(ModelScopeBinding.from_dict(scope.to_dict()), scope)
        for changes in (
            {"model_selector": "some-model"},
            {"parent_binding_identity": canonical_identity("generation-scope")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(scope, **changes)

    def test_planning_uses_the_component_identity_without_a_model_override(self):
        identity = canonical_identity("component")
        snapshot = SimpleNamespace(
            require_unchanged=lambda: None,
            authority=SimpleNamespace(
                lock=SimpleNamespace(
                    nodes=(
                        SimpleNamespace(revision=SimpleNamespace(identity=identity)),
                    )
                )
            ),
        )
        scopes = LockedComponentModelSelectionAdapter().bindings(
            snapshot, coding_cli=RETAINED_SOURCE_PROVIDER
        )
        self.assertEqual(
            scopes, {identity.uri: ModelScopeBinding.for_retained_source(identity)}
        )
        with self.assertRaises(ValueError):
            LockedComponentModelSelectionAdapter(pipeline_model="model").bindings(
                snapshot, coding_cli=RETAINED_SOURCE_PROVIDER
            )


if __name__ == "__main__":
    unittest.main()
