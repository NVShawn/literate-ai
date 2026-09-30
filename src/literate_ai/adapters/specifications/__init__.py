"""Specification provider adapters."""

from .dmn import DmnError, DmnProvider, LoadedDmn
from .literate_markdown import (
    CONTEXT_PATH,
    LiterateMarkdownProvider,
    LiterateSpecificationContext,
    LiterateSpecificationNode,
    format_literate_markdown_document,
)
from .openspec import (
    LoadedOpenSpec,
    LoadedSpecificationArtifacts,
    OpenSpecError,
    OpenSpecProvider,
)
from .registry import (
    SPECIFICATION_PROVIDER_ERRORS,
    LoadedSpecification,
    load_specification_provider,
)
from .scxml import LoadedScxml, ScxmlError, SCXMLProvider, ScxmlProvider

__all__ = [
    "CONTEXT_PATH",
    "DmnError",
    "DmnProvider",
    "LiterateMarkdownProvider",
    "LiterateSpecificationContext",
    "LiterateSpecificationNode",
    "format_literate_markdown_document",
    "LoadedOpenSpec",
    "LoadedDmn",
    "LoadedScxml",
    "LoadedSpecification",
    "LoadedSpecificationArtifacts",
    "OpenSpecError",
    "OpenSpecProvider",
    "SPECIFICATION_PROVIDER_ERRORS",
    "SCXMLProvider",
    "ScxmlError",
    "ScxmlProvider",
    "load_specification_provider",
]
