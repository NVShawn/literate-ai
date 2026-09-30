"""Project live admitted SDKs into a direct consumer's generation recipe."""

from dataclasses import replace

from literate_ai.adapters.models import GenerationRecipe
from literate_ai.adapters.models.native_sdk_recipe import RecipeNativeSdkDependency
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.sbom import project_component_lock_managed_graph


def bind_native_sdk_generation_inputs(
    recipe: GenerationRecipe, inputs: NativeSdkConsumerInputs, revision: ContentIdentity
) -> GenerationRecipe:
    """Bind public SDK contracts and import mappings; grant no build permission.

    The public preparation path must still acquire this live custody and bind the
    same inputs into its generation key and Standard lifecycle before opening its
    repository-source guard. Deserialized recipe metadata cannot replace custody.
    """
    if not isinstance(recipe, GenerationRecipe) or not isinstance(
        inputs, NativeSdkConsumerInputs
    ):
        raise TypeError(
            "SDK generation requires a recipe and live admitted consumer inputs"
        )
    inputs.require_unchanged()
    snapshot = inputs.snapshot
    lock = snapshot.authority.lock
    if (
        recipe.component_lock_identity != lock.identity
        or recipe.managed_sbom_graph
        != project_component_lock_managed_graph(lock, revision)
    ):
        raise ValueError("SDK generation recipe differs from its locked consumer graph")
    node = next(node for node in lock.nodes if node.revision.identity == revision)
    bindings = inputs.for_consumer(
        revision, target_identity=node.target_flavor_selection.identity
    )
    dependencies = []
    for binding in bindings:
        selected = binding.build.selection
        product = binding.build.product.snapshot
        contract = selected.source_lock.dependency.integration_contract
        if contract is None:
            raise ValueError(
                "SDK generation lacks its locked public integration contract"
            )
        content = snapshot.component_content(
            node.revision.authoring_identity, contract
        ).decode("utf-8")
        dependencies.append(
            RecipeNativeSdkDependency(
                selected.recipe.dependency_id,
                revision,
                binding.identity,
                selected.target_identity,
                selected.identity,
                selected.source_lock.identity,
                product.recipe_identity,
                product.identity,
                product.import_surface,
                contract,
                content,
            )
        )
    selected_dependencies = tuple(
        sorted(dependencies, key=lambda item: item.dependency_id)
    )
    if (
        recipe.native_sdk_dependencies
        and recipe.native_sdk_dependencies != selected_dependencies
    ):
        raise ValueError("SDK generation recipe contains different admitted inputs")
    inputs.require_unchanged()
    return replace(recipe, native_sdk_dependencies=selected_dependencies)


def native_sdk_generation_identities(inputs, snapshot):
    """Return direct owner IDs only after checking current live admission custody."""
    if inputs is None:
        return None
    if not isinstance(inputs, NativeSdkConsumerInputs):
        raise TypeError("SDK generation requires live admitted consumer inputs")
    inputs.require_unchanged()
    snapshot.require_unchanged()
    if inputs.snapshot.authority.lock.identity != snapshot.authority.lock.identity:
        raise ValueError("SDK generation inputs differ from the Component lock")
    return {
        node.revision.identity.uri: tuple(
            sorted(
                (
                    binding.identity
                    for binding in inputs.for_consumer(
                        node.revision.identity,
                        target_identity=node.target_flavor_selection.identity,
                    )
                ),
                key=lambda identity: identity.uri,
            )
        )
        for node in snapshot.authority.lock.nodes
        if node.revision.repository_sources
    }
