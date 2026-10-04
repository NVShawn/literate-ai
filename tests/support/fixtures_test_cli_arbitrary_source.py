from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_cli_arbitrary_source``."""


import io















from literate_ai.cli import main







from tests.unit.root_parent_adapter import root_parent_for_fixture_project

KEY = b"local-bootstrap-key-material-32-bytes-minimum"

def invoke(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    with root_parent_for_fixture_project(arguments):
        status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()

def reviewed_static_graph(bundle: dict[str, object], *, kind: str = "library") -> dict:
    result = bundle["result"]
    component = result["component_definition_draft"]
    observations = result["observations"]
    evidence = {
        item["evidence_id"]: item
        for observation in observations
        for item in observation["evidence"]
    }
    capabilities = (
        [
            "sample.config",
            "sample.ice",
            "sample.logging",
            "sample.performance",
        ]
        if kind == "library"
        else sorted(component["provided_capabilities"])
    )
    return {
        "schema": "urn:literate-ai:schema:v3:component-graph-draft",
        "source_snapshot_id": result["request"]["source_snapshot_id"],
        "root_coordinate": component["coordinate"],
        "nodes": [
            {
                "coordinate": component["coordinate"],
                "title": component["title"],
                "kind": kind,
                "profiles": [kind, "portable"],
                "provided_capabilities": capabilities,
                "capability_contracts": [
                    {
                        "name": capability,
                        "contract": f"Provides reviewed {capability} behavior.",
                        "evidence_ids": sorted(evidence),
                    }
                    for capability in capabilities
                ],
                "entrypoints": (
                    []
                    if kind == "library"
                    else [
                        {
                            "name": "run",
                            "kind": kind,
                            "path": "main.py",
                            "evidence_ids": sorted(evidence),
                        }
                    ]
                ),
                "build_needs": ["implementation.language-ecosystem"],
                "source_paths": sorted({item["path"] for item in evidence.values()}),
                "observation_ids": sorted(
                    item["observation_id"] for item in observations
                ),
                "evidence_ids": sorted(evidence),
            }
        ],
        "edges": [],
    }

