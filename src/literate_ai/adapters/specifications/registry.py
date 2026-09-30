"""Shared dispatch for exact Component specification providers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from literate_ai.contracts import SpecificationSet

from .dmn import DmnError, DmnProvider
from .literate_markdown import LiterateMarkdownProvider
from .openspec import OpenSpecError, OpenSpecProvider
from .scxml import ScxmlError, ScxmlProvider


class LoadedSpecification(Protocol):
    specification_set: SpecificationSet
    contents: tuple[tuple[str, bytes], ...]
    context_document: tuple[str, bytes] | None

    def require_unchanged(self, root: Path) -> None: ...


SPECIFICATION_PROVIDER_ERRORS = (OpenSpecError, DmnError, ScxmlError)


def load_specification_provider(
    provider_kind: str,
    root: Path,
    paths: Iterable[str],
    *,
    id_prefix: str,
) -> LoadedSpecification:
    """Load one registered provider without leaking provider branches to callers."""

    if provider_kind == "literate-markdown":
        return LiterateMarkdownProvider().load(root, paths, id_prefix=id_prefix)
    providers = {
        "openspec": OpenSpecProvider,
        "dmn": DmnProvider,
        "scxml": ScxmlProvider,
    }
    try:
        provider = providers[provider_kind]()
    except KeyError as exc:
        raise LookupError(provider_kind) from exc
    return provider.load(root, paths)


__all__ = [
    "LoadedSpecification",
    "SPECIFICATION_PROVIDER_ERRORS",
    "load_specification_provider",
]
