"""CAS-backed cache admission for verified external repository source."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.application.repository_sources import RepositoryCheckout
from literate_ai.contracts import (
    ContentIdentity,
    ProjectSourceIntelligencePolicy,
    RepositorySourceAdmission,
    SourceIntelligenceArtifact,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from literate_ai.sources import QuarantineStore, SourceCapture
from literate_ai.storage import FileSystemCAS, ReferenceIndex


class RepositorySourceCachePolicyError(RuntimeError):
    """Repository-source admission cannot honor its intelligence policy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class QuarantineRepositorySourceCache:
    """Materialize source only after the application service admits its build."""

    quarantine: QuarantineStore
    cas: FileSystemCAS
    references: ReferenceIndex | None = None
    source_intelligence_provider: object | None = None
    source_intelligence_mode: SourceIntelligenceMode = SourceIntelligenceMode.OFF
    source_intelligence_unavailable_reason: str | None = None
    source_intelligence_provider_id: str = "none"
    source_intelligence_artifact_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_intelligence_mode, SourceIntelligenceMode):
            raise TypeError("repository source-intelligence mode must be typed")
        if self.source_intelligence_mode is SourceIntelligenceMode.OFF:
            if self.source_intelligence_provider is not None:
                raise ValueError("off repository intelligence cannot select a provider")
            if self.source_intelligence_unavailable_reason is not None:
                raise ValueError(
                    "off repository intelligence has no unavailable reason"
                )
        elif self.source_intelligence_provider is None:
            if self.source_intelligence_mode is SourceIntelligenceMode.REQUIRED:
                raise RepositorySourceCachePolicyError(
                    "repository-source.source-intelligence-provider-required",
                    "repository source admission requires an explicit "
                    "intelligence provider",
                )
            if not self.source_intelligence_unavailable_reason:
                raise ValueError(
                    "preferred unavailable intelligence requires a stable reason code"
                )
        elif self.source_intelligence_unavailable_reason is not None:
            raise ValueError("an available intelligence provider cannot be unavailable")
        if self.source_intelligence_provider is not None and (
            not isinstance(self.source_intelligence_artifact_path, str)
            or not self.source_intelligence_artifact_path.strip()
        ):
            raise ValueError("repository intelligence artifact path must not be empty")

    @classmethod
    def from_project_policy(
        cls,
        quarantine: QuarantineStore,
        cas: FileSystemCAS,
        policy: ProjectSourceIntelligencePolicy,
        references: ReferenceIndex | None = None,
    ) -> QuarantineRepositorySourceCache:
        """Compose the repository cache at the project/provider adapter boundary."""

        mode = policy.mode_for(SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION)
        from literate_ai.adapters.intelligence.generated import (
            SourceIntelligenceError,
            select_source_intelligence_provider,
        )

        try:
            selection = select_source_intelligence_provider(
                policy, SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION
            )
        except SourceIntelligenceError as exc:
            raise RepositorySourceCachePolicyError(exc.code, str(exc)) from exc
        return cls(
            quarantine,
            cas,
            references,
            source_intelligence_provider=selection.provider,
            source_intelligence_mode=mode,
            source_intelligence_unavailable_reason=selection.unavailable_reason,
            source_intelligence_provider_id=selection.provider_id,
            source_intelligence_artifact_path=policy.artifact_path,
        )

    def admit(
        self,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
        admission: RepositorySourceAdmission,
    ) -> ContentIdentity:
        if admission.source_snapshot != capture.snapshot.identity:
            raise ValueError("repository cache admission targets another snapshot")
        if admission.source_tree != capture.snapshot.tree_identity:
            raise ValueError("repository cache admission targets another source tree")
        provider = self.source_intelligence_provider
        unavailable_reason = self.source_intelligence_unavailable_reason

        def source_files(tree):
            return {
                entry.path: tree.joinpath(*entry.path.split("/")).read_bytes()
                for entry in capture.snapshot.entries
                if entry.entry_type.value == "file"
            }

        def describe_materialized_source(tree) -> SourceIntelligenceArtifact:
            files = source_files(tree)
            index = getattr(provider, "index", None)
            verify = getattr(provider, "verify", None)
            if not callable(index) or not callable(verify):
                raise TypeError("repository source-intelligence provider is invalid")
            result = index(tree, files)
            if not isinstance(result, SourceIntelligenceArtifact):
                raise TypeError("repository source-intelligence evidence is invalid")
            verified = verify(tree, files, result)
            if not isinstance(verified, SourceIntelligenceArtifact):
                raise TypeError(
                    "repository intelligence verifier returned invalid evidence"
                )
            return verified

        record = self.quarantine.materialize(capture, checkout.source_root)
        source_intelligence: SourceIntelligenceArtifact | None = None
        if provider is not None:
            verify = getattr(provider, "verify", None)
            if not callable(verify):
                raise TypeError("repository source-intelligence provider is invalid")
            if record.source_intelligence is not None:
                try:
                    source_intelligence = verify(
                        record.tree_path,
                        source_files(record.tree_path),
                        record.source_intelligence,
                    )
                except Exception:
                    record = self.quarantine.detach_source_intelligence(record)
            try:
                record, source_intelligence = (
                    self.quarantine.attach_source_intelligence(
                        record, describe_materialized_source
                    )
                )
                source_intelligence = verify(
                    record.tree_path,
                    source_files(record.tree_path),
                    source_intelligence,
                )
            except Exception:
                record = self.quarantine.detach_source_intelligence(
                    record,
                    artifact_path=self.source_intelligence_artifact_path,
                )
                if self.source_intelligence_mode is SourceIntelligenceMode.REQUIRED:
                    raise
                provider = None
                source_intelligence = None
                unavailable_reason = "source-intelligence.provider-execution-failed"
        elif record.source_intelligence is not None:
            record = self.quarantine.detach_source_intelligence(record)
        record.verify()
        from literate_ai.adapters.intelligence.generated import (
            SourceIntelligenceProviderSelection,
        )

        selection = SourceIntelligenceProviderSelection(
            SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION,
            self.source_intelligence_mode,
            self.source_intelligence_provider_id,
            provider,
            unavailable_reason,
        )
        intelligence_status = selection.status(current=source_intelligence is not None)
        manifest = {
            "schema": "urn:literate-ai:schema:v2:repository-source-cache-record",
            "admission": admission.to_dict(),
            "admission_identity": admission.identity.to_dict(),
            "capture_manifest": record.manifest_ref.to_dict(),
            "source_snapshot": capture.snapshot.identity.to_dict(),
            "source_tree": capture.snapshot.tree_identity.to_dict(),
            "materialized_source_intelligence": (
                None if source_intelligence is None else source_intelligence.to_dict()
            ),
            "source_intelligence_status": intelligence_status,
        }
        reference = self.cas.put_manifest(
            manifest,
            media_type="application/vnd.literate-ai.repository-source-cache+json",
        )
        if self.references is not None:
            name = admission.cache_key.digest
            current = self.references.resolve("repository-source", name)
            if current is None or current.target != reference:
                self.references.set(
                    "repository-source",
                    name,
                    reference,
                    expected_generation=(0 if current is None else current.generation),
                )
        return ContentIdentity.parse_uri(reference.identity)


__all__ = [
    "QuarantineRepositorySourceCache",
    "RepositorySourceCachePolicyError",
]
