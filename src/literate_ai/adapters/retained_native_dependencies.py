"""Re-observed native graph and materialized-file guards for execution boundaries.

This observes native imports at exact artifact paths. It does not establish that
the selected paths and loader inputs cover an application's entire environment.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.dependencies.observation import PortableHostDependencyObserver
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths
from literate_ai.adapters.retained_native_files import (
    RetainedNativeFiles,
    capture_retained_native_files,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes


@dataclass(frozen=True)
class RetainedNativeDependencies:
    selection_content: bytes
    files: RetainedNativeFiles

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-native-dependencies@1",
                "selection": json.loads(self.selection_content),
                "files": self.files.identity.uri,
            }
        )

    def require_unchanged(self):
        self.files.require_unchanged()
        selected = json.loads(self.selection_content)
        if selected["platform"] != sys.platform:
            raise ValueError("retained.native.host-platform-changed")
        observation = PortableHostDependencyObserver(
            artifact_files=tuple(Path(p) for p in selected["artifact_files"]),
            library_roots=tuple(Path(p) for p in selected["library_roots"]),
            windows_environment=selected["windows_environment"],
            macos_loader_paths=(
                MacOsLoaderPaths.from_environment(selected["macos_environment"])
                if "macos_environment" in selected
                else None
            ),
            linux_loader_paths=(
                LinuxLoaderPaths.from_environment(selected["linux_environment"])
                if "linux_environment" in selected
                else None
            ),
        ).observe(
            {"artifact_path": selected["artifact_root"]}, root_ref=selected["root_ref"]
        )
        identity = canonical_identity(
            {
                "components": list(observation.components),
                "edges": [list(e) for e in observation.edges],
            }
        )
        if identity.uri != self.files.observation_identity:
            raise ValueError("retained.native.observation-changed")
        self.files.require_unchanged()


def observe_retained_native_dependencies(
    *,
    artifact_root,
    artifact_files,
    root_ref,
    library_roots=(),
    windows_environment=None,
    macos_environment=None,
    linux_environment=None,
):
    if (
        not isinstance(artifact_root, Path)
        or not artifact_root.is_absolute()
        or ".." in artifact_root.parts
        or artifact_files is None
        or not isinstance(root_ref, str)
        or not root_ref
        or len(root_ref) > 1024
        or "\x00" in root_ref
    ):
        raise ValueError("retained.native.selection-invalid")
    macos_paths = (
        MacOsLoaderPaths.from_environment(macos_environment)
        if macos_environment is not None
        else None
    )
    linux_paths = (
        LinuxLoaderPaths.from_environment(linux_environment)
        if linux_environment is not None
        else None
    )
    observer = PortableHostDependencyObserver(
        artifact_files=artifact_files,
        library_roots=library_roots,
        windows_environment=windows_environment,
        macos_loader_paths=macos_paths,
        linux_loader_paths=linux_paths,
    )
    selected = {
        "platform": sys.platform,
        "artifact_root": str(artifact_root),
        "artifact_files": [str(p) for p in observer.artifact_files],
        "library_roots": [str(p) for p in observer.library_roots],
        "windows_environment": observer.windows_environment,
        "root_ref": root_ref,
    }
    if macos_paths is not None:
        selected["macos_environment"] = macos_paths.to_environment()
    if linux_paths is not None:
        selected["linux_environment"] = linux_paths.to_environment()
    selection = canonical_json_bytes(selected)
    observation = observer.observe(
        {"artifact_path": str(artifact_root)}, root_ref=root_ref
    )
    result = RetainedNativeDependencies(
        selection, capture_retained_native_files(observation)
    )
    result.require_unchanged()
    return result
