"""Verifier-owned four-Component portfolio support for ADR 0027.

The production Component resolver intentionally has one root per immutable lock.  The
durable split-service topology has two independent roots that share one cache provider:
frontend -> API -> cache, and collector -> cache.  This module keeps the portfolio
manifest and the deterministic union checks in Python so the model is responsible only
for each Component's source, not for reconstructing repository or SBOM mechanics.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.browser_acceptance import BrowserObservation
from literate_ai.adapters.builders import BuildError, run_bounded_process
from literate_ai.adapters.component_acceptance import (
    load_browser_interaction_acceptance,
)
from literate_ai.adapters.component_lock_planning import (
    ComponentCatalogSnapshot,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.dependencies import (
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.intelligence import DisabledGenerationIndexer
from literate_ai.adapters.lifecycle import (
    LocalIndependentAcceptanceCase,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalResolvedExecutionCommand,
    _child_process_environment,
)
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    StandardProjectExecutionRequest,
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
    project_locked_generation_authority,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleResult,
)
from literate_ai.application.standard_project_services import (
    StandardProjectApplicationService,
)
from literate_ai.contracts import (
    ComponentChangeSurface,
    ComponentInvalidationDecision,
    ComponentLock,
    ContentIdentity,
    ContentReference,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    DependencyKind,
    SourceDerivationCacheKey,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.sbom import ManagedComponentKind
from literate_ai.storage import FileSystemCAS

PORTFOLIO_SCHEMA = "literate-ai/durable-split-service-portfolio@1"
PORTFOLIO_REFERENCE_KIND = "sample-portfolio"
UPSTREAM_REFERENCE_KIND = "verifier-fixture"
BROWSER_REFERENCE_KIND = "browser-acceptance"
EXECUTION_REFERENCE_KIND = "sample-execution-interface"
ORACLE_REFERENCE_KIND = "acceptance-oracle"
ROLE_COORDINATES = {
    "frontend": "component://samples/durable-split-service",
    "api": "component://samples/durable-split-api",
    "collector": "component://samples/durable-split-collector",
    "cache": "component://samples/durable-snapshot-cache",
}
ROOT_COORDINATES = (
    ROLE_COORDINATES["frontend"],
    ROLE_COORDINATES["collector"],
)
EDGE_COORDINATES = frozenset(
    {
        (
            ROLE_COORDINATES["frontend"],
            ROLE_COORDINATES["api"],
            "literate-ai.durable-snapshot-api",
        ),
        (
            ROLE_COORDINATES["api"],
            ROLE_COORDINATES["cache"],
            "literate-ai.durable-snapshot-read",
        ),
        (
            ROLE_COORDINATES["collector"],
            ROLE_COORDINATES["cache"],
            "literate-ai.durable-snapshot-write",
        ),
    }
)
_DURABLE_SCHEMA_COLUMNS = {
    "snapshot": (
        ("snapshot_id", "INTEGER", True, 1),
        ("window", "TEXT", True, 0),
        ("status", "TEXT", True, 0),
        ("started_at", "TEXT", True, 0),
        ("published_at", "TEXT", False, 0),
    ),
    "snapshot_metric": (
        ("snapshot_id", "INTEGER", True, 1),
        ("metric", "TEXT", True, 2),
        ("value", "NUMERIC", True, 0),
    ),
    "current_snapshot": (
        ("id", "INTEGER", True, 1),
        ("snapshot_id", "INTEGER", True, 0),
    ),
    "collection_window": (
        ("window", "TEXT", True, 1),
        ("selected_at", "TEXT", True, 0),
        ("state", "TEXT", True, 0),
        ("lease_owner", "TEXT", False, 0),
        ("lease_expires_at", "TEXT", False, 0),
        ("retry_count", "INTEGER", True, 0),
        ("retry_limit", "INTEGER", True, 0),
        ("last_error_class", "TEXT", False, 0),
        ("started_at", "TEXT", False, 0),
        ("completed_at", "TEXT", False, 0),
    ),
}
_DURABLE_SCHEMA_FOREIGN_KEYS = frozenset(
    {
        ("snapshot_metric", "snapshot_id", "snapshot", "snapshot_id"),
        ("current_snapshot", "snapshot_id", "snapshot", "snapshot_id"),
    }
)


class DurableSplitPortfolioError(RuntimeError):
    """The verifier-owned portfolio authority is unsafe, stale, or contradictory."""


def _strict_object(
    value: object, *, fields: frozenset[str], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DurableSplitPortfolioError(f"{label} must be an object")
    if set(value) != fields:
        raise DurableSplitPortfolioError(f"{label} has unexpected fields")
    return value


def _pinned_file(root: Path, reference: ContentReference, *, label: str) -> Path:
    relative = Path(reference.uri)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise DurableSplitPortfolioError(f"{label} path is unsafe")
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise DurableSplitPortfolioError(f"{label} cannot be a symbolic link")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DurableSplitPortfolioError(f"{label} is unavailable") from exc
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise DurableSplitPortfolioError(f"{label} escapes its harness")
    content = resolved.read_bytes()
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise DurableSplitPortfolioError(f"{label} identity is stale")
    return resolved


@dataclass(frozen=True, slots=True)
class DurableSplitComponentHarness:
    """Pinned verifier inputs for one independently generated Component."""

    role: str
    execution_reference: ContentReference
    execution_path: Path
    oracle_reference: ContentReference
    oracle_path: Path

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/durable-split-component-harness@1",
                "role": self.role,
                "execution": self.execution_reference.identity.uri,
                "oracle": self.oracle_reference.identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class DurableSplitPortfolioManifest:
    """Strict verifier authority naming the two roots and four logical roles."""

    reference: ContentReference
    path: Path
    roles: tuple[tuple[str, str], ...]
    roots: tuple[str, ...]
    edges: tuple[tuple[str, str, str], ...]
    upstream_reference: ContentReference
    upstream_path: Path
    browser_reference: ContentReference
    browser_path: Path
    component_harnesses: tuple[DurableSplitComponentHarness, ...]

    def harness_for(self, role: str) -> DurableSplitComponentHarness:
        try:
            return next(item for item in self.component_harnesses if item.role == role)
        except StopIteration as exc:
            raise DurableSplitPortfolioError(
                f"portfolio lacks verifier harness for role {role!r}"
            ) from exc

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": PORTFOLIO_SCHEMA,
                "manifest": self.reference.identity.uri,
                "roles": dict(self.roles),
                "roots": list(self.roots),
                "edges": [list(item) for item in self.edges],
                "upstream": self.upstream_reference.identity.uri,
                "browser_acceptance": self.browser_reference.identity.uri,
                "component_harnesses": [
                    item.identity.uri for item in self.component_harnesses
                ],
            }
        )


def load_durable_split_portfolio(
    harness_root: Path, metadata: dict[str, Any]
) -> DurableSplitPortfolioManifest:
    """Load the exact portfolio and fixture without exposing them to generation."""

    try:
        reference = ContentReference.from_dict(metadata["portfolio_manifest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableSplitPortfolioError(
            "durable split sample lacks a valid portfolio manifest reference"
        ) from exc
    if reference.kind != PORTFOLIO_REFERENCE_KIND or reference.uri != "portfolio.json":
        raise DurableSplitPortfolioError(
            "durable split portfolio reference is not canonical"
        )
    path = _pinned_file(harness_root, reference, label="portfolio manifest")
    try:
        document = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DurableSplitPortfolioError(
            "portfolio manifest is not valid JSON"
        ) from exc
    value = _strict_object(
        document,
        fields=frozenset(
            {
                "schema",
                "roles",
                "roots",
                "edges",
                "upstream_fixture",
                "browser_acceptance",
                "component_harnesses",
            }
        ),
        label="portfolio manifest",
    )
    if value["schema"] != PORTFOLIO_SCHEMA:
        raise DurableSplitPortfolioError("portfolio manifest schema is unsupported")
    roles_value = value["roles"]
    if roles_value != {key: ROLE_COORDINATES[key] for key in sorted(ROLE_COORDINATES)}:
        raise DurableSplitPortfolioError(
            "portfolio roles differ from the four-boundary contract"
        )
    roots_value = value["roots"]
    if roots_value != list(ROOT_COORDINATES):
        raise DurableSplitPortfolioError(
            "portfolio roots differ from the independent topology"
        )
    edges_value = value["edges"]
    if not isinstance(edges_value, list):
        raise DurableSplitPortfolioError("portfolio edges must be an array")
    edges: list[tuple[str, str, str]] = []
    for index, item in enumerate(edges_value):
        edge = _strict_object(
            item,
            fields=frozenset({"consumer", "provider", "capability"}),
            label=f"portfolio edge {index}",
        )
        if any(not isinstance(edge[key], str) for key in edge):
            raise DurableSplitPortfolioError("portfolio edge values must be strings")
        edges.append((edge["consumer"], edge["provider"], edge["capability"]))
    if frozenset(edges) != EDGE_COORDINATES or len(edges) != len(EDGE_COORDINATES):
        raise DurableSplitPortfolioError(
            "portfolio edges violate the public capability topology"
        )
    try:
        upstream = ContentReference.from_dict(value["upstream_fixture"])
    except (TypeError, ValueError) as exc:
        raise DurableSplitPortfolioError(
            "upstream fixture reference is invalid"
        ) from exc
    if (
        upstream.kind != UPSTREAM_REFERENCE_KIND
        or upstream.uri != "acceptance/upstream.json"
    ):
        raise DurableSplitPortfolioError("upstream fixture reference is not canonical")
    upstream_path = _pinned_file(harness_root, upstream, label="upstream fixture")
    try:
        browser = ContentReference.from_dict(value["browser_acceptance"])
    except (TypeError, ValueError) as exc:
        raise DurableSplitPortfolioError(
            "browser acceptance reference is invalid"
        ) from exc
    if (
        browser.kind != BROWSER_REFERENCE_KIND
        or browser.uri != "acceptance/browser.json"
    ):
        raise DurableSplitPortfolioError(
            "browser acceptance reference is not canonical"
        )
    browser_path = _pinned_file(
        harness_root, browser, label="browser acceptance contract"
    )
    harnesses_value = value["component_harnesses"]
    if not isinstance(harnesses_value, dict) or set(harnesses_value) != set(
        ROLE_COORDINATES
    ):
        raise DurableSplitPortfolioError(
            "portfolio Component harnesses must cover exactly four roles"
        )
    harness_collection = harness_root.parent
    component_harnesses = []
    for role in sorted(ROLE_COORDINATES):
        item = _strict_object(
            harnesses_value[role],
            fields=frozenset({"execution", "oracle"}),
            label=f"portfolio {role} harness",
        )
        try:
            execution = ContentReference.from_dict(item["execution"])
            oracle = ContentReference.from_dict(item["oracle"])
        except (TypeError, ValueError) as exc:
            raise DurableSplitPortfolioError(
                f"portfolio {role} harness reference is invalid"
            ) from exc
        expected_prefix = f"{Path(ROLE_COORDINATES[role]).name}/acceptance/"
        if (
            execution.kind != EXECUTION_REFERENCE_KIND
            or execution.uri != expected_prefix + "execution.json"
            or oracle.kind != ORACLE_REFERENCE_KIND
            or oracle.uri != expected_prefix + "oracle.json"
        ):
            raise DurableSplitPortfolioError(
                f"portfolio {role} harness references are not canonical"
            )
        component_harnesses.append(
            DurableSplitComponentHarness(
                role,
                execution,
                _pinned_file(
                    harness_collection,
                    execution,
                    label=f"{role} execution interface",
                ),
                oracle,
                _pinned_file(
                    harness_collection,
                    oracle,
                    label=f"{role} acceptance oracle",
                ),
            )
        )
    return DurableSplitPortfolioManifest(
        reference,
        path,
        tuple(sorted(roles_value.items())),
        tuple(roots_value),
        tuple(sorted(edges)),
        upstream,
        upstream_path,
        browser,
        browser_path,
        tuple(component_harnesses),
    )


@dataclass(frozen=True, slots=True)
class DurableSplitRootAuthority:
    component_root: Path
    authority: LockedGenerationAuthority
    catalog: ComponentCatalogSnapshot

    @property
    def lock(self) -> ComponentLock:
        return self.authority.lock


@dataclass(frozen=True, slots=True)
class ResolvedDurableSplitPortfolio:
    manifest: DurableSplitPortfolioManifest
    frontend: DurableSplitRootAuthority
    collector: DurableSplitRootAuthority
    managed_graphs: tuple[CycloneDxManagedGraph, ...]
    source_bom_identities: tuple[ContentIdentity, ...]

    @property
    def locks(self) -> tuple[ComponentLock, ...]:
        return (self.frontend.lock, self.collector.lock)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/resolved-durable-split-service-portfolio@1",
                "manifest": self.manifest.identity.uri,
                "locks": [item.identity.uri for item in self.locks],
                "managed_graphs": [item.identity.uri for item in self.managed_graphs],
                "source_boms": [item.uri for item in self.source_bom_identities],
            }
        )


def _component_root(project_root: Path, coordinate: str) -> Path:
    from literate_ai.adapters.component_markdown import (
        ComponentMarkdownError,
        parse_component_markdown,
    )
    from literate_ai.projects import discover_project

    project = discover_project(project_root)
    if project is None:
        raise DurableSplitPortfolioError("portfolio requires a declared project")
    matches = []
    for catalog in project.roots("component"):
        for manifest in catalog.rglob("component.md"):
            if manifest.is_symlink() or not manifest.is_file():
                continue
            try:
                authoring = parse_component_markdown(
                    manifest,
                    manifest.read_text(encoding="utf-8"),
                    project_root=project.root,
                )
            except (OSError, UnicodeError, ComponentMarkdownError) as exc:
                raise DurableSplitPortfolioError(
                    "portfolio Component catalog contains invalid authoring"
                ) from exc
            if authoring.coordinate.uri == coordinate:
                matches.append(manifest.parent.resolve(strict=True))
    if len(matches) != 1:
        raise DurableSplitPortfolioError(
            f"portfolio requires exactly one Component {coordinate!r}"
        )
    return matches[0]


def _root_authority(
    component_root: Path,
    *,
    target_name: str,
    selectors: tuple[str, ...],
) -> DurableSplitRootAuthority:
    planner = FilesystemComponentLockPlanner()
    plan, snapshot = planner.plan_with_snapshot(
        component_root,
        target_name=target_name,
        flavor_selectors=selectors,
    )
    resolved = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    authority = project_locked_generation_authority(
        resolved.lock,
        root_authoring=snapshot.root_authoring,
        authorings=resolved.lock.authorings,
        flavor_catalog=snapshot.flavor_revisions,
        target_name=target_name,
        flavor_selectors=selectors,
    )
    return DurableSplitRootAuthority(component_root, authority, snapshot)


def _lock_edges(locks: tuple[ComponentLock, ...]) -> frozenset[tuple[str, str, str]]:
    revisions: dict[str, str] = {}
    for lock in locks:
        for node in lock.nodes:
            prior = revisions.setdefault(
                node.revision.identity.uri, node.revision.coordinate.uri
            )
            if prior != node.revision.coordinate.uri:
                raise DurableSplitPortfolioError(
                    "Component revision identity is ambiguous"
                )
    observed = set()
    for lock in locks:
        for edge in lock.edges:
            if edge.kind is not DependencyKind.GENERATION or edge.optional:
                raise DurableSplitPortfolioError(
                    "portfolio edges must be required generation capabilities"
                )
            if edge.public_interface_identity is None:
                raise DurableSplitPortfolioError(
                    "portfolio edge omitted its public interface"
                )
            observed.add(
                (
                    revisions[edge.consumer_revision.uri],
                    revisions[edge.provider_revision.uri],
                    edge.capability,
                )
            )
    return frozenset(observed)


def resolve_durable_split_portfolio(
    sample_root: Path, *, platform: str
) -> ResolvedDurableSplitPortfolio:
    """Resolve both ordinary root locks and prove their exact four-node union."""

    if platform not in {"linux", "macos", "windows"}:
        raise DurableSplitPortfolioError(f"unsupported portfolio platform: {platform}")
    root = sample_root.resolve(strict=True)
    harness = root.parent / "_harness" / root.name
    metadata = json.loads((harness / "sample.json").read_bytes())
    manifest = load_durable_split_portfolio(harness, metadata)
    paths = {
        role: _component_root(root, coordinate)
        for role, coordinate in ROLE_COORDINATES.items()
    }
    os_selector = f"+os:{platform}"
    frontend = _root_authority(
        paths["frontend"],
        target_name=f"durable-split-frontend-{platform}",
        selectors=(
            os_selector,
            f"{ROLE_COORDINATES['frontend']}::+language:javascript",
            f"{ROLE_COORDINATES['api']}::+language:python",
            f"{ROLE_COORDINATES['cache']}::+language:python",
        ),
    )
    collector = _root_authority(
        paths["collector"],
        target_name=f"durable-split-collector-{platform}",
        selectors=(
            os_selector,
            f"{ROLE_COORDINATES['collector']}::+language:python",
            f"{ROLE_COORDINATES['cache']}::+language:python",
        ),
    )
    locks = (frontend.lock, collector.lock)
    revisions_by_coordinate: dict[str, set[str]] = {}
    for lock in locks:
        for node in lock.nodes:
            revisions_by_coordinate.setdefault(node.revision.coordinate.uri, set()).add(
                node.revision.identity.uri
            )
    if set(revisions_by_coordinate) != set(ROLE_COORDINATES.values()) or any(
        len(identities) != 1 for identities in revisions_by_coordinate.values()
    ):
        raise DurableSplitPortfolioError(
            "portfolio locks do not preserve exactly four stable Component revisions"
        )
    if _lock_edges(locks) != EDGE_COORDINATES:
        raise DurableSplitPortfolioError(
            "portfolio lock union differs from its public capability manifest"
        )
    graphs = tuple(CycloneDxManagedGraph.from_component_lock(lock) for lock in locks)
    graph_components = {
        item.name
        for graph in graphs
        for item in graph.components
        if item.kind in {ManagedComponentKind.ROOT, ManagedComponentKind.COMPONENT}
    }
    if graph_components != set(ROLE_COORDINATES.values()):
        raise DurableSplitPortfolioError(
            "CycloneDX lock projections lost a portfolio Component"
        )
    source_bom_identities = []
    for graph in graphs:
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
        )
        checked = validate_cyclonedx_bom(
            content,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=graph,
        )
        if checked != binding:
            raise DurableSplitPortfolioError(
                "CycloneDX source binding did not round-trip"
            )
        source_bom_identities.append(binding.bom_identity)
    return ResolvedDurableSplitPortfolio(
        manifest,
        frontend,
        collector,
        graphs,
        tuple(source_bom_identities),
    )


class _PortableComponentAcceptanceOracle:
    """Project a pinned sample harness into the Standard portable oracle port."""

    def __init__(
        self,
        component_root: Path,
        harness: DurableSplitComponentHarness,
        root_revision: ContentIdentity,
    ) -> None:
        self.root_revision = root_revision
        self._cases, verifier_identity = _component_acceptance_cases(
            component_root, harness
        )
        self._identity = canonical_identity(
            {
                "schema": "literate-ai/durable-split-portable-oracle@1",
                "component_revision": root_revision.uri,
                "verifier": verifier_identity.uri,
            }
        )

    @property
    def identity(self) -> ContentIdentity:
        return self._identity

    def cases(self, component_lock: ComponentLock):
        if component_lock.root_revision != self.root_revision:
            raise DurableSplitPortfolioError(
                "portable oracle received a different Component root"
            )
        return self._cases


def _component_acceptance_cases(
    component_root: Path,
    harness: DurableSplitComponentHarness,
) -> tuple[tuple[LocalIndependentAcceptanceCase, ...], ContentIdentity]:
    """Load one pinned execution/oracle pair through the ordinary sample parser."""

    from tests.conformance.support.sample_runner import (
        _execution_contract,
        _execution_oracle,
        _load_markdown_component,
    )

    definition, loaded, _authoring = _load_markdown_component(component_root)
    contract, execution_document = _execution_contract(
        component_root, definition, loaded
    )
    if (
        harness.execution_path
        != (
            component_root.parent
            / "_harness"
            / component_root.name
            / "acceptance"
            / "execution.json"
        ).resolve(strict=True)
        or ContentIdentity.parse_uri(execution_document.identity)
        != harness.execution_reference.identity
    ):
        raise DurableSplitPortfolioError(
            f"{harness.role} execution parser differs from its portfolio pin"
        )
    invocations = contract.get("invocations")
    if not isinstance(invocations, list):
        raise DurableSplitPortfolioError(
            f"{harness.role} execution interface omitted its invocations"
        )
    invocation_ids = [item["case_id"] for item in invocations]
    local_oracle_reference = ContentReference(
        harness.oracle_reference.kind,
        "acceptance/oracle.json",
        harness.oracle_reference.identity,
    )
    oracle, _reference = _execution_oracle(
        component_root,
        local_oracle_reference,
        harness.execution_reference.identity,
        invocation_ids,
        contract["result_shape"],
    )
    if harness.oracle_path != (
        component_root.parent
        / "_harness"
        / component_root.name
        / "acceptance"
        / "oracle.json"
    ).resolve(strict=True):
        raise DurableSplitPortfolioError(
            f"{harness.role} oracle parser differs from its portfolio pin"
        )
    expected_by_case = {
        item["case_id"]: item["expected_result"] for item in oracle["oracle_results"]
    }
    cases = tuple(
        LocalIndependentAcceptanceCase.create(
            item["case_id"],
            item["arguments"],
            expected_by_case[item["case_id"]],
        )
        for item in invocations
    )
    return cases, canonical_identity(
        {
            "schema": "literate-ai/durable-split-component-verifier@1",
            "role": harness.role,
            "execution": harness.execution_reference.identity.uri,
            "oracle": harness.oracle_reference.identity.uri,
        }
    )


class _SemanticHtmlParser(HTMLParser):
    """Extract only the static semantic facts this non-browser driver can observe."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.landmarks: set[str] = set()
        self.regions: list[tuple[str | None, str | None]] = []
        self._stack: list[tuple[str, str | None]] = []
        self._text_by_id: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        self._stack.append((tag, element_id))
        if element_id is not None:
            self._text_by_id.setdefault(element_id, [])
        role = attributes.get("role")
        if tag == "main" or role == "main":
            self.landmarks.add("main")
        if role == "region" or (
            tag in {"section", "aside"}
            and ("aria-label" in attributes or "aria-labelledby" in attributes)
        ):
            self.regions.append(
                (attributes.get("aria-label"), attributes.get("aria-labelledby"))
            )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for _tag, element_id in self._stack:
            if element_id is not None:
                self._text_by_id.setdefault(element_id, []).append(data)

    def observed_regions(self) -> tuple[tuple[str, str], ...]:
        observed = []
        for label, labelled_by in self.regions:
            name = label
            if name is None and labelled_by is not None:
                name = " ".join(
                    " ".join(self._text_by_id.get(identifier, ())).strip()
                    for identifier in labelled_by.split()
                ).strip()
            if name:
                observed.append(("region", " ".join(name.split())))
        return tuple(sorted(set(observed)))


