"""Shared test fixtures extracted from test_cli_generation."""

from __future__ import annotations

from dataclasses import replace


def bind_tree(generated, root, **candidate_updates):
    from literate_ai.adapters.lifecycle import local_generated_source_tree_identity

    candidate = replace(
        generated.output.candidate,
        tree_identity=local_generated_source_tree_identity(root),
        **candidate_updates,
    )
    provenance = replace(
        generated.output.provenance, candidate_identity=candidate.identity
    )
    output = replace(
        generated.output,
        candidate=candidate,
        candidate_identity=candidate.identity,
        provenance=provenance,
        provenance_identity=provenance.identity,
    )
    return replace(generated, output=output, output_identity=output.identity)
