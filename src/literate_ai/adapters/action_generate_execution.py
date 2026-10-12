"""Execute admitted GENERATE with private runtime resolution and owned workspace."""

import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import capture_generate_result
from literate_ai.adapters.action_retained_source import (
    materialize_retained_input,
    transfer_retained_input,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.generation_workspace import GenerationWorkspaceBinding
from literate_ai.adapters.models.coding_cli import GenerationRecipe
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.application.source_generation_scheduling import (
    execute_component_source_generation_node,
)
from literate_ai.storage.cas import BlobNotFoundError


@dataclass(frozen=True)
class GenerationWorkerRuntime:
    definition: object
    recipe: GenerationRecipe
    runner: CachedCodingCliSourceGenerationRunner


def execute_worker_generation(
    *,
    input_record,
    input_identity,
    deadline,
    cas,
    workspace_root,
    runtime_factory,
    admission_guard,
    blob_source=None,
):
    """Private runtime construction cannot precede exact prompt admission."""
    if (
        not callable(runtime_factory)
        or not callable(admission_guard)
        or (blob_source is not None and not callable(blob_source))
    ):
        raise TypeError(
            "GENERATION requires private runtime, live admission and explicit transport"
        )
    value = GenerateWorkerInput.admit(input_record, input_identity, deadline)

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    try:
        prompt = cas.get_bytes(value.prompt)
    except BlobNotFoundError:
        if blob_source is None:
            raise
        prompt = blob_source(value.prompt)
        current()
        value.read_prompt(prompt, deadline)
        if cas.put_bytes(prompt, media_type=value.prompt.media_type) != value.prompt:
            raise ActionWireError(
                "action_generate.prompt_invalid", "prompt transport changed custody"
            ) from None
    value.read_prompt(prompt, deadline)
    if value.retained is not None:
        transfer_retained_input(
            value.retained,
            cas=cas,
            deadline=deadline,
            admission_guard=current,
            blob_source=blob_source,
        )
    current()
    root = Path(workspace_root)
    require_safe_directory(root)
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise ActionWireError(
            "action_generate.workspace_invalid", "worker root must be canonical"
        )
    parent = directory_node(root)
    stage = Path(tempfile.mkdtemp(prefix="generate-", dir=root))
    owned = directory_node(stage)
    try:
        if directory_node(root) != parent:
            raise ActionWireError(
                "action_generate.workspace_changed", "worker root changed"
            )
        binding = GenerationWorkspaceBinding.admit(value, stage, deadline)
        with ExitStack() as contexts:
            if value.retained is not None:
                retained_source = contexts.enter_context(
                    materialize_retained_input(
                        value.retained,
                        cas=cas,
                        root=root,
                        deadline=deadline,
                        admission_guard=current,
                    )
                )
                binding = replace(binding, retained_source=retained_source)
            runtime = contexts.enter_context(runtime_factory(binding))
            current()
            if (
                not isinstance(runtime, GenerationWorkerRuntime)
                or not isinstance(runtime.runner, CachedCodingCliSourceGenerationRunner)
                or runtime.runner.workspace_binding is not binding
                or runtime.runner.cas.root != cas.root
            ):
                raise ActionWireError(
                    "action_generate.runtime_invalid",
                    "private generation runtime differs",
                )
            retained = runtime.runner.retained_source
            if value.retained is None:
                if retained is not None:
                    raise ActionWireError(
                        "action_generate.runtime_invalid",
                        "unrequested retained source in private runtime",
                    )
            elif (
                not isinstance(retained, RetainedSourceInput)
                or retained is not binding.retained_source
                or retained.identity != value.retained.authorization
            ):
                raise ActionWireError(
                    "action_generate.runtime_invalid",
                    "private retained source differs from reviewed request",
                )
            else:
                retained.require_authorization(value.retained.authorization.uri)
            prepared = binding.prepare(runtime.definition, runtime.recipe, prompt)
            current()
            execution = execute_component_source_generation_node(
                prepared,
                candidate=None,
                explicitly_invalid=True,
                runner=runtime.runner,
            )
            if execution.output is None:
                if execution.failure_cause is not None:
                    raise execution.failure_cause
                raise ActionWireError(
                    "action_generate.failed", "generation produced no source output"
                )
            current()
            binding.require_current(prepared)
            result = capture_generate_result(
                input_record=input_record,
                input_identity=input_identity,
                output=execution.output,
                cas=cas,
                deadline=deadline,
            )
            current()
            binding.require_current(prepared)
            content = result.to_bytes()
        current()
    finally:
        try:
            same_parent = directory_node(root) == parent
        except (OSError, ValueError):
            same_parent = False
        if same_parent:
            _remove_owned_stage(stage, owned)
    current()
    return content