class _SemanticHtmlDriver:
    """HTTP/HTML semantic adapter used only for the portfolio lifecycle gate.

    It does not claim JavaScript, layout, console, or interaction observations. The
    portfolio's separately executed HTTP flow proves proxy behavior; ADR 0028 remains
    the authority for full browser interaction acceptance.
    """

    @property
    def tool_identity(self) -> ContentIdentity:
        return canonical_identity(
            {"adapter": "literate-ai.semantic-html-http-driver", "version": 1}
        )

    def observe(self, base_url: str, viewport, contract) -> BrowserObservation:
        if (
            contract.steps
            or contract.postconditions
            or contract.reject_horizontal_overflow
        ):
            raise DurableSplitPortfolioError(
                "semantic HTML driver received a browser-only observation contract"
            )
        content = _http_bytes(base_url + "/", timeout_seconds=10.0)
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise DurableSplitPortfolioError("frontend document is not UTF-8") from exc
        parser = _SemanticHtmlParser()
        parser.feed(text)
        return BrowserObservation(
            viewport.width,
            viewport.height,
            viewport.width,
            request_count=0,
            observed_roles=parser.observed_regions(),
            observed_landmarks=tuple(sorted(parser.landmarks)),
            accessibility_snapshot=canonical_json_bytes(
                {
                    "landmarks": sorted(parser.landmarks),
                    "roles": [list(item) for item in parser.observed_regions()],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class _ExecutedRoot:
    role: str
    authority: DurableSplitRootAuthority
    lifecycle: StandardProjectLifecycleResult
    lifecycle_ports: Any
    report: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _RootGenerationAuthority:
    """Exact shared inputs for planning or executing one portfolio root."""

    snapshot: Any
    model_bindings: Mapping[str, Any]
    execution_plan: Any
    revisions: Mapping[str, Any]
    roots_by_revision: Mapping[str, Path]
    invocation_arguments: Mapping[str, list[object]]


def _root_generation_authority(
    *,
    sample_root: Path,
    portfolio: ResolvedDurableSplitPortfolio,
    root: DurableSplitRootAuthority,
    source_generator: Any,
    pipeline_model: str | None,
) -> _RootGenerationAuthority:
    """Resolve the immutable node authority used on both sides of generation."""

    from tests.conformance.support.sample_runner import (
        _locked_standard_sample_model_bindings,
        _StandardSampleAuthoritySnapshot,
    )

    snapshot = _StandardSampleAuthoritySnapshot(root.authority, root.catalog)
    selection = source_generator.selection
    model_bindings = _locked_standard_sample_model_bindings(
        snapshot,
        coding_cli=selection.name,
        pipeline_model=pipeline_model,
    )
    execution_plan = StandardProjectApplicationService.plan(
        root.lock,
        model_identities={
            revision: binding.identity for revision, binding in model_bindings.items()
        },
    )
    revisions = {node.revision.identity.uri: node.revision for node in root.lock.nodes}
    roots_by_revision: dict[str, Path] = {}
    invocation_arguments: dict[str, list[object]] = {}
    for revision_uri, revision in revisions.items():
        node_role = next(
            name
            for name, coordinate in ROLE_COORDINATES.items()
            if coordinate == revision.coordinate.uri
        )
        component_root = _component_root(sample_root, revision.coordinate.uri)
        roots_by_revision[revision_uri] = component_root
        cases, _identity = _component_acceptance_cases(
            component_root, portfolio.manifest.harness_for(node_role)
        )
        arguments = json.loads(cases[0].arguments_document)
        if not isinstance(arguments, list):
            raise DurableSplitPortfolioError(
                f"{node_role} verifier arguments are not an array"
            )
        invocation_arguments[revision_uri] = arguments
    return _RootGenerationAuthority(
        snapshot,
        model_bindings,
        execution_plan,
        revisions,
        roots_by_revision,
        invocation_arguments,
    )


def _root_generation_invocation(
    *,
    portfolio: ResolvedDurableSplitPortfolio,
    root: DurableSplitRootAuthority,
    authority: _RootGenerationAuthority,
    source_generator: Any,
    node: Any,
) -> CodingCliSourceGenerationInvocation:
    """Construct the exact source invocation shared by planning and execution."""

    from tests.conformance.support.sample_runner import (
        _load_markdown_component,
        _standard_sample_stage_request,
    )

    revision = authority.revisions[node.plan.component_revision.uri]
    component_root = authority.roots_by_revision[node.plan.component_revision.uri]
    definition, _loaded, _authoring = _load_markdown_component(component_root)
    if definition.coordinate != revision.coordinate:
        raise DurableSplitPortfolioError(
            "generation root differs from its locked Component"
        )
    model_plan, request = _standard_sample_stage_request(
        sample_root=component_root,
        node=node,
        revision=revision,
        source_generator=source_generator,
        forbidden_acceptance_arguments=authority.invocation_arguments[
            node.plan.component_revision.uri
        ],
    )
    return CodingCliSourceGenerationInvocation.create(
        model_plan,
        request,
        application_root_revision_identity=root.lock.root_revision,
        readiness_identity=canonical_identity(
            {
                "durable-split-readiness": node.plan.component_revision.uri,
                "portfolio": portfolio.identity.uri,
            }
        ),
    )


def plan_durable_split_service_derivations(
    *,
    sample_root: Path,
    source_generator: Any,
    scratch: Path,
    pipeline_model: str | None = None,
) -> tuple[tuple[SourceDerivationCacheKey, ContentIdentity], ...]:
    """Plan both root locks without generating, building, or running product code."""

    from tests.conformance.support.sample_runner import (
        _host_os,
        _prepare_standard_sample_project,
    )

    portfolio = resolve_durable_split_portfolio(sample_root, platform=_host_os())
    results: list[tuple[SourceDerivationCacheKey, ContentIdentity]] = []
    for role, root in (
        ("frontend", portfolio.frontend),
        ("collector", portfolio.collector),
    ):
        authority = _root_generation_authority(
            sample_root=sample_root,
            portfolio=portfolio,
            root=root,
            source_generator=source_generator,
            pipeline_model=pipeline_model,
        )
        prepared = _prepare_standard_sample_project(
            snapshot=authority.snapshot,
            execution_plan=authority.execution_plan,
            source_root=scratch / f"{role}-root" / "workspaces",
            coding_cli=source_generator.selection.name,
            pipeline_model=pipeline_model,
        )
        for node in prepared.nodes:
            invocation = _root_generation_invocation(
                portfolio=portfolio,
                root=root,
                authority=authority,
                source_generator=source_generator,
                node=node,
            )
            results.append(
                (
                    source_generator.derivation_cache_key(
                        node.recipe,
                        execution_plan=invocation.execution_plan,
                        stage_request=invocation.stage_request,
                        bounded_prompt=node.request.prompt,
                    ),
                    root.lock.identity,
                )
            )
    return tuple(results)


def _execute_root(
    *,
    sample_root: Path,
    portfolio: ResolvedDurableSplitPortfolio,
    root: DurableSplitRootAuthority,
    role: str,
    source_generator: Any,
    scratch: Path,
    object_root: Path,
    pipeline_model: str | None,
) -> _ExecutedRoot:
    """Generate, build, test, smoke, and independently accept one ordinary lock."""

    from tests.conformance.support.sample_runner import (
        SampleFailure,
        _standard_sample_budget,
        project_standard_sample_execution_report,
    )

    authority = _root_generation_authority(
        sample_root=sample_root,
        portfolio=portfolio,
        root=root,
        source_generator=source_generator,
        pipeline_model=pipeline_model,
    )
    snapshot = authority.snapshot
    selection = source_generator.selection
    model_bindings = authority.model_bindings
    execution_plan = authority.execution_plan
    roots_by_revision = authority.roots_by_revision

    generation_errors: dict[str, str] = {}

    def invocation(node):
        return _root_generation_invocation(
            portfolio=portfolio,
            root=root,
            authority=authority,
            source_generator=source_generator,
            node=node,
        )

    source_runner = CachedCodingCliSourceGenerationRunner(
        source_generator,
        cas=FileSystemCAS(scratch / "source-generation-cas"),
        invocation_provider=invocation,
    )

    def generate_node(node):
        try:
            return source_runner(node)
        except Exception as exc:
            generation_errors[node.plan.component_revision.uri] = (
                f"{type(exc).__name__}: {getattr(exc, 'code', '')}"
            )
            raise

    source_trees = LocalSourceTreeRegistry()
    toolchain_closure = project_locked_standard_toolchain_closure(
        snapshot, execution_plan
    )
    if role == "frontend":
        oracle: object = load_browser_interaction_acceptance(
            portfolio.manifest.browser_path,
            Path(ROLE_COORDINATES["frontend"]).name,
        )
        browser_driver: object | None = _SemanticHtmlDriver()
        independent_case_count = len(oracle.viewports)
    elif role == "collector":
        oracle = _PortableComponentAcceptanceOracle(
            roots_by_revision[root.lock.root_revision.uri],
            portfolio.manifest.harness_for("collector"),
            root.lock.root_revision,
        )
        browser_driver = None
        independent_case_count = len(oracle.cases(root.lock))
    else:
        raise DurableSplitPortfolioError(f"unsupported portfolio root role: {role}")
    runtime = assemble_filesystem_standard_project_runtime(
        generator=generate_node,
        object_root=object_root,
        toolchain_closure=toolchain_closure,
        source_trees=source_trees,
        indexer=DisabledGenerationIndexer(source_trees, artifact_root=None),
        source_cache_publisher=source_runner,
        independent_acceptance_oracle=oracle,
        browser_driver=browser_driver,
        node_preparation=LockedComponentNodePreparationAdapter(
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=pipeline_model
            ),
            coding_cli=selection.name,
        ),
    )
    ordered_revisions = tuple(
        plan.component_revision for plan in execution_plan.generation_plans
    )
    try:
        executed = runtime.execute(
            snapshot,
            StandardProjectExecutionRequest(
                PlannedStandardProject(selection, execution_plan),
                scratch / "workspaces",
                ComponentInvalidationDecision(
                    f"durable-split-{role}",
                    root.lock.root_revision,
                    ComponentChangeSurface.LOCAL_AUTHORITY,
                    ordered_revisions,
                    ordered_revisions,
                    ordered_revisions,
                ),
                max_parallelism=2,
                budget=_standard_sample_budget(),
            ),
        )
    except LocalStandardLifecycleError as exc:
        raise SampleFailure(f"durable split {role} lifecycle failed: {exc}") from exc
    if not executed.lifecycle.successful:
        failures = {
            item.component_revision.uri: item.failure_code
            for item in executed.lifecycle.node_results
        }
        raise SampleFailure(
            f"durable split {role} lifecycle failed: {failures!r}; "
            f"generation={generation_errors!r}; "
            f"build={runtime.lifecycle_ports.failure_diagnostics!r}"
        )
    report = project_standard_sample_execution_report(
        executed.lifecycle,
        component_lock=root.lock,
        sample_id=f"durable-split-{role}",
        variant_id="host",
        independent_acceptance_case_count=independent_case_count,
        model_bindings=model_bindings,
    )
    return _ExecutedRoot(
        role,
        root,
        executed.lifecycle,
        runtime.lifecycle_ports,
        report,
    )


def _command_for(root: _ExecutedRoot, role: str) -> LocalResolvedExecutionCommand:
    coordinate = ROLE_COORDINATES[role]
    revision = next(
        node.revision.identity
        for node in root.authority.lock.nodes
        if node.revision.coordinate.uri == coordinate
    )
    build_plan = root.lifecycle.project_build_plan
    if build_plan is None:
        raise DurableSplitPortfolioError("accepted root omitted its build plan")
    plan = next(
        item for item in build_plan.components if item.component_revision == revision
    )
    result = next(
        item
        for item in root.lifecycle.node_results
        if item.component_revision == revision
    )
    return root.lifecycle_ports.execution_command(plan, result.exports)


def _run_json(
    command: LocalResolvedExecutionCommand,
    request: Mapping[str, object],
) -> dict[str, object]:
    arguments = (
        *command.argv,
        canonical_json_bytes([dict(request)]).decode("utf-8"),
    )
    try:
        completed = run_bounded_process(
            arguments,
            cwd=command.cwd,
            environment=_child_process_environment(dict(command.environment)),
            timeout_seconds=60.0,
            stdout_limit_bytes=1024 * 1024,
            stderr_limit_bytes=1024 * 1024,
            error_prefix="durable_split_flow",
        )
    except BuildError as exc:
        raise DurableSplitPortfolioError(
            f"generated portfolio command could not complete: {exc.code}"
        ) from exc
    if completed.returncode != 0:
        raise DurableSplitPortfolioError(
            f"generated portfolio command exited {completed.returncode}"
        )
    payload = completed.stdout.rstrip(b"\r\n")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DurableSplitPortfolioError(
            "generated portfolio command did not return JSON"
        ) from exc
    if not isinstance(value, dict):
        raise DurableSplitPortfolioError(
            "generated portfolio command result is not one JSON object"
        )
    return value


def _finite_json_identity(value: object) -> str:
    """Bind finite product JSON into canonical evidence without binary floats."""

    def project(item: object) -> object:
        if isinstance(item, float):
            if not math.isfinite(item):
                raise DurableSplitPortfolioError(
                    "generated portfolio result contains a non-finite number"
                )
            decimal = Decimal(str(item))
            return {
                "encoding": "decimal-v1",
                "value": (
                    "0" if decimal.is_zero() else format(decimal.normalize(), "f")
                ),
            }
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise DurableSplitPortfolioError(
                    "generated portfolio result contains a non-string object key"
                )
            return {key: project(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [project(nested) for nested in item]
        return item

    return canonical_identity(project(value)).uri


_READY_HEALTH_STATUSES = frozenset({200, 204})


def _http_exchange(
    url: str,
    *,
    timeout_seconds: float,
    allowed_statuses: frozenset[int],
) -> bytes:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status not in allowed_statuses:
                raise DurableSplitPortfolioError(
                    f"generated service returned HTTP {response.status}"
                )
            body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise DurableSplitPortfolioError(
            f"generated service returned HTTP {exc.code}"
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise DurableSplitPortfolioError(
            "generated service request did not complete"
        ) from exc
    if len(body) > 1024 * 1024:
        raise DurableSplitPortfolioError(
            "generated service response exceeded its bound"
        )
    return body


def _http_bytes(url: str, *, timeout_seconds: float = 10.0) -> bytes:
    return _http_exchange(
        url, timeout_seconds=timeout_seconds, allowed_statuses=frozenset({200})
    )


def _http_health(url: str, *, timeout_seconds: float = 10.0) -> None:
    """Liveness only: the capability requires answering GET /health, not a body."""

    _http_exchange(
        url,
        timeout_seconds=timeout_seconds,
        allowed_statuses=_READY_HEALTH_STATUSES,
    )


def _http_json(url: str) -> dict[str, object]:
    content = _http_bytes(url)
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DurableSplitPortfolioError(
            "generated service response is not JSON"
        ) from exc
    if not isinstance(value, dict):
        raise DurableSplitPortfolioError(
            "generated service response is not a JSON object"
        )
    return value


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _service_stderr_excerpt(stream) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - 16 * 1024))
    lines = stream.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        collapsed = " ".join(line.split())
        if collapsed and set(collapsed) != {"-"}:
            return collapsed[-500:]
    return ""


@contextmanager
def _running_service(
    command: LocalResolvedExecutionCommand,
    arguments_for_port,
) -> Iterator[str]:
    port = _reserve_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    arguments = (
        *command.argv,
        "--litai-serve",
        *(str(item) for item in arguments_for_port(port)),
    )
    environment = _child_process_environment(dict(command.environment))
    ownership = create_process_tree_ownership()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                arguments,
                cwd=command.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                **ownership.popen_options,
            )
        except OSError as exc:
            ownership.release()
            raise DurableSplitPortfolioError(
                "generated service process could not start"
            ) from exc
        ownership.bind(process.pid)
        failure: Exception | None = None
        try:
            deadline = time.monotonic() + 15.0
            while True:
                if process.poll() is not None:
                    raise DurableSplitPortfolioError(
                        "generated service exited during readiness: "
                        f"{process.returncode}"
                    )
                try:
                    _http_health(base_url + "/health", timeout_seconds=0.5)
                    break
                except DurableSplitPortfolioError as exc:
                    if time.monotonic() >= deadline:
                        raise DurableSplitPortfolioError(
                            "generated service did not become ready"
                        ) from exc
                    time.sleep(0.05)
            yield base_url
        except Exception as exc:
            failure = exc
        finally:
            terminate_process_tree(process, ownership=ownership)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)
            if process.poll() is None:
                process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5.0)
            ownership.release()
        if failure is not None:
            message = str(failure)
            excerpt = _service_stderr_excerpt(stderr)
            if excerpt:
                message += f"; service stderr: {excerpt}"
            raise DurableSplitPortfolioError(message) from failure


