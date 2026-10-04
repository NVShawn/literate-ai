from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_source_generation_custody_contracts``."""


from literate_ai.application.standard_project_services import (
    PreparedExecutableProject,
    StandardProjectApplicationService,
)
from literate_ai.contracts.executable_components import (
    ProjectSourceGenerationCustody,
)
from tests.support.fixtures_test_component_execution_planning import _diamond_lock
from tests.support.fixtures_test_component_generation_scheduling import (
    _decision,
    _names,
    _prepared_execution,
)
from tests.support.fixtures_test_standard_project_lifecycle import (
    LifecyclePorts,
    _prepared_nodes,
)


def _generated_custody() -> ProjectSourceGenerationCustody:
    lock = _diamond_lock()
    execution, requests = _prepared_execution(lock)
    prepared_by_revision = _prepared_nodes(execution, requests)
    prepared = PreparedExecutableProject(
        execution,
        tuple(
            prepared_by_revision[plan.component_revision.uri]
            for plan in execution.generation_plans
        ),
    )
    names = _names(lock)
    return StandardProjectApplicationService.generate_sources(
        prepared,
        invalidation=_decision(execution, names, "money", tuple(names.values())),
        runner=LifecyclePorts(execution, names),
        max_parallelism=2,
    )
