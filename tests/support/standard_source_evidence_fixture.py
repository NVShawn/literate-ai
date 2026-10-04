"""Real source-SBOM and generated-suite custody for Standard adapter tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    local_generated_source_tree_identity,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxLifecycle,
    GeneratedSourceCandidate,
    canonical_json_bytes,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
    validate_generated_test_suite,
)


def register_strict_source(
    registry: LocalSourceTreeRegistry,
    source: Path,
    *,
    snapshot: object,
    generation_plan: object,
    identity_namespace: str,
    additional_components: Sequence[Mapping[str, object]] = (),
    root_dependency_refs: Sequence[str] = (),
    additional_edges: Sequence[tuple[str, str]] = (),
) -> GeneratedSourceCandidate:
    projection = LockedComponentNodePreparationAdapter().project(
        snapshot, generation_plan
    )
    recipe = projection.recipe
    reference = recipe.non_acceptance_document_paths[0]
    cases = []
    for category in ("example", "boundary", "invariant"):
        value = f"generated-{category}"
        cases.append(
            {
                "case_id": f"fixture-{category}",
                "category": category,
                "specification_refs": [reference],
                "arguments": [{"component": value}],
                "expected_result": {"component": value},
            }
        )
    suite_content = canonical_json_bytes(
        {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": recipe.identity,
            "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
            "cases": cases,
        }
    )
    suite = validate_generated_test_suite(
        suite_content,
        recipe_identity=recipe.identity,
        specification_references=recipe.non_acceptance_document_paths,
        acceptance_arguments=([{"component": projection.definition.coordinate.name}],),
        result_shape={"component": "string"},
    )
    suite_path = source.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_bytes(suite_content)
    source_bom_content, source_bom = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=recipe.managed_sbom_graph,
        additional_components=additional_components,
        additional_edges=tuple(
            (recipe.managed_sbom_graph.root_ref, reference)
            for reference in root_dependency_refs
        )
        + tuple(additional_edges),
    )
    source_bom_path = source.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
    source_bom_path.parent.mkdir(parents=True, exist_ok=True)
    source_bom_path.write_bytes(source_bom_content)

    def identity(label: str):
        return canonical_identity({identity_namespace: label})

    candidate = GeneratedSourceCandidate(
        generation_plan.component_revision,
        identity("orchestration-request"),
        identity("planned-coding-cli-request"),
        generation_plan.identity,
        generation_plan.generation_key.identity,
        identity("context"),
        identity("prompt"),
        ContentIdentity.parse_uri(recipe.identity),
        identity("workspace"),
        local_generated_source_tree_identity(source),
        identity("bundle"),
        identity("manifest"),
        source_bom.bom_identity,
        ContentIdentity.parse_uri(suite.content_identity),
    )
    registry.register(candidate, source, recipe=recipe)
    return candidate


__all__ = ["register_strict_source"]
