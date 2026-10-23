"""Explicit metadata-only original adoption, preserving the actual Git tree."""

import os
import shutil
import subprocess
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
)
from literate_ai.adapters.conversion_authority import FilesystemConversionAuthorityStore
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.original_project import OriginalProjectAdoptionPlan
from literate_ai.projects import discover_project

EVIDENCE_PATH = ".literate/original-adoption.json"


class OriginalProjectAdoptionError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = "original_adoption." + code, message
        super().__init__(message)


def _git(root, *arguments):
    executable = shutil.which("git")
    if executable is None:
        raise OriginalProjectAdoptionError(
            "git-required", "Original adoption requires Git"
        )
    # No hooks, builds, provider calls or network operations are part of adoption.
    result = subprocess.run(
        (executable, "-c", "core.fsmonitor=false", "-C", str(root), *arguments),
        capture_output=True,
        timeout=20,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise OriginalProjectAdoptionError(
            "git-unavailable", "Git could not inspect a bounded original project"
        )
    return result.stdout


def _project(root):
    root = Path(root).absolute()
    try:
        require_safe_directory(root)
    except UnsafeFilesystemPathError as exc:
        raise OriginalProjectAdoptionError(
            "redirected-root", "Select a direct safe project root"
        ) from exc
    if root.resolve(strict=True) != root:
        raise OriginalProjectAdoptionError(
            "redirected-root", "Select the direct canonical project root"
        )
    project = discover_project(root)
    if project is None or project.root != root:
        raise OriginalProjectAdoptionError(
            "project-required",
            "Original adoption requires an authored canonical project "
            "at the selected root",
        )
    top = _git(root, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve(strict=True) != root:
        raise OriginalProjectAdoptionError(
            "git-root-required", "Select the project's actual Git worktree root"
        )
    return project


def _require_clean(root, *, allowed_new=()):
    entries = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    allowed = {b"?? " + name.encode("utf-8") for name in allowed_new}
    if any(entry and entry not in allowed for entry in entries.split(b"\0")):
        raise OriginalProjectAdoptionError(
            "dirty-source",
            "Commit or resolve current project changes before the initial "
            "original-source binding",
        )


def _plan(project, *, allowed_new=()):
    if FilesystemConversionAuthorityStore(project.root).load_optional() is not None:
        raise OriginalProjectAdoptionError(
            "authority-exists",
            "Existing source/conversion authority cannot be replaced "
            "by original adoption",
        )
    _require_clean(project.root, allowed_new=allowed_new)
    authority = validated_project_authority_identity(project.root)
    commit = _git(project.root, "rev-parse", "HEAD").decode().strip()
    tree = _git(project.root, "rev-parse", "HEAD^{tree}").decode().strip()
    _require_clean(project.root, allowed_new=allowed_new)
    if _git(project.root, "rev-parse", "HEAD").decode().strip() != commit:
        raise OriginalProjectAdoptionError(
            "source-changed", "Git changed during original adoption planning"
        )
    return OriginalProjectAdoptionPlan(
        project.definition.project_id, authority, commit, tree
    )


def plan_original_project_adoption(root):
    """Read only; the complete project must already have authored authority."""
    return _plan(_project(root))


def apply_original_project_adoption(root, *, expected_plan_identity, acknowledge):
    if acknowledge is not True:
        raise OriginalProjectAdoptionError(
            "acknowledgement-required",
            "Original adoption requires acknowledgment of the reviewed plan",
        )
    expected = ContentIdentity.parse_uri(expected_plan_identity)
    project = _project(root)
    plan = _plan(project)
    if plan.identity != expected:
        raise OriginalProjectAdoptionError(
            "plan-changed", "Original adoption plan differs from the reviewed identity"
        )
    directory = ensure_safe_directory(project.root / ".literate")
    evidence = directory / "original-adoption.json"
    try:
        with evidence.open("xb") as output:
            output.write(canonical_json_bytes(plan.to_dict()))
    except FileExistsError as exc:
        raise OriginalProjectAdoptionError(
            "evidence-exists",
            "Original adoption evidence already exists; it was preserved",
        ) from exc
    # Own metadata is the sole permitted difference. Never silently admit a source
    # edit that appeared between review and projection. Failed evidence stays.
    if _plan(project, allowed_new=(EVIDENCE_PATH,)) != plan:
        raise OriginalProjectAdoptionError(
            "plan-changed", "Project changed before original authority publication"
        )
    state = FilesystemConversionAuthorityStore(project.root).initialize_original(
        project_id=project.definition.project_id, evidence_identity=plan.identity
    )
    return {
        "schema": "literate-ai/original-adoption-result@1",
        "plan_identity": plan.identity.uri,
        "plan": plan.to_dict(),
        "authority": state.to_dict(),
        "writes": True,
        "admitted": False,
    }