class _UpstreamFixture:
    """Bounded loopback fixture with an observable request/lease rendezvous."""

    def __init__(self, payload: Mapping[str, object], *, block: bool = False) -> None:
        self._payload = dict(payload)
        try:
            self._content = json.dumps(
                self._payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DurableSplitPortfolioError(
                "upstream fixture payload is not finite JSON"
            ) from exc
        self._lock = threading.Lock()
        self._request_count = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with fixture._lock:
                    fixture._request_count += 1
                fixture.entered.set()
                if not fixture.release.wait(timeout=30.0):
                    self.send_error(503)
                    return
                content = fixture._content
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="literate-ai-durable-upstream",
            daemon=True,
        )

    @property
    def url(self) -> str:
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}/snapshot"

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def __enter__(self) -> _UpstreamFixture:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise DurableSplitPortfolioError("upstream fixture did not stop")


def _collector_request(
    *,
    database: Path,
    upstream_url: str,
    window: str,
    owner: str,
    now: str,
    retry_limit: int = 3,
    fail_after: int | None = None,
) -> dict[str, object]:
    request: dict[str, object] = {
        "database": os.fspath(database),
        "upstream_url": upstream_url,
        "window": window,
        "owner": owner,
        "now": now,
        "lease_seconds": 30,
        "retry_limit": retry_limit,
    }
    if fail_after is not None:
        request["fail_after"] = fail_after
    return request


