"""Retained only as a patch target; provider-build tests were removed.

``tests.support.fixtures_test_action_execute_providers`` patches
``tests.unit.test_action_provider_build._fixture``, so the module and that
name must stay importable until the support fixture drops the patch.
"""

from tests.support.fixtures_test_component_node_generation_preparation import (
    _fixture,
)

__all__ = ["_fixture"]
