"""Shared fixtures extracted from ``tests.unit.test_action_build_record``."""

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


def build_worker_input(fixture):
    _, execution = _fixture()
    return BuildWorkerInput(
        execution.identity,
        fixture.candidate.component_generation_plan_identity,
        fixture.candidate,
        fixture.plan,
        fixture.inputs,
        fixture.files,
        fixture.validation,
        fixture.custody.source_generation_identity,
        fixture.custody.identity,
        execution.generation_plans[0],
        execution,
    )