def _require_durable_schema(database: Path) -> None:
    """Verify deterministic cache DDL before running independently generated peers."""

    try:
        uri = database.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (OSError, sqlite3.Error) as exc:
        raise DurableSplitPortfolioError(
            "durable cache schema is unavailable after migration"
        ) from exc
    try:
        for table, expected in _DURABLE_SCHEMA_COLUMNS.items():
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            observed = tuple(
                sorted(
                    (
                        str(row[1]),
                        str(row[2]).upper(),
                        bool(row[3])
                        or (bool(row[5]) and str(row[2]).upper() == "INTEGER"),
                        int(row[5]),
                    )
                    for row in rows
                )
            )
            if observed != tuple(sorted(expected)):
                raise DurableSplitPortfolioError(
                    f"durable cache table {table!r} violates its public column contract"
                )
        foreign_keys = frozenset(
            (
                table,
                str(row[3]),
                str(row[2]),
                str(row[4]),
            )
            for table in _DURABLE_SCHEMA_COLUMNS
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        )
        if foreign_keys != _DURABLE_SCHEMA_FOREIGN_KEYS:
            raise DurableSplitPortfolioError(
                "durable cache schema violates its public foreign-key contract"
            )
        current_rows = connection.execute(
            "SELECT id, snapshot_id FROM current_snapshot"
        ).fetchall()
        if current_rows:
            raise DurableSplitPortfolioError(
                "durable cache migration created a current snapshot before publication"
            )
    except sqlite3.Error as exc:
        raise DurableSplitPortfolioError(
            "durable cache schema cannot be inspected safely"
        ) from exc
    finally:
        connection.close()


