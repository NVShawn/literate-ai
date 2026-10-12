"""Publish verified worker source into the controller's allocated workspace."""

import json
import tempfile
from pathlib import Path

from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_generate_import import import_generate_result
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.exclusive_directory import (
    directory_node,
    publish_directory_exclusive,
)
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.application.source_generation_scheduling import (
    validate_prepared_component_generation_node,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.executable_components.packages import SourceBundleFile


def receive_generated_source(
    *,
    content,
    result_identity,
    input_record,
    input_identity,
    prepared,
    deadline,
    cas,
    registry,
    admission_guard,
    blob_source=None,
):
    """Revalidate proof and recipe, stage exact files, then register local custody."""
    if not isinstance(registry, LocalSourceTreeRegistry) or not callable(
        admission_guard
    ):
        raise TypeError(
            "GENERATION requires a local source registry and live admission"
        )
    value = GenerateWorkerInput.admit(input_record, input_identity, deadline)

    def current():
        deadline.remaining()
        admission_guard()
        validate_prepared_component_generation_node(prepared)
        if (
            prepared.plan != value.plan
            or prepared.request.request != value.request
            or ContentIdentity.parse_uri(prepared.recipe.identity)
            != value.recipe_identity
            or prepared.workspace.allocation_identity
            != value.workspace_allocation_identity
        ):
            raise ActionWireError(
                "action_generate.input_changed", "controller generation input changed"
            )
        value.read_prompt(prepared.request.prompt, deadline)

    current()
    CachedCodingCliSourceGenerationRunner._require_fresh_workspace(prepared)
    root = Path(prepared.workspace.locator)
    parent, original = directory_node(root.parent), directory_node(root)

    def fresh():
        current()
        if directory_node(root.parent) != parent or directory_node(root) != original:
            raise ActionWireError(
                "action_generate.workspace_changed", "controller workspace changed"
            )
        CachedCodingCliSourceGenerationRunner._require_fresh_workspace(prepared)

    result = import_generate_result(
        content=content,
        result_identity=result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        cas=cas,
        admission_guard=fresh,
        blob_source=blob_source,
    )
    fresh()
    # Import has already verified this closed bundle and every referenced byte.
    refs = {ref.identity: ref for ref in result.evidence_records}

    tree = json.loads(
        cas.get_bytes(refs[result.output.candidate.source_bundle_identity.uri])
    )
    files = tuple(SourceBundleFile.from_dict(item) for item in tree["files"])
    stage = Path(tempfile.mkdtemp(prefix="generated-", dir=root.parent))
    owned = directory_node(stage)
    try:
        for item in files:
            fresh()
            if directory_node(stage) != owned:
                raise ActionWireError(
                    "action_generate.workspace_changed", "source stage changed"
                )
            target = stage.joinpath(*item.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            cas.copy_to(item.blob, target)
        fresh()
        # Run the existing SBOM, generated-test and tree checks before publication.
        checked = LocalSourceTreeRegistry()
        checked.register(
            result.output.candidate,
            stage,
            source_generation_identity=result.output.identity,
            recipe=prepared.recipe,
        )
        fresh()
        root.rmdir()  # Only the pinned, still-empty allocation may be replaced.
        publish_directory_exclusive(
            stage, root, expected_source=owned, expected_parent=parent
        )
        current()
        if directory_node(root.parent) != parent or directory_node(root) != owned:
            raise ActionWireError(
                "action_generate.workspace_changed", "published source changed"
            )
        registry.register(
            result.output.candidate,
            root,
            source_generation_identity=result.output.identity,
            recipe=prepared.recipe,
        )
        return result.output
    except BaseException:
        if directory_node(root.parent) == parent:
            for location in (stage, root):
                try:
                    location.lstat()
                except FileNotFoundError:
                    continue
                _remove_owned_stage(location, owned)
        raise
