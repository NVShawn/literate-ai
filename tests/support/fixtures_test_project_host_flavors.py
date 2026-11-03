"""Public initialization fixture for project-local host Flavor observations."""

from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.contracts import (
    ProjectInitializationOrigin,
    RepositoryLineage,
    RepositoryParentSelection,
)


def initialize_root(target):
    FilesystemProjectInitializationAdapter(
        standard_binding_provider=lambda: None,
        initialization_origin_provider=lambda: ProjectInitializationOrigin(
            "https://example.test/literate-ai.git", "a" * 40, "literate-ai", "0.2.0"
        ),
        repository_lineage_resolver=lambda selection: RepositoryLineage(
            selection, (), ()
        ),
    ).initialize(
        target,
        flavor_selectors=("+python", "+macos"),
        source_intelligence_provider="none",
        empty=True,
        parent_selection=RepositoryParentSelection.root(),
    )