def _require_collector_result(
    value: Mapping[str, object],
    *,
    status: str,
    window: str,
    owner: str,
) -> None:
    if set(value) != {"status", "window", "owner", "metric_count", "retry_count"}:
        raise DurableSplitPortfolioError(
            f"collector result violated the {status!r} field contract"
        )
    if value.get("status") != status or value.get("window") != window:
        raise DurableSplitPortfolioError(
            f"collector result violated the {status!r} outcome contract"
        )
    if value.get("owner") != owner:
        raise DurableSplitPortfolioError(
            f"collector {status!r} result owner did not echo the invocation owner"
        )
    if (
        isinstance(value.get("metric_count"), bool)
        or not isinstance(value.get("metric_count"), int)
        or isinstance(value.get("retry_count"), bool)
        or not isinstance(value.get("retry_count"), int)
    ):
        raise DurableSplitPortfolioError(
            f"collector result violated the {status!r} count contract"
        )


def _require_snapshot(
    response: Mapping[str, object],
    *,
    window: str,
    metrics: Mapping[str, object],
) -> None:
    if set(response) != {"snapshot", "freshness", "progress"}:
        raise DurableSplitPortfolioError("snapshot API returned unexpected fields")
    snapshot = response.get("snapshot")
    freshness = response.get("freshness")
    progress = response.get("progress")
    if not isinstance(snapshot, dict) or not isinstance(freshness, dict):
        raise DurableSplitPortfolioError("snapshot API omitted a published snapshot")
    observed_metrics = snapshot.get("metrics")
    if (
        snapshot.get("window") != window
        or not isinstance(snapshot.get("published_at"), str)
        or observed_metrics != dict(sorted(metrics.items()))
        or freshness.get("window") != window
        or freshness.get("published_at") != snapshot.get("published_at")
        or not isinstance(progress, dict)
    ):
        raise DurableSplitPortfolioError(
            "snapshot API did not expose the expected durable snapshot"
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in observed_metrics.values()
    ):
        raise DurableSplitPortfolioError("snapshot API returned a non-finite metric")


