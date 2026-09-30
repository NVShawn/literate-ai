"""Strict component.md authoring adapter tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
    render_component_markdown,
)

PIN = "sha256:" + "a" * 64


def document(*, line_ending: str = "\n") -> str:
    lines = [
        "---",
        "namespace: samples",
        "name: invoice-cli",
        "version: 1.0.0",
        "display_name: Invoice CLI",
        "profiles:",
        "  - sample",
        "  - portable",
        "sample: true",
        "provides:",
        "  - name: invoice-api",
        "    version: 1.0.0",
        "    interface:",
        "      uri: interfaces/invoice.json",
        f"      pin: {PIN}",
        "requires:",
        "  - requirement_id: money",
        "    capability: money-api",
        "    version_range: '>=1,<2'",
        "    dependency_kind: generation",
        "    optional: false",
        "    constraints:",
        "      - key: platform",
        "        operator: equals",
        "        values:",
        "          - host",
        "specification_provider: literate-markdown",
        "specification_roots:",
        "  - specs/spec.md",
        "  - specs/contracts.json",
        "authoring_inputs:",
        "  - kind: specification-to-source-skill",
        "    uri: skills/plan.json",
        "  - kind: specification-to-source-skill",
        "    uri: skills/implement.json",
        "workflow_definition:",
        "  uri: workflows/host.json",
        f"  pin: {PIN}",
        "routing_policy: routing/default.json",
        "flavor_slots:",
        "  - slot_id: language",
        "    axis: implementation.language-ecosystem",
        "    cardinality: exactly-one",
        "    capability_contract: invoice-api",
        "entrypoints:",
        "  - name: run",
        "    kind: portable-application",
        "    path: run",
        "acceptance_contracts:",
        "  - acceptance/execution.json",
        "source_dependencies:",
        "  - dependency_id: sqlite",
        "    repository_url: https://github.com/sqlite/sqlite.git",
        "    revision_selector:",
        "      kind: branch",
        "      value: trunk",
        "    dependency_kind: build",
        "    integration_contract: dependencies/sqlite.md",
        "assets:",
        "  - asset_id: tax-table",
        "    source: assets/tax-table.csv",
        "    path: source/data/tax-table.csv",
        "    role: runtime-data",
        "    media_type: text/csv",
        f"    pin: {PIN}",
        "---",
        "A portable invoice application.",
        "",
        "The prose remains readable Markdown.",
    ]
    return line_ending.join(lines)


class ComponentMarkdownAdapterTests(unittest.TestCase):
    def test_colon_bearing_constraint_values_round_trip(self) -> None:
        values = (
            "sample::add",
            "sha256:" + "a" * 64,
            "https://example.invalid/api",
            "key:",
            "ordinary",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            text = document().replace(
                "          - host",
                "\n".join("          - " + json.dumps(value) for value in values),
            )
            parsed = parse_component_markdown(path, text, project_root=root)
            rendered = render_component_markdown(parsed, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)
        self.assertEqual(reparsed, parsed)
        self.assertIn(f"pin: {PIN}", rendered)
        self.assertIn("repository_url: https://github.com/sqlite/sqlite.git", rendered)

    def test_entrypoint_deployment_unit_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            value = document().replace(
                "    path: run\n", "    path: run\n    deployment_unit: invoice-cli\n"
            )
            parsed = parse_component_markdown(path, value, project_root=root)
            rendered = render_component_markdown(parsed, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)

        self.assertEqual(parsed.entrypoints[0].deployment_unit, "invoice-cli")
        self.assertIn("deployment_unit: invoice-cli", rendered)
        self.assertEqual(reparsed, parsed)

    def test_single_file_defaults_make_component_markdown_the_behavior_root(
        self,
    ) -> None:
        value = (
            document()
            .replace(
                "specification_provider: literate-markdown\n"
                "specification_roots:\n"
                "  - specs/spec.md\n"
                "  - specs/contracts.json\n",
                "",
            )
            .replace(
                "acceptance_contracts:\n  - acceptance/execution.json\n",
                "acceptance_contracts: []\n",
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            parsed = parse_component_markdown(path, value, project_root=root)
            rendered = render_component_markdown(parsed, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)

        self.assertEqual(parsed.specification_provider, "literate-markdown")
        self.assertEqual(parsed.specification_roots, ("component.md",))
        self.assertEqual(parsed.acceptance_contracts, ())
        self.assertTrue(parsed.inheritable)
        self.assertNotIn("inheritable:", rendered)
        self.assertNotIn("specification_provider:", rendered)
        self.assertNotIn("specification_roots:", rendered)
        self.assertEqual(reparsed, parsed)

    def test_inheritance_opt_out_is_explicit_and_round_trips(self) -> None:
        value = document().replace(
            "sample: true\n", "sample: true\ninheritable: false\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            parsed = parse_component_markdown(path, value, project_root=root)
            rendered = render_component_markdown(parsed, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)

        self.assertFalse(parsed.inheritable)
        self.assertIn("inheritable: false\n", rendered)
        self.assertEqual(reparsed, parsed)

    def test_full_document_projects_to_exact_authoring_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            parsed = parse_component_markdown(path, document(), project_root=root)

        self.assertEqual(parsed.coordinate.namespace, "samples")
        self.assertEqual(parsed.coordinate.name, "invoice-cli")
        self.assertEqual(parsed.profiles, ("portable", "sample"))
        self.assertEqual(
            parsed.description,
            "A portable invoice application.\n\nThe prose remains readable Markdown.",
        )
        self.assertEqual(
            parsed.specification_roots, ("specs/spec.md", "specs/contracts.json")
        )
        self.assertEqual(parsed.workflow_definition.pin.uri, PIN)
        self.assertEqual(parsed.provides[0].interface.pin.uri, PIN)
        self.assertEqual(parsed.requires[0].constraints[0].values, ("host",))
        self.assertEqual(
            tuple(item.uri for item in parsed.authoring_inputs),
            ("skills/implement.json", "skills/plan.json"),
        )
        self.assertEqual(parsed.source_dependencies[0].revision_selector.value, "trunk")
        self.assertEqual(parsed.assets[0].asset_id, "tax-table")
        self.assertEqual(parsed.assets[0].source, "assets/tax-table.csv")
        self.assertEqual(parsed.assets[0].path, "source/data/tax-table.csv")
        self.assertEqual(parsed.assets[0].pin.uri, PIN)

    def test_path_and_line_endings_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            relative = Path("components") / "invoice-cli" / "component.md"
            unix = parse_component_markdown(relative, document(), project_root=root)
            windows = parse_component_markdown(
                root / relative,
                document(line_ending="\r\n"),
                project_root=root,
            )

        self.assertEqual(unix, windows)
        self.assertEqual(unix.identity, windows.identity)

    def test_required_fields_prose_and_path_derived_name_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            empty_prose = document().rsplit("---", 1)[0] + "---\n"
            cases = (
                (
                    document().replace("version: 1.0.0\n", "", 1),
                    "frontmatter_required",
                ),
                (empty_prose, "description_missing"),
                (
                    document().replace("name: invoice-cli", "name: another"),
                    "coordinate_mismatch",
                ),
                (document().replace("sample: true", "sample: yes"), "value_invalid"),
                (
                    document().replace("sample: true", "unknown: value\nsample: true"),
                    "key_unknown",
                ),
            )
            for value, code in cases:
                with self.subTest(code=code):
                    with self.assertRaisesRegex(ComponentMarkdownError, code):
                        parse_component_markdown(path, value, project_root=root)

            with self.assertRaisesRegex(ComponentMarkdownError, "path_invalid"):
                parse_component_markdown(
                    path.with_name("manifest.md"), document(), project_root=root
                )

    def test_single_file_prose_limit_is_explicit_and_never_truncates(self) -> None:
        frontmatter = document().rsplit("---", 1)[0] + "---\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            maximum = parse_component_markdown(
                path,
                frontmatter + "x" * 16_384,
                project_root=root,
            )
            with self.assertRaisesRegex(
                ComponentMarkdownError,
                "description_limit.*explicit specification roots",
            ):
                parse_component_markdown(
                    path,
                    frontmatter + "x" * 16_385,
                    project_root=root,
                )

        self.assertEqual(len(maximum.description), 16_384)

    def test_general_yaml_features_are_rejected(self) -> None:
        replacements = (
            ("sample: true", "sample: &enabled true"),
            ("sample: true", "sample: *enabled"),
            ("sample: true", "sample: !!bool true"),
            ("display_name: Invoice CLI", "display_name: |"),
            ("profiles:\n  - sample\n  - portable", "profiles: [sample, portable]"),
            ("sample: true", "sample: true\nsample: false"),
            ("namespace: samples", "%YAML 1.2\nnamespace: samples"),
            (
                "  - name: invoice-api",
                "  - <<: *provider\n    name: invoice-api",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            for before, after in replacements:
                with self.subTest(after=after):
                    with self.assertRaises(ComponentMarkdownError):
                        parse_component_markdown(
                            path, document().replace(before, after), project_root=root
                        )

    def test_render_is_a_stable_human_readable_inverse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            original = parse_component_markdown(path, document(), project_root=root)

            rendered = render_component_markdown(original, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)

            self.assertEqual(reparsed, original)
            self.assertEqual(reparsed.identity, original.identity)
            self.assertEqual(
                render_component_markdown(reparsed, path, project_root=root), rendered
            )

        self.assertNotIn("\nname: invoice-cli\n", rendered)
        self.assertIn("provides:\n  - name: invoice-api", rendered)
        self.assertIn("workflow_definition:\n  uri: workflows/host.json", rendered)
        self.assertIn(f"  pin: {PIN}", rendered)
        self.assertTrue(
            rendered.endswith(
                "---\nA portable invoice application.\n\n"
                "The prose remains readable Markdown.\n"
            )
        )

    def test_render_quotes_ambiguous_strings_and_preserves_markdown(self) -> None:
        ambiguous_names = (
            "true",
            "123",
            "> folded",
            "# heading",
            "value: comment",
            " padded ",
            "tab\tvalue",
            'true # "quoted": value',
        )
        description = "# Invoice behavior\n\n---\n\n* Unicode café remains prose."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            original = parse_component_markdown(path, document(), project_root=root)
            for display_name in ambiguous_names:
                with self.subTest(display_name=display_name):
                    candidate = replace(
                        original,
                        display_name=display_name,
                        description=description,
                    )
                    rendered = render_component_markdown(
                        candidate, path, project_root=root
                    )
                    self.assertEqual(
                        parse_component_markdown(path, rendered, project_root=root),
                        candidate,
                    )
                    self.assertIn(
                        f"display_name: {json.dumps(display_name, ensure_ascii=False)}",
                        rendered,
                    )
                    self.assertTrue(rendered.endswith(description + "\n"))

    def test_render_handles_empty_collections_without_flowing_nested_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            original = parse_component_markdown(path, document(), project_root=root)
            candidate = replace(
                original,
                profiles=(),
                provides=(),
                requires=(),
                authoring_inputs=(),
                flavor_slots=(),
                entrypoints=(),
                acceptance_contracts=(),
                source_dependencies=(),
                assets=(),
            )

            rendered = render_component_markdown(candidate, path, project_root=root)
            reparsed = parse_component_markdown(path, rendered, project_root=root)

        self.assertEqual(reparsed, candidate)
        for key in (
            "profiles",
            "provides",
            "requires",
            "authoring_inputs",
            "flavor_slots",
            "entrypoints",
            "acceptance_contracts",
            "source_dependencies",
        ):
            self.assertIn(f"{key}: []\n", rendered)

    def test_render_rejects_values_outside_the_parsers_canonical_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "components" / "invoice-cli" / "component.md"
            original = parse_component_markdown(path, document(), project_root=root)

            with self.assertRaisesRegex(ComponentMarkdownError, "coordinate_mismatch"):
                render_component_markdown(
                    original,
                    root / "components" / "other" / "component.md",
                    project_root=root,
                )
            for description in (" leading", "trailing ", "windows\r\ntext"):
                with self.subTest(description=description):
                    with self.assertRaisesRegex(
                        ComponentMarkdownError, "description_invalid"
                    ):
                        render_component_markdown(
                            replace(original, description=description),
                            path,
                            project_root=root,
                        )
            with self.assertRaises(TypeError):
                render_component_markdown(  # type: ignore[arg-type]
                    object(), path, project_root=root
                )


if __name__ == "__main__":
    unittest.main()


class ScalarRoundTripTests(unittest.TestCase):
    def test_colon_bearing_list_scalars_survive_render_and_parse(self):
        from literate_ai.adapters.component_markdown import (
            _parse_yaml_subset,
            _yaml_string,
        )

        values = (
            "sample::add",
            "sha256:" + "a" * 64,
            "https://example.invalid/api",
            "key:",
            "ordinary",
        )
        lines = [
            "values:",
            *("  - " + _yaml_string(value, sequence_item=True) for value in values),
        ]
        self.assertEqual(_parse_yaml_subset(lines), {"values": list(values)})
