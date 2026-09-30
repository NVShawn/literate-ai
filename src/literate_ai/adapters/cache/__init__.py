"""Accepted generated-source cache adapters."""

from .build_result import BuildResultCacheError, CachedBuildAdapter
from .filesystem import (
    FileSystemSourceCache,
    FinalPathSourceIntelligenceVerifier,
    MaterializedCachedSource,
    SourceCacheCandidate,
    SourceCacheError,
    SourceCacheMaterializer,
    SourceCacheResolver,
    project_source_cache_protected_paths,
)
from .generated_source import CachedCodingCliSourceGenerator, GeneratedSourceCacheError
from .http_artifacts import HttpSharedArtifactCache
from .layered_artifacts import LayeredSharedArtifactCache, SharedArtifactLookup
from .project_publication import (
    RUNTIME_SOURCE_CACHE_TARGET_ID,
    FilesystemProjectSourceCachePublicationAdapter,
)
from .rebuild import (
    ProtocolDirectoryIdentity,
    RebuildSourceCacheProtocolError,
    RebuildSourceCacheSession,
    protocol_directory_identity,
    read_rebuild_source_cache_control,
    read_rebuild_source_cache_decision,
    read_rebuild_source_cache_derivation_manifest,
    read_rebuild_source_cache_lifecycle,
    read_rebuild_source_cache_publication,
    source_cache_derivation_manifest_path,
    source_cache_protocol_path_present,
    source_cache_protocol_paths,
    write_rebuild_source_cache_control,
    write_rebuild_source_cache_decision,
    write_rebuild_source_cache_derivation_manifest,
    write_rebuild_source_cache_lifecycle,
    write_rebuild_source_cache_publication,
)
from .shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
    SharedCacheArtifactManifest,
)
from .standard import (
    FilesystemStandardAcceptedSourcePublisher,
    FilesystemStandardSourceAdmissionPublisher,
    FilesystemStandardSourceRestorer,
    RestoredStandardSource,
    StandardSourceCachePublicationError,
    StandardSourceCacheRestorationError,
    restore_standard_source_cache_membership,
)
from .standard_checkpoint import (
    FilesystemStandardLifecycleCheckpointStore,
    StandardLifecycleCheckpointError,
)

__all__ = [
    "BuildResultCacheError",
    "CachedBuildAdapter",
    "CachedCodingCliSourceGenerator",
    "FileSystemSourceCache",
    "FilesystemStandardAcceptedSourcePublisher",
    "FilesystemStandardSourceAdmissionPublisher",
    "FilesystemStandardSourceRestorer",
    "FilesystemStandardLifecycleCheckpointStore",
    "FinalPathSourceIntelligenceVerifier",
    "FilesystemProjectSourceCachePublicationAdapter",
    "GeneratedSourceCacheError",
    "HttpSharedArtifactCache",
    "LayeredSharedArtifactCache",
    "SharedArtifactLookup",
    "MaterializedCachedSource",
    "LocalSharedArtifactCache",
    "ProtocolDirectoryIdentity",
    "RebuildSourceCacheProtocolError",
    "RebuildSourceCacheSession",
    "RUNTIME_SOURCE_CACHE_TARGET_ID",
    "RestoredStandardSource",
    "SourceCacheCandidate",
    "SourceCacheError",
    "SourceCacheMaterializer",
    "SourceCacheResolver",
    "StandardSourceCachePublicationError",
    "StandardSourceCacheRestorationError",
    "StandardLifecycleCheckpointError",
    "SharedArtifactCacheError",
    "SharedCacheArtifactManifest",
    "project_source_cache_protected_paths",
    "protocol_directory_identity",
    "read_rebuild_source_cache_control",
    "read_rebuild_source_cache_derivation_manifest",
    "read_rebuild_source_cache_decision",
    "read_rebuild_source_cache_lifecycle",
    "read_rebuild_source_cache_publication",
    "restore_standard_source_cache_membership",
    "source_cache_protocol_paths",
    "source_cache_derivation_manifest_path",
    "source_cache_protocol_path_present",
    "write_rebuild_source_cache_control",
    "write_rebuild_source_cache_derivation_manifest",
    "write_rebuild_source_cache_decision",
    "write_rebuild_source_cache_lifecycle",
    "write_rebuild_source_cache_publication",
]
