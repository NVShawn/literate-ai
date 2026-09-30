from __future__ import annotations

import unittest

from literate_ai.contracts import (
    CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
    ModelScopeBinding,
    ModelScopeDecision,
    ModelScopeError,
    ModelScopeKind,
    SpecificationToSourceSkill,
    canonical_identity,
    resolve_model_scope,
)
from tests.unit.test_schema_catalog import SchemaCatalog


class ModelScopeTests(unittest.TestCase):
    @staticmethod
    def owner(label: str):
        return canonical_identity({"scope-owner": label})

    def test_nested_override_restores_by_retaining_immutable_parent(self) -> None:
        pipeline = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=self.owner("command"),
            provider_id="codex",
            candidates=((self.owner("argument"), "pipeline-model"),),
        )
        component = resolve_model_scope(
            scope_kind=ModelScopeKind.COMPONENT,
            owner_identity=self.owner("component"),
            provider_id="codex",
            parent=pipeline,
            candidates=((self.owner("component-model"), "component-model"),),
        )
        skill = resolve_model_scope(
            scope_kind=ModelScopeKind.SKILL_INVOCATION,
            owner_identity=self.owner("skill"),
            provider_id="codex",
            parent=component,
            candidates=((self.owner("skill-model"), "skill-model"),),
        )
        sibling = resolve_model_scope(
            scope_kind=ModelScopeKind.COMPONENT,
            owner_identity=self.owner("sibling"),
            provider_id="codex",
            parent=pipeline,
        )

        self.assertEqual(skill.explicit_model, "skill-model")
        self.assertEqual(skill.parent_binding_identity, component.identity)
        self.assertEqual(sibling.explicit_model, "pipeline-model")
        self.assertEqual(sibling.parent_binding_identity, pipeline.identity)
        self.assertEqual(
            sibling.resolution_trace[-1].decision, ModelScopeDecision.INHERIT
        )
        self.assertEqual(pipeline.explicit_model, "pipeline-model")

        reparsed = ModelScopeBinding.from_dict(skill.to_dict())
        self.assertEqual(reparsed, skill)
        SchemaCatalog().validate(skill.SCHEMA, skill.to_dict())

    def test_omitted_pipeline_model_preserves_cli_default(self) -> None:
        binding = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=self.owner("command"),
            provider_id="claude",
        )
        self.assertEqual(binding.model_selector, CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR)
        self.assertIsNone(binding.explicit_model)
        self.assertEqual(
            binding.resolution_trace[-1].decision, ModelScopeDecision.DEFAULT
        )

    def test_opencode_model_scope_round_trips_through_public_schema(self) -> None:
        binding = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=self.owner("opencode-command"),
            provider_id="opencode",
            candidates=((self.owner("argument"), "openai/gpt-fixture"),),
        )
        self.assertEqual(binding.explicit_model, "openai/gpt-fixture")
        self.assertEqual(ModelScopeBinding.from_dict(binding.to_dict()), binding)
        SchemaCatalog().validate(binding.SCHEMA, binding.to_dict())

    def test_equally_specific_conflicting_flavors_fail_closed(self) -> None:
        parent = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=self.owner("command"),
            provider_id="cursor-agent",
        )
        with self.assertRaises(ModelScopeError) as raised:
            resolve_model_scope(
                scope_kind=ModelScopeKind.FLAVOR_ROLE,
                owner_identity=self.owner("component"),
                provider_id="cursor-agent",
                parent=parent,
                candidates=(
                    (self.owner("flavor-a"), "model-a"),
                    (self.owner("flavor-b"), "model-b"),
                ),
            )
        self.assertEqual(raised.exception.code, "model_scope.ambiguous")

    def test_provider_and_candidate_owner_ambiguity_fail_closed(self) -> None:
        with self.assertRaises(ModelScopeError) as unsupported:
            resolve_model_scope(
                scope_kind=ModelScopeKind.PIPELINE,
                owner_identity=self.owner("command"),
                provider_id="unknown-agent",
            )
        self.assertEqual(unsupported.exception.code, "model_scope.provider_unsupported")

        candidate = self.owner("duplicate")
        with self.assertRaises(ModelScopeError) as duplicate:
            resolve_model_scope(
                scope_kind=ModelScopeKind.PIPELINE,
                owner_identity=self.owner("command"),
                provider_id="codex",
                candidates=((candidate, "model"), (candidate, "model")),
            )
        self.assertEqual(duplicate.exception.code, "model_scope.candidate_duplicate")

    def test_skill_model_selection_round_trips_through_public_schema(self) -> None:
        value = {
            "schema": SpecificationToSourceSkill.SCHEMA,
            "skill_id": "bounded-implementation",
            "version": "1.0.0",
            "title": "Bounded implementation",
            "stages": ["generate"],
            "dependencies": [],
            "instructions": "Implement only the selected Component.",
            "limitations": ["Do not flatten dependency internals."],
            "models": {"codex": "skill-model", "opencode": "openai/gpt-fixture"},
            "trust": "repository-reviewed",
        }

        skill = SpecificationToSourceSkill.from_dict(value)

        self.assertEqual(skill.model_for("codex"), "skill-model")
        self.assertEqual(skill.model_for("opencode"), "openai/gpt-fixture")
        self.assertIsNone(skill.model_for("claude"))
        self.assertEqual(SpecificationToSourceSkill.from_dict(skill.to_dict()), skill)
        SchemaCatalog().validate(skill.SCHEMA, skill.to_dict())

    def test_skill_rejects_unknown_model_provider(self) -> None:
        value = {
            "schema": SpecificationToSourceSkill.SCHEMA,
            "skill_id": "bounded-implementation",
            "version": "1.0.0",
            "title": "Bounded implementation",
            "stages": ["generate"],
            "dependencies": [],
            "instructions": "Implement only the selected Component.",
            "limitations": ["Do not flatten dependency internals."],
            "models": {"unknown-agent": "model"},
            "trust": "repository-reviewed",
        }

        with self.assertRaises(ValueError):
            SpecificationToSourceSkill.from_dict(value)


if __name__ == "__main__":
    unittest.main()
