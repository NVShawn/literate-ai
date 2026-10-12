"""Private worker workspace binding preserves logical GENERATE allocation custody."""

from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.models.coding_cli import GenerationRecipe
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    PreparedComponentGenerationNode,
)
from literate_ai.application.source_generation_scheduling import (
    validate_prepared_component_generation_node,
)
from literate_ai.contracts import ContentIdentity


@dataclass(frozen=True)
class GenerationWorkspaceBinding:
    """Created by trusted worker startup after allocating an owned empty directory."""

    value: GenerateWorkerInput
    path: Path
    node: tuple[int, int, int]
    parent_node: tuple[int, int, int]
    deadline: object
    retained_source: RetainedSourceInput | None = None

    @classmethod
    def admit(cls, value, path, deadline):
        raw = value.to_bytes()
        value = GenerateWorkerInput.admit(raw, record_identity(raw), deadline)
        path = Path(path)
        if not path.is_absolute() or path != path.resolve(strict=True):
            raise ActionWireError(
                "action_generate.workspace_invalid",
                "worker workspace must be canonical",
            )
        binding = cls(
            value, path, directory_node(path), directory_node(path.parent), deadline
        )
        binding._require_directory(fresh=True)
        return binding

    def _require_directory(self, *, fresh=False):
        self.deadline.remaining()
        if self.retained_source is not None:
            if self.value.retained is None:
                raise ActionWireError(
                    "action_generate.runtime_invalid", "unrequested retained binding"
                )
            self.retained_source.require_authorization(
                self.value.retained.authorization.uri
            )
        if (
            directory_node(self.path.parent) != self.parent_node
            or directory_node(self.path) != self.node
            or (fresh and any(self.path.iterdir()))
        ):
            raise ActionWireError(
                "action_generate.workspace_changed", "worker workspace custody changed"
            )

    def prepare(self, definition, recipe, prompt):
        self._require_directory(fresh=True)
        if (
            not isinstance(recipe, GenerationRecipe)
            or ContentIdentity.parse_uri(recipe.identity) != self.value.recipe_identity
        ):
            raise ActionWireError(
                "action_generate.recipe_changed", "worker recipe differs from request"
            )
        request = self.value.read_prompt(prompt, self.deadline)
        plan = self.value.plan
        prepared = PreparedComponentGenerationNode(
            plan,
            definition,
            recipe,
            request,
            ComponentGenerationWorkspaceDescriptor(
                plan.component_revision,
                plan.identity,
                plan.generation_key.identity,
                self.value.workspace_allocation_identity,
                str(self.path),
            ),
        )
        self.require_current(prepared)
        return prepared

    def require_current(self, prepared, *, fresh=False):
        self._require_directory(fresh=fresh)
        validate_prepared_component_generation_node(prepared)
        if (
            prepared.plan != self.value.plan
            or prepared.request.request != self.value.request
            or prepared.workspace.allocation_identity
            != self.value.workspace_allocation_identity
            or prepared.workspace.locator != str(self.path)
            or not isinstance(prepared.recipe, GenerationRecipe)
            or ContentIdentity.parse_uri(prepared.recipe.identity)
            != self.value.recipe_identity
        ):
            raise ActionWireError(
                "action_generate.workspace_input_changed",
                "worker preparation differs from bound input",
            )
