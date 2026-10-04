from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_source_cache_hardening``."""




from pathlib import Path



from literate_ai.contracts import (
    ProjectDefinition,
)

from literate_ai.projects import LoadedProject


from tests.support.fixtures_test_source_cache import (
    _source_intelligence_policy,
)

def _project(root: Path) -> LoadedProject:
    return LoadedProject(
        root,
        ProjectDefinition(
            project_id="cache-hardening",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
        ),
    )

