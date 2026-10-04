from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogPlan,
    RepositoryCatalogError,
    catalog_imports_for_plan,
    materialize_inherited_catalogs,
    plan_inherited_catalogs,
)
from literate_ai.application.repository_lineage import (
    RepositoryCatalogFile,
    ResolvedRepositoryCatalog,
    repository_lineage_authority_graph,
)
from literate_ai.authority_graph import AuthorityGraph
from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentSelection,
)
from tests.support.fixtures_test_repository_lineage import fixture, identity, reference

ROOT = Path(__file__).resolve().parents[2]


def definition(project_id: str) -> ProjectDefinition:
    value = json.loads((ROOT / "literate.project.json").read_bytes())
    value["project_id"] = project_id
    return ProjectDefinition.from_dict(value)


class CatalogProvider:
    def __init__(self, values: dict[str, ResolvedRepositoryCatalog]) -> None:
        self.values = values

    def catalog(self, node: RepositoryLineageNode) -> ResolvedRepositoryCatalog:
        return self.values[node.project_id]


def catalog(
    node: RepositoryLineageNode, *files: tuple[str, bytes]
) -> ResolvedRepositoryCatalog:
    ordered = tuple(
        sorted(
            (RepositoryCatalogFile(path, content) for path, content in files),
            key=lambda item: item.path,
        )
    )
    return ResolvedRepositoryCatalog(node, definition(node.project_id), ordered)


