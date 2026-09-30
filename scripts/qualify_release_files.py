"""Build once, qualify, and retain wheel/plugin files for release publication."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from literate_ai.contracts import canonical_identity
from literate_ai.release_files import (
    SCHEMA,
    file_identity,
    release_file_path,
    validate_release_files,
)
from literate_ai.version import DISTRIBUTION_VERSION
from scripts.build_plugin_bundle import build_release_plugins


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout:
        raise RuntimeError("artifact qualification requires a clean prepared revision")
    output = release_file_path(root, f"_build/qualified-release/{revision}")
    output.mkdir(parents=True, exist_ok=True)
    import shutil

    from literate_ai.project_releases import _build_wheel

    built = _build_wheel(root)
    if built is None:
        raise RuntimeError("framework release requires a wheel")
    wheel = release_file_path(root, (output / built.name).relative_to(root).as_posix())
    shutil.copy2(built, wheel)
    wheel_identity = file_identity(wheel)
    subprocess.run(
        [sys.executable, str(root / "scripts/wheel_smoke.py"), "--wheel", str(wheel)],
        cwd=root,
        check=True,
    )
    if file_identity(wheel) != wheel_identity:
        raise RuntimeError("wheel changed during qualification")
    plugins = build_release_plugins(repository=root, output=output)
    files = [
        {
            "role": "wheel",
            "path": str(wheel),
            "size": wheel.stat().st_size,
            "identity": wheel_identity,
        },
        *plugins["artifacts"],
    ]
    for item in files:
        item["path"] = Path(item["path"]).relative_to(root).as_posix()
    value = {
        "schema": SCHEMA,
        "revision": revision,
        "version": DISTRIBUTION_VERSION,
        "files": sorted(files, key=lambda item: item["role"]),
    }
    value["identity"] = canonical_identity(value).uri
    validate_release_files(
        root,
        value,
        revision=revision,
        version=DISTRIBUTION_VERSION,
        required_roles=("wheel", "codex-plugin", "claude-plugin"),
    )
    destination = release_file_path(root, "_build/qualified-release/manifest.json")
    temporary = release_file_path(root, "_build/qualified-release/manifest.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
