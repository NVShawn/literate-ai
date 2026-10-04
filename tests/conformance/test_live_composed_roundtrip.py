from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.contracts import ContentIdentity

from .test_live_bidirectional_roundtrip import (
    host_flavor,
    invoke,
    locked_sample_copy,
    run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT = REPO_ROOT / "samples" / "service-stack"
HARNESS = REPO_ROOT / "samples" / "_harness" / "service-stack"
EXECUTION = HARNESS / "acceptance" / "execution.json"
ORACLE = HARNESS / "acceptance" / "oracle.json"


def _semantic_graph(bundle: dict[str, object]) -> tuple[set[str], set[str]]:
    graph = bundle["result"]["component_graph_draft"]
    if not isinstance(graph, dict):
        raise AssertionError("inverse result omitted its Component graph")
    nodes = graph["nodes"]
    edges = graph["edges"]
    if len(nodes) != 3 or len(edges) != 2:
        raise AssertionError(f"expected a three-node/two-edge graph, got {graph!r}")
    node_descriptions = {
        str(node["coordinate"]): " ".join(
            [
                str(node["coordinate"]),
                str(node["title"]),
                *node["provided_capabilities"],
            ]
        ).lower()
        for node in nodes
    }
    node_text = set(node_descriptions.values())
    edge_capabilities = {str(edge["capability"]).lower() for edge in edges}

    root_coordinate = str(graph["root_coordinate"])
    invoice_coordinates = {
        coordinate
        for coordinate, description in node_descriptions.items()
        if coordinate != root_coordinate
        and "invoice" in description
        and "service" in description
    }
    money_coordinates = {
        coordinate
        for coordinate, description in node_descriptions.items()
        if coordinate != root_coordinate
        and "money" in description
        and "calcul" in description
    }
    if len(invoice_coordinates) != 1:
        raise AssertionError("inverse graph lost the invoice-service boundary")
    if len(money_coordinates) != 1:
        raise AssertionError("inverse graph lost the money-calculation boundary")

    invoice_coordinate = next(iter(invoice_coordinates))
    money_coordinate = next(iter(money_coordinates))
    dependency_pairs = {
        (str(edge["source_coordinate"]), str(edge["target_coordinate"]))
        for edge in edges
    }
    if (root_coordinate, invoice_coordinate) not in dependency_pairs:
        raise AssertionError("inverse graph lost the invoice-service dependency")
    if (invoice_coordinate, money_coordinate) not in dependency_pairs:
        raise AssertionError("inverse graph lost the money-calculation dependency")
    return node_text, edge_capabilities


def _promoted_composition(project: Path, root_manifest: Path):
    del project
    return (
        FilesystemLockedGenerationAuthorityReader()
        .read(
            root_manifest.parent,
            target_name="host",
        )
        .authority.lock
    )


def _root_generated_source(
    generation: dict[str, object], root_revision: ContentIdentity
) -> Path:
    custody = generation.get("standard_source_generation")
    if not isinstance(custody, dict) or not isinstance(custody.get("components"), list):
        raise AssertionError("generation omitted typed per-Component source custody")
    roots = [
        item
        for item in custody["components"]
        if isinstance(item, dict)
        and item.get("component_revision") == root_revision.to_dict()
    ]
    if len(roots) != 1:
        raise AssertionError("generation custody did not identify one root Component")
    locator = roots[0].get("workspace_locator")
    if not isinstance(locator, str):
        raise AssertionError("root Component custody omitted its workspace locator")
    source = Path(locator) / "source"
    if source.is_symlink() or not source.is_dir():
        raise AssertionError("root Component custody source is unavailable")
    return source


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_RUN_LIVE_COMPOSED_ROUNDTRIP") == "1",
    "set LITERATE_AI_RUN_LIVE_COMPOSED_ROUNDTRIP=1 for composed live round-trip",
)
class LiveComposedRoundTripTests(unittest.TestCase):
    def test_invoice_graph_forward_inverse_promote_and_regenerate(self) -> None:
        bazel = shutil.which("bazel") or shutil.which("bazelisk")
        if bazel is None:
            self.skipTest("Bazel is required for the composed live round-trip")
        execution = json.loads(EXECUTION.read_text())
        expected = {
            item["case_id"]: item["expected_result"]
            for item in json.loads(ORACLE.read_text())["oracle_results"]
        }
        with tempfile.TemporaryDirectory(
            prefix="literate-ai-live-composed-roundtrip-"
        ) as temporary:
            root = Path(temporary)
            key = root / "trust.key"
            key.write_bytes(b"live-composed-roundtrip-local-trust-key")
            forward = root / "forward"
            locked_component = locked_sample_copy(root, "python", component=COMPONENT)
            forward_generation = invoke(
                "generate",
                str(locked_component),
                "--output",
                str(forward),
                "--flavor",
                f"+{host_flavor()}",
                "--flavor",
                "+python",
            )
            forward_composition = (
                FilesystemLockedGenerationAuthorityReader()
                .read(locked_component, target_name="host")
                .authority.lock
            )
            source = _root_generated_source(
                forward_generation,
                forward_composition.root_revision,
            )
            attestation_path = root / "attestation.json"
            attestation_path.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/cli-result@1",
                        "ok": True,
                        "command": "spec.attest",
                        "result": invoke(
                            "spec",
                            "attest",
                            str(source),
                            "--signer",
                            "live-composed-roundtrip",
                            "--machine",
                            platform.node() or "local-host",
                            "--key",
                            str(key),
                        ),
                    }
                )
            )
            bundle = invoke(
                "spec",
                "derive",
                str(source),
                "--translator",
                "coding-cli",
                "--allow-model-egress",
                "--attestation",
                str(attestation_path),
                "--trust-key",
                str(key),
            )
            _semantic_graph(bundle)
            bundle_path = root / "bundle.json"
            bundle_path.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/cli-result@1",
                        "ok": True,
                        "command": "spec.derive",
                        "result": bundle,
                    }
                )
            )
            review_arguments = [
                "spec",
                "review",
                str(bundle_path),
                "--actor",
                "live-composed-roundtrip",
                "--key",
                str(key),
            ]
            for uncertainty in bundle["result"]["uncertainty"]["items"]:
                review_arguments.extend(("--resolve", uncertainty["uncertainty_id"]))
            review_path = root / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/cli-result@1",
                        "ok": True,
                        "command": "spec.review",
                        "result": invoke(*review_arguments),
                    }
                )
            )
            accepted = root / "accepted"
            promoted = root / "promoted"
            acceptance = invoke(
                "spec",
                "accept",
                str(source),
                str(bundle_path),
                "--review",
                str(review_path),
                "--target",
                str(accepted),
                "--project-target",
                str(promoted),
                "--trust-key",
                str(key),
            )
            root_manifest = promoted / acceptance["promoted_project"]["component"]
            composition = _promoted_composition(promoted, root_manifest)
            self.assertEqual(len(composition.nodes), 3)
            self.assertEqual(len(composition.edges), 2)
            graph = bundle["result"]["component_graph_draft"]
            python_flavor_path = promoted / "flavors" / "lang-python" / "flavor.md"
            python_flavor = parse_flavor_markdown(
                python_flavor_path.read_bytes(), source=python_flavor_path.as_posix()
            )
            self.assertEqual(
                python_flavor.applicable_capabilities,
                tuple(
                    sorted(
                        {
                            capability
                            for node in graph["nodes"]
                            for capability in node["provided_capabilities"]
                        }
                    )
                ),
            )

            regenerated = root / "regenerated"
            regenerated_generation = invoke(
                "generate",
                str(root_manifest.parent),
                "--output",
                str(regenerated),
                "--flavor",
                "+python",
                "--flavor",
                "+bazel",
            )
            regenerated_source = _root_generated_source(
                regenerated_generation,
                composition.root_revision,
            )
            output_base = root / "bazel-regenerated"
            common = [bazel, f"--output_base={output_base}"]
            run([*common, "test", "//..."], cwd=regenerated_source)
            observed = {}
            for invocation in execution["invocations"]:
                argument = json.dumps(
                    invocation["arguments"], sort_keys=True, separators=(",", ":")
                )
                observed[invocation["case_id"]] = json.loads(
                    run(
                        [*common, "run", "//:run", "--", argument],
                        cwd=regenerated_source,
                    )
                )
            self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
