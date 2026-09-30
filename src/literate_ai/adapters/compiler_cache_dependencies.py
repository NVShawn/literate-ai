"""Immutable native dependency evidence for an executable compiler-cache tool."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.dependencies import (
    HostDependencyObservation,
    PortableHostDependencyObserver,
)
from literate_ai.adapters.dependencies.observation import _normalized_observation
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths
from literate_ai.contracts import canonical_identity, canonical_json_bytes

_ROOT = "urn:literate-ai:compiler-cache-tool"


@dataclass(frozen=True, slots=True)
class CompilerCacheDependencies:
    # Store canonical bytes: observation dictionaries must not mutate a bound identity.
    document: bytes

    @classmethod
    def observe(cls, tool, environment: Mapping[str, str]):
        executable = Path(tool.executable)
        tool.require_unchanged()
        observation = PortableHostDependencyObserver(
            toolchain_commands=(tool.command,),
            artifact_files=(executable,),
            windows_environment=environment if sys.platform == "win32" else None,
            macos_loader_paths=(
                MacOsLoaderPaths.from_environment(environment)
                if sys.platform == "darwin"
                else None
            ),
            linux_loader_paths=(
                LinuxLoaderPaths.from_environment(environment)
                if sys.platform.startswith("linux")
                else None
            ),
        ).observe({"artifact_path": str(executable.parent)}, root_ref=_ROOT)
        tool.require_unchanged()
        if not observation.components or not observation.edges:
            raise ValueError("compiler-cache tool dependency closure is empty")
        return cls(
            canonical_json_bytes(
                {
                    "schema": "literate-ai/compiler-cache-dependencies@1",
                    "tool_identity": tool.toolchain_identity.uri,
                    "components": sorted(
                        observation.components, key=lambda item: item["bom-ref"]
                    ),
                    "edges": sorted(observation.edges),
                }
            )
        )

    @property
    def identity(self):
        return canonical_identity(json.loads(self.document))

    def include_in(self, observation: HostDependencyObservation):
        """Attach cache-tool provenance to the existing Component root.

        Cache runtime images have their own roles, distinct from compiler/toolchain
        images. Give those observations independent references to preserve both sets
        of facts when an underlying library is used in both roles.
        """
        refs = {item["bom-ref"] for item in observation.components}
        roots = {source for source, _ in observation.edges if source not in refs}
        if len(roots) != 1:
            raise ValueError("compiler-cache evidence requires one Component root")
        root = next(iter(roots))
        document = json.loads(self.document)
        remap = {
            item["bom-ref"]: "urn:literate-ai:cache-dependency:"
            + canonical_identity(item).digest
            for item in document["components"]
        }
        remap[_ROOT] = root
        return _normalized_observation(
            (
                *observation.components,
                *(
                    item | {"bom-ref": remap[item["bom-ref"]]}
                    for item in document["components"]
                ),
            ),
            (
                *observation.edges,
                *((remap[a], remap[b]) for a, b in document["edges"]),
            ),
        )
