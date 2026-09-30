"""Human-readable hierarchical specification parsing and context assembly."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications import (
    LiterateMarkdownProvider,
    OpenSpecError,
)


def _document(
    *,
    name: str,
    summary: str,
    kind: str,
    extra: str = "",
    requirement: str = "Behavior",
) -> str:
    return f"""---
name: {name}
summary: {summary}
kind: {kind}
{extra}---
# {name}

### Requirement: {requirement}

The Component SHALL preserve the declared {name} behavior.

#### Scenario: Declared behavior

- **WHEN** the Component is generated
- **THEN** the {name} behavior remains observable
"""


class LiterateMarkdownProviderTests(unittest.TestCase):
    def test_component_markdown_is_a_single_file_root_not_a_spec_node(self) -> None:
        component = """---
namespace: examples
version: 1.0.0
display_name: Single file greeter
profiles: []
sample: false
provides: []
requires: []
authoring_inputs: []
workflow_definition: workflows/host.json
routing_policy: routing/default.json
flavor_slots: []
entrypoints: []
acceptance_contracts: []
source_dependencies: []
---
# Observable behavior

### Requirement: Greeting

The Component SHALL return `hello` for the `greet` operation.

#### Scenario: Greet

- **WHEN** the `greet` operation is requested
- **THEN** the result is exactly `hello`
"""
        boundary = _document(
            name="Error contract",
            summary="Named public error behavior",
            kind="contract",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "greeter"
            root.mkdir()
            root.joinpath("component.md").write_text(
                component, encoding="utf-8", newline="\n"
            )
            root.joinpath("errors.md").write_text(
                boundary, encoding="utf-8", newline="\n"
            )

            loaded = LiterateMarkdownProvider().load(
                root,
                ("component.md", "errors.md"),
                id_prefix="examples.greeter",
            )

        self.assertEqual(loaded.contents.count(("component.md", component.encode())), 1)
        self.assertEqual(
            tuple(item.uri for item in loaded.specification_set.artifacts),
            ("component.md", "errors.md"),
        )
        context = json.loads(loaded.context_document[1])
        nodes = {item["id"]: item for item in context["nodes"]}
        self.assertEqual(context["root_id"], "examples.greeter")
        self.assertEqual(nodes["examples.greeter"]["path"], "component.md")
        self.assertEqual(nodes["examples.greeter.errors"]["parent"], "examples.greeter")
        self.assertTrue(
            any(
                item.requirement_id.endswith("greeting")
                for item in loaded.specification_set.requirements
            )
        )

    def test_vfi_shape_has_derived_hierarchy_references_and_effective_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {
                "spec/spec.md": _document(
                    name="VFI",
                    summary="Virtual factory viewer and editor",
                    kind="app",
                    extra="id: vfi\nstatus: review\n",
                ),
                "spec/contract/spec.md": _document(
                    name="Component contract",
                    summary="Public behavior shared by VFI components",
                    kind="contract",
                ),
                "spec/components/spec.md": _document(
                    name="Components",
                    summary="Registry of independently generatable capabilities",
                    kind="registry",
                ),
                "spec/components/viewer/spec.md": _document(
                    name="Viewer",
                    summary="One narrow VFI viewer capability",
                    kind="component",
                    extra="references:\n  - vfi.contract\n",
                ),
                "spec/components/viewer/protocol.md": _document(
                    name="Viewer protocol",
                    summary="Public frontend and backend messages",
                    kind="part",
                ),
                "spec/components/viewer/backend.md": _document(
                    name="Viewer backend",
                    summary="Server-side scene responsibility",
                    kind="part",
                    extra=(
                        "parent: vfi.components.viewer\n"
                        "references: [vfi.components.viewer.protocol]\n"
                    ),
                ),
            }
            for relative, content in documents.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            loaded = LiterateMarkdownProvider().load(
                root, tuple(documents), id_prefix="vfi"
            )

            self.assertEqual(
                loaded.specification_set.provider_kind, "literate-markdown"
            )
            self.assertEqual(
                len(loaded.specification_set.requirements), 2 * len(documents)
            )
            self.assertIsNotNone(loaded.context_document)
            context = json.loads(loaded.context_document[1])
            self.assertEqual(context["root_id"], "vfi")
            nodes = {item["id"]: item for item in context["nodes"]}
            self.assertEqual(nodes["vfi.components.viewer"]["parent"], "vfi.components")
            self.assertEqual(
                nodes["vfi.components.viewer.backend"]["references"],
                ["vfi.components.viewer.protocol"],
            )
            effective = {
                item["node_id"]: item["document_ids"]
                for item in context["effective_documents"]
            }
            self.assertEqual(
                effective["vfi.components.viewer.backend"],
                [
                    "vfi",
                    "vfi.components",
                    "vfi.components.viewer",
                    "vfi.components.viewer.protocol",
                    "vfi.components.viewer.backend",
                ],
            )

    def test_free_form_markdown_needs_no_requirement_boilerplate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("spec.md").write_text(
                """---
name: VFI application
summary: A navigable virtual factory viewer
kind: app
---
# VFI application

The viewer presents a digital twin. This body may contain ordinary prose,
tables, diagrams, and code without a mandatory heading ritual.
""",
                encoding="utf-8",
            )

            loaded = LiterateMarkdownProvider().load(
                root, ("spec.md",), id_prefix="vfi"
            )

            requirement = loaded.specification_set.requirements[0]
            self.assertEqual(requirement.requirement_id, "vfi.document")
            self.assertEqual(
                requirement.statement, "A navigable virtual factory viewer"
            )
            self.assertEqual(
                requirement.scenarios[0].then,
                (
                    "its complete Markdown artifact is supplied under its exact "
                    "content identity",
                ),
            )

    def test_ids_and_parents_are_derived_not_independently_maintained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("spec").mkdir()
            root.joinpath("spec/spec.md").write_text(
                _document(
                    name="Root",
                    summary="Root behavior",
                    kind="app",
                    extra="id: wrong\n",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenSpecError, "does not match derived id"):
                LiterateMarkdownProvider().load(
                    root, ("spec/spec.md",), id_prefix="expected"
                )

    def test_unknown_boilerplate_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("spec.md").write_text(
                _document(
                    name="Root",
                    summary="Root behavior",
                    kind="app",
                    extra="revision: 7\n",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenSpecError, "Unknown frontmatter key"):
                LiterateMarkdownProvider().load(root, ("spec.md",), id_prefix="example")

            root.joinpath("spec.md").write_text(
                _document(
                    name="'unclosed",
                    summary="Root behavior",
                    kind="app",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenSpecError, "Invalid quoted"):
                LiterateMarkdownProvider().load(root, ("spec.md",), id_prefix="example")

    def test_reference_cycles_and_missing_parent_specs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "spec/spec.md": _document(
                    name="Root",
                    summary="Root behavior",
                    kind="app",
                    extra="references: [example.child]\n",
                ),
                "spec/child.md": _document(
                    name="Child",
                    summary="Child behavior",
                    kind="part",
                    extra="references: [example]\n",
                ),
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(OpenSpecError, "reference cycle"):
                LiterateMarkdownProvider().load(root, tuple(files), id_prefix="example")

            nested = root / "spec/missing/backend.md"
            nested.parent.mkdir()
            nested.write_text(
                _document(
                    name="Backend",
                    summary="Backend behavior",
                    kind="part",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenSpecError, "no containing spec.md"):
                LiterateMarkdownProvider().load(
                    root,
                    ("spec/spec.md", "spec/missing/backend.md"),
                    id_prefix="example",
                )


if __name__ == "__main__":
    unittest.main()
