#!/usr/bin/env python3
"""Run the installed-CLI end-to-end proof, skipping it when the CLI has not changed.

Installing ``litai`` and driving a real project through its deterministic command-worker
lane is the only offline check that exercises the packaging, launcher, and installed-
runtime path without requiring coding-agent credentials. It is also slow, so it is gated
on a content identity over the CLI surface: when that identity matches the last recorded
success, the proof is skipped. Any change to the CLI, its packaging, or its installer
invalidates the sentinel and forces a fresh run. The separately named
``installed-project-e2e`` target retains the authenticated generation/build/test/run
proof.

The sentinel records a *successful* run only. A failure leaves the previous sentinel
untouched, so a broken CLI cannot be skipped past on the next invocation.

Usage:
    installed_e2e_gate.py --repository <root> [--build-root <dir>] [--force] [--status]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from literate_ai.evidence_ledger import (
    EvidenceNode,
    attach_run,
    retained_directory,
)

SENTINEL_NAME = ".literate-ai-installed-e2e.json"
# Version 1 could record dirty working-tree bytes after proving an older HEAD. Never
# accept one as evidence under the corrected exported-tree binding.
SENTINEL_SCHEMA = "literate-ai/installed-e2e-sentinel@2"

# The CLI surface: everything whose change can alter what an installed litai does.
SURFACE_TREES = ("src/literate_ai", "flavors")
SURFACE_FILES = (
    "pyproject.toml",
    "scripts/install_litai.py",
    "scripts/litai-launcher",
    "scripts/litai-launcher.cmd",
    "scripts/installed_project_smoke.py",
)
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_PARTS = {"__pycache__"}


class _WorkspaceFailure(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def cli_surface_identity(repository: Path) -> tuple[str, int]:
    """Hash the CLI surface in canonical path order, ignoring build detritus."""

    digest = hashlib.sha256()
    counted = 0
    members: list[Path] = []
    for tree in SURFACE_TREES:
        base = repository / tree
        if base.is_dir():
            members.extend(p for p in base.rglob("*") if p.is_file())
    for name in SURFACE_FILES:
        candidate = repository / name
        if candidate.is_file():
            members.append(candidate)
    for path in sorted(members, key=lambda p: p.relative_to(repository).as_posix()):
        if path.suffix in SKIP_SUFFIXES or SKIP_PARTS & set(path.parts):
            continue
        digest.update(path.relative_to(repository).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        counted += 1
    return "sha256:" + digest.hexdigest(), counted


def read_sentinel(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return value if value.get("schema") == SENTINEL_SCHEMA else None


def _run_workspace(
    *,
    repository: Path,
    workspace: Path,
    sentinel_path: Path,
    working_tree_identity: str,
) -> int:
    try:
        with retained_directory(
            workspace,
            node_path="installed/e2e/workspace",
            operation="installed.e2e.workspace",
            role="installed-e2e-workspace",
        ):
            # The build backend refuses a dirty checkout, untracked files included,
            # because a wheel built from one would carry a provenance claim it cannot
            # honour. Export HEAD into a clean worktree and install from that, so this
            # runs against a working tree in any state. The cost is that it proves HEAD,
            # not uncommitted edits.
            export = workspace / "head"
            head = subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "--short", "HEAD"),
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            added = subprocess.run(
                (
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(export),
                    "HEAD",
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if added.returncode != 0:
                print(
                    f"could not export HEAD: {added.stderr.strip()}",
                    file=sys.stderr,
                )
                raise _WorkspaceFailure(2)
            proved_identity, proved_count = cli_surface_identity(export)
            if working_tree_identity != proved_identity:
                print(
                    "note             : working tree differs from HEAD; "
                    "proving HEAD only"
                )
            print(f"installed E2E    : proving HEAD {head} via a PREFIX in {workspace}")

            # Point the exported smoke run at the build root rather than the system
            # temp directory, without depending on a flag that HEAD's copy may not
            # carry.
            run_root = workspace / "run"
            run_root.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                (
                    sys.executable,
                    str(export / "scripts" / "installed_project_smoke.py"),
                    "--repository",
                    str(export),
                ),
                env={
                    **os.environ,
                    "TMPDIR": str(run_root),
                    "LITAI_NO_SELF_UPDATE": "1",
                },
                check=False,
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "remove",
                    "--force",
                    str(export),
                ),
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                # Leave any previous sentinel alone: a failure must never look like
                # a pass.
                print(
                    "installed E2E    : FAILED; sentinel not updated",
                    file=sys.stderr,
                )
                raise _WorkspaceFailure(1)

            sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            sentinel_path.write_text(
                json.dumps(
                    {
                        "schema": SENTINEL_SCHEMA,
                        "cli_surface_identity": proved_identity,
                        "cli_surface_files": proved_count,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"installed E2E    : passed; recorded {sentinel_path.name}")
            return 0
    except _WorkspaceFailure as failure:
        return failure.status


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--build-root",
        type=Path,
        help="workspace root for the temporary PREFIX; defaults to <repo>/generated",
    )
    parser.add_argument(
        "--force", action="store_true", help="run even when the sentinel is current"
    )
    parser.add_argument(
        "--status", action="store_true", help="report sentinel state without running"
    )
    args = parser.parse_args(argv)

    repository = args.repository.resolve()
    build_root = (args.build_root or repository / "_build").resolve()
    sentinel_path = build_root / SENTINEL_NAME
    identity, counted = cli_surface_identity(repository)
    sentinel = read_sentinel(sentinel_path)
    current = bool(sentinel) and sentinel.get("cli_surface_identity") == identity

    print(f"cli surface      : {counted} files, {identity}")
    print(f"sentinel         : {'current' if current else 'stale or absent'}")

    if args.status:
        return 0 if current else 1
    if current and not args.force:
        print(
            "installed E2E    : skipped; the CLI has not changed since it last passed"
        )
        return 0

    workspace = build_root / "installed-e2e"
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return _run_workspace(
        repository=repository,
        workspace=workspace,
        sentinel_path=sentinel_path,
        working_tree_identity=identity,
    )


def main(argv: list[str] | None = None) -> int:
    run = attach_run()
    if run is None:
        return _main(argv)
    context = run.node(
        "installed/e2e",
        operation="installed.e2e",
        parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
    )
    with context as node:
        status = _main(argv)
        if status and isinstance(node, EvidenceNode):
            node.fail(f"installed E2E exited with status {status}")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
