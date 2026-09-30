"""Filesystem planning adapter for readable Component authoring and exact locks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.flavor_markdown import (
    FlavorMarkdownError,
    parse_flavor_markdown,
)
from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.adapters.specifications import (
    SPECIFICATION_PROVIDER_ERRORS,
    OpenSpecError,
    OpenSpecProvider,
    load_specification_provider,
)
from literate_ai.application.component_lock_resolution import (
    CatalogAttribute,
    ComponentLockResolutionPlan,
    FlavorCandidateInput,
    RequirementProviderInput,
    ResolvedComponentNodeInput,
)
from literate_ai.application.flavor_selection import (
    FlavorSelectionError,
    apply_flavor_selectors,
    flavor_candidate_aliases,
    parse_flavor_selector_body,
    parse_scoped_flavor_selector,
)
from literate_ai.application.repository_sources import (
    RepositorySourceLockResolver,
    RepositorySourceResolutionError,
)
from literate_ai.composition import version_satisfies
from literate_ai.contracts import RepositorySourceDependency, RepositorySourceLock
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.component_locking import (
    ComponentAuthoring,
    ComponentContentSelector,
    ComponentLock,
    LockedFlavorRequirement,
    ResolvedComponentAsset,
)
from literate_ai.contracts.flavors import (
    CandidateStatus,
    FlavorCardinality,
    FlavorDefinition,
    FlavorRevision,
    FlavorSelectionCandidate,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    ContentReference,
    HashAlgorithm,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.provider_resolution import (
    ProviderResolution,
    ProviderResolutionError,
    resolve_provider_declaration,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    PinnedInputClosure,
    PinnedInputClosureError,
    ProjectConfigurationStore,
    ProjectError,
    discover_project,
    project_boundary,
)
from literate_ai.storage import FileSystemCAS

_MAXIMUM_FILE_BYTES = 2 * 1024 * 1024
_MAXIMUM_CATALOG_ENTRIES = 16_384
_MAXIMUM_CATALOG_FILES = 4_096
_MAXIMUM_CATALOG_TOTAL_BYTES = 64 * 1024 * 1024
# Convert lift-shift retained source lives beside component.md; it is not catalog.
_RETAINED_IMPLEMENTATION_DIRECTORY = "implementation"
_MAXIMUM_FLAVOR_SELECTORS = 64
_MAXIMUM_ASSET_BYTES = 4 * 1024 * 1024 * 1024
_ASSET_READ_CHUNK = 1024 * 1024
_RESOLVER_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/component-lock-resolver-implementation@1",
        "implementation": "literate-ai.application.component_lock_resolution",
        "contract": "urn:literate-ai:schema:v2:component-lock",
    }
)


class ComponentLockPlanningError(RuntimeError):
    """Filesystem authoring/catalog inputs are unsafe, incomplete, or ambiguous."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _LoadedFlavor:
    path: Path
    definition: FlavorDefinition
    revision: FlavorRevision
    selection: FlavorSelectionCandidate
    source_identity: ContentIdentity


@dataclass(frozen=True, slots=True)
class _LoadedAuthoring:
    root: Path
    authoring: ComponentAuthoring
    source_identity: ContentIdentity


@dataclass(frozen=True, slots=True)
class _ComponentFlavorSelection:
    authoring_identity: ContentIdentity
    selected: tuple[FlavorSelectionCandidate, ...]
    requirements: tuple[LockedFlavorRequirement, ...]


@dataclass(frozen=True, slots=True)
class _ComponentClosure:
    authorings: tuple[ComponentAuthoring, ...]
    providers: tuple[RequirementProviderInput, ...]
    flavor_selections: tuple[_ComponentFlavorSelection, ...]
    consumed_selectors: frozenset[int]


@dataclass(frozen=True, slots=True)
class ComponentCatalogSnapshot:
    """One bounded, safely traversed Component and Flavor catalog observation."""

    root: Path
    boundary: Path
    project: Any
    authoring_roots: tuple[Path, ...]
    authoring_paths: tuple[Path, ...]
    flavor_roots: tuple[Path, ...]
    _authorings: tuple[_LoadedAuthoring, ...]
    _flavors: tuple[_LoadedFlavor, ...]

    @property
    def authorings(self) -> tuple[ComponentAuthoring, ...]:
        return tuple(item.authoring for item in self._authorings)

    @property
    def flavor_revisions(self) -> tuple[FlavorRevision, ...]:
        return tuple(item.revision for item in self._flavors)

    @property
    def root_authoring(self) -> ComponentAuthoring:
        selected = next(
            (item.authoring for item in self._authorings if item.root == self.root),
            None,
        )
        if selected is None:
            raise ComponentLockPlanningError(
                "component_lock.component_missing",
                "Component directory must contain a matching component.md",
            )
        return selected

    @property
    def catalog_audit_identity(self) -> ContentIdentity:
        """Identify the full safe observation without making it derivation input."""

        return canonical_identity(
            {
                "schema": "literate-ai/component-lock-catalog@1",
                "authorings": [
                    item.authoring.identity.uri
                    for item in sorted(
                        self._authorings,
                        key=lambda value: value.authoring.identity.uri,
                    )
                ],
                "flavors": [
                    {
                        "revision": item.revision.identity.uri,
                        "source": item.source_identity.uri,
                    }
                    for item in sorted(
                        self._flavors, key=lambda value: value.revision.identity.uri
                    )
                ],
            }
        )

    def component_authoring_content(self, authoring_identity: ContentIdentity) -> bytes:
        entry = self._component_entry(authoring_identity)
        content = _bounded_bytes(
            entry.root / "component.md", code="component_lock.stale"
        )
        if _bytes_identity(content) != entry.source_identity:
            raise ComponentLockPlanningError(
                "component_lock.stale", "selected Component authoring changed"
            )
        return content

    def component_content(
        self, authoring_identity: ContentIdentity, reference: ContentReference
    ) -> bytes:
        entry = self._component_entry(authoring_identity)
        content = _bounded_bytes(
            _component_reference_path(entry.root, reference, boundary=self.boundary),
            code="component_lock.stale",
        )
        if _bytes_identity(content) != reference.identity:
            raise ComponentLockPlanningError(
                "component_lock.stale", "selected Component content changed"
            )
        return content

    def admit_component_asset(
        self,
        authoring_identity: ContentIdentity,
        asset: ResolvedComponentAsset,
        cas: FileSystemCAS,
    ) -> BlobRef:
        """Stream one locked asset into CAS without making bytes model context."""

        entry = self._component_entry(authoring_identity)
        source = asset.selector.source
        if source.startswith(("http://", "https://")):
            admitted = _remote_asset_to_cas(
                source, media_type=asset.selector.media_type, cas=cas
            )
        else:
            path = _asset_local_path(entry.root, source, boundary=self.boundary)
            assert path is not None
            admitted = cas.put_file(path, media_type=asset.selector.media_type)
        if admitted != asset.blob:
            raise ComponentLockPlanningError(
                "component_lock.stale",
                f"locked asset {asset.selector.asset_id!r} changed or disappeared",
            )
        return admitted

    def flavor_content(
        self, flavor_revision: ContentIdentity, reference: ContentReference
    ) -> bytes:
        entry = next(
            (
                item
                for item in self._flavors
                if item.revision.identity == flavor_revision
            ),
            None,
        )
        if entry is None:
            raise ComponentLockPlanningError(
                "component_lock.stale",
                "selected Flavor revision is absent from the current catalog",
            )
        content = _bounded_bytes(
            _resolve_path(entry.path.parent, reference.uri, boundary=self.boundary),
            code="component_lock.stale",
        )
        if _bytes_identity(content) != reference.identity:
            raise ComponentLockPlanningError(
                "component_lock.stale", "selected Flavor content changed"
            )
        return content

    def _component_entry(self, authoring_identity: ContentIdentity) -> _LoadedAuthoring:
        entry = next(
            (
                item
                for item in self._authorings
                if item.authoring.identity == authoring_identity
            ),
            None,
        )
        if entry is None:
            raise ComponentLockPlanningError(
                "component_lock.stale",
                "selected Component authoring is absent from the current catalog",
            )
        return entry

    def require_unchanged(
        self, *, nodes: tuple[ResolvedComponentNodeInput, ...] = ()
    ) -> None:
        """Recheck exact catalog bytes, membership, and optional selected content."""

        _require_inputs_unchanged(
            root=self.root,
            boundary=self.boundary,
            project=self.project,
            authoring_roots=self.authoring_roots,
            authoring_paths=self.authoring_paths,
            flavor_roots=self.flavor_roots,
            authorings=self._authorings,
            flavors=self._flavors,
            nodes=nodes,
        )

    def locked_input_closure(
        self,
        lock: ComponentLock,
        *,
        lock_path: Path | None = None,
        lock_boundary: Path | None = None,
    ) -> PinnedInputClosure:
        """Capture every current byte selected by one already-validated lock."""

        authoring_by_identity = {
            item.authoring.identity.uri: item for item in self._authorings
        }
        flavor_by_identity = {
            item.revision.identity.uri: item for item in self._flavors
        }
        closure = PinnedInputClosure()
        try:
            closure.pin(
                lock_path or self.root / "component.lock.json",
                boundary=lock_boundary or self.boundary,
                label="component-lock",
                expected_content=canonical_json_bytes(lock.to_dict()) + b"\n",
            )
            for node_index, node in enumerate(lock.nodes):
                entry = authoring_by_identity.get(node.revision.authoring_identity.uri)
                if entry is None:
                    raise ComponentLockPlanningError(
                        "component_lock.stale",
                        "Component lock selects authoring absent from the catalog",
                    )
                closure.pin(
                    entry.root / "component.md",
                    boundary=self.boundary,
                    label=f"locked-authoring:{node_index}",
                    expected_identity=entry.source_identity,
                )
                references = (
                    *node.revision.specifications,
                    *node.revision.authoring_inputs,
                    node.revision.workflow_definition,
                    node.revision.routing_policy,
                    *node.revision.acceptance_contracts,
                    *node.revision.public_interfaces,
                    *(
                        item.dependency.integration_contract
                        for item in node.revision.repository_sources
                        if item.dependency.integration_contract is not None
                    ),
                )
                for reference_index, reference in enumerate(references):
                    closure.pin(
                        _component_reference_path(
                            entry.root, reference, boundary=self.boundary
                        ),
                        boundary=self.boundary,
                        label=f"locked-component-content:{node_index}:{reference_index}",
                        expected_identity=reference.identity,
                    )
                for asset_index, asset in enumerate(node.revision.assets):
                    current = _resolved_asset(
                        entry.root, asset.selector, boundary=self.boundary
                    )
                    if current.blob != asset.blob:
                        raise ComponentLockPlanningError(
                            "component_lock.stale",
                            f"locked asset {asset.selector.asset_id!r} changed",
                        )
                    local_path = _asset_local_path(
                        entry.root, asset.selector.source, boundary=self.boundary
                    )
                    if local_path is not None:
                        closure.pin(
                            local_path,
                            boundary=self.boundary,
                            label=f"locked-component-asset:{node_index}:{asset_index}",
                            expected_identity=ContentIdentity.parse_uri(
                                asset.blob.identity
                            ),
                        )
            selected_flavors = {
                identity.uri
                for node in lock.nodes
                for identity in node.revision.selected_flavor_revisions
            }
            for flavor_index, identity in enumerate(sorted(selected_flavors)):
                entry = flavor_by_identity.get(identity)
                if entry is None:
                    raise ComponentLockPlanningError(
                        "component_lock.stale",
                        "Component lock selects a Flavor absent from the catalog",
                    )
                closure.pin(
                    entry.path,
                    boundary=self.boundary,
                    label=f"locked-flavor:{flavor_index}",
                    expected_identity=entry.source_identity,
                )
                references = (
                    *entry.definition.specification_fragments,
                    *entry.definition.authoring_inputs,
                    *(item.content for item in entry.definition.contributions),
                )
                for reference_index, reference in enumerate(references):
                    closure.pin(
                        _resolve_path(
                            entry.path.parent,
                            reference.uri,
                            boundary=self.boundary,
                        ),
                        boundary=self.boundary,
                        label=f"locked-flavor-content:{flavor_index}:{reference_index}",
                        expected_identity=reference.identity,
                    )
            closure.require_unchanged()
        except PinnedInputClosureError as exc:
            raise ComponentLockPlanningError(
                "component_lock.stale",
                "Component lock exact input closure is stale: " + exc.message,
            ) from exc
        return closure


