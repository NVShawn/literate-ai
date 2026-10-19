"""Native dependency drift custody for retained commands, not SDK admission.

The existing native observer inspects captured inputs and tools without executing
them. This owner freezes that observation and compares a fresh observation at
command boundaries. It does not infer original-language package requirements,
admit SDK products, verify a source/resolved BOM or issue acceptance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from literate_ai.adapters.dependencies import PortableHostDependencyObserver
from literate_ai.adapters.dependencies.types import (
    DependencyObservationError,
    HostDependencyObservation,
)
from literate_ai.adapters.freebsd_loader_paths import FreeBsdLoaderPaths
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.sbom import retained_project_bom_ref

_MAX_OBSERVATION_BYTES = 16 * 1024 * 1024


def _observer(commands, environment):
    return PortableHostDependencyObserver(
        toolchain_commands=commands,
        lifecycle_commands=(),
        windows_environment=environment,
        macos_loader_paths=MacOsLoaderPaths.from_environment(environment),
        linux_loader_paths=LinuxLoaderPaths.from_environment(environment),
        freebsd_loader_paths=FreeBsdLoaderPaths.from_environment(environment),
    )


class RetainedProjectDependencyError(ValueError):
    """Retained command dependency custody could not be established or retained."""


def _freeze(observation: HostDependencyObservation, root_ref: str) -> bytes:
    if not isinstance(observation, HostDependencyObservation):
        raise ValueError("retained.dependencies.observation-type")
    if not isinstance(observation.components, tuple) or not isinstance(
        observation.edges, tuple
    ):
        raise ValueError("retained.dependencies.observation-type")
    components = {}
    for component in observation.components:
        if not isinstance(component, dict):
            raise ValueError("retained.dependencies.component-invalid")
        ref = component.get("bom-ref")
        if not isinstance(ref, str) or not ref or ref == root_ref or ref in components:
            raise ValueError("retained.dependencies.component-invalid")
        components[ref] = component
    if not components:
        raise ValueError("retained.dependencies.observation-empty")
    edges = set()
    known = {*components, root_ref}
    for edge in observation.edges:
        if (
            not isinstance(edge, tuple)
            or len(edge) != 2
            or any(not isinstance(ref, str) for ref in edge)
            or edge in edges
            or edge[0] not in known
            or edge[1] not in components
        ):
            raise ValueError("retained.dependencies.edge-invalid")
        edges.add(edge)
    reached = {root_ref}
    pending = [root_ref]
    children = {}
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in reached:
                reached.add(child)
                pending.append(child)
    if not set(components) <= reached:
        raise ValueError("retained.dependencies.disconnected-observation")
    payload = canonical_json_bytes(
        {
            "components": [components[ref] for ref in sorted(components)],
            "edges": [list(edge) for edge in sorted(edges)],
        }
    )
    if len(payload) > _MAX_OBSERVATION_BYTES:
        raise ValueError("retained.dependencies.observation-budget")
    return payload


class RetainedProjectDependencyGuard:
    """One immutable observation of the actual captured-command environment."""

    def __init__(
        self,
        inputs: Path,
        manifest: RetainedProjectManifest,
        plan: RetainedProjectPlan,
        tools: tuple[LocalComponentToolBinding, ...],
        environment: Mapping[str, str],
    ) -> None:
        if not isinstance(manifest, RetainedProjectManifest) or not isinstance(
            plan, RetainedProjectPlan
        ):
            raise TypeError(
                "retained dependency custody requires typed inputs and plan"
            )
        if (
            manifest.identity != plan.manifest_identity
            or not isinstance(tools, tuple)
            or not tools
            or any(not isinstance(tool, LocalComponentToolBinding) for tool in tools)
        ):
            raise ValueError("retained.dependencies.input-mismatch")
        self._inputs = Path(inputs)
        self._manifest = manifest
        self._plan_identity = plan.identity
        self._root_ref = retained_project_bom_ref(plan.identity)
        expected = sorted(plan.profile.tools)
        members = {member.path: member for member in manifest.members}
        if len(tools) != len(expected):
            raise ValueError("retained.dependencies.tool-closure-mismatch")
        for binding, (_name, path, identity) in zip(tools, expected, strict=True):
            member = members.get(path)
            if (
                binding.command != (str(self._inputs / path),)
                or binding.environment
                or binding.authority_identity != identity
                or member is None
                or member.role not in {"sdk", "toolchain"}
                or member.mode != 0o755
                or ContentIdentity.parse_uri("sha256:" + member.sha256) != identity
            ):
                raise ValueError("retained.dependencies.tool-closure-mismatch")
        self._tools = tools
        if not isinstance(environment, Mapping):
            raise TypeError(
                "retained dependency environment must be an explicit mapping"
            )
        self._environment = dict(environment)
        self._canonical = self._observe()

    def _observe(self) -> bytes:
        try:
            verify_retained_project_inputs(self._inputs, self._manifest)
            for binding in self._tools:
                binding.require_unchanged()
            observer = _observer(
                tuple(binding.command for binding in self._tools), self._environment
            )
            observation = observer.observe(
                {"artifact_path": str(self._inputs)},
                root_ref=self._root_ref,
            )
            payload = _freeze(observation, self._root_ref)
            for binding in self._tools:
                binding.require_unchanged()
            verify_retained_project_inputs(self._inputs, self._manifest)
            return payload
        except (
            DependencyObservationError,
            LocalStandardLifecycleError,
            OSError,
            ValueError,
            TypeError,
            RecursionError,
        ) as exc:
            raise RetainedProjectDependencyError(
                "retained.dependencies.observation-rejected: " + str(exc)
            ) from exc

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-native-dependency-observation@1",
                "plan_identity": self._plan_identity.uri,
                "manifest_identity": self._manifest.identity.uri,
                "environment_identity": canonical_identity(self._environment).uri,
                "observation": json.loads(self._canonical),
            }
        )

    @property
    def observation(self) -> HostDependencyObservation:
        # Return a new object: HostDependencyObservation's dictionaries are mutable.
        value = json.loads(self._canonical)
        return HostDependencyObservation(
            tuple(value["components"]),
            tuple(tuple(edge) for edge in value["edges"]),
        )

    def require_unchanged(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        tools: tuple[LocalComponentToolBinding, ...] | None = None,
    ) -> None:
        if (environment is not None and dict(environment) != self._environment) or (
            tools is not None and tools != self._tools
        ):
            raise RetainedProjectDependencyError(
                "retained.dependencies.command-context-changed"
            )
        if self._observe() != self._canonical:
            raise RetainedProjectDependencyError(
                "retained.dependencies.observation-changed"
            )


def observe_retained_export_dependencies(
    root, manifest, plan, environment, *, allow_empty=False
):
    """Observe sealed exports; an explicitly data-only closure may have no images."""
    verify_retained_project_inputs(root, manifest)
    root_ref = retained_project_bom_ref(plan.identity)
    observation = _observer((), environment).observe(
        {"artifact_path": str(root)}, root_ref=root_ref
    )
    if (
        allow_empty
        and isinstance(observation, HostDependencyObservation)
        and (observation.components == () and observation.edges == ())
    ):
        payload = canonical_json_bytes({"components": [], "edges": []})
    else:
        payload = _freeze(observation, root_ref)
    verify_retained_project_inputs(root, manifest)
    value = json.loads(payload)
    return HostDependencyObservation(
        tuple(value["components"]), tuple(tuple(edge) for edge in value["edges"])
    )


def retained_export_dependency_identity(root, manifest, plan, environment):
    """Freeze actual exported native dependencies; this is not SDK/BOM admission."""
    observation = observe_retained_export_dependencies(
        root, manifest, plan, environment
    )
    root_ref = retained_project_bom_ref(plan.identity)
    payload = _freeze(observation, root_ref)
    return canonical_identity(
        {
            "schema": "literate-ai/retained-export-dependency-observation@1",
            "plan_identity": plan.identity.uri,
            "output_manifest_identity": manifest.identity.uri,
            "environment_identity": canonical_identity(dict(environment)).uri,
            "observation": json.loads(payload),
        }
    )
