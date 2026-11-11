"""Keep independently projected authored recipe and native command inputs live.

Neither an archive nor a caller-supplied map selects the expected inputs. Current
project/lock/private-oracle owners and the actual Standard projection adapters
own them. These scopes authorize no generation, publication or consumer action.
"""

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType

from literate_ai.adapters.authored_qualification_execution import (
    CurrentAuthoredQualificationExecution,
)
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_reopening import (
    reopen_authored_qualification,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
    PreparedLockedGeneration,
)
from literate_ai.adapters.models.coding_cli import GenerationRecipe
from literate_ai.adapters.standard_project import (
    ProjectedStandardToolchainClosure,
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationResult,
)
from literate_ai.contracts.executable_components import ComponentCommandContract
from literate_ai.contracts.identity import ContentIdentity


@dataclass(frozen=True, slots=True)
class CurrentAuthoredQualificationProjection:
    """Original bounded input owner; no decoder and no publication grant."""

    current: CurrentAuthoredQualificationPlan
    _recipes: Mapping[ContentIdentity, GenerationRecipe] = field(repr=False)
    _commands: Mapping[ContentIdentity, ComponentCommandContract] = field(repr=False)
    _check: Callable[[object], None] = field(repr=False)

    def require_unchanged(self):
        self._check(self)

    @property
    def recipes(self):
        self.require_unchanged()
        return self._recipes

    @property
    def commands(self):
        self.require_unchanged()
        return self._commands


@contextmanager
def _open_projection(current, *, native_sdk_inputs, execution):
    if not isinstance(current, CurrentAuthoredQualificationPlan):
        raise TypeError("original current authored plan required")
    if execution is not None and (
        not isinstance(execution, CurrentAuthoredQualificationExecution)
        or not isinstance(execution.result, AuthoredQualificationResult)
        or execution._lifecycle.authored_plan is not current
        or execution.result.plan != current.plan
    ):
        raise TypeError("projection requires the original authored execution owner")
    active = True

    def authority():
        if not active:
            raise ValueError("qualification.authored.projection_owner_expired")
        current.require_unchanged()
        if execution is not None:
            execution.require_unchanged()

    authority()
    with current.oracles.capture_current():
        snapshot = current.oracles.authority.locked
        plan = current.plan.execution_plan
        projector = LockedComponentNodePreparationAdapter(
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=current._pipeline_model
            ),
            coding_cli=current.standard.coding_cli.name,
            native_sdk_inputs=native_sdk_inputs,
        )
        projector.guard(snapshot)
        closure = project_locked_standard_toolchain_closure(
            snapshot, plan, native_sdk_inputs=native_sdk_inputs
        )
        if not isinstance(closure, ProjectedStandardToolchainClosure):
            raise TypeError("original Standard toolchain projection required")
        recipes = {
            p.component_revision: projector.project(snapshot, p).recipe
            for p in plan.generation_plans
        }
        commands = {c.component_revision: c for c in closure.contracts}
        revisions = {p.component_revision for p in plan.generation_plans}
        if (
            len(commands) != len(closure.contracts)
            or set(commands) != revisions
            or set(recipes) != revisions
            or any(not isinstance(r, GenerationRecipe) for r in recipes.values())
            or any(
                not isinstance(c, ComponentCommandContract) for c in commands.values()
            )
        ):
            raise ValueError("qualification.authored.projection_membership_mismatch")

        recipe_views, command_views = (
            MappingProxyType(recipes),
            MappingProxyType(commands),
        )
        # Seal immutable projections under the current source/model/SDK guards.
        # Closing checks reopen those inputs and validate the original values;
        # they do not manufacture replacement expectations from archive data.
        recipe_identities = {revision: r.identity for revision, r in recipes.items()}

        def check(candidate):
            if (
                candidate is not owner
                or candidate.current is not current
                or candidate._recipes is not recipe_views
                or candidate._commands is not command_views
            ):
                raise ValueError("qualification.authored.original_projection_required")
            authority()
            snapshot.require_unchanged()
            projector.guard(snapshot)
            closure.require_unchanged()
            if (
                snapshot.authority.lock.identity
                != current.plan.origin.component_lock_identity
                or closure.record.component_lock_identity
                != snapshot.authority.lock.identity
                or closure.record.execution_plan_identity != plan.identity
                or {c.component_revision: c for c in closure.contracts} != commands
                or len(closure.contracts) != len(commands)
                or {revision: r.identity for revision, r in recipes.items()}
                != recipe_identities
            ):
                raise ValueError("qualification.authored.projection_changed")
            authority()

        owner = CurrentAuthoredQualificationProjection(
            current, recipe_views, command_views, check
        )
        try:
            owner.require_unchanged()
            yield owner
            owner.require_unchanged()
        finally:
            active = False


@contextmanager
def open_current_authored_qualification_projection(current, *, native_sdk_inputs=None):
    """Independently reopen canonical current inputs without model generation.

    Native SDK inputs, when selected, must be actual producer-backed owners. The
    existing Standard projector refuses absent, stale or substituted SDK custody.
    Importer review, archive trust and consumer execution remain separate gates.
    """

    with _open_projection(
        current, native_sdk_inputs=native_sdk_inputs, execution=None
    ) as projection:
        yield projection


@contextmanager
def open_authored_execution_projection(execution):
    """Keep original run/package ownership joined through projection and reopening."""

    if not isinstance(execution, CurrentAuthoredQualificationExecution):
        raise TypeError("original authored execution owner required")
    execution.require_unchanged()
    if not isinstance(execution._lifecycle.prepared, PreparedLockedGeneration):
        raise TypeError("original authored generation preparation required")
    with _open_projection(
        execution._lifecycle.authored_plan,
        native_sdk_inputs=execution._lifecycle.prepared.native_sdk_inputs,
        execution=execution,
    ) as projection:
        yield projection


def reopen_projected_authored_qualification(
    projection, reader, products_identity, *, max_package_bytes
):
    """Return verified observations under live independent projections only."""

    if not isinstance(projection, CurrentAuthoredQualificationProjection):
        raise TypeError("original current authored projection owner required")
    projection.require_unchanged()
    result = reopen_authored_qualification(
        projection.current,
        reader,
        products_identity,
        current_recipes=projection.recipes,
        current_commands=projection.commands,
        max_package_bytes=max_package_bytes,
    )
    projection.require_unchanged()
    return result
