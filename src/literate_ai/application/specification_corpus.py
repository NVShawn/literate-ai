"""Read-only application tooling for a literate Markdown specification corpus."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from literate_ai.contracts import SpecificationSet


class LoadedSpecificationCorpus(Protocol):
    """Structural result required from a corpus provider adapter."""

    specification_set: SpecificationSet
    contents: tuple[tuple[str, bytes], ...]
    context_document: tuple[str, bytes] | None


class SpecificationCorpusProvider(Protocol):
    """Application port for one strict specification provider."""

    def load(
        self, root: Path, paths: Iterable[str], *, id_prefix: str
    ) -> LoadedSpecificationCorpus: ...


class SpecificationDocumentFormatter(Protocol):
    """Application port for canonical in-memory document formatting."""

    def __call__(self, path: str, content: bytes) -> bytes: ...


class SpecificationCorpusError(ValueError):
    """A corpus could not be inspected or canonically formatted."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SpecificationCorpusNode:
    """One derived node plus the exact document context it imports."""

    node_id: str
    name: str
    summary: str
    kind: str
    path: str
    parent: str | None
    references: tuple[str, ...]
    status: str | None
    content_identity: str
    effective_documents: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "name": self.name,
            "summary": self.summary,
            "kind": self.kind,
            "path": self.path,
            "parent": self.parent,
            "references": list(self.references),
            "status": self.status,
            "content_identity": self.content_identity,
            "effective_documents": list(self.effective_documents),
        }


@dataclass(frozen=True, slots=True)
class SpecificationCorpusReport:
    """Deterministic validation and explanation of one complete corpus."""

    id_prefix: str
    root_id: str
    specification_set_identity: str
    nodes: tuple[SpecificationCorpusNode, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/specification-corpus-report@1",
            "provider": "literate-markdown@1",
            "id_prefix": self.id_prefix,
            "root_id": self.root_id,
            "specification_set_identity": self.specification_set_identity,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def node(self, node_id: str) -> SpecificationCorpusNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise SpecificationCorpusError(
            "specification_corpus.node_missing",
            f"The corpus has no specification node {node_id!r}",
        )


@dataclass(frozen=True, slots=True)
class FormattedSpecificationArtifact:
    """An in-memory canonical artifact; callers decide whether to write it."""

    path: str
    content: bytes
    changed: bool


@dataclass(frozen=True, slots=True)
class FormattedSpecificationCorpus:
    """Canonical corpus preview with no filesystem mutation capability."""

    report: SpecificationCorpusReport
    artifacts: tuple[FormattedSpecificationArtifact, ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.artifacts if item.changed)


class SpecificationCorpusService:
    """Validate, explain, and preview-format ``literate-markdown@1`` corpora.

    ``paths`` uses the provider contract: the root ``spec.md`` is first.  Remaining
    paths are canonicalized lexically, making reports independent of manifest order
    without guessing which nested ``spec.md`` is the corpus root.
    """

    def __init__(
        self,
        provider: SpecificationCorpusProvider,
        formatter: SpecificationDocumentFormatter,
    ) -> None:
        self._provider = provider
        self._formatter = formatter

    def validate(
        self, root: Path, paths: Iterable[str], *, id_prefix: str
    ) -> SpecificationCorpusReport:
        """Validate and describe a corpus without changing any artifact."""

        loaded = self._load(root, paths, id_prefix=id_prefix)
        return _report(loaded, id_prefix=id_prefix)

    def explain(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        id_prefix: str,
        node_id: str | None = None,
    ) -> SpecificationCorpusReport | SpecificationCorpusNode:
        """Explain the full graph, or one node and its effective context."""

        report = self.validate(root, paths, id_prefix=id_prefix)
        return report if node_id is None else report.node(node_id)

    def format(
        self, root: Path, paths: Iterable[str], *, id_prefix: str
    ) -> FormattedSpecificationCorpus:
        """Return a canonical in-memory preview; never write source artifacts."""

        loaded = self._load(root, paths, id_prefix=id_prefix)
        report = _report(loaded, id_prefix=id_prefix)
        artifacts: list[FormattedSpecificationArtifact] = []
        for path, content in loaded.contents:
            canonical = (
                self._formatter(path, content) if path.endswith(".md") else content
            )
            artifacts.append(
                FormattedSpecificationArtifact(path, canonical, canonical != content)
            )
        return FormattedSpecificationCorpus(report, tuple(artifacts))

    def _load(
        self, root: Path, paths: Iterable[str], *, id_prefix: str
    ) -> LoadedSpecificationCorpus:
        declared = tuple(paths)
        if declared:
            declared = (declared[0], *sorted(declared[1:], key=_corpus_path_key))
        try:
            return self._provider.load(root, declared, id_prefix=id_prefix)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                raise
            raise SpecificationCorpusError(code, str(exc)) from exc


def _corpus_path_key(path: str) -> tuple[str, ...]:
    parts = PurePosixPath(path).parts
    if parts and parts[-1] == "spec.md":
        return (*parts[:-1], "")
    return parts


def _report(
    loaded: LoadedSpecificationCorpus, *, id_prefix: str
) -> SpecificationCorpusReport:
    if loaded.context_document is None:  # pragma: no cover - provider invariant
        raise SpecificationCorpusError(
            "specification_corpus.context_missing",
            "The literate Markdown provider returned no context document",
        )
    try:
        raw = json.loads(loaded.context_document[1])
        effective = {
            str(item["node_id"]): tuple(str(value) for value in item["document_ids"])
            for item in raw["effective_documents"]
        }
        nodes = tuple(
            SpecificationCorpusNode(
                node_id=str(item["id"]),
                name=str(item["name"]),
                summary=str(item["summary"]),
                kind=str(item["kind"]),
                path=str(item["path"]),
                parent=str(item["parent"]) if item["parent"] is not None else None,
                references=tuple(str(value) for value in item["references"]),
                status=str(item["status"]) if item["status"] is not None else None,
                content_identity=str(item["content_identity"]),
                effective_documents=effective[str(item["id"])],
            )
            for item in raw["nodes"]
        )
        return SpecificationCorpusReport(
            id_prefix=id_prefix,
            root_id=str(raw["root_id"]),
            specification_set_identity=loaded.specification_set.identity.uri,
            nodes=nodes,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SpecificationCorpusError(
            "specification_corpus.context_invalid",
            "The literate Markdown provider returned invalid context",
        ) from exc


__all__ = [
    "FormattedSpecificationArtifact",
    "FormattedSpecificationCorpus",
    "LoadedSpecificationCorpus",
    "SpecificationCorpusError",
    "SpecificationCorpusNode",
    "SpecificationCorpusReport",
    "SpecificationCorpusService",
    "SpecificationCorpusProvider",
    "SpecificationDocumentFormatter",
]
