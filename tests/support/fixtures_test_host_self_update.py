"""Shared test fixtures extracted from test_host_self_update."""

from __future__ import annotations

import json
import os
from pathlib import Path

from literate_ai.adapters.user_paths import (
    HostInstallLayout,
)


def _python_path(layout: HostInstallLayout) -> Path:
    scripts = Path(layout.environment) / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True, exist_ok=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_text("python\n", encoding="utf-8")
    return python


def _write_manifest(
    layout: HostInstallLayout,
    *,
    schema: str,
    self_update: bool | None = True,
) -> None:
    Path(layout.environment).mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).write_text("launcher\n", encoding="utf-8")
    document: dict[str, object] = {
        "schema": schema,
        "prefix": str(layout.prefix),
        "environment": str(layout.environment),
        "launcher": str(layout.launcher),
    }
    if schema.endswith("@2"):
        document["self_update"] = bool(self_update)
    Path(layout.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.manifest).write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
