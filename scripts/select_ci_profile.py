"""Select explicit, fail-closed CI feedback from the exact changed paths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_DOCUMENTATION_FILES = frozenset(
    {"CHANGELOG.md", "CONTRIBUTING.md", "PROJECT.md", "README.md"}
)
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
_ELIXIR_PREFIXES = (
    "flavors/build-mix/",
    "flavors/lang-elixir/",
    "scripts/stage_elixir_",
    "skills/specification-to-source/elixir-portable-application/",
    "skills/specification-to-source/mix-build-system/",
    "src/literate_ai/adapters/builders/elixir.py",
    "src/literate_ai/adapters/builders/hex.py",
    "src/literate_ai/adapters/builders/mix",
    "src/literate_ai/adapters/lifecycle/standard_mix.py",
    "src/literate_ai/adapters/qualification_mix.py",
    "src/literate_ai/contracts/elixir_libraries.py",
    "src/literate_ai/contracts/mix_projects.py",
    "src/literate_ai/source_to_specification/builtin_skills/language-elixir/",
)


def _scope(path: str) -> str | None:
    if path in _DOCUMENTATION_FILES or path.startswith(_DOCUMENTATION_PREFIXES):
        return "documentation"
    if path in _CI_POLICY_FILES:
        return "ci-policy"
    if path.startswith(_ELIXIR_PREFIXES) or "test_elixir" in path:
        return "elixir"
    return None


def _changed_paths(
    base_sha: str, head_sha: str, *, push: bool = False
) -> tuple[str, ...] | None:
    """Compare PR merge-base paths or exact push endpoints; fail closed on error."""

    if not base_sha or not head_sha:
        return None
    revisions = (base_sha, head_sha) if push else (f"{base_sha}...{head_sha}",)
    try:
        completed = subprocess.run(
            ("git", "diff", "--name-only", "-z", *revisions),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return tuple(
            item.decode("utf-8") for item in completed.stdout.split(b"\0") if item
        )
    except UnicodeDecodeError:
        return None


def _scoped_main_push(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None
) -> bool:
    return (
        event == "push"
        and base_ref == "refs/heads/main"
        and bool(changed_paths)
        and all(
            _scope(path) in {"documentation", "ci-policy"} for path in changed_paths
        )
    )


def select_matrix(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> dict:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / ".github/ci-matrix.json").read_text()
    )
    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    if _scoped_main_push(event, base_ref, changed_paths):
        scopes = {_scope(path) for path in changed_paths}
        return {"include": [row for row in rows if row.get("scoped") in scopes]}
    if not ordinary_pr:
        return {"include": [row for row in rows if "scoped" not in row]}
    if changed_paths:
        scopes = {_scope(path) for path in changed_paths}
        if None not in scopes:
            if "elixir" in scopes:
                return {
                    "include": [
                        row
                        for row in rows
                        if row.get("pr")
                        or row["task"] == "elixir-native"
                        or row.get("scoped") in scopes
                    ]
                }
            return {"include": [row for row in rows if row.get("scoped") in scopes]}
    return {"include": [row for row in rows if row["pr"]]}


def select_full_qualification(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> bool:
    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    return not ordinary_pr and not _scoped_main_push(event, base_ref, changed_paths)


def select_freebsd_acl(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> bool:
    """Run the privileged ACL probe only where it can qualify the change."""

    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    if _scoped_main_push(event, base_ref, changed_paths):
        return any(path in _FREEBSD_ACL_FILES for path in changed_paths)
    if not ordinary_pr:
        return True
    if not changed_paths:
        return True
    return any(path in _FREEBSD_ACL_FILES for path in changed_paths)


if __name__ == "__main__":
    event = os.environ["CI_EVENT"]
    base_ref = os.environ.get("CI_BASE_REF", "")
    paths = None
    if (event == "pull_request" and not base_ref.startswith("release/")) or (
        event == "push" and base_ref == "refs/heads/main"
    ):
        paths = _changed_paths(
            os.environ.get("CI_BASE_SHA", ""),
            os.environ.get("CI_HEAD_SHA", ""),
            push=event == "push",
        )
    matrix = select_matrix(event, base_ref, paths)
    print("matrix=" + json.dumps(matrix, separators=(",", ":")))
    print("freebsd_acl=" + str(select_freebsd_acl(event, base_ref, paths)).lower())
    print(
        "full_qualification="
        + str(select_full_qualification(event, base_ref, paths)).lower()
    )