@dataclass(slots=True)
class _CatalogBudget:
    entries: int = 0
    files: int = 0
    total_bytes: int = 0

    def admit_entry(self) -> None:
        self.entries += 1
        if self.entries > _MAXIMUM_CATALOG_ENTRIES:
            raise ComponentLockPlanningError(
                "component_lock.catalog_limit",
                "Component or Flavor catalog contains too many filesystem entries",
            )

    def admit_authority_file(self, metadata: os.stat_result) -> None:
        self.files += 1
        self.total_bytes += metadata.st_size
        if metadata.st_size > _MAXIMUM_FILE_BYTES:
            raise ComponentLockPlanningError(
                "component_lock.catalog_limit",
                "Component or Flavor catalog authority file exceeds the byte limit",
            )
        if self.files > _MAXIMUM_CATALOG_FILES:
            raise ComponentLockPlanningError(
                "component_lock.catalog_limit",
                "Component or Flavor catalog contains too many authority files",
            )
        if self.total_bytes > _MAXIMUM_CATALOG_TOTAL_BYTES:
            raise ComponentLockPlanningError(
                "component_lock.catalog_limit",
                "Component or Flavor catalog exceeds the total byte limit",
            )


def _effective_authority_graph_identity(project) -> str | None:
    """Bind project-mode planning without creating an adapter import cycle.

    Bind only repository lineage (issue #36): the full project entity graph churns
    on every unrelated sibling Component/Flavor/skill edit, which invalidated every
    other Component's lock in the same project regardless of relevance. Lineage is
    the one piece of graph-wide state a lock must still fail closed on; everything
    else this lock cares about is already carried by its own authorings/flavors.
    """

    if project is None:
        return None
    from literate_ai.project_authority_graph import project_lineage_authority_identity

    return project_lineage_authority_identity(project.root)


