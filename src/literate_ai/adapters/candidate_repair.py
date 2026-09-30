"""Conservative production adapter for bounded generated-candidate repair."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
)
from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
)
from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.contracts import ContentIdentity, HashAlgorithm
from literate_ai.contracts.executable_components import (
    CandidateFailureClassification,
    CandidateRepairDiagnostic,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
)

_RETRYABLE_CODES = frozenset(
    {
        "builder.generated-source-rejected",
        "dependencies.import-bom-mismatch",
        "generated-test.failed",
    }
)
_MISSING_INCLUDE = re.compile(
    r"fatal error:\s*['\"]?([A-Za-z0-9_.+-]{1,128})['\"]?\s+file not found",
    re.IGNORECASE,
)
_GENERATED_TEST_FACTS = (
    "generated-test runner did not emit attributable case results",
    "generated-test runner emitted an invalid case result protocol",
    "generated-test runner emitted an invalid or failed case result",
    "generated-test runner did not execute every and only selected case",
)
_GENERATED_BUILD_FACTS = ("generated build did not produce the exact declared export",)
_IMPORT_BOM_MISMATCH_FACT = (
    "generated external imports are absent from manifests and the source BOM"
)
_ZIPAPP_ARGV_FAILURE = re.compile(
    r"\b_cli\(\) missing 1 required positional argument: ['\"]argv['\"]"
)


def _prompt_identity(prompt: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(prompt).hexdigest())


def _safe_diagnostic(failure: StandardNodeFailureEvidence, raw: str | None) -> str:
    """Return only a bounded, portable fact that is useful for a fresh replacement."""

    phase = failure.phase.value
    if isinstance(raw, str):
        match = _MISSING_INCLUDE.search(raw[:8192])
        if match is not None:
            return (
                f"generated {phase} failed because required file "
                f"{match.group(1)} was unavailable to a generated target"
            )
        for fact in _GENERATED_TEST_FACTS:
            if fact in raw[:8192]:
                return f"generated {phase} failed because its {fact}"
        for fact in _GENERATED_BUILD_FACTS:
            if fact in raw[:8192]:
                return (
                    "generated build failed because it did not produce the exact "
                    "declared export"
                )
        if _IMPORT_BOM_MISMATCH_FACT in raw[:8192]:
            return (
                "generated build failed because an imported module is absent from "
                "both generated source paths and declared dependency metadata"
            )
        if _ZIPAPP_ARGV_FAILURE.search(raw[:8192]) is not None:
            return (
                "generated test failed because its Python zipapp entry point requires "
                "argv even though the archive invokes that callable with no arguments"
            )
    return (
        f"generated source failed its {phase} command; regenerate a complete "
        "replacement whose source, generated tests, and build metadata agree"
    )


class FilesystemStandardCandidateRepairAdapter:
    """Retry only attributable build/test rejection in a fresh sibling workspace."""

    def __init__(self, diagnostics: Mapping[str, str]) -> None:
        self._diagnostics = diagnostics

    def diagnose(
        self,
        failure: StandardNodeFailureEvidence,
        output: SourceGenerationRunOutput,
    ) -> CandidateRepairDiagnostic:
        if not isinstance(failure, StandardNodeFailureEvidence) or not isinstance(
            output, SourceGenerationRunOutput
        ):
            raise TypeError("candidate repair requires typed failure and source output")
        if output.candidate.component_revision != failure.component_revision:
            raise ValueError("candidate repair output names another Component")
        retryable = failure.code in _RETRYABLE_CODES and failure.phase in {
            StandardNodeFailurePhase.BUILD,
            StandardNodeFailurePhase.TEST,
        }
        return CandidateRepairDiagnostic(
            failure.component_revision,
            failure.phase,
            failure.code,
            _safe_diagnostic(
                failure, self._diagnostics.get(failure.component_revision.uri)
            ),
            (
                CandidateFailureClassification.RETRYABLE
                if retryable
                else CandidateFailureClassification.TERMINAL
            ),
            failure.identity,
        )

    def prepare_repair(
        self,
        original: PreparedComponentGenerationNode[object, object],
        diagnostic: CandidateRepairDiagnostic,
        predecessor_attempt_identities: tuple[ContentIdentity, ...],
    ) -> PreparedComponentGenerationNode[object, object]:
        if not isinstance(original, PreparedComponentGenerationNode):
            raise TypeError("candidate repair requires one prepared Component")
        if (
            not isinstance(diagnostic, CandidateRepairDiagnostic)
            or diagnostic.classification is not CandidateFailureClassification.RETRYABLE
            or not predecessor_attempt_identities
            or len(predecessor_attempt_identities) > 2
            or any(
                not isinstance(item, ContentIdentity)
                for item in predecessor_attempt_identities
            )
            or len({item.uri for item in predecessor_attempt_identities})
            != len(predecessor_attempt_identities)
            or diagnostic.component_revision != original.plan.component_revision
        ):
            raise ValueError("candidate repair request is not an exact retryable chain")
        prompt = (
            original.request.prompt
            + (
                "\n\n## Bounded candidate-repair evidence\n"
                f"Failure: {diagnostic.sanitized_text}\n"
                f"Diagnostic identity: {diagnostic.identity.uri}\n"
                "Predecessor candidate attempt identities:\n"
                + "".join(
                    f"- {identity.uri}\n" for identity in predecessor_attempt_identities
                )
                + "Produce a complete fresh replacement; do not reuse prior candidate "
                "files.\n"
            ).encode()
        )
        request = replace(
            original.request.request,
            prompt_identity=_prompt_identity(prompt),
        )
        original_workspace = Path(original.workspace.locator).resolve(strict=True)
        workspace = FilesystemComponentWorkspaceAllocator(
            original_workspace.parent
        ).allocate(original.plan)
        return replace(
            original,
            request=PreparedComponentGenerationRequest(request, prompt),
            workspace=workspace,
        )


__all__ = ["FilesystemStandardCandidateRepairAdapter"]
