from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_repository_updates``."""





from pathlib import Path








from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogItem,
)







from literate_ai.contracts import (
    ProjectInitializationOrigin,
)



ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = ROOT / "src" / "literate_ai" / "project_template"

def origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "https://example.test/literate-ai.git",
        "a" * 40,
        "literate-ai",
        "0.2.0",
    )

def flavor_item(node, name: str, source_name: str) -> InheritedCatalogItem:
    source = TEMPLATE / "flavors" / source_name
    return InheritedCatalogItem(
        "flavor",
        name,
        node,
        tuple(
            InheritedCatalogFile(
                f"flavors/{name}/{path.relative_to(source).as_posix()}",
                path.read_bytes(),
                False,
            )
            for path in sorted(source.rglob("*"))
            if path.is_file()
        ),
    )

from tests.support.fixtures_test_repository_lineage import fixture  # noqa: F401
# NOTE: names not defined at top level of tests.unit.test_repository_updates: ['fixture']