class FilesystemComponentLockPlanner:
    """Build exact pure-resolver inputs from one project-local Component graph."""

    def __init__(
        self, *, repository_source_resolver: RepositorySourceLockResolver | None = None
    ) -> None:
        self.repository_source_resolver = (
            repository_source_resolver
            or RepositorySourceLockResolver(
                acquirer=GitRepositorySourceAcquirer(),
                capturer=GitRepositorySourceCapturer(),
            )
        )

    def plan(
        self,
        component_root: Path,
        *,
        target_name: str,
        flavor_selectors: tuple[str, ...] = (),
        flavor_roots: tuple[Path, ...] = (),
    ) -> ComponentLockResolutionPlan:
        plan, _snapshot = self.plan_with_snapshot(
            component_root,
            target_name=target_name,
            flavor_selectors=flavor_selectors,
            flavor_roots=flavor_roots,
        )
        return plan

    def plan_with_snapshot(
        self,
        component_root: Path,
        *,
        target_name: str,
        flavor_selectors: tuple[str, ...] = (),
        flavor_roots: tuple[Path, ...] = (),
    ) -> tuple[ComponentLockResolutionPlan, ComponentCatalogSnapshot]:
        """Plan from one catalog observation and return that exact observation."""

        snapshot = self.snapshot(component_root, flavor_roots=flavor_roots)
        plan = self.plan_snapshot(
            snapshot,
            target_name=target_name,
            flavor_selectors=flavor_selectors,
        )
        return plan, snapshot

    def plan_snapshot(
        self,
        snapshot: ComponentCatalogSnapshot,
        *,
        target_name: str,
        flavor_selectors: tuple[str, ...] = (),
        locked_repository_sources: tuple[RepositorySourceLock, ...] | None = None,
    ) -> ComponentLockResolutionPlan:
        """Reproduce captured authority, optionally replaying its exact source locks.

        Replay never fetches missing dependencies and grants no source admission.
        Ordinary locking omits the replay inputs and resolves repositories normally.
        """

        if not isinstance(snapshot, ComponentCatalogSnapshot):
            raise TypeError("Component lock planning requires a catalog snapshot")
        project = snapshot.project
        boundary = snapshot.boundary
        authorings = snapshot.authorings
        loaded_authorings = snapshot._authorings
        root_authoring = snapshot.root_authoring
        flavors = snapshot._flavors
        _require_known_selectors(
            flavors,
            flavor_selectors,
            coordinates=frozenset(item.coordinate.uri for item in authorings),
        )
        defaults = (
            ()
            if project is None
            else tuple(project.definition.default_flavor_selectors)
        )
        closure = _component_closure(
            root_authoring,
            authorings,
            flavors,
            defaults=defaults,
            selectors=flavor_selectors,
        )
        selected_authorings = closure.authorings
        roots_by_authoring = {
            item.authoring.identity.uri: item.root for item in loaded_authorings
        }

        _require_known_selectors(
            flavors,
            flavor_selectors,
            coordinates=frozenset(item.coordinate.uri for item in selected_authorings),
        )
        selection_by_authoring = {
            item.authoring_identity.uri: item for item in closure.flavor_selections
        }
        provider_resolutions = _resolved_provider_resolutions(closure, flavors=flavors)
        resolved_nodes: list[ResolvedComponentNodeInput] = []
        source_locks: dict[ContentIdentity, RepositorySourceLock] = {}
        if locked_repository_sources is not None:
            if not isinstance(locked_repository_sources, tuple):
                raise TypeError("replayed repository source locks must be a tuple")
            for source in locked_repository_sources:
                if not isinstance(source, RepositorySourceLock):
                    raise TypeError("replayed source must be a RepositorySourceLock")
                previous = source_locks.setdefault(source.dependency.identity, source)
                if previous != source:
                    raise ComponentLockPlanningError(
                        "component_lock.repository_source_conflict",
                        "replayed repository dependency has conflicting exact locks",
                    )
        for authoring in selected_authorings:
            flavor_selection = selection_by_authoring[authoring.identity.uri]
            component_directory = roots_by_authoring[authoring.identity.uri]
            resolved_nodes.append(
                _resolved_node(
                    authoring,
                    component_directory,
                    boundary=boundary,
                    flavors=flavors,
                    selected=flavor_selection.selected,
                    flavor_requirements=flavor_selection.requirements,
                    repository_source_resolver=self.repository_source_resolver,
                    source_locks=source_locks,
                    resolve_sources=locked_repository_sources is None,
                )
            )
        _require_selectors_consumed(flavor_selectors, set(closure.consumed_selectors))

        selected_node_flavors = tuple(
            {
                "authoring": node.authoring.identity.uri,
                "slot": candidate.slot_id,
                "flavor": candidate.flavor_revision.uri,
            }
            for node, candidate in sorted(
                (
                    (node, candidate)
                    for node in resolved_nodes
                    for candidate in node.flavor_candidates
                    if candidate.status is CandidateStatus.SELECTED
                ),
                key=lambda item: (
                    item[0].authoring.identity.uri,
                    item[1].slot_id,
                    item[1].flavor_revision.uri,
                ),
            )
        )
        target_profile_identity = canonical_identity(
            {
                "schema": "literate-ai/component-lock-target@1",
                "target_name": target_name,
                "selected_node_flavors": selected_node_flavors,
            }
        )
        selection_policy_identity = canonical_identity(
            {
                "schema": "literate-ai/component-lock-selection-policy@1",
                "selected_node_flavors": selected_node_flavors,
            }
        )
        catalog_identity = canonical_identity(
            {
                "schema": "literate-ai/component-lock-catalog@2",
                "effective_authority_graph": _effective_authority_graph_identity(
                    project
                ),
                "authorings": [
                    item.identity.uri
                    for item in sorted(authorings, key=lambda value: value.identity.uri)
                ],
                "flavors": [
                    {
                        "revision": item.revision.identity.uri,
                        "source": item.source_identity.uri,
                    }
                    for item in sorted(
                        flavors, key=lambda value: value.revision.identity.uri
                    )
                ],
            }
        )
        plan = ComponentLockResolutionPlan(
            target_name=target_name,
            target_profile_identity=target_profile_identity,
            selection_policy_identity=selection_policy_identity,
            resolver_identity=_RESOLVER_IDENTITY,
            catalog_identity=catalog_identity,
            root_authoring_identity=root_authoring.identity,
            nodes=tuple(resolved_nodes),
            requirement_providers=closure.providers,
            provider_resolutions=provider_resolutions,
        )
        snapshot.require_unchanged(nodes=tuple(resolved_nodes))
        return plan

    def snapshot(
        self,
        component_root: Path,
        *,
        flavor_roots: tuple[Path, ...] = (),
    ) -> ComponentCatalogSnapshot:
        """Load and immediately revalidate one safe reusable catalog snapshot."""

        configured_component_root = Path(os.path.abspath(component_root))
        root = _direct_directory(component_root, code="component_lock.component_unsafe")
        project = discover_project(root)
        boundary = project_boundary(root, legacy=root.parent)
        _require_lexical_directory_safe(
            configured_component_root,
            boundary=boundary,
            code="component_lock.component_unsafe",
        )
        authoring_roots = _component_authoring_roots(root, project)
        authoring_paths = _component_markdown_paths(authoring_roots, boundary=boundary)
        loaded_authorings = tuple(
            _load_authoring_entry(path, project_root=boundary)
            for path in authoring_paths
        )
        authorings = tuple(item.authoring for item in loaded_authorings)
        coordinates = tuple((item.coordinate.uri, item.version) for item in authorings)
        if len(set(coordinates)) != len(authorings):
            raise ComponentLockPlanningError(
                "component_lock.component_duplicate",
                "Component catalog repeats one authored coordinate and version",
            )
        if not any(item.root == root for item in loaded_authorings):
            raise ComponentLockPlanningError(
                "component_lock.component_missing",
                "Component directory must contain a matching component.md",
            )
        configured_flavor_roots = flavor_roots
        if not configured_flavor_roots and project is not None:
            configured_flavor_roots = project.roots("flavor")
        snapshot = ComponentCatalogSnapshot(
            root,
            boundary,
            project,
            authoring_roots,
            authoring_paths,
            configured_flavor_roots,
            loaded_authorings,
            _load_flavors(configured_flavor_roots, boundary=boundary),
        )
        snapshot.require_unchanged()
        return snapshot


def _component_authoring_roots(root: Path, project: Any) -> tuple[Path, ...]:
    if project is None:
        return (root,)
    roots = tuple(project.roots("component"))
    return roots if roots else (root,)


def _component_markdown_paths(
    roots: tuple[Path, ...], *, boundary: Path
) -> tuple[Path, ...]:
    budget = _CatalogBudget()
    paths: set[Path] = set()
    for configured_root in roots:
        _require_lexical_directory_safe(
            configured_root,
            boundary=boundary,
            code="component_lock.component_root_unsafe",
        )
        root = _direct_directory(
            configured_root, code="component_lock.component_root_unsafe"
        )
        _require_within_boundary(
            root, boundary=boundary, code="component_lock.component_root_unsafe"
        )
        pending = [root]
        while pending:
            directory = pending.pop()
            children = _catalog_entries(
                directory,
                budget=budget,
                code="component_lock.component_unsafe",
            )
            for candidate, metadata in children:
                if stat.S_ISDIR(metadata.st_mode):
                    if _is_retained_implementation_directory(
                        candidate, siblings=children
                    ):
                        continue
                    pending.append(candidate)
                    continue
                if candidate.name == "component.md":
                    budget.admit_authority_file(metadata)
                    paths.add(candidate)
    return tuple(sorted(paths))


def _load_authoring_entry(path: Path, *, project_root: Path) -> _LoadedAuthoring:
    content = _bounded_bytes(path, code="component_lock.authoring_unavailable")
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ComponentLockPlanningError(
            "component_lock.authoring_invalid", "component.md must be UTF-8"
        ) from exc
    try:
        authoring = parse_component_markdown(path, text, project_root=project_root)
    except (TypeError, ValueError) as exc:
        raise ComponentLockPlanningError(
            "component_lock.authoring_invalid", "component.md is invalid"
        ) from exc
    return _LoadedAuthoring(path.parent, authoring, _bytes_identity(content))


