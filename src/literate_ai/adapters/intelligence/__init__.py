"""Code-intelligence engine adapters."""

from .command import JsonCommandIntelligenceEngine
from .generated import (
    SourceIntelligenceArtifact,
    SourceIntelligenceError,
    SourceIntelligenceProvider,
    SourceIntelligenceProviderSelection,
    generated_source_tree_identity,
    select_source_intelligence_provider,
)
from .standard import DisabledGenerationIndexer, GeneratedSourceTreeResolver
from .structured import (
    DurableIntelligenceAdapter,
    IntelligenceAdapterError,
    StructuredIntelligenceEngine,
)

__all__ = [
    "DisabledGenerationIndexer",
    "DurableIntelligenceAdapter",
    "GeneratedSourceTreeResolver",
    "JsonCommandIntelligenceEngine",
    "SourceIntelligenceArtifact",
    "SourceIntelligenceError",
    "SourceIntelligenceProvider",
    "SourceIntelligenceProviderSelection",
    "IntelligenceAdapterError",
    "StructuredIntelligenceEngine",
    "generated_source_tree_identity",
    "select_source_intelligence_provider",
]
