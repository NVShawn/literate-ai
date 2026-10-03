"""Portable recipe-derived inputs for revalidating transferred source evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass

from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.models.coding_cli import (
    _acceptance_argument_vectors,
    _acceptance_result_shape,
)
from literate_ai.contracts import (
    ContentIdentity,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.generated_tests import validate_generated_test_suite

_SCHEMA = "literate-ai/source-evidence-validation@1"
_MAX_BYTES = 16 * 1024 * 1024
_FIELDS = frozenset(
    {
        "schema",
        "recipe_identity",
        "managed_graph",
        "specification_references",
        "acceptance",
    }
)


@dataclass(frozen=True, slots=True)
class SourceEvidenceValidationInputs:
    """An immutable snapshot of validation authority, without host locations."""

    recipe_identity: str
    managed_graph: CycloneDxManagedGraph
    specification_references: tuple[str, ...]
    acceptance_document: bytes

    def __post_init__(self):
        ContentIdentity.parse_uri(self.recipe_identity)
        if not isinstance(self.managed_graph, CycloneDxManagedGraph):
            raise TypeError("source validation requires a managed dependency graph")
        if (
            not isinstance(self.specification_references, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.specification_references
            )
            or self.specification_references
            != tuple(sorted(set(self.specification_references)))
        ):
            raise ValueError("source validation references must be canonical strings")
        if not isinstance(self.acceptance_document, bytes):
            raise TypeError("source validation acceptance must be immutable JSON bytes")
        value = json.loads(self.acceptance_document)
        if (
            not isinstance(value, dict)
            or set(value) != {"arguments", "result_shape"}
            or not isinstance(value["arguments"], list)
            or any(not isinstance(item, list) for item in value["arguments"])
            or canonical_json_bytes(value) != self.acceptance_document
        ):
            raise ValueError(
                "source validation acceptance must use exact canonical fields"
            )

    @classmethod
    def from_recipe(cls, recipe):
        return cls(
            str(recipe.identity),
            recipe.managed_sbom_graph,
            tuple(getattr(recipe, "non_acceptance_document_paths", ())),
            canonical_json_bytes(
                {
                    "arguments": list(_acceptance_argument_vectors(recipe)),
                    "result_shape": _acceptance_result_shape(recipe),
                }
            ),
        )

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            "recipe_identity": self.recipe_identity,
            "managed_graph": self.managed_graph.to_dict(),
            "specification_references": list(self.specification_references),
            "acceptance": json.loads(self.acceptance_document),
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != _FIELDS
            or value["schema"] != _SCHEMA
            or not isinstance(value["specification_references"], list)
            or len(canonical_json_bytes(value)) > _MAX_BYTES
        ):
            raise ValueError("invalid portable source validation inputs")
        return cls(
            value["recipe_identity"],
            CycloneDxManagedGraph.from_dict(value["managed_graph"]),
            tuple(value["specification_references"]),
            canonical_json_bytes(value["acceptance"]),
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def validate(self, source_bom_content: bytes, suite_content: bytes):
        acceptance = json.loads(self.acceptance_document)
        bom = validate_cyclonedx_bom(
            source_bom_content,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=self.managed_graph,
        )
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=self.recipe_identity,
            specification_references=self.specification_references,
            acceptance_arguments=acceptance["arguments"],
            result_shape=acceptance["result_shape"],
        )
        return bom, suite