def _component_closure(
    root: ComponentAuthoring,
    catalog: tuple[ComponentAuthoring, ...],
    flavors: tuple[_LoadedFlavor, ...],
    *,
    defaults: tuple[str, ...],
    selectors: tuple[str, ...],
) -> _ComponentClosure:
    selected: dict[str, ComponentAuthoring] = {root.identity.uri: root}
    providers: list[RequirementProviderInput] = []
    selections: list[_ComponentFlavorSelection] = []
    consumed_selectors: set[int] = set()
    flavors_by_revision = {item.revision.identity.uri: item for item in flavors}
    pending = [root]
    while pending:
        consumer = pending.pop()
        selected_flavors, consumed = _selected_flavors(
            consumer,
            flavors,
            defaults=defaults,
            selectors=selectors,
        )
        consumed_selectors.update(consumed)
        flavor_requirements = tuple(
            LockedFlavorRequirement(flavor.revision, requirement)
            for selected_flavor in selected_flavors
            for flavor in (flavors_by_revision[selected_flavor.candidate_identity],)
            for requirement in flavor.definition.requires
        )
        effective_requirements = (
            *consumer.requires,
            *(item.requirement for item in flavor_requirements),
        )
        requirement_ids = tuple(item.requirement_id for item in effective_requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ComponentLockPlanningError(
                "component_lock.requirement_duplicate",
                "Component and selected Flavor requirement IDs must be unique for "
                f"{consumer.coordinate.uri}",
            )
        selections.append(
            _ComponentFlavorSelection(
                consumer.identity,
                selected_flavors,
                tuple(
                    sorted(
                        flavor_requirements,
                        key=lambda item: item.requirement.requirement_id,
                    )
                ),
            )
        )
        for requirement in effective_requirements:
            candidates = tuple(
                item for item in catalog if _provider_satisfies(item, requirement)
            )
            if not candidates and requirement.optional:
                continue
            if len(candidates) != 1:
                reason = "unresolved" if not candidates else "ambiguous"
                raise ComponentLockPlanningError(
                    f"component_lock.requirement_{reason}",
                    f"requirement {requirement.requirement_id!r} does not select one "
                    "provider",
                )
            provider = candidates[0]
            providers.append(
                RequirementProviderInput(
                    consumer.identity,
                    requirement.requirement_id,
                    provider.identity,
                )
            )
            if provider.identity.uri not in selected:
                selected[provider.identity.uri] = provider
                pending.append(provider)
    return _ComponentClosure(
        authorings=tuple(sorted(selected.values(), key=lambda item: item.identity.uri)),
        providers=tuple(
            sorted(
                providers,
                key=lambda item: (
                    item.consumer_authoring_identity.uri,
                    item.requirement_id,
                ),
            )
        ),
        flavor_selections=tuple(
            sorted(selections, key=lambda item: item.authoring_identity.uri)
        ),
        consumed_selectors=frozenset(consumed_selectors),
    )


def _resolved_provider_resolutions(
    closure: _ComponentClosure,
    *,
    flavors: tuple[_LoadedFlavor, ...],
) -> tuple[ProviderResolution, ...]:
    authorings = {item.identity.uri: item for item in closure.authorings}
    flavors_by_revision = {item.revision.identity.uri: item for item in flavors}
    declarations = tuple(
        declaration
        for authoring in closure.authorings
        for declaration in authoring.provider_resolutions
    )
    declaration_ids = tuple(item.resolution_id for item in declarations)
    if len(set(declaration_ids)) != len(declaration_ids):
        raise ComponentLockPlanningError(
            "provider_resolution.declaration_duplicate",
            "provider resolution IDs must be unique across the Component closure",
        )
    known_ids = set(declaration_ids)
    selected_overrides = tuple(
        {
            (override.identity.uri, flavor.revision.identity.uri): (
                override,
                flavor.revision.identity,
            )
            for selection in closure.flavor_selections
            for selected in selection.selected
            for flavor in (flavors_by_revision[selected.candidate_identity],)
            for override in flavor.definition.provider_overrides
        }.values()
    )
    unknown = sorted(
        {
            override.resolution_id
            for override, _provenance in selected_overrides
            if override.resolution_id not in known_ids
        }
    )
    if unknown:
        raise ComponentLockPlanningError(
            "provider_resolution.override_unknown",
            "selected Flavor overrides undeclared provider resolutions: "
            + ", ".join(unknown),
        )

    results: list[ProviderResolution] = []
    for selection in closure.flavor_selections:
        authoring = authorings[selection.authoring_identity.uri]
        selected_revision_ids = {item.candidate_identity for item in selection.selected}
        for declaration in authoring.provider_resolutions:
            overrides = tuple(
                (override, provenance)
                for override, provenance in selected_overrides
                if override.resolution_id == declaration.resolution_id
                and provenance.uri in selected_revision_ids
            )
            if len(overrides) > 1:
                raise ComponentLockPlanningError(
                    "provider_resolution.override_ambiguous",
                    "multiple selected Flavors override provider resolution "
                    f"{declaration.resolution_id!r}",
                )
            try:
                result = (
                    resolve_provider_declaration(declaration)
                    if not overrides
                    else resolve_provider_declaration(
                        declaration,
                        overrides[0][0],
                        override_provenance_identity=overrides[0][1],
                    )
                )
            except ProviderResolutionError as exc:
                raise ComponentLockPlanningError(exc.code, exc.message) from exc
            results.append(result)
    return tuple(sorted(results, key=lambda item: item.request.resolution_id))


def _provider_satisfies(provider: ComponentAuthoring, requirement: Any) -> bool:
    capability = next(
        (
            item
            for item in provider.provides
            if item.name == requirement.capability
            and version_satisfies(item.version, requirement.version_range)
        ),
        None,
    )
    if capability is None:
        return False
    attributes = {"profiles": set(provider.profiles)}
    for constraint in requirement.constraints:
        actual = attributes.get(constraint.key)
        if actual is None:
            raise ComponentLockPlanningError(
                "component_lock.constraint_catalog_missing",
                f"no catalog attribute source exists for {constraint.key!r}",
            )
        expected = set(constraint.values)
        if constraint.operator in {"in", "equals"}:
            accepted = bool(actual & expected)
        elif constraint.operator == "not-in":
            accepted = not bool(actual & expected)
        elif constraint.operator == "contains-all":
            accepted = expected <= actual
        else:
            raise ComponentLockPlanningError(
                "component_lock.constraint_operator_unsupported",
                f"unsupported constraint operator {constraint.operator!r}",
            )
        if not accepted:
            return False
    return True


def _load_flavors(
    roots: tuple[Path, ...], *, boundary: Path
) -> tuple[_LoadedFlavor, ...]:
    paths = _flavor_definition_paths(roots, boundary=boundary)
    loaded: list[_LoadedFlavor] = []
    for path in paths:
        content = _bounded_bytes(path, code="component_lock.flavor_unavailable")
        try:
            if path.name == "flavor.md":
                authoring = parse_flavor_markdown(content, source=path.as_posix())
                definition = authoring.resolve(
                    lambda uri, flavor_root=path.parent: _bounded_bytes(
                        _resolve_path(flavor_root, uri, boundary=boundary),
                        code="component_lock.flavor_authority_unavailable",
                    )
                )
            else:
                definition = FlavorDefinition.from_dict(json.loads(content))
        except (
            FlavorMarkdownError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise ComponentLockPlanningError(
                "component_lock.flavor_invalid", "Flavor definition is invalid"
            ) from exc
        if len(definition.supported_targets) != 1:
            raise ComponentLockPlanningError(
                "component_lock.flavor_invalid",
                "lockable Flavor must provide exactly one target value",
            )
        specification_count = 0
        for reference in definition.specification_fragments:
            fragment = _bounded_bytes(
                _resolve_path(path.parent, reference.uri, boundary=boundary),
                code="component_lock.flavor_specification_unavailable",
            )
            if _bytes_identity(fragment) != reference.identity:
                raise ComponentLockPlanningError(
                    "component_lock.flavor_specification_changed",
                    "Flavor specification no longer matches its exact identity",
                )
            try:
                fragment.decode("utf-8")
            except UnicodeError as exc:
                raise ComponentLockPlanningError(
                    "component_lock.flavor_specification_invalid",
                    "Flavor specification must be valid UTF-8 recipe content",
                ) from exc
            specification_count += 1
        if not specification_count:
            raise ComponentLockPlanningError(
                "component_lock.flavor_invalid", "Flavor requires a specification"
            )
        try:
            loaded_specification = OpenSpecProvider().load(
                path.parent,
                tuple(item.uri for item in definition.specification_fragments),
            )
            loaded_specification.require_unchanged(path.parent)
        except (OpenSpecError, OSError) as exc:
            raise ComponentLockPlanningError(
                "component_lock.flavor_specification_invalid",
                "Flavor specification provider rejected or lost an exact input",
            ) from exc
        if tuple(
            item.identity for item in loaded_specification.specification_set.artifacts
        ) != tuple(item.identity for item in definition.specification_fragments):
            raise ComponentLockPlanningError(
                "component_lock.flavor_specification_changed",
                "Flavor specification provider resolved different exact artifacts",
            )
        for role, reference in (
            *(
                (f"authoring input {index}", item)
                for index, item in enumerate(definition.authoring_inputs)
            ),
            *(
                (f"contribution {item.contribution_id}", item.content)
                for item in definition.contributions
            ),
        ):
            referenced = _bounded_bytes(
                _resolve_path(path.parent, reference.uri, boundary=boundary),
                code="component_lock.flavor_authority_unavailable",
            )
            if _bytes_identity(referenced) != reference.identity:
                raise ComponentLockPlanningError(
                    "component_lock.flavor_authority_changed",
                    f"Flavor {role} no longer matches its exact identity",
                )
        source_identity = _bytes_identity(content)
        revision = FlavorRevision(
            definition,
            source_identity if path.name == "flavor.md" else None,
            (),
        )
        loaded.append(
            _LoadedFlavor(
                path,
                definition,
                revision,
                FlavorSelectionCandidate(
                    candidate_identity=revision.identity.uri,
                    flavor_id=definition.coordinate.name,
                    axis=definition.primary_axis.value,
                    value=definition.supported_targets[0],
                    conflicts=definition.conflicts,
                    coordinate_uri=definition.coordinate.uri,
                ),
                source_identity,
            )
        )
    aliases = [item.selection.flavor_id for item in loaded]
    if len(aliases) != len(set(aliases)):
        raise ComponentLockPlanningError(
            "component_lock.flavor_duplicate", "Flavor catalog repeats an ID"
        )
    return tuple(loaded)


def _flavor_definition_paths(
    roots: tuple[Path, ...], *, boundary: Path
) -> tuple[Path, ...]:
    budget = _CatalogBudget()
    paths: set[Path] = set()
    for configured in roots:
        _require_lexical_directory_safe(
            configured,
            boundary=boundary,
            code="component_lock.flavor_root_unsafe",
        )
        root = _direct_directory(configured, code="component_lock.flavor_root_unsafe")
        _require_within_boundary(
            root, boundary=boundary, code="component_lock.flavor_root_unsafe"
        )
        entries = _catalog_entries(
            root, budget=budget, code="component_lock.flavor_unsafe"
        )
        for candidate, metadata in entries:
            if stat.S_ISREG(metadata.st_mode) and candidate.name in {
                "flavor.md",
                "flavor.json",
            }:
                budget.admit_authority_file(metadata)
                paths.add(candidate)
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            for nested, nested_metadata in _catalog_entries(
                candidate,
                budget=budget,
                code="component_lock.flavor_unsafe",
            ):
                if stat.S_ISREG(nested_metadata.st_mode) and nested.name in {
                    "flavor.md",
                    "flavor.json",
                }:
                    budget.admit_authority_file(nested_metadata)
                    paths.add(nested)
    by_parent: dict[Path, set[str]] = {}
    for path in paths:
        by_parent.setdefault(path.parent, set()).add(path.name)
    ambiguous = tuple(
        parent
        for parent, names in by_parent.items()
        if names == {"flavor.md", "flavor.json"}
    )
    if ambiguous:
        raise ComponentLockPlanningError(
            "component_lock.flavor_authority_ambiguous",
            "Flavor catalog entry contains both flavor.md and flavor.json",
        )
    return tuple(sorted(paths))


def _selected_flavors(
    authoring: ComponentAuthoring,
    catalog: tuple[_LoadedFlavor, ...],
    *,
    defaults: tuple[str, ...],
    selectors: tuple[str, ...],
) -> tuple[tuple[FlavorSelectionCandidate, ...], frozenset[int]]:
    recipes = tuple(
        item.selection
        for item in catalog
        if any(_flavor_is_eligible(slot, item) for slot in authoring.flavor_slots)
    )
    aliases = {alias for item in recipes for alias in flavor_candidate_aliases(item)}
    applicable_defaults = tuple(
        effective
        for selector in defaults
        for coordinate, effective in (_scoped_selector(selector),)
        if (coordinate is None or coordinate == authoring.coordinate.uri)
        and _selector_applies(effective, aliases=aliases, slots={})
    )
    slots = {item.slot_id: item.axis.value for item in authoring.flavor_slots}
    applicable_selector_entries = tuple(
        (index, effective)
        for index, selector in enumerate(selectors)
        for coordinate, effective in (_scoped_selector(selector),)
        if (coordinate is None or coordinate == authoring.coordinate.uri)
        and _selector_applies(effective, aliases=aliases, slots=slots)
    )
    multi_slots = frozenset(
        item.slot_id
        for item in authoring.flavor_slots
        if item.cardinality is FlavorCardinality.ONE_OR_MORE
        or (
            item.cardinality is FlavorCardinality.BOUNDED
            and item.maximum is not None
            and item.maximum > 1
        )
    )
    multi_axes = frozenset(
        item.axis.value
        for item in authoring.flavor_slots
        if item.slot_id in multi_slots
    )
    try:
        initial = apply_flavor_selectors(
            recipes,
            applicable_defaults,
            multi_value_axes=multi_axes,
            slot_axes=slots,
            multi_value_slots=multi_slots,
        )
        selected = initial
        effective_selectors: set[int] = set()
        prefix: list[str] = []
        for selector_index, selector in applicable_selector_entries:
            before = _flavor_selection_signature(selected)
            prefix.append(selector)
            selected = apply_flavor_selectors(
                recipes,
                prefix,
                initial=initial,
                initial_is_preferences=True,
                multi_value_axes=multi_axes,
                slot_axes=slots,
                multi_value_slots=multi_slots,
            )
            if _flavor_selection_signature(selected) != before:
                effective_selectors.add(selector_index)
    except FlavorSelectionError as exc:
        suffix = exc.code.removeprefix("flavor_selection.")
        raise ComponentLockPlanningError(
            f"component_lock.{suffix}", exc.message
        ) from exc
    _require_flavor_cardinality(authoring, selected)
    _require_flavor_relations(selected, catalog)
    return selected, frozenset(effective_selectors)


def _flavor_selection_signature(
    selected: tuple[FlavorSelectionCandidate, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted((item.candidate_identity, item.slot_ids) for item in selected))


def _require_known_selectors(
    catalog: tuple[_LoadedFlavor, ...],
    selectors: tuple[str, ...],
    *,
    coordinates: frozenset[str],
) -> None:
    if len(selectors) > _MAXIMUM_FLAVOR_SELECTORS:
        raise ComponentLockPlanningError(
            "component_lock.flavor_selector_limit",
            "Flavor selector request exceeds the bounded operation limit",
        )
    aliases: dict[str, set[str]] = {}
    for item in catalog:
        for alias in flavor_candidate_aliases(item.selection):
            aliases.setdefault(alias, set()).add(item.revision.identity.uri)
    for raw_selector in selectors:
        coordinate, selector = _scoped_selector(raw_selector)
        if coordinate is not None and coordinate not in coordinates:
            raise ComponentLockPlanningError(
                "component_lock.flavor_selector_component_unknown",
                "Flavor selector Component is not reachable from the locked root: "
                f"{coordinate!r}",
            )
        if len(selector) < 2 or selector[0] not in {"+", "-"}:
            raise ComponentLockPlanningError(
                "component_lock.flavor_selector_invalid",
                "Flavor selectors must begin with + or -",
            )
        try:
            _, alias = parse_flavor_selector_body(selector[1:])
        except FlavorSelectionError as exc:
            raise ComponentLockPlanningError(
                "component_lock.flavor_selector_invalid", exc.message
            ) from exc
        if not alias or alias not in aliases:
            raise ComponentLockPlanningError(
                "component_lock.flavor_unknown",
                f"Flavor selector is unknown: {alias!r}",
            )
        if len(aliases[alias]) != 1:
            candidates = sorted(aliases[alias])
            raise ComponentLockPlanningError(
                "component_lock.flavor_ambiguous",
                f"Flavor selector {alias!r} is ambiguous; use one of: "
                + ", ".join(candidates),
            )


def _scoped_selector(selector: str) -> tuple[str | None, str]:
    """Split ``component://namespace/name::+selector`` without weakening selectors."""

    try:
        return parse_scoped_flavor_selector(selector)
    except FlavorSelectionError as exc:
        raise ComponentLockPlanningError(
            exc.code.replace("flavor_selection.", "component_lock.", 1),
            exc.message,
        ) from exc


def _require_selectors_consumed(selectors: tuple[str, ...], consumed: set[int]) -> None:
    for index, selector in enumerate(selectors):
        if index not in consumed:
            raise ComponentLockPlanningError(
                "component_lock.flavor_selector_noop",
                "Flavor selector has no effect on any reachable applicable "
                f"Component slot: {selector!r}",
            )


def _selector_applies(
    selector: str, *, aliases: set[str], slots: dict[str, str]
) -> bool:
    try:
        slot_id, alias = parse_flavor_selector_body(
            selector[1:], known_slots=frozenset(slots)
        )
    except FlavorSelectionError:
        return False
    return alias in aliases and (slot_id is None or slot_id in slots)


def _require_flavor_relations(
    selected: tuple[FlavorSelectionCandidate, ...], catalog: tuple[_LoadedFlavor, ...]
) -> None:
    selected_references = {
        reference for item in selected for reference in item.reference_ids
    }
    definitions = {
        item.selection.candidate_identity: item.definition for item in catalog
    }
    axis_values: dict[str, set[str]] = {}
    slot_values: dict[tuple[str, str], set[str]] = {}
    for item in selected:
        axis_values.setdefault(item.axis, set()).add(item.value)
        for slot_id in item.slot_ids:
            slot_values.setdefault((item.axis, slot_id), set()).add(item.value)
    for item in selected:
        definition = definitions[item.candidate_identity]
        missing = tuple(
            reference
            for reference in definition.co_requisites
            if reference not in selected_references
        )
        if missing:
            raise ComponentLockPlanningError(
                "component_lock.flavor_corequisite_missing",
                f"Flavor {item.flavor_id!r} requires {missing[0]!r}",
            )
        for group in definition.co_requisite_groups:
            matches = sorted(set(group.alternatives).intersection(selected_references))
            if len(matches) != 1:
                detail = "none selected" if not matches else ", ".join(matches)
                raise ComponentLockPlanningError(
                    "component_lock.flavor_corequisite_group_unsatisfied",
                    f"Flavor {item.flavor_id!r} requires exactly one realization "
                    f"from {group.group_id!r}; {detail}",
                )
        for constraint in definition.secondary_constraints:
            actual = (
                slot_values.get((constraint.axis.value, constraint.slot_id), set())
                if constraint.slot_id is not None
                else axis_values.get(constraint.axis.value, set())
            )
            if constraint.value not in actual:
                raise ComponentLockPlanningError(
                    "component_lock.flavor_target_unsatisfied",
                    f"Flavor {item.flavor_id!r} requires target value "
                    f"{constraint.value!r} on {constraint.axis.value}",
                )


def _require_flavor_cardinality(
    authoring: ComponentAuthoring, selected: tuple[FlavorSelectionCandidate, ...]
) -> None:
    counts: Counter[str] = Counter()
    slots_by_axis: dict[str, list[str]] = {}
    for slot in authoring.flavor_slots:
        slots_by_axis.setdefault(slot.axis.value, []).append(slot.slot_id)
    for flavor in selected:
        targeted = flavor.slot_ids or tuple(slots_by_axis.get(flavor.axis, ()))
        if len(targeted) != 1 and not flavor.slot_ids:
            raise ComponentLockPlanningError(
                "component_lock.flavor_slot_required",
                f"axis {flavor.axis!r} requires explicit slot-qualified selection",
            )
        counts.update(targeted)
    for slot in authoring.flavor_slots:
        count = counts[slot.slot_id]
        minimum = 0 if slot.cardinality is FlavorCardinality.ZERO_OR_ONE else 1
        if slot.cardinality is FlavorCardinality.BOUNDED:
            minimum = slot.minimum or 0
        maximum = (
            slot.maximum
            if slot.cardinality is FlavorCardinality.BOUNDED
            else None
            if slot.cardinality is FlavorCardinality.ONE_OR_MORE
            else 1
        )
        if count < minimum or (maximum is not None and count > maximum):
            raise ComponentLockPlanningError(
                "component_lock.flavor_cardinality",
                f"Flavor slot {slot.slot_id!r} has {count} selections",
            )


def _resolved_node(
    authoring: ComponentAuthoring,
    root: Path,
    *,
    boundary: Path,
    flavors: tuple[_LoadedFlavor, ...],
    selected: tuple[FlavorSelectionCandidate, ...],
    flavor_requirements: tuple[LockedFlavorRequirement, ...],
    repository_source_resolver: RepositorySourceLockResolver,
    source_locks: dict[ContentIdentity, RepositorySourceLock],
    resolve_sources: bool = True,
) -> ResolvedComponentNodeInput:
    repository_sources: list[RepositorySourceLock] = []
    for item in authoring.source_dependencies:
        dependency = RepositorySourceDependency(
            item.dependency_id,
            item.repository_url,
            item.revision_selector,
            item.dependency_kind,
            item.optional,
            None
            if item.integration_contract is None
            else _selector_reference(
                root, item.integration_contract, boundary=boundary
            ),
        )
        try:
            if dependency.identity not in source_locks:
                if not resolve_sources:
                    raise ComponentLockPlanningError(
                        "component_lock.repository_source_replay_missing",
                        "captured repository lock does not cover "
                        "the current dependency",
                    )
                source_locks[dependency.identity] = repository_source_resolver.lock(
                    dependency
                )
            repository_sources.append(source_locks[dependency.identity])
        except RepositorySourceResolutionError as exc:
            raise ComponentLockPlanningError(
                "component_lock.repository_source_unresolved",
                f"repository dependency {item.dependency_id!r} could not be locked: "
                f"{exc.code}",
            ) from exc
    selected_by_slot: dict[str, set[str]] = {}
    slots_by_axis: dict[str, tuple[str, ...]] = {}
    for slot in authoring.flavor_slots:
        slots_by_axis[slot.axis.value] = (
            *slots_by_axis.get(slot.axis.value, ()),
            slot.slot_id,
        )
    for item in selected:
        for slot_id in item.slot_ids or slots_by_axis.get(item.axis, ()):
            selected_by_slot.setdefault(slot_id, set()).add(item.candidate_identity)
    candidates: list[FlavorCandidateInput] = []
    for slot in authoring.flavor_slots:
        for flavor in flavors:
            if not _flavor_is_eligible(slot, flavor):
                continue
            status, reasons = _candidate_decision(
                slot,
                flavor,
                selected=selected,
                selected_by_slot=selected_by_slot,
                catalog=flavors,
            )
            candidates.append(
                FlavorCandidateInput(
                    slot.slot_id,
                    flavor.selection.value,
                    flavor.revision.identity,
                    status,
                    reasons,
                )
            )
    return ResolvedComponentNodeInput(
        authoring=authoring,
        specifications=_specification_references(authoring, root, boundary=boundary),
        authoring_inputs=tuple(
            _selector_reference(root, item, boundary=boundary)
            for item in authoring.authoring_inputs
        ),
        workflow_definition=_selector_reference(
            root, authoring.workflow_definition, boundary=boundary
        ),
        routing_policy=_selector_reference(
            root, authoring.routing_policy, boundary=boundary
        ),
        acceptance_contracts=tuple(
            _selector_reference(root, item, boundary=boundary)
            for item in authoring.acceptance_contracts
        ),
        repository_sources=tuple(repository_sources),
        public_interfaces=tuple(
            _selector_reference(root, provided.interface, boundary=boundary)
            for provided in authoring.provides
            if provided.interface is not None
        ),
        assets=tuple(
            _resolved_asset(root, item, boundary=boundary) for item in authoring.assets
        ),
        flavor_candidates=tuple(candidates),
        flavor_requirements=flavor_requirements,
        catalog_attributes=(CatalogAttribute("profiles", authoring.profiles),)
        if authoring.profiles
        else (),
    )


def _flavor_is_eligible(slot: Any, flavor: _LoadedFlavor) -> bool:
    return flavor.selection.axis == slot.axis.value and (
        not flavor.definition.applicable_capabilities
        or slot.capability_contract in flavor.definition.applicable_capabilities
    )


def _candidate_decision(
    slot: Any,
    flavor: _LoadedFlavor,
    *,
    selected: tuple[FlavorSelectionCandidate, ...],
    selected_by_slot: dict[str, set[str]],
    catalog: tuple[_LoadedFlavor, ...],
) -> tuple[CandidateStatus, tuple[str, ...]]:
    slot_id = slot.slot_id
    revision = flavor.revision.identity.uri
    if revision in selected_by_slot.get(slot_id, set()):
        return CandidateStatus.SELECTED, ()

    selected_references = {
        reference for item in selected for reference in item.reference_ids
    }
    candidate_references = flavor.selection.reference_ids
    conflicting = tuple(
        sorted(
            item.flavor_id
            for item in selected
            if bool(
                candidate_references.intersection(item.conflicts)
                or item.reference_ids.intersection(flavor.definition.conflicts)
            )
        )
    )
    if conflicting:
        return CandidateStatus.CONFLICT, (
            "conflicts with selected Flavor " + conflicting[0],
        )

    missing = tuple(
        reference
        for reference in flavor.definition.co_requisites
        if reference not in selected_references
    )
    if missing:
        return CandidateStatus.UNAVAILABLE, (
            "missing selected co-requisite " + missing[0],
        )

    axis_values: dict[str, set[str]] = {}
    slot_values: dict[tuple[str, str], set[str]] = {}
    for item in selected:
        axis_values.setdefault(item.axis, set()).add(item.value)
        for selected_slot in item.slot_ids:
            slot_values.setdefault((item.axis, selected_slot), set()).add(item.value)
    for constraint in flavor.definition.secondary_constraints:
        actual = (
            slot_values.get((constraint.axis.value, constraint.slot_id), set())
            if constraint.slot_id is not None
            else axis_values.get(constraint.axis.value, set())
        )
        if constraint.value not in actual:
            target = (
                f" slot {constraint.slot_id}" if constraint.slot_id is not None else ""
            )
            return CandidateStatus.UNAVAILABLE, (
                "requires selected target "
                f"{constraint.axis.value}{target}={constraint.value}",
            )

    selected_for_slot = selected_by_slot.get(slot_id, set())
    exclusive = slot.cardinality in {
        FlavorCardinality.EXACTLY_ONE,
        FlavorCardinality.ZERO_OR_ONE,
    } or (slot.cardinality is FlavorCardinality.BOUNDED and slot.maximum == 1)
    if selected_for_slot and exclusive:
        selected_names = sorted(
            item.selection.flavor_id
            for item in catalog
            if item.revision.identity.uri in selected_for_slot
        )
        return CandidateStatus.REJECTED, (
            "slot selected another Flavor " + selected_names[0],
        )
    return CandidateStatus.REJECTED, ("not selected by the effective target policy",)


def _specification_references(
    authoring: ComponentAuthoring, root: Path, *, boundary: Path
) -> tuple[ContentReference, ...]:
    references = tuple(
        _reference(root, "specification", uri, boundary=boundary)
        for uri in authoring.specification_roots
    )
    try:
        try:
            loaded = load_specification_provider(
                authoring.specification_provider,
                root,
                authoring.specification_roots,
                id_prefix=(
                    f"{authoring.coordinate.namespace}.{authoring.coordinate.name}"
                ),
            )
        except LookupError as exc:
            raise ComponentLockPlanningError(
                "component_lock.specification_provider_unsupported",
                "unsupported specification provider "
                f"{authoring.specification_provider!r}",
            ) from exc
        loaded.require_unchanged(root)
    except (*SPECIFICATION_PROVIDER_ERRORS, OSError) as exc:
        raise ComponentLockPlanningError(
            "component_lock.specification_invalid",
            "Component specification provider rejected or lost an authored root",
        ) from exc
    expected = tuple(item.identity for item in references)
    actual = tuple(item.identity for item in loaded.specification_set.artifacts)
    if actual != expected:
        raise ComponentLockPlanningError(
            "component_lock.specification_changed",
            "specification provider resolved different exact artifacts",
        )
    return references


def _selector_reference(
    root: Path, selector: ComponentContentSelector, *, boundary: Path
) -> ContentReference:
    reference = _reference(
        _selector_base(root, selector.kind, boundary=boundary),
        selector.kind,
        selector.uri,
        boundary=boundary,
    )
    if selector.pin is not None and selector.pin != reference.identity:
        raise ComponentLockPlanningError(
            "component_lock.selector_pin_mismatch",
            f"authored pin changed for {selector.uri!r}",
        )
    return reference


def _selector_base(root: Path, kind: str, *, boundary: Path) -> Path:
    if kind in {
        "model-selection",
        "routing-policy",
        "specification-to-source-skill",
        "toolchain-constraint",
        "workflow",
    }:
        return boundary
    return root


def _component_reference_path(
    root: Path, reference: ContentReference, *, boundary: Path
) -> Path:
    return _resolve_path(
        _selector_base(root, reference.kind, boundary=boundary),
        reference.uri,
        boundary=boundary,
    )


def _reference(root: Path, kind: str, uri: str, *, boundary: Path) -> ContentReference:
    path = _resolve_path(root, uri, boundary=boundary)
    return ContentReference(
        kind,
        uri,
        _bytes_identity(
            _bounded_bytes(path, code="component_lock.content_unavailable")
        ),
    )


def _resolve_path(root: Path, uri: str, *, boundary: Path) -> Path:
    configured = Path(os.path.abspath(root.joinpath(*Path(uri).parts)))
    return _safe_regular_file(
        configured,
        boundary=boundary,
        unsafe_code="component_lock.content_unsafe",
        unavailable_code="component_lock.content_unavailable",
    )


def _resolved_asset(
    component_root: Path, selector: Any, *, boundary: Path
) -> ResolvedComponentAsset:
    source = selector.source
    if source.startswith(("http://", "https://")):
        blob = _remote_asset_blob(source, media_type=selector.media_type)
    else:
        safe = _asset_local_path(component_root, source, boundary=boundary)
        assert safe is not None
        blob = _local_asset_blob(safe, media_type=selector.media_type)
    if selector.pin is not None and selector.pin.uri != blob.identity:
        raise ComponentLockPlanningError(
            "component_lock.asset_pin_mismatch",
            f"asset {selector.asset_id!r} does not match its authored pin",
        )
    return ResolvedComponentAsset(selector, blob)


def _asset_local_path(
    component_root: Path, source: str, *, boundary: Path
) -> Path | None:
    if source.startswith(("http://", "https://")):
        return None
    if source.startswith("project:///"):
        relative = source.removeprefix("project:///")
        configured = boundary.joinpath(*Path(relative).parts)
    else:
        configured = component_root.joinpath(*Path(source).parts)
    return _safe_regular_file(
        configured,
        boundary=boundary,
        unsafe_code="component_lock.asset_unsafe",
        unavailable_code="component_lock.asset_unavailable",
    )


def _local_asset_blob(path: Path, *, media_type: str) -> BlobRef:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ComponentLockPlanningError(
                    "component_lock.asset_unsafe", "asset must be a regular file"
                )
            while chunk := stream.read(_ASSET_READ_CHUNK):
                size += len(chunk)
                if size > _MAXIMUM_ASSET_BYTES:
                    raise ComponentLockPlanningError(
                        "component_lock.asset_limit",
                        "asset exceeds the four GiB planning limit",
                    )
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except ComponentLockPlanningError:
        raise
    except OSError as exc:
        raise ComponentLockPlanningError(
            "component_lock.asset_unavailable", "asset could not be read"
        ) from exc

    def signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    if signature(before) != signature(after):
        raise ComponentLockPlanningError(
            "component_lock.asset_changed", "asset changed while it was read"
        )
    return BlobRef(digest.hexdigest(), size, media_type=media_type)


def _remote_asset_blob(uri: str, *, media_type: str) -> BlobRef:
    request = urllib.request.Request(
        uri,
        headers={"User-Agent": "literate-ai-asset-resolver/1"},
        method="GET",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final = response.geturl()
            if not final.startswith(("http://", "https://")):
                raise ComponentLockPlanningError(
                    "component_lock.asset_unsafe",
                    "remote asset redirected to a non-HTTP(S) URI",
                )
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > _MAXIMUM_ASSET_BYTES:
                raise ComponentLockPlanningError(
                    "component_lock.asset_limit",
                    "remote asset exceeds the four GiB planning limit",
                )
            while chunk := response.read(_ASSET_READ_CHUNK):
                size += len(chunk)
                if size > _MAXIMUM_ASSET_BYTES:
                    raise ComponentLockPlanningError(
                        "component_lock.asset_limit",
                        "remote asset exceeds the four GiB planning limit",
                    )
                digest.update(chunk)
    except ComponentLockPlanningError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ComponentLockPlanningError(
            "component_lock.asset_unavailable",
            "remote asset is not reachable during Component compilation",
        ) from exc
    return BlobRef(digest.hexdigest(), size, media_type=media_type)


def _remote_asset_to_cas(uri: str, *, media_type: str, cas: FileSystemCAS) -> BlobRef:
    request = urllib.request.Request(
        uri,
        headers={"User-Agent": "literate-ai-asset-resolver/1"},
        method="GET",
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix="litai-asset-")
    temporary = Path(temporary_name)
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    final = response.geturl()
                    if not final.startswith(("http://", "https://")):
                        raise ComponentLockPlanningError(
                            "component_lock.asset_unsafe",
                            "remote asset redirected to a non-HTTP(S) URI",
                        )
                    while chunk := response.read(_ASSET_READ_CHUNK):
                        size += len(chunk)
                        if size > _MAXIMUM_ASSET_BYTES:
                            raise ComponentLockPlanningError(
                                "component_lock.asset_limit",
                                "remote asset exceeds the four GiB limit",
                            )
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            except ComponentLockPlanningError:
                raise
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise ComponentLockPlanningError(
                    "component_lock.asset_unavailable",
                    "remote asset is not reachable during Component compilation",
                ) from exc
        return cas.put_file(temporary, media_type=media_type)
    finally:
        temporary.unlink(missing_ok=True)


def _require_inputs_unchanged(
    *,
    root: Path,
    boundary: Path,
    project: Any,
    authoring_roots: tuple[Path, ...],
    authoring_paths: tuple[Path, ...],
    flavor_roots: tuple[Path, ...],
    authorings: tuple[_LoadedAuthoring, ...],
    flavors: tuple[_LoadedFlavor, ...],
    nodes: tuple[ResolvedComponentNodeInput, ...],
) -> None:
    """Pin every consumed file and recheck catalog membership before returning."""

    closure = PinnedInputClosure()
    try:
        if project is not None:
            try:
                project_snapshot = ProjectConfigurationStore(project.root).read()
            except ProjectError as exc:
                raise ComponentLockPlanningError(
                    "component_lock.project_unavailable",
                    "project configuration is unavailable during lock planning",
                ) from exc
            closure.pin(
                project_snapshot.root / PROJECT_FILENAME,
                boundary=project.root,
                label="project-manifest",
                expected_content=project_snapshot.content,
            )
            if project_snapshot.definition != project.definition:
                raise ComponentLockPlanningError(
                    "component_lock.project_changed",
                    "project configuration changed during lock planning",
                )
        for index, item in enumerate(authorings):
            closure.pin(
                item.root / "component.md",
                boundary=boundary,
                label=f"component-authoring:{index}",
                expected_identity=item.source_identity,
            )
        roots_by_identity = {
            item.authoring.identity.uri: item.root for item in authorings
        }
        for node_index, node in enumerate(nodes):
            node_root = roots_by_identity[node.authoring.identity.uri]
            references = (
                *node.specifications,
                *node.authoring_inputs,
                node.workflow_definition,
                node.routing_policy,
                *node.acceptance_contracts,
                *node.public_interfaces,
                *(
                    item.dependency.integration_contract
                    for item in node.repository_sources
                    if item.dependency.integration_contract is not None
                ),
            )
            for reference_index, reference in enumerate(references):
                closure.pin(
                    _component_reference_path(node_root, reference, boundary=boundary),
                    boundary=boundary,
                    label=f"component-content:{node_index}:{reference_index}",
                    expected_identity=reference.identity,
                )
            for asset_index, asset in enumerate(node.assets):
                current = _resolved_asset(node_root, asset.selector, boundary=boundary)
                if current.blob != asset.blob:
                    raise ComponentLockPlanningError(
                        "component_lock.asset_changed",
                        f"asset {asset.selector.asset_id!r} changed during lock "
                        "planning",
                    )
                local_path = _asset_local_path(
                    node_root, asset.selector.source, boundary=boundary
                )
                if local_path is not None:
                    closure.pin(
                        local_path,
                        boundary=boundary,
                        label=f"component-asset:{node_index}:{asset_index}",
                        expected_identity=ContentIdentity.parse_uri(
                            asset.blob.identity
                        ),
                    )
        for flavor_index, flavor in enumerate(flavors):
            closure.pin(
                flavor.path,
                boundary=boundary,
                label=f"flavor-manifest:{flavor_index}",
                expected_identity=flavor.source_identity,
            )
            references = (
                *flavor.definition.specification_fragments,
                *flavor.definition.authoring_inputs,
                *(item.content for item in flavor.definition.contributions),
            )
            for reference_index, reference in enumerate(references):
                closure.pin(
                    _resolve_path(flavor.path.parent, reference.uri, boundary=boundary),
                    boundary=boundary,
                    label=f"flavor-content:{flavor_index}:{reference_index}",
                    expected_identity=reference.identity,
                )
        if (
            _component_markdown_paths(authoring_roots, boundary=boundary)
            != authoring_paths
        ):
            raise ComponentLockPlanningError(
                "component_lock.catalog_changed",
                "Component catalog membership changed during lock planning",
            )
        if _flavor_definition_paths(flavor_roots, boundary=boundary) != tuple(
            item.path for item in flavors
        ):
            raise ComponentLockPlanningError(
                "component_lock.catalog_changed",
                "Flavor catalog membership changed during lock planning",
            )
        closure.require_unchanged()
    except PinnedInputClosureError as exc:
        raise ComponentLockPlanningError(exc.code, exc.message) from exc


def _is_retained_implementation_directory(
    candidate: Path,
    *,
    siblings: tuple[tuple[Path, os.stat_result], ...],
) -> bool:
    """Skip convert retained source that sits beside a Component authoring file."""

    if candidate.name != _RETAINED_IMPLEMENTATION_DIRECTORY:
        return False
    return any(
        sibling.name == "component.md" and stat.S_ISREG(metadata.st_mode)
        for sibling, metadata in siblings
    )


def _direct_directory(path: Path, *, code: str) -> Path:
    supplied = Path(path)
    try:
        metadata = supplied.lstat()
    except OSError as exc:
        raise ComponentLockPlanningError(
            code, "configured directory is unavailable"
        ) from exc
    if stat_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ComponentLockPlanningError(
            code, "configured path must be a direct directory"
        )
    try:
        resolved = supplied.resolve(strict=True)
        require_safe_directory(resolved)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ComponentLockPlanningError(
            code, "configured directory has an unsafe or unavailable path"
        ) from exc
    return resolved


def _require_within_boundary(path: Path, *, boundary: Path, code: str) -> None:
    try:
        resolved_boundary = Path(boundary).resolve(strict=True)
    except OSError as exc:
        raise ComponentLockPlanningError(
            code, "project boundary is unavailable"
        ) from exc
    if not path.is_relative_to(resolved_boundary):
        raise ComponentLockPlanningError(
            code, "configured catalog must remain within the project boundary"
        )


def _require_lexical_directory_safe(path: Path, *, boundary: Path, code: str) -> None:
    configured = Path(os.path.abspath(path))
    try:
        resolved_boundary = Path(boundary).resolve(strict=True)
        if configured.is_relative_to(resolved_boundary):
            require_safe_directory(configured)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ComponentLockPlanningError(
            code, "configured catalog path traverses a link or reparse point"
        ) from exc


def _catalog_entries(
    directory: Path,
    *,
    budget: _CatalogBudget,
    code: str,
) -> tuple[tuple[Path, os.stat_result], ...]:
    try:
        require_safe_directory(directory)
        entries: list[tuple[Path, os.stat_result]] = []
        with os.scandir(directory) as stream:
            for entry in stream:
                budget.admit_entry()
                metadata = entry.stat(follow_symlinks=False)
                if stat_is_link_or_reparse(metadata):
                    raise ComponentLockPlanningError(
                        code,
                        "Component and Flavor catalogs cannot contain links or "
                        "reparse points",
                    )
                if not (
                    stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
                ):
                    raise ComponentLockPlanningError(
                        code,
                        "Component and Flavor catalogs require regular entries",
                    )
                entries.append((Path(entry.path), metadata))
    except ComponentLockPlanningError:
        raise
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ComponentLockPlanningError(
            code, "Component or Flavor catalog became unsafe or unavailable"
        ) from exc
    return tuple(sorted(entries, key=lambda item: item[0].name))


def _safe_regular_file(
    path: Path,
    *,
    boundary: Path,
    unsafe_code: str,
    unavailable_code: str,
) -> Path:
    configured = Path(os.path.abspath(path))
    try:
        resolved_boundary = Path(boundary).resolve(strict=True)
    except OSError as exc:
        raise ComponentLockPlanningError(
            unavailable_code, "project boundary is unavailable"
        ) from exc
    if not configured.is_relative_to(resolved_boundary):
        raise ComponentLockPlanningError(
            unsafe_code, "Component content escapes the project boundary"
        )
    try:
        require_safe_directory(configured.parent)
        metadata = configured.lstat()
    except UnsafeFilesystemPathError as exc:
        raise ComponentLockPlanningError(
            unsafe_code, "Component content traverses an unsafe directory"
        ) from exc
    except OSError as exc:
        raise ComponentLockPlanningError(
            unavailable_code, "Component content is unavailable"
        ) from exc
    if stat_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ComponentLockPlanningError(
            unsafe_code,
            "Component content must be a direct regular file without reparse points",
        )
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise ComponentLockPlanningError(
            unavailable_code, "Component content is unavailable"
        ) from exc
    if not resolved.is_relative_to(resolved_boundary):
        raise ComponentLockPlanningError(
            unsafe_code, "Component content escapes the project boundary"
        )
    return configured


def _bounded_bytes(path: Path, *, code: str) -> bytes:
    try:
        before = path.lstat()
        if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise ComponentLockPlanningError(
                code, "required content must be a direct regular file"
            )
        if before.st_size > _MAXIMUM_FILE_BYTES:
            raise ComponentLockPlanningError(
                code, "required content exceeds the size limit"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                stat_is_link_or_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _node_signature(before) != _node_signature(opened)
            ):
                raise ComponentLockPlanningError(
                    code, "required content changed while it was opened"
                )
            content = stream.read(_MAXIMUM_FILE_BYTES + 1)
        after = path.lstat()
    except OSError as exc:
        raise ComponentLockPlanningError(
            code, "required content is unavailable"
        ) from exc
    if (
        stat_is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or _node_signature(before) != _node_signature(after)
    ):
        raise ComponentLockPlanningError(
            code, "required content changed while it was read"
        )
    if len(content) > _MAXIMUM_FILE_BYTES:
        raise ComponentLockPlanningError(
            code, "required content exceeds the size limit"
        )
    return content


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _bytes_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


__all__ = [
    "ComponentCatalogSnapshot",
    "ComponentLockPlanningError",
    "FilesystemComponentLockPlanner",
]
