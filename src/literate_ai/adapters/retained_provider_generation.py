"""Reopen current provider generation inputs without allocating or generating source."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
    PreparedLockedGeneration,
)
from literate_ai.adapters.models import GenerationRecipe
from literate_ai.adapters.project_initialization import flavor_selector_directory
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import (
    ComponentExecutionPlan,
    ComponentGenerationClosure,
    ContentIdentity,
)
from literate_ai.source_to_specification.promotion_materialization import (
    PromotionInputKind,
    VerifiedSourcePromotionEvidence,
    generation_input_subset_identity,
)


@dataclass(frozen=True, slots=True)
class RetainedProviderGeneration:
    """Current locked recipes for every provider node, with source-input custody.

    Logical provider/model selection is explicit importing authority, not a claim
    that a model executable exists. Promotion, installed lifecycle authority,
    native tools and retained consumer gates must be resolved separately.
    """

    prepared: PreparedLockedGeneration
    execution: ComponentExecutionPlan
    recipes: Mapping[ContentIdentity, GenerationRecipe]

    def __post_init__(self) -> None:
        recipes = dict(self.recipes)
        expected = {
            node.revision.identity
            for node in self.prepared.locked_authority_snapshot.authority.lock.nodes
        }
        if set(recipes) != expected or any(
            not isinstance(recipe, GenerationRecipe) for recipe in recipes.values()
        ):
            raise ValueError("retained.provider.recipe-closure-invalid")
        object.__setattr__(self, "recipes", MappingProxyType(recipes))

    def require_unchanged(self) -> None:
        FilesystemLockedGenerationApplicationAdapter().require_unchanged(self.prepared)

    def generation_closure(
        self,
        project_root: Path,
        evidence: VerifiedSourcePromotionEvidence,
        *,
        qualification_identity: ContentIdentity,
    ) -> ComponentGenerationClosure:
        """Derive current closure inputs instead of copying a historical closure.

        The caller must reopen the exact qualification result separately and check
        the returned closure against it. This method checks current locked inputs
        and the audit's actual files; it grants no qualification or consumption.
        """
        if not isinstance(evidence, VerifiedSourcePromotionEvidence) or not isinstance(
            qualification_identity, ContentIdentity
        ):
            raise TypeError("retained closure requires typed audit and qualification")
        self.require_unchanged()
        root = Path(project_root).absolute()
        if ".." in root.parts:
            raise ValueError("retained.provider.project-path-unsafe")
        require_safe_directory(root)
        component = self.prepared.component_root.relative_to(root) / "component.md"
        audit = evidence.audit_containing(component.as_posix())
        audit.verify(root)
        prefixes = tuple(
            f"flavors/{flavor_selector_directory(f'+{flavor.flavor_id}')}"
            for flavor in self.prepared.selected_flavors
        )
        kinds = frozenset({PromotionInputKind.REVIEWED_FLAVOR})
        # A nonempty union alone could silently omit one selected Flavor.
        for prefix in prefixes:
            generation_input_subset_identity(
                audit, kinds=kinds, target_prefixes=(prefix,)
            )
        closure = ComponentGenerationClosure(
            generation_input_subset_identity(
                audit, kinds=kinds, target_prefixes=prefixes
            ),
            generation_input_subset_identity(
                audit, kinds=frozenset({PromotionInputKind.FORWARD_SKILL})
            ),
            self.prepared.definition.workflow_definition.identity,
            self.prepared.definition.routing_policy.identity,
            ContentIdentity.parse_uri(audit.identity),
            ContentIdentity.parse_uri(audit.materialized_tree_identity),
            qualification_identity,
        )
        audit.verify(root)
        self.require_unchanged()
        return closure


def read_retained_provider_generation(
    request: GenerationPreparationRequest,
    *,
    provider_id: str,
    pipeline_model: str | None = None,
) -> RetainedProviderGeneration:
    """Read current files and project the same model-bound recipes as Standard.

    Callers resolve the request and logical model selection independently of the
    candidate archive. This uses the ordinary bounded lock/catalog reader, but
    creates no workspace, source cache or CAS and never selects or invokes a
    coding executable. The returned recipes are current expectations for evidence
    reopening, not newly qualified generation output.
    """
    if not isinstance(request, GenerationPreparationRequest):
        raise TypeError("retained provider requires a generation preparation request")
    if pipeline_model is not None and (
        not isinstance(pipeline_model, str) or not pipeline_model.strip()
    ):
        raise ValueError("retained.provider.model-invalid")
    adapter = FilesystemLockedGenerationApplicationAdapter()
    prepared = adapter.prepare(request)
    snapshot = prepared.locked_authority_snapshot
    selector = LockedComponentModelSelectionAdapter(pipeline_model=pipeline_model)
    execution = FilesystemStandardProjectPlanningAdapter(
        model_selector=selector
    ).plan_for_coding_cli(snapshot, coding_cli=provider_id)
    projector = LockedComponentNodePreparationAdapter(
        model_selector=selector, coding_cli=provider_id
    )
    recipes = {
        plan.component_revision: projector.project(snapshot, plan).recipe
        for plan in execution.generation_plans
    }
    result = RetainedProviderGeneration(prepared, execution, recipes)
    result.require_unchanged()
    return result
