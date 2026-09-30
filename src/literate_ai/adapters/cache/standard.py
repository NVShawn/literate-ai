"""Durable filesystem bridge for complete Standard accepted-source custody."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardAcceptedSourcePublication,
    StandardSourceCacheMembership,
    rebind_standard_source_admission_generation,
    rebind_standard_source_cache_generation,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    AcceptedSourceDerivation,
    CachedSourceFile,
    ComponentExecutionPlan,
    ContentIdentity,
    SourceDerivationCacheKey,
    StandardSourceAdmissionCacheEntry,
    StandardSourceAdmissionMembership,
)
from literate_ai.contracts.generation_cache import (
    AcceptedSourceCacheEntry,
    StandardAcceptedSourceCacheEntry,
)
from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
from literate_ai.storage import FileSystemCAS

from .filesystem import (
    FileSystemSourceCache,
    SourceCacheMaterializer,
    SourceCacheResolver,
)


class StandardSourceCachePublicationError(RuntimeError):
    """Complete accepted custody cannot be persisted without substitution."""


class StandardSourceCacheRestorationError(RuntimeError):
    """A durable hit cannot become exact Standard source custody."""


def _require_retained_cache_namespace(
    provenance, key: SourceDerivationCacheKey
) -> None:
    retained = provenance.retained_source_identity
    if retained is not None and (
        key.source_semantics_identity != retained or key.request_identity != retained
    ):
        raise StandardSourceCachePublicationError(
            "retained source cannot be published under a model-generation cache key"
        )


@dataclass(frozen=True, slots=True)
class RestoredStandardSource:
    """Verified source bytes plus untrusted prior lifecycle membership."""

    membership: StandardSourceCacheMembership | StandardSourceAdmissionMembership
    entry_identity: ContentIdentity
    source_root: Path
    current_acceptance_trusted: bool = field(default=False, init=False)


class FilesystemStandardSourceRestorer:
    """Resolve and materialize one exact durable Standard source-cache hit."""

    def __init__(
        self,
        *,
        resolver: SourceCacheResolver,
        materializer: SourceCacheMaterializer,
        cache_key: Callable[
            [PreparedComponentGenerationNode[Any, Any]], SourceDerivationCacheKey
        ]
        | None = None,
        source_trees: object | None = None,
        cache_key_recorder: Callable[[Any, SourceDerivationCacheKey], None]
        | None = None,
    ) -> None:
        if not isinstance(resolver, SourceCacheResolver):
            raise TypeError("resolver must be a SourceCacheResolver")
        if not isinstance(materializer, SourceCacheMaterializer):
            raise TypeError("materializer must be a SourceCacheMaterializer")
        self.resolver = resolver
        self.materializer = materializer
        if cache_key is not None and not callable(cache_key):
            raise TypeError("cache_key must be callable")
        if source_trees is not None and not callable(
            getattr(source_trees, "register", None)
        ):
            raise TypeError("source_trees must provide register")
        if cache_key_recorder is not None and not callable(cache_key_recorder):
            raise TypeError("cache_key_recorder must be callable")
        self.cache_key = cache_key
        self.source_trees = source_trees
        self.cache_key_recorder = cache_key_recorder

    def restore(
        self,
        key: SourceDerivationCacheKey,
        destination: Path,
        *,
        component_lock_identity: ContentIdentity,
        force_regeneration: bool = False,
    ) -> RestoredStandardSource | None:
        """Return one exact Standard hit; legacy entries never impersonate it."""

        candidates = self.resolver.resolve(
            key,
            force_regeneration=force_regeneration,
            component_lock_identity=component_lock_identity,
        )
        standard = tuple(
            item
            for item in candidates
            if type(item.entry)
            in {
                StandardAcceptedSourceCacheEntry,
                StandardSourceAdmissionCacheEntry,
            }
        )
        if not standard:
            return None
        if len(standard) != 1:
            raise StandardSourceCacheRestorationError(
                "exact Standard cache key has multiple accepted entries"
            )
        candidate = standard[0]
        membership = restore_standard_source_cache_membership(candidate.entry)
        if membership is None:  # pragma: no cover - exact type selection above
            raise StandardSourceCacheRestorationError(
                "Standard cache entry has no restorable lifecycle membership"
            )
        materialized = self.materializer.materialize(
            candidate,
            destination,
            replace_empty_destination=True,
        )
        if materialized.entry_identity != candidate.identity:
            raise StandardSourceCacheRestorationError(
                "materialized source names another cache entry"
            )
        return RestoredStandardSource(
            membership=membership,
            entry_identity=materialized.entry_identity,
            source_root=materialized.source_root,
        )

    def restore_prepared(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared: PreparedComponentGenerationNode[Any, Any],
        *,
        force_regeneration: bool = False,
    ) -> StandardSourceCacheMembership | StandardSourceAdmissionMembership | None:
        """Restore one prepared workspace and register current source custody."""

        if self.cache_key is None or self.source_trees is None:
            raise StandardSourceCacheRestorationError(
                "prepared restoration requires cache-key and source-tree adapters"
            )
        key = self.cache_key(prepared)
        if not isinstance(key, SourceDerivationCacheKey):
            raise StandardSourceCacheRestorationError(
                "prepared source-cache lookup has no exact derivation key"
            )
        restored = self.restore(
            key,
            Path(prepared.workspace.locator),
            component_lock_identity=execution_plan.component_lock_identity,
            force_regeneration=force_regeneration,
        )
        if restored is None:
            return None
        rebound = (
            rebind_standard_source_admission_generation(
                execution_plan,
                prepared,
                restored.membership,
            )
            if isinstance(restored.membership, StandardSourceAdmissionMembership)
            else rebind_standard_source_cache_generation(
                execution_plan,
                prepared,
                restored.membership,
            )
        )
        self.source_trees.register(
            rebound.output.candidate,
            restored.source_root,
            source_generation_identity=rebound.output.identity,
            recipe=prepared.recipe,
        )
        if self.cache_key_recorder is not None:
            self.cache_key_recorder(rebound.output.candidate, key)
        return restored.membership


class FilesystemStandardAcceptedSourcePublisher:
    """Build and publish one immutable Standard cache entry from typed custody."""

    def __init__(
        self,
        *,
        cache: FileSystemSourceCache | SourceCacheResolver,
        caller_cas: FileSystemCAS,
        source_root: Callable[[ContentIdentity], Path],
        source_custody: Callable[[ContentIdentity], object],
        resolved_sbom_content: Callable[[StandardAcceptedSourcePublication], bytes],
        cache_key: Callable[
            [StandardAcceptedSourcePublication], SourceDerivationCacheKey
        ],
    ) -> None:
        if not isinstance(cache, (FileSystemSourceCache, SourceCacheResolver)):
            raise TypeError("cache must be a filesystem source cache or resolver")
        if not isinstance(caller_cas, FileSystemCAS):
            raise TypeError("caller_cas must be a FileSystemCAS")
        self.cache = cache
        self.caller_cas = caller_cas
        self.source_root = source_root
        self.source_custody = source_custody
        self.resolved_sbom_content = resolved_sbom_content
        if not callable(cache_key):
            raise TypeError("cache_key must be callable")
        self.cache_key = cache_key

    def publish_accepted(
        self, publication: StandardAcceptedSourcePublication
    ) -> ContentIdentity:
        _key, entry = self._build_entry(publication)
        self.cache.publish(entry, caller_cas=self.caller_cas)
        return publication.membership.identity

    def _build_entry(
        self, publication: StandardAcceptedSourcePublication
    ) -> tuple[SourceDerivationCacheKey, StandardAcceptedSourceCacheEntry]:
        if not isinstance(publication, StandardAcceptedSourcePublication):
            raise TypeError("publication must be complete Standard accepted custody")
        key = self.cache_key(publication)
        if not isinstance(key, SourceDerivationCacheKey):
            raise StandardSourceCachePublicationError(
                "accepted Component has no exact derivation cache key"
            )
        candidate = publication.source_output.candidate
        _require_retained_cache_namespace(publication.source_output.provenance, key)
        if key.recipe_identity != candidate.recipe_identity:
            raise StandardSourceCachePublicationError(
                "derivation cache key differs from the accepted source recipe"
            )
        root = self.source_root(candidate.tree_identity).resolve(strict=True)
        source_files = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise StandardSourceCachePublicationError(
                    "accepted source cache input contains a symbolic link"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise StandardSourceCachePublicationError(
                    "accepted source cache input contains a non-regular entry"
                )
            relative = path.relative_to(root).as_posix()
            media_type = (
                "application/vnd.cyclonedx+json"
                if relative == CYCLONEDX_SOURCE_SBOM_PATH
                else (
                    "application/json"
                    if relative == GENERATED_TEST_SUITE_PATH
                    else "application/octet-stream"
                )
            )
            source_files.append(
                CachedSourceFile(
                    relative, self.caller_cas.put_file(path, media_type=media_type)
                )
            )
        source_files.sort(key=lambda item: item.path)
        custody = self.source_custody(candidate.tree_identity)
        managed_graph = getattr(custody, "managed_graph", None)
        source_bom_content = getattr(custody, "source_bom_content", None)
        test_suite_content = getattr(custody, "generated_test_suite_content", None)
        if not isinstance(source_bom_content, bytes) or not isinstance(
            test_suite_content, bytes
        ):
            raise StandardSourceCachePublicationError(
                "accepted source lacks strict SBOM or generated-test custody"
            )
        source_bom = self.caller_cas.put_bytes(
            source_bom_content, media_type="application/vnd.cyclonedx+json"
        )
        resolved_bom = self.caller_cas.put_bytes(
            self.resolved_sbom_content(publication),
            media_type="application/vnd.cyclonedx+json",
        )
        generated_tests = self.caller_cas.put_bytes(
            test_suite_content, media_type="application/json"
        )
        build_evidence = self.caller_cas.put_manifest(
            publication.build_evidence.to_dict()
        )
        test_evidence = self.caller_cas.put_manifest(
            publication.generated_test_evidence.to_dict()
        )
        acceptance_evidence = self.caller_cas.put_manifest(
            publication.acceptance_evidence.to_dict()
        )
        provenance_evidence = self.caller_cas.put_manifest(
            publication.source_output.provenance.to_dict()
        )
        derivation = AcceptedSourceDerivation(
            key,
            publication.source_output.provenance.component_lock_identity,
            candidate.tree_identity,
            publication.build_evidence.source_sbom.managed_graph_identity,
            publication.build_evidence.source_sbom.bom_identity,
            publication.build_evidence.resolved_sbom.bom_identity,
            publication.build_evidence.source_sbom,
            publication.build_evidence.resolved_sbom,
            candidate.generated_test_suite_identity,
            publication.build_evidence.identity,
            publication.generated_test_evidence.identity,
            publication.acceptance_evidence.identity,
            publication.source_output.provenance.identity,
        )
        return key, StandardAcceptedSourceCacheEntry(
            derivation=derivation,
            source_files=tuple(source_files),
            managed_sbom_graph=managed_graph,
            source_sbom=source_bom,
            resolved_sbom=resolved_bom,
            generated_test_suite=generated_tests,
            build_evidence=build_evidence,
            test_evidence=test_evidence,
            acceptance_evidence=acceptance_evidence,
            provenance_evidence=provenance_evidence,
            repository_resolutions=(),
            standard_source_membership=publication.membership.to_document(),
        )

    def retire_prior_membership(
        self, publication: StandardAcceptedSourcePublication
    ) -> None:
        """Retire stale exact-key memberships after a deliberate forced accept.

        Called only for Components the caller explicitly marked for forced
        regeneration -- an ordinary cache miss never has a prior membership at its
        exact key to retire, since a genuine ambiguity would already have raised
        ``source-cache.ambiguous`` on read. The just-published entry for this
        exact ``publication`` becomes the sole current candidate for future
        exact-key lookups. Earlier entries are never deleted -- their immutable
        manifests and CAS objects remain on disk -- but they stop being exact-key
        candidates, so neither ordinary reuse nor explicit
        ``--source-cache-entry`` selection reaches them again once retired.
        """

        key, entry = self._build_entry(publication)
        self.cache.retire_other_memberships(key, keep=entry.identity)


class FilesystemStandardSourceAdmissionPublisher:
    """Persist source-only verifier admission without fabricating build evidence."""

    def __init__(
        self,
        *,
        cache: FileSystemSourceCache | SourceCacheResolver,
        caller_cas: FileSystemCAS,
        source_root: Callable[[ContentIdentity], Path],
        source_custody: Callable[[ContentIdentity], object],
        cache_key: Callable[
            [StandardSourceAdmissionMembership], SourceDerivationCacheKey
        ],
    ) -> None:
        if not isinstance(cache, (FileSystemSourceCache, SourceCacheResolver)):
            raise TypeError("cache must be a filesystem source cache or resolver")
        if not isinstance(caller_cas, FileSystemCAS):
            raise TypeError("caller_cas must be a FileSystemCAS")
        if not callable(source_root) or not callable(source_custody):
            raise TypeError("source root and custody providers must be callable")
        if not callable(cache_key):
            raise TypeError("cache_key must be callable")
        self.cache = cache
        self.caller_cas = caller_cas
        self.source_root = source_root
        self.source_custody = source_custody
        self.cache_key = cache_key

    def publish(self, membership: StandardSourceAdmissionMembership) -> ContentIdentity:
        if not isinstance(membership, StandardSourceAdmissionMembership):
            raise TypeError("membership must be a StandardSourceAdmissionMembership")
        candidate = membership.generation.output.candidate
        key = self.cache_key(membership)
        if (
            not isinstance(key, SourceDerivationCacheKey)
            or key.recipe_identity != candidate.recipe_identity
            or key.coding_cli_tool_binding_identity
            != membership.evidence.coding_cli_tool_binding_identity
            or key.request_identity
            != membership.evidence.planned_coding_cli_request_identity
        ):
            raise StandardSourceCachePublicationError(
                "source admission has no exact derivation cache key"
            )
        _require_retained_cache_namespace(membership.generation.output.provenance, key)
        root = self.source_root(candidate.tree_identity).resolve(strict=True)
        source_files = []
        for path in root.rglob("*"):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise StandardSourceCachePublicationError(
                    "source admission contains an unsafe filesystem entry"
                )
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            media_type = (
                "application/vnd.cyclonedx+json"
                if relative == CYCLONEDX_SOURCE_SBOM_PATH
                else (
                    "application/json"
                    if relative == GENERATED_TEST_SUITE_PATH
                    else "application/octet-stream"
                )
            )
            source_files.append(
                CachedSourceFile(
                    relative,
                    self.caller_cas.put_file(path, media_type=media_type),
                )
            )
        source_files.sort(key=lambda item: item.path)
        custody = self.source_custody(candidate.tree_identity)
        source_bom_content = getattr(custody, "source_bom_content", None)
        test_suite_content = getattr(custody, "generated_test_suite_content", None)
        if not isinstance(source_bom_content, bytes) or not isinstance(
            test_suite_content, bytes
        ):
            raise StandardSourceCachePublicationError(
                "source admission lacks strict SBOM or generated-test custody"
            )
        manifest_path = (
            self.caller_cas.blob_root
            / candidate.source_manifest_identity.digest[:2]
            / candidate.source_manifest_identity.digest
        )
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise StandardSourceCachePublicationError(
                "source admission lacks its exact source manifest in CAS"
            )
        source_manifest = self.caller_cas.put_file(
            manifest_path,
            media_type="application/vnd.literate-ai.generated-source-manifest+json",
        )
        entry = StandardSourceAdmissionCacheEntry(
            cache_key=key,
            membership=membership,
            source_files=tuple(source_files),
            source_manifest=source_manifest,
            source_bom=self.caller_cas.put_bytes(
                source_bom_content,
                media_type="application/vnd.cyclonedx+json",
            ),
            generated_test_suite=self.caller_cas.put_bytes(
                test_suite_content, media_type="application/json"
            ),
            provenance_evidence=self.caller_cas.put_manifest(
                membership.generation.output.provenance.to_dict()
            ),
            admission_evidence=self.caller_cas.put_manifest(
                membership.evidence.semantic_dict()
            ),
        )
        self.cache.publish(entry, caller_cas=self.caller_cas)
        return membership.identity


def restore_standard_source_cache_membership(
    entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
) -> StandardSourceCacheMembership | StandardSourceAdmissionMembership | None:
    """Restore exact Standard evidence; legacy entries deliberately return no hit."""

    if not isinstance(
        entry, (AcceptedSourceCacheEntry, StandardSourceAdmissionCacheEntry)
    ):
        raise TypeError("source cache entry must be a typed accepted source entry")
    if isinstance(entry, StandardSourceAdmissionCacheEntry):
        return entry.membership
    if type(entry) is not StandardAcceptedSourceCacheEntry:
        return None
    return StandardSourceCacheMembership.from_dict(
        entry.standard_source_membership.to_dict()
    )


__all__ = [
    "FilesystemStandardAcceptedSourcePublisher",
    "FilesystemStandardSourceAdmissionPublisher",
    "FilesystemStandardSourceRestorer",
    "RestoredStandardSource",
    "StandardSourceCachePublicationError",
    "StandardSourceCacheRestorationError",
    "restore_standard_source_cache_membership",
]
