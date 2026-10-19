"""Exact retained source/resolved BOM custody; not SDK or lifecycle admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.native_sdk_dependencies import (
    NativeSdkDependencyEvidence,
    merge_native_sdk_dependencies,
    merge_sdk_host_observations,
)
from literate_ai.adapters.retained_project_dependencies import _freeze
from literate_ai.contracts import (
    ComponentLock,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.sbom import (
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    ManagedComponentKind,
)


def _manifest_component(manifest, kind):
    identity = manifest.identity
    return {
        "type": "file",
        "bom-ref": f"urn:literate-ai:retained-{kind}-manifest:{identity.digest}",
        "name": f"retained-{kind}-manifest",
        "version": "sha256-" + identity.digest,
        "hashes": [{"alg": "SHA-256", "content": identity.digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "build"},
            {"name": "literate-ai:dependency-scope", "value": "build"},
            {"name": f"literate-ai:retained-{kind}-manifest", "value": identity.uri},
        ],
    }


def _declared_inputs(manifest):
    components = [_manifest_component(manifest, "input")]
    for member in manifest.members:
        if member.role not in {"sdk", "toolchain", "dependency"}:
            continue
        identity = canonical_identity(member.to_dict())
        kind = "toolchain" if member.role in {"sdk", "toolchain"} else "build"
        component = {
            "type": "file",
            "bom-ref": "urn:literate-ai:retained-declared-input:" + identity.digest,
            "name": member.path,
            "version": "sha256-" + member.sha256,
            "hashes": [{"alg": "SHA-256", "content": member.sha256}],
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": kind},
                {"name": "literate-ai:dependency-scope", "value": kind},
                {"name": "literate-ai:retained-member", "value": identity.uri},
            ],
        }
        components.append(component)
    return tuple(components)


@dataclass(frozen=True, slots=True)
class RetainedProjectSourceBom:
    plan: RetainedProjectPlan
    manifest: RetainedProjectManifest
    managed_graph: CycloneDxManagedGraph
    component_locks: tuple[ComponentLock, ...]
    content: bytes = field(init=False, repr=False)
    binding: CycloneDxBomBinding = field(init=False)

    def __post_init__(self):
        if (
            not isinstance(self.plan, RetainedProjectPlan)
            or not isinstance(self.manifest, RetainedProjectManifest)
            or not isinstance(self.managed_graph, CycloneDxManagedGraph)
        ):
            raise TypeError("retained.bom.typed-source-required")
        root = next(c for c in self.managed_graph.components if c.kind.is_root)
        if (
            self.manifest.identity != self.plan.manifest_identity
            or self.managed_graph.resolved_graph_identity != self.plan.identity
            or root.kind is not ManagedComponentKind.RETAINED_PROJECT
            or root.identity != self.plan.identity
            or root.name != self.plan.project_id
        ):
            raise ValueError("retained.bom.source-authority-mismatch")
        if self.managed_graph != project_retained_project_managed_graph(
            self.plan,
            self.component_locks,
            version=root.version,
        ):
            raise ValueError("retained.bom.selected-lock-graph-mismatch")
        if self.plan.profile.toolchain_identity != self.manifest.role_identity(
            {"sdk", "toolchain"}
        ) or self.plan.profile.dependency_identity != self.manifest.role_identity(
            {"dependency"}
        ):
            raise ValueError("retained.bom.declared-role-closure-mismatch")
        members = {member.path: member for member in self.manifest.members}
        for _name, path, identity in self.plan.profile.tools:
            member = members.get(path)
            if (
                member is None
                or member.mode != 0o755
                or member.role not in {"sdk", "toolchain"}
                or identity.uri != "sha256:" + member.sha256
            ):
                raise ValueError("retained.bom.declared-tool-mismatch")
        declared = _declared_inputs(self.manifest)
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=self.managed_graph,
            additional_components=declared,
            additional_edges=tuple(
                (root.bom_ref, item["bom-ref"]) for item in declared
            ),
        )
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "binding", binding)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/retained-source-bom-observation@1",
                "plan_identity": self.plan.identity.uri,
                "input_manifest_identity": self.manifest.identity.uri,
                "managed_graph_identity": self.managed_graph.identity.uri,
                "source_bom_identity": self.binding.bom_identity.uri,
                "sdk_admitted": False,
                "lifecycle_receipt": False,
            }
        )

    def require_content(self, content):
        if type(content) is not bytes or content != self.content:
            raise ValueError("retained.bom.source-content-changed")


def _frozen_export(observed, root_ref):
    if isinstance(observed, HostDependencyObservation) and (
        observed.components == () and observed.edges == ()
    ):
        return canonical_json_bytes({"components": [], "edges": []})
    return _freeze(observed, root_ref)


def _observation(content, root_ref, *, export=False):
    if type(content) is not bytes or len(content) > 16 * 1024 * 1024:
        raise ValueError("retained.bom.observation-budget")
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("retained.bom.observation-invalid") from None
    if not isinstance(value, dict) or set(value) != {"components", "edges"}:
        raise ValueError("retained.bom.observation-invalid")
    if not isinstance(value["components"], list) or not isinstance(
        value["edges"], list
    ):
        raise ValueError("retained.bom.observation-invalid")
    observed = HostDependencyObservation(
        tuple(value["components"]),
        tuple(tuple(edge) for edge in value["edges"]),
    )
    frozen = (
        _frozen_export(observed, root_ref) if export else _freeze(observed, root_ref)
    )
    if frozen != content:
        raise ValueError("retained.bom.observation-not-canonical")
    return observed


@dataclass(frozen=True, slots=True)
class RetainedProjectResolvedBom:
    source: RetainedProjectSourceBom
    output_manifest: RetainedProjectManifest
    tool_observation: bytes = field(repr=False)
    export_observation: bytes = field(repr=False)
    sdk_dependencies: tuple[NativeSdkDependencyEvidence, ...] = ()
    content: bytes = field(init=False, repr=False)
    binding: CycloneDxBomBinding = field(init=False)

    def __post_init__(self):
        if not isinstance(self.source, RetainedProjectSourceBom) or not isinstance(
            self.output_manifest, RetainedProjectManifest
        ):
            raise TypeError("retained.bom.typed-resolution-required")
        graph = self.source.managed_graph
        tools = _observation(self.tool_observation, graph.root_ref)
        exports = _observation(self.export_observation, graph.root_ref, export=True)
        observed = merge_sdk_host_observations(tools, exports)
        if type(self.sdk_dependencies) is not tuple or any(
            not isinstance(item, NativeSdkDependencyEvidence)
            for item in self.sdk_dependencies
        ):
            raise TypeError("retained.bom.typed-sdk-dependencies-required")
        sdk_observation, resolutions = merge_native_sdk_dependencies(
            self.sdk_dependencies
        )
        observed = merge_sdk_host_observations(observed, sdk_observation)
        declared = _declared_inputs(self.source.manifest)
        output = _manifest_component(self.output_manifest, "output")
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=graph,
            source_bom=self.source.content,
            additional_components=(*declared, output, *observed.components),
            additional_edges=(
                *((graph.root_ref, item["bom-ref"]) for item in (*declared, output)),
                *observed.edges,
            ),
            repository_resolutions=resolutions,
        )
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "binding", binding)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/retained-resolved-bom-observation@1",
                "source_observation_identity": self.source.identity.uri,
                "output_manifest_identity": self.output_manifest.identity.uri,
                "resolved_bom_identity": self.binding.bom_identity.uri,
                "tool_observation_identity": "sha256:"
                + hashlib.sha256(self.tool_observation).hexdigest(),
                "export_observation_identity": "sha256:"
                + hashlib.sha256(self.export_observation).hexdigest(),
                "sdk_admitted": False,
                "lifecycle_receipt": False,
                **(
                    {
                        "sdk_dependencies": [
                            item.identity.uri for item in self.sdk_dependencies
                        ]
                    }
                    if self.sdk_dependencies
                    else {}
                ),
            }
        )

    def require_content(self, content):
        if type(content) is not bytes or content != self.content:
            raise ValueError("retained.bom.resolved-content-changed")


def resolve_retained_project_bom(
    source, output_manifest, tools, exports, *, sdk_dependencies=()
):
    if not isinstance(source, RetainedProjectSourceBom):
        raise TypeError("retained.bom.typed-source-required")
    return RetainedProjectResolvedBom(
        source,
        output_manifest,
        _freeze(tools, source.managed_graph.root_ref),
        _frozen_export(exports, source.managed_graph.root_ref),
        sdk_dependencies,
    )
