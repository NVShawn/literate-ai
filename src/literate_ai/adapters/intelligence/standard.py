"""Bind generated source identity without a source-graph sidecar."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from literate_ai.contracts.identity import ContentIdentity, canonical_identity


class GeneratedSourceTreeResolver(Protocol):
    def resolve(self, identity: ContentIdentity) -> Path: ...


class DisabledGenerationIndexer:
    """Return a stable identity for an exact generated tree without indexing it."""

    def __init__(
        self,
        source_trees: GeneratedSourceTreeResolver,
        *,
        artifact_root: Path | None = None,
        provider: object | None = None,
    ) -> None:
        del artifact_root, provider
        self.source_trees = source_trees
        self._evidence_recorder = None
        self._started = False

    def retain_evidence_with(self, recorder) -> None:
        """Retain the selected index policy before any source is admitted."""

        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceRecorder,
        )

        if not isinstance(recorder, QualificationEvidenceRecorder):
            raise TypeError("evidence recorder must be a QualificationEvidenceRecorder")
        if self._started:
            raise ValueError("evidence capture must precede source indexing")
        self._evidence_recorder = recorder

    def index(
        self, component_revision: ContentIdentity, source: ContentIdentity
    ) -> ContentIdentity:
        if not isinstance(component_revision, ContentIdentity) or not isinstance(
            source, ContentIdentity
        ):
            raise TypeError("GenerationIndexer identities must be typed")
        self.source_trees.resolve(source)
        self._started = True
        document = {
            "schema": "literate-ai/disabled-source-index@1",
            "component_revision": component_revision.uri,
            "source": source.uri,
        }
        if self._evidence_recorder is not None:
            return self._evidence_recorder.remember_json(document)
        return canonical_identity(document)


__all__ = [
    "DisabledGenerationIndexer",
    "GeneratedSourceTreeResolver",
]
