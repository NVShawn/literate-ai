"""Strict BUILD source custody from bounded worker CAS materialization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_source_index import materialize_action_source
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.application.standard_build_inputs import (
    validate_standard_build_authority,
)
from literate_ai.application.standard_plan_finalization import (
    StandardPlanFinalizationInputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
)
from literate_ai.contracts import ContentIdentity, GeneratedSourceCandidate
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS


@contextmanager
def materialize_build_source(
    *,
    plan: StandardComponentBuildPlan,
    inputs: StandardPlanFinalizationInputs,
    candidate: GeneratedSourceCandidate,
    files: tuple[CachedSourceFile, ...],
    validation_inputs: SourceEvidenceValidationInputs,
    source_generation_identity: ContentIdentity,
    source_custody_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    cas: FileSystemCAS,
    workspace_root: Path,
    blob_source: Callable[[BlobRef], bytes] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    admission_guard: Callable[[], None] = lambda: None,
) -> Iterator[LocalSourceTreeRegistry]:
    """Yield verified source custody; this operation grants no host execution."""

    if not callable(admission_guard):
        raise TypeError("BUILD source requires a callable admission guard")

    def require_current():
        deadline.remaining()
        admission_guard()
        validate_standard_build_authority(plan, inputs, now=clock())
        deadline.remaining()

    require_current()
    if (
        not isinstance(candidate, GeneratedSourceCandidate)
        or not isinstance(validation_inputs, SourceEvidenceValidationInputs)
        or not isinstance(source_generation_identity, ContentIdentity)
        or not isinstance(source_custody_identity, ContentIdentity)
        or candidate.component_revision != plan.component_revision
        or candidate.tree_identity != plan.request.source_tree_identity
        or candidate.source_bundle_identity != inputs.intent.source_bundle_identity
    ):
        raise ActionWireError(
            "action_build.source_invalid",
            "BUILD source authority differs from its plan",
        )
    with materialize_action_source(
        files,
        candidate.tree_identity,
        deadline,
        cas=cas,
        workspace_root=workspace_root,
        blob_source=blob_source,
        require_current=require_current,
    ) as root:
        require_current()
        registry = LocalSourceTreeRegistry()
        registry.register(
            candidate,
            root,
            source_generation_identity=source_generation_identity,
            validation_inputs=validation_inputs,
        )
        if (
            registry.evidence(candidate.tree_identity).identity
            != source_custody_identity
        ):
            raise ActionWireError(
                "action_build.source_invalid", "BUILD source custody identity differs"
            )
        require_current()
        yield registry
        require_current()