class RepositoryCatalogCompositionTests(unittest.TestCase):
    def test_component_inheritance_defaults_true_and_explicit_false_opts_out(
        self,
    ) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(root),
                "child": catalog(
                    child,
                    (
                        "samples/demo/component.md",
                        b"---\ninheritable: false\n---\ndemo\n",
                    ),
                    (
                        "samples/hello-component/component.lock.json",
                        b"derived lock\n",
                    ),
                    (
                        "samples/hello-component/component.md",
                        b"---\nsample: true\n---\nhello\n",
                    ),
                    (
                        "samples/hello-component/component.resolution-audit.host.json",
                        b"derived audit\n",
                    ),
                ),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)

        self.assertEqual(
            tuple((item.kind, item.name) for item in plan.items),
            (("component", "hello-component"),),
        )
        self.assertEqual(
            tuple(file.destination for file in plan.items[0].files),
            ("samples/hello-component/component.md",),
        )
        self.assertEqual(
            tuple(
                (
                    item.item.kind,
                    item.item.name,
                    item.item.source.project_id,
                    item.disposition,
                )
                for item in plan.decisions
            ),
            (
                ("component", "demo", "child", "withheld"),
                ("component", "hello-component", "child", "effective"),
            ),
        )

    def test_component_inheritance_excludes_repository_local_lock_evidence(
        self,
    ) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(root),
                "child": catalog(
                    child,
                    ("samples/hello-component/acceptance/oracle.json", b"oracle\n"),
                    (
                        "samples/hello-component/component.lock.json",
                        b"parent lock\n",
                    ),
                    ("samples/hello-component/component.md", b"hello\n"),
                    (
                        "samples/hello-component/component.resolution-audit.host.json",
                        b"parent audit\n",
                    ),
                ),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)

        self.assertEqual(len(plan.items), 1)
        self.assertEqual(
            tuple(item.destination for item in plan.items[0].files),
            (
                "samples/hello-component/acceptance/oracle.json",
                "samples/hello-component/component.md",
            ),
        )

    def test_descendant_overrides_ancestor_and_nested_components_stay_separate(
        self,
    ) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(
                    root,
                    ("components/shared/component.md", b"root\n"),
                    ("components/shared/data.txt", b"root-data\n"),
                    ("components/shared/nested/component.md", b"nested\n"),
                    ("components/shared/nested/data.txt", b"nested-data\n"),
                    ("flavors/os-linux/flavor.md", b"linux\n"),
                    ("routing/default.json", b"root-route\n"),
                    ("workflows/default.md", b"root-workflow\n"),
                ),
                "child": catalog(
                    child,
                    ("components/shared/component.md", b"child\n"),
                    ("components/shared/data.txt", b"child-data\n"),
                    ("routing/specialized.json", b"specialized-route\n"),
                    ("skills/agent/review/SKILL.md", b"review\n"),
                    ("workflows/default.md", b"child-workflow\n"),
                ),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)

        self.assertEqual(
            tuple(
                (item.kind, item.name, item.source.project_id) for item in plan.items
            ),
            (
                ("component", "shared", "child"),
                ("component", "shared/nested", "root"),
                ("flavor", "os-linux", "root"),
                ("routing", "default.json", "root"),
                ("routing", "specialized.json", "child"),
                ("skill", "agent/review", "child"),
                ("workflow", "default.md", "child"),
            ),
        )
        shared = plan.items[0]
        self.assertEqual(
            tuple(item.destination for item in shared.files),
            ("components/shared/component.md", "components/shared/data.txt"),
        )
        self.assertEqual(shared.files[0].content, b"child\n")
        workflow = next(item for item in plan.items if item.kind == "workflow")
        self.assertEqual(workflow.files[0].destination, "workflows/default.md")
        self.assertEqual(workflow.files[0].content, b"child-workflow\n")
        shared_decisions = tuple(
            (
                item.item.source.project_id,
                item.disposition,
                item.selected_source_project_id,
            )
            for item in plan.decisions
            if item.item.kind == "component" and item.item.name == "shared"
        )
        self.assertEqual(
            shared_decisions,
            (("child", "effective", None), ("root", "shadowed", "child")),
        )

    def test_nested_skills_and_workflows_keep_parent_and_child_separate(self) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(
                    root,
                    ("skills/agent/SKILL.md", b"parent-agent\n"),
                    (
                        "skills/agent/develop-in-production-workflow/SKILL.md",
                        b"production\n",
                    ),
                    (
                        "skills/agent/develop-in-production-workflow/staging/SKILL.md",
                        b"staging\n",
                    ),
                    (
                        "skills/agent/develop-in-production-workflow/staging/dev/SKILL.md",
                        b"dev\n",
                    ),
                    ("workflows/production/workflow.md", b"production-flow\n"),
                    (
                        "workflows/production/staging/workflow.md",
                        b"staging-flow\n",
                    ),
                    (
                        "workflows/production/staging/dev/workflow.md",
                        b"dev-flow\n",
                    ),
                    ("routing/production/routing.json", b"production-route\n"),
                    ("routing/production/staging/routing.json", b"staging-route\n"),
                    (
                        "routing/production/staging/dev/routing.json",
                        b"dev-route\n",
                    ),
                ),
                "child": catalog(child),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)
        named = {(item.kind, item.name): item for item in plan.items}
        self.assertEqual(
            named[("skill", "agent")].files[0].destination,
            "skills/agent/SKILL.md",
        )
        self.assertEqual(
            named[("skill", "agent/develop-in-production-workflow")].files[0].content,
            b"production\n",
        )
        self.assertEqual(
            named[("skill", "agent/develop-in-production-workflow/staging/dev")]
            .files[0]
            .content,
            b"dev\n",
        )
        self.assertEqual(
            named[("workflow", "production/staging/dev/workflow.md")].files[0].content,
            b"dev-flow\n",
        )
        self.assertEqual(
            named[("routing", "production/staging/dev/routing.json")].files[0].content,
            b"dev-route\n",
        )
        parent_files = {file.destination for file in named[("skill", "agent")].files}
        self.assertEqual(parent_files, {"skills/agent/SKILL.md"})

    def test_component_inheritance_excludes_parent_target_lock_evidence(self) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(root),
                "child": catalog(
                    child,
                    ("components/shared/.component.lock.write.lock", b""),
                    ("components/shared/component.lock.json", b"{}\n"),
                    ("components/shared/component.md", b"shared\n"),
                    (
                        "components/shared/component.resolution-audit.host.json",
                        b"{}\n",
                    ),
                    ("components/shared/data.txt", b"data\n"),
                ),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)

        self.assertEqual(
            tuple(file.destination for file in plan.items[0].files),
            ("components/shared/component.md", "components/shared/data.txt"),
        )

    def test_private_descendant_withholds_and_shadows_ancestor_candidate(self) -> None:
        _selection, root, child, lineage = fixture()
        provider = CatalogProvider(
            {
                "root": catalog(root, ("components/shared/component.md", b"root\n")),
                "child": catalog(
                    child,
                    (
                        "components/shared/component.md",
                        b"---\ninheritable: false\n---\nprivate\n",
                    ),
                ),
            }
        )

        plan = plan_inherited_catalogs(lineage, provider)

        self.assertEqual(plan.items, ())
        self.assertEqual(
            tuple(
                (
                    item.item.source.project_id,
                    item.disposition,
                    item.selected_source_project_id,
                )
                for item in plan.decisions
            ),
            (
                ("child", "withheld", None),
                ("root", "shadowed", "child"),
            ),
        )

    def test_incomparable_origins_cannot_define_the_same_coordinate(self) -> None:
        left_ref = reference("left")
        right_ref = reference("right")
        left = RepositoryLineageNode(
            "left",
            identity("3"),
            left_ref.repository_url,
            left_ref.requested_revision,
            "c" * 40,
            RepositoryParentSelection.root(),
            (),
        )
        right = replace(
            left,
            project_id="right",
            project_identity=identity("4"),
            repository_url=right_ref.repository_url,
            resolved_revision="d" * 40,
        )
        selection = RepositoryParentSelection.inherit((left_ref, right_ref))
        lineage = RepositoryLineage(
            selection,
            (left, right),
            tuple(sorted((left.identity, right.identity), key=lambda item: item.uri)),
        )
        for path in (
            "components/shared/component.md",
            "routing/default.json",
            "workflows/default.md",
        ):
            with self.subTest(path=path):
                provider = CatalogProvider(
                    {
                        "left": catalog(left, (path, b"left\n")),
                        "right": catalog(right, (path, b"right\n")),
                    }
                )

                with self.assertRaises(RepositoryCatalogError) as raised:
                    plan_inherited_catalogs(lineage, provider)
                self.assertEqual(
                    raised.exception.code, "repository_catalog.conflicting_origin"
                )

    def test_materialization_writes_canonical_paths_and_exact_provenance(self) -> None:
        _selection, root, child, lineage = fixture()
        plan = plan_inherited_catalogs(
            lineage,
            CatalogProvider(
                {
                    "root": catalog(
                        root,
                        ("flavors/os-linux/flavor.md", b"linux\n"),
                        ("routing/default.json", b"route\n"),
                    ),
                    "child": catalog(
                        child,
                        ("skills/agent/review/SKILL.md", b"review\n"),
                        ("workflows/default.md", b"workflow\n"),
                    ),
                }
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)

            created = materialize_inherited_catalogs(target, plan)

            self.assertIn(".literate/imports.json", created)
            self.assertEqual(
                (target / "flavors" / "os-linux" / "flavor.md").read_bytes(),
                b"linux\n",
            )
            self.assertEqual(
                (target / "routing" / "default.json").read_bytes(), b"route\n"
            )
            self.assertEqual(
                (target / "workflows" / "default.md").read_bytes(), b"workflow\n"
            )
            imports = json.loads(
                (target / ".literate" / "imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [(item["kind"], item["name"]) for item in imports["imports"]],
                [
                    ("flavor", "os-linux"),
                    ("routing", "default.json"),
                    ("skill", "agent/review"),
                    ("workflow", "default.md"),
                ],
            )
            child_source = imports["imports"][2]["source"]
            self.assertTrue(child_source["ref"].endswith("@" + "b" * 40))
            self.assertEqual(
                [item["project_id"] for item in child_source["transitive_ancestors"]],
                ["root"],
            )
            self.assertEqual(
                [item["disposition"] for item in imports["decisions"]],
                ["effective", "effective", "effective", "effective"],
            )

    def test_plan_carries_one_solved_graph_through_provenance_projection(self) -> None:
        _selection, root, child, lineage = fixture()
        plan = plan_inherited_catalogs(
            lineage,
            CatalogProvider(
                {
                    "root": catalog(root, ("routing/default.json", b"root\n")),
                    "child": catalog(child),
                }
            ),
        )
        graph = plan.authority_graph
        self.assertIsInstance(graph, AuthorityGraph)
        identity = graph.identity

        with mock.patch(
            "literate_ai.adapters.repository_catalogs.repository_lineage_authority_graph",
            side_effect=AssertionError("provenance rebuilt the solved graph"),
        ):
            imports = catalog_imports_for_plan(
                plan, materialized_at="2026-01-01T00:00:00Z"
            )

        self.assertEqual(graph.identity, identity)
        self.assertEqual(imports.imports[0].source.project_id, "root")

    def test_plan_rejects_a_graph_with_the_right_nodes_but_wrong_edges(self) -> None:
        _selection, _root, _child, lineage = fixture()
        solved = repository_lineage_authority_graph(lineage)
        wrong = AuthorityGraph.create(solved.project_id, solved.nodes, ())

        with self.assertRaisesRegex(ValueError, "differs from its exact"):
            InheritedCatalogPlan(lineage, (), authority_graph=wrong)


if __name__ == "__main__":
    unittest.main()
