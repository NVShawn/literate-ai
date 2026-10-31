"""Select explicit, fail-closed CI feedback from the exact changed paths."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_DOCUMENTATION_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "PROJECT.md",
        "README.md",
        "labs/README.md",
        "scripts/build_conference_site.mjs",
        "scripts/verify_conference_browser.mjs",
        "verification/1.3.1-release-publication.json",
        "verification/1.3.1-windows-qualification-exception.json",
    }
)
_DOCUMENTATION_PREFIXES = ("docs/",)
_CI_POLICY_FILES = frozenset(
    {
        ".github/ci-matrix.json",
        ".github/workflows/ci.yml",
        "scripts/select_ci_profile.py",
        "tests/critical/test_ci_action_pins.py",
        ".github/workflows/native-sdk-qualification.yml",
        ".github/qualifications/native_sdk/qualify_public_sdk.py",
        ".github/qualifications/native_sdk/source_transport.py",
        ".github/qualifications/native_sdk/run_cell.py",
        ".github/qualifications/native_sdk/collect_results.py",
        "tests/critical/test_native_sdk_qualification_harness.py",
        "verification/freebsd-native-sdk-qualification.json",
    }
)
_QUALIFICATION_FIXTURE_FILES = frozenset(
    {
        "tests/critical/test_roadmap_dependencies.py",
        "tests/critical/test_retained_posix_acl.py",
        "verification/windows-portable-fixtures-qualification.json",
    }
)
_FREEBSD_ACL_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        "src/literate_ai/adapters/retained_posix_acl.py",
        "src/literate_ai/adapters/retained_project_execution.py",
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


def _documentation_manifest_change() -> bool:
    """Only an exact documentation-roots-only JSON edit is documentation scope."""
    base = os.environ.get("CI_BASE_SHA", "")
    head = os.environ.get("CI_HEAD_SHA", "")
    if not base or not head:
        return False
    try:
        manifests = []
        for revision in (base, head):
            result = subprocess.run(
                ("git", "show", f"{revision}:literate.project.json"),
                check=False,
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                return False

            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate manifest key")
                    value[key] = item
                return value

            value = json.loads(result.stdout, object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                return False
            roots = value.pop("documentation_roots")
            if (
                not isinstance(roots, list)
                or not roots
                or not all(isinstance(p, str) for p in roots)
            ):
                return False
            manifests.append((value, roots))
        return manifests[0][0] == manifests[1][0] and manifests[0][1] != manifests[1][1]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return False


def _scope(path: str) -> str | None:
    if path in _DOCUMENTATION_FILES or path.startswith(_DOCUMENTATION_PREFIXES):
        return "documentation"
    if path in _CI_POLICY_FILES:
        return "ci-policy"
    if path == "literate.project.json" and _documentation_manifest_change():
        return "documentation"
    if path.startswith("labs/") or path == "verification/unprompted-ai-labs-1.3.1.json":
        return "labs"
    if path in _QUALIFICATION_FIXTURE_FILES:
        return "qualification-fixtures"
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
            _scope(path)
            in {"documentation", "ci-policy", "qualification-fixtures", "labs"}
            for path in changed_paths
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
    fixture_changed = bool(changed_paths) and any(
        _scope(path) == "qualification-fixtures" for path in changed_paths
    )
    return {
        "include": [
            row
            for row in rows
            if row["pr"]
            or (fixture_changed and row.get("scoped") == "qualification-fixtures")
        ]
    }


def select_full_qualification(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> bool:
    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    return not ordinary_pr and not _scoped_main_push(event, base_ref, changed_paths)


def select_freebsd_acl(
    event: str, base_ref: str, changed_paths: tuple[str, ...] | None = None
) -> bool:
    """Run the privileged ACL probe only where it can qualify the change."""

    def relevant(path: str) -> bool:
        if path == ".github/workflows/ci.yml" and _only_scoped_feedback_steps_changed():
            return False
        return path in _FREEBSD_ACL_FILES

    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    if _scoped_main_push(event, base_ref, changed_paths):
        return any(relevant(path) for path in changed_paths)
    if not ordinary_pr:
        return True
    if not changed_paths:
        return True
    return any(relevant(path) for path in changed_paths)


def _only_scoped_feedback_steps_changed() -> bool:
    """Recognize exact doc/lab/policy step edits; preserve every other section."""
    base = os.environ.get("CI_BASE_SHA", "")
    head = os.environ.get("CI_HEAD_SHA", "")
    if not base or not head:
        return False
    try:
        versions = []
        for revision in (base, head):
            result = subprocess.run(
                ("git", "show", f"{revision}:.github/workflows/ci.yml"),
                check=False,
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                return False
            text = result.stdout.decode("utf-8")
            if re.search(r"(?m)(?:^|\s)[&*][A-Za-z0-9_-]+", text):
                # Cross-section YAML references can make an apparently local
                # edit affect another job. Do not infer independence there.
                return False
            # Only step blocks within jobs.checks are eligible. A changed profile,
            # FreeBSD job, shared permission, runner or unknown section stays visible.
            match = re.search(r"(?ms)^  checks:\n.*?(?=^  [a-zA-Z0-9_-]+:|\Z)", text)
            if match is None:
                return False
            checks = match.group()
            blocks = re.split(r"(?m)(?=^      - )", checks)
            kept = [
                block
                for block in blocks
                if not re.search(
                    r"(?m)^        if: matrix\.task == "
                    r"'(?:documentation|labs|ci-policy)'\s*$",
                    block,
                )
            ]
            versions.append(text[: match.start()] + "".join(kept) + text[match.end() :])
        return versions[0] == versions[1]
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired):
        return False


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
