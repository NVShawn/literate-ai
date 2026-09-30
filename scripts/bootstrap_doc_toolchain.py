#!/usr/bin/env python3
"""Detect or install the pinned document-pair authoring toolchain into OBJ_DIR."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tools" / "doc-toolchain" / "authoring-toolchain.json"
REQUIREMENTS = REPO / "tools" / "doc-toolchain" / "requirements.txt"


class ToolchainBootstrapError(SystemExit):
    """Stable detect-before-install failure."""


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def obj_dir() -> Path:
    return Path(os.environ.get("OBJ_DIR") or (REPO / "_build"))


def _bootstrap_root() -> Path:
    return obj_dir() / "doc-toolchain"


def _python_for(root: Path) -> Path:
    windows = sys.platform == "win32"
    return root / ("Scripts/python.exe" if windows else "bin/python")


def toolchain_python() -> Path | None:
    """Return the OBJ_DIR venv interpreter when it exists."""

    root = _bootstrap_root()
    names = (
        ("Scripts/python.exe",)
        if sys.platform == "win32"
        else ("bin/python3", "bin/python")
    )
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _missing_packages(python: Path | None) -> list[str]:
    packages = _manifest()["packages"]
    assert isinstance(packages, list)
    missing: list[str] = []
    for item in packages:
        assert isinstance(item, dict)
        module = str(item["import"])
        if python is None:
            try:
                __import__(module)
            except ImportError:
                missing.append(str(item["name"]))
            continue
        probe = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            missing.append(str(item["name"]))
    return missing


def detect() -> dict[str, object]:
    python = toolchain_python()
    missing = _missing_packages(python)
    return {
        "schema": "literate-ai/document-pair-authoring-toolchain-status@1",
        "state": "ready" if not missing else "missing",
        "missing": missing,
        "bootstrap_root": str(_bootstrap_root()),
        "python": None if python is None else str(python),
        "reason": (
            None
            if not missing
            else "authoring toolchain packages are not importable; "
            "run `make doc-toolchain-bootstrap` (requires network and explicit "
            "install authorization) or set OBJ_DIR to an installed toolchain"
        ),
    }


def install(*, allow_install: bool) -> dict[str, object]:
    if not allow_install:
        raise ToolchainBootstrapError(
            "authoring toolchain install requires --allow-install; refusing to "
            "download packages without authorization"
        )
    root = _bootstrap_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    if not _python_for(root).is_file():
        subprocess.run([sys.executable, "-m", "venv", str(root)], check=True)
    python = _python_for(root)
    pip = subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        capture_output=True,
        text=True,
    )
    if pip.returncode != 0:
        detail = (pip.stderr or pip.stdout or "pip install failed").strip()
        if "Could not find a version" in detail or "No matching distribution" in detail:
            raise ToolchainBootstrapError(
                "authoring toolchain install is offline or the pin is unavailable: "
                + detail.splitlines()[-1]
            )
        raise ToolchainBootstrapError(
            detail.splitlines()[-1] if detail else "pip failed"
        )
    return {"state": "installed", "bootstrap_root": str(root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--detect", action="store_true")
    mode.add_argument("--install", action="store_true")
    parser.add_argument(
        "--allow-install",
        action="store_true",
        help="authorize downloading the pinned packages into OBJ_DIR",
    )
    args = parser.parse_args(argv)
    try:
        if args.detect:
            status = detect()
            json.dump(status, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0 if status["state"] == "ready" else 2
        result = install(allow_install=args.allow_install)
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except ToolchainBootstrapError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
