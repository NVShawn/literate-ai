"""Project reviewed captured runtime paths; never inherit loader injection."""

import sys
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile


def captured_runtime_environment(
    inputs: Path, manifest: RetainedProjectManifest, profile: RetainedProjectProfile
) -> dict[str, str]:
    if not profile.runtime_library_paths:
        return {}
    if sys.platform.startswith("freebsd") or sys.platform == "linux":
        variable = "LD_LIBRARY_PATH"
    elif sys.platform == "darwin":
        variable = "DYLD_LIBRARY_PATH"
    else:
        raise ValueError("retained.environment.runtime-loader-unsupported")
    paths = []
    for name in profile.runtime_library_paths:
        directory = inputs / name
        if name not in manifest.directories:
            raise ValueError("retained.environment.runtime-directory-not-captured")
        require_safe_directory(directory)
        if directory.absolute() != directory.resolve(strict=True):
            raise ValueError("retained.environment.runtime-directory-redirected")
        members = tuple(m for m in manifest.members if m.path.startswith(name + "/"))
        if not members or any(
            m.role not in {"sdk", "toolchain", "dependency"} for m in members
        ):
            raise ValueError("retained.environment.runtime-directory-role")
        paths.append(str(directory))
    return {variable: ":".join(paths)}
