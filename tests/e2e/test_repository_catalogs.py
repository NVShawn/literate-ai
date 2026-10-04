from __future__ import annotations

import json
import unittest
from pathlib import Path

from literate_ai.adapters.repository_catalogs import (
    plan_inherited_catalogs,
)
from literate_ai.application.repository_lineage import (
    RepositoryCatalogFile,
    ResolvedRepositoryCatalog,
)
from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryLineageNode,
)
from tests.support.fixtures_test_repository_lineage import fixture

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


if __name__ == "__main__":
    unittest.main()
