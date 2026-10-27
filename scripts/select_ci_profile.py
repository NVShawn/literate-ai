"""Select explicit, fail-closed CI feedback from the exact changed paths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_DOCUMENTATION_FILES = frozenset({"CHANGELOG.md", "README.md"})
_DOCUMENTATION_PREFIXES = ("docs/",)
_CI_POLICY_FILES = frozenset(
    {
        ".github/ci-matrix.json",
        ".github/workflows/ci.yml",
        "scripts/select_ci_profile.py",
        "tests/critical/test_ci_action_pins.py",
    }
)
_FREEBSD_ACL_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        "src/literate_ai/adapters/retained_posix_acl.py",
        "src/literate_ai/adapters/retained_project_execution.py",
        "tests/critical/test_ci_action_pins.py",
        "tests/critical/test_retained_posix_acl.py",
        "tests/critical/test_retained_project_execution.py",
    }
)


def _scope(path: str) -> str | None:
    if path in _DOCUMENTATION_FILES or path.startswith(_DOCUMENTATION_PREFIXES):
        return "documentation"
    if path in _CI_POLICY_FILES:
        return "ci-policy"
    return None


def _changed_paths(base_sha: str, head_sha: str) -> tuple[str, ...] | None:
    """Return exact PR paths, or ``None`` so selection fails closed to normal CI."""

    if not base_sha or not head_sha:
        return None
    completed = subprocess.run(
        ("git", "diff", "--name-only", "-z", f"{base_sha}...{head_sha}"),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    try:
        return tuple(
            item.decode("utf-8") for item in completed.stdout.split(b"\0") if item
        )
    except UnicodeDecodeError:
        return None


def select_matrix(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> dict:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / ".github/ci-matrix.json").read_text()
    )
    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    if not ordinary_pr:
        return {"include": [row for row in rows if "scoped" not in row]}
    if changed_paths:
        scopes = {_scope(path) for path in changed_paths}
        if None not in scopes:
            return {"include": [row for row in rows if row.get("scoped") in scopes]}
    return {"include": [row for row in rows if row["pr"]]}


def select_freebsd_acl(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> bool:
    """Run the privileged ACL probe only where it can qualify the change."""

    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    if not ordinary_pr:
        return True
    if not changed_paths:
        return True
    return any(path in _FREEBSD_ACL_FILES for path in changed_paths)


if __name__ == "__main__":
    event = os.environ["CI_EVENT"]
    base_ref = os.environ.get("CI_BASE_REF", "")
    paths = None
    if event == "pull_request" and not base_ref.startswith("release/"):
        paths = _changed_paths(
            os.environ.get("CI_BASE_SHA", ""), os.environ.get("CI_HEAD_SHA", "")
        )
    matrix = select_matrix(event, base_ref, paths)
    print("matrix=" + json.dumps(matrix, separators=(",", ":")))
    print("freebsd_acl=" + str(select_freebsd_acl(event, base_ref, paths)).lower())