def _expire_lease(database: Path, *, window: str, now: str) -> None:
    try:
        with closing(sqlite3.connect(database, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA busy_timeout=5000")
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(collection_window)"
                ).fetchall()
            }
            expected = {
                "window",
                "selected_at",
                "state",
                "lease_owner",
                "lease_expires_at",
                "retry_count",
                "retry_limit",
                "last_error_class",
                "started_at",
                "completed_at",
            }
            if columns != expected:
                raise DurableSplitPortfolioError(
                    "generated cache does not expose the exact collection_window schema"
                )
            connection.execute(
                "INSERT INTO collection_window "
                "(window, selected_at, state, lease_owner, lease_expires_at, "
                "retry_count, retry_limit, last_error_class, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    window,
                    now,
                    "running",
                    "expired-owner",
                    "2026-01-01T00:00:00Z",
                    0,
                    3,
                    None,
                    now,
                    None,
                ),
            )
    except sqlite3.Error as exc:
        raise DurableSplitPortfolioError(
            "could not inject the expired-lease regression state"
        ) from exc


def _execute_cross_process_flow(
    *,
    portfolio: ResolvedDurableSplitPortfolio,
    frontend: _ExecutedRoot,
    collector: _ExecutedRoot,
    scratch: Path,
) -> dict[str, object]:
    """Exercise persistence, failure, lease, retry, and freshness contracts."""

    cache_command = _command_for(collector, "cache")
    collector_command = _command_for(collector, "collector")
    api_command = _command_for(frontend, "api")
    frontend_command = _command_for(frontend, "frontend")
    database = scratch / "state" / "snapshots.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        migration = _run_json(
            cache_command, {"action": "migrate", "database": os.fspath(database)}
        )
        if migration != {"status": "migrated"}:
            raise DurableSplitPortfolioError("cache migration is not idempotent")
    _require_durable_schema(database)

    try:
        fixture_document = json.loads(portfolio.manifest.upstream_path.read_bytes())
        snapshots = fixture_document["snapshots"]
        first_metrics = snapshots["first"]
        replacement_metrics = snapshots["replacement"]
        recovery_metrics = snapshots["recovery"]
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise DurableSplitPortfolioError("upstream fixture shape is invalid") from exc
    if any(
        not isinstance(item, dict)
        for item in (first_metrics, replacement_metrics, recovery_metrics)
    ):
        raise DurableSplitPortfolioError("upstream fixture metrics are invalid")

    first_window = "2026-01-01T00:00:00Z"
    with _UpstreamFixture(first_metrics) as upstream:
        initial = _run_json(
            collector_command,
            _collector_request(
                database=database,
                upstream_url=upstream.url,
                window=first_window,
                owner="collector-initial",
                now="2026-01-01T00:00:01Z",
            ),
        )
        _require_collector_result(
            initial,
            status="published",
            window=first_window,
            owner="collector-initial",
        )
        if upstream.request_count != 1:
            raise DurableSplitPortfolioError(
                "initial collector did not make exactly one upstream request"
            )
        with _running_service(
            api_command, lambda port: (port, os.fspath(database))
        ) as api_url:
            api_snapshot = _http_json(api_url + "/snapshot")
            _require_snapshot(api_snapshot, window=first_window, metrics=first_metrics)
            with _running_service(
                frontend_command, lambda port: (port, api_url)
            ) as frontend_url:
                proxied_snapshot = _http_json(frontend_url + "/api/snapshot")
                if proxied_snapshot != api_snapshot:
                    raise DurableSplitPortfolioError(
                        "frontend proxy changed the API snapshot"
                    )
                page = _http_bytes(frontend_url + "/").decode("utf-8")
                parser = _SemanticHtmlParser()
                parser.feed(page)
                if (
                    "main" not in parser.landmarks
                    or (
                        "region",
                        "Snapshot metrics",
                    )
                    not in parser.observed_regions()
                ):
                    raise DurableSplitPortfolioError(
                        "frontend page omitted its semantic snapshot boundary"
                    )
        first_request_count = upstream.request_count

    # Both readers restart after the upstream process has stopped. No collector is
    # launched here, so byte-for-byte JSON equality is direct persistence evidence.
    with _running_service(
        api_command, lambda port: (port, os.fspath(database))
    ) as restarted_api_url:
        restarted_snapshot = _http_json(restarted_api_url + "/snapshot")
        with _running_service(
            frontend_command, lambda port: (port, restarted_api_url)
        ) as restarted_frontend_url:
            restarted_proxy = _http_json(restarted_frontend_url + "/api/snapshot")
    if (
        restarted_snapshot != api_snapshot
        or restarted_proxy != api_snapshot
        or first_request_count != 1
    ):
        raise DurableSplitPortfolioError(
            "reader restart did not preserve the collection-independent snapshot"
        )

    failure_window = "2026-01-02T00:00:00Z"
    with _UpstreamFixture(replacement_metrics) as upstream:
        failure_results = []
        for index in range(2):
            result = _run_json(
                collector_command,
                _collector_request(
                    database=database,
                    upstream_url=upstream.url,
                    window=failure_window,
                    owner=f"collector-failure-{index + 1}",
                    now=f"2026-01-02T00:00:0{index + 1}Z",
                    retry_limit=2,
                    fail_after=1,
                ),
            )
            _require_collector_result(
                result,
                status="failed",
                window=failure_window,
                owner=f"collector-failure-{index + 1}",
            )
            failure_results.append(result)
        requests_before_exhausted = upstream.request_count
        exhausted = _run_json(
            collector_command,
            _collector_request(
                database=database,
                upstream_url=upstream.url,
                window=failure_window,
                owner="collector-exhausted",
                now="2026-01-02T00:00:03Z",
                retry_limit=2,
            ),
        )
        _require_collector_result(
            exhausted,
            status="failed",
            window=failure_window,
            owner="collector-exhausted",
        )
        if (
            requests_before_exhausted != 2
            or upstream.request_count != requests_before_exhausted
            or failure_results[-1]["retry_count"] != 2
            or exhausted["retry_count"] != 2
        ):
            raise DurableSplitPortfolioError(
                "collector retry limit did not prevent another upstream request"
            )
    with _running_service(
        api_command, lambda port: (port, os.fspath(database))
    ) as api_url:
        after_failure = _http_json(api_url + "/snapshot")
    _require_snapshot(after_failure, window=first_window, metrics=first_metrics)
    progress = after_failure["progress"]
    if (
        not isinstance(progress, dict)
        or progress.get("window") != failure_window
        or progress.get("state") != "failed"
        or progress.get("retry_count") != 2
    ):
        raise DurableSplitPortfolioError(
            "partial failure did not expose bounded retry progress"
        )

    concurrent_window = "2026-01-03T00:00:00Z"
    with _UpstreamFixture(replacement_metrics, block=True) as upstream:
        first_request = _collector_request(
            database=database,
            upstream_url=upstream.url,
            window=concurrent_window,
            owner="collector-winner",
            now="2026-01-03T00:00:01Z",
        )
        second_request = _collector_request(
            database=database,
            upstream_url=upstream.url,
            window=concurrent_window,
            owner="collector-loser",
            now="2026-01-03T00:00:02Z",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner_future = pool.submit(_run_json, collector_command, first_request)
            if not upstream.entered.wait(timeout=10.0):
                upstream.release.set()
                raise DurableSplitPortfolioError(
                    "collector contacted no upstream after acquiring its lease"
                )
            loser_future = pool.submit(_run_json, collector_command, second_request)
            try:
                loser = loser_future.result(timeout=5.0)
            except FutureTimeoutError as exc:
                upstream.release.set()
                raise DurableSplitPortfolioError(
                    "concurrent collector blocked behind live upstream work"
                ) from exc
            upstream.release.set()
            winner = winner_future.result(timeout=30.0)
        _require_collector_result(
            winner,
            status="published",
            window=concurrent_window,
            owner="collector-winner",
        )
        _require_collector_result(
            loser,
            status="lease-held",
            window=concurrent_window,
            owner="collector-loser",
        )
        if upstream.request_count != 1:
            raise DurableSplitPortfolioError(
                "concurrent collectors made more than one upstream request"
            )

    recovery_window = "2026-01-04T00:00:00Z"
    _expire_lease(
        database,
        window=recovery_window,
        now="2026-01-04T00:00:01Z",
    )
    with _UpstreamFixture(recovery_metrics) as upstream:
        recovery = _run_json(
            collector_command,
            _collector_request(
                database=database,
                upstream_url=upstream.url,
                window=recovery_window,
                owner="collector-recovery",
                now="2026-01-04T00:01:00Z",
            ),
        )
        _require_collector_result(
            recovery,
            status="published",
            window=recovery_window,
            owner="collector-recovery",
        )
        already_complete = _run_json(
            collector_command,
            _collector_request(
                database=database,
                upstream_url=upstream.url,
                window=recovery_window,
                owner="collector-repeat",
                now="2026-01-04T00:01:01Z",
            ),
        )
        _require_collector_result(
            already_complete,
            status="already-complete",
            window=recovery_window,
            owner="collector-repeat",
        )
        if upstream.request_count != 1:
            raise DurableSplitPortfolioError(
                "completed window contacted upstream more than once"
            )
    with _running_service(
        api_command, lambda port: (port, os.fspath(database))
    ) as api_url:
        final_snapshot = _http_json(api_url + "/snapshot")
    _require_snapshot(final_snapshot, window=recovery_window, metrics=recovery_metrics)
    final_progress = final_snapshot["progress"]
    if (
        not isinstance(final_progress, dict)
        or final_progress.get("window") != recovery_window
        or final_progress.get("state") not in {"complete", "completed", "published"}
    ):
        raise DurableSplitPortfolioError(
            "recovered collection did not expose completed freshness progress"
        )

    evidence = {
        "schema": "literate-ai/durable-split-cross-process-flow@1",
        "initial_status": initial["status"],
        "restart_snapshot_identity": _finite_json_identity(restarted_snapshot),
        "failure_statuses": [item["status"] for item in failure_results],
        "exhausted_retry_count": exhausted["retry_count"],
        "concurrent_statuses": sorted([winner["status"], loser["status"]]),
        "recovery_status": recovery["status"],
        "repeat_status": already_complete["status"],
        "final_snapshot_identity": _finite_json_identity(final_snapshot),
        "upstream_request_counts": {
            "initial": first_request_count,
            "failed_attempts": requests_before_exhausted,
            "concurrent": 1,
            "recovery": 1,
        },
    }
    return {**evidence, "identity": canonical_identity(evidence).uri}


EXPECTED_ASSERTIONS = (
    "four-boundary-public-capability-portfolio",
    "collector-only-upstream-access",
    "reader-restart-serves-persisted-snapshot",
    "partial-write-preserves-prior-snapshot",
    "single-winner-lease-and-expiry-recovery",
    "bounded-retry-and-freshness-progress",
    "component-locks-preserve-four-boundary-composition",
    "cyclonedx-preserves-four-boundary-composition",
    "generated-independent-flow-passes",
)


def execute_durable_split_service_portfolio(
    *,
    sample_root: Path,
    source_generator: Any,
    scratch: Path,
    object_root: Path,
    allow_host_execution: bool,
    pipeline_model: str | None = None,
) -> dict[str, object]:
    """Execute both root locks and their real cross-process persistence flow."""

    from tests.conformance.support.sample_runner import SampleFailure, _host_os

    if not allow_host_execution:
        raise SampleFailure(
            "durable split-service proof requires explicitly authorized host execution"
        )
    portfolio = resolve_durable_split_portfolio(sample_root, platform=_host_os())
    frontend = _execute_root(
        sample_root=sample_root,
        portfolio=portfolio,
        root=portfolio.frontend,
        role="frontend",
        source_generator=source_generator,
        scratch=scratch / "frontend-root",
        object_root=object_root / "frontend-root",
        pipeline_model=pipeline_model,
    )
    collector = _execute_root(
        sample_root=sample_root,
        portfolio=portfolio,
        root=portfolio.collector,
        role="collector",
        source_generator=source_generator,
        scratch=scratch / "collector-root",
        object_root=object_root / "collector-root",
        pipeline_model=pipeline_model,
    )
    flow = _execute_cross_process_flow(
        portfolio=portfolio,
        frontend=frontend,
        collector=collector,
        scratch=scratch / "cross-process-flow",
    )
    document: dict[str, object] = {
        "schema": "literate-ai/durable-split-service-execution@1",
        "proof_status": "passed",
        "portfolio_identity": portfolio.identity.uri,
        "component_lock_identities": [lock.identity.uri for lock in portfolio.locks],
        "source_bom_identities": [
            identity.uri for identity in portfolio.source_bom_identities
        ],
        "frontend_root": dict(frontend.report),
        "collector_root": dict(collector.report),
        "flow": flow,
        "assertions": list(EXPECTED_ASSERTIONS),
    }
    document["identity"] = canonical_identity(document).uri
    return document


def verify_durable_split_service_report(
    executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    """Fail closed if the sample runner did not retain one complete portfolio proof."""

    if len(executions) != 1:
        raise DurableSplitPortfolioError(
            "durable split-service requires one combined execution report"
        )
    report = executions[0]
    if (
        report.get("schema") != "literate-ai/durable-split-service-execution@1"
        or report.get("proof_status") != "passed"
        or report.get("assertions") != list(EXPECTED_ASSERTIONS)
        or not isinstance(report.get("frontend_root"), dict)
        or not isinstance(report.get("collector_root"), dict)
        or not isinstance(report.get("flow"), dict)
        or report["flow"].get("schema")
        != "literate-ai/durable-split-cross-process-flow@1"
    ):
        raise DurableSplitPortfolioError(
            "durable split-service execution report is incomplete"
        )
    return EXPECTED_ASSERTIONS


__all__ = [
    "DurableSplitPortfolioError",
    "DurableSplitComponentHarness",
    "DurableSplitPortfolioManifest",
    "EDGE_COORDINATES",
    "PORTFOLIO_SCHEMA",
    "ROLE_COORDINATES",
    "ROOT_COORDINATES",
    "ResolvedDurableSplitPortfolio",
    "execute_durable_split_service_portfolio",
    "load_durable_split_portfolio",
    "plan_durable_split_service_derivations",
    "resolve_durable_split_portfolio",
    "verify_durable_split_service_report",
]
