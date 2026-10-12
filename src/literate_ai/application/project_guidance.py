"""Project configured, operation-specific guidance for coding agents."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

from literate_ai.application.project_tracker import (
    GitRemote,
    TrackerInspect,
    exact_git_revision,
    inspect_remotes,
)
from literate_ai.contracts import canonical_identity
from literate_ai.projects import ProjectConfigurationStore

PROJECT_GUIDANCE_SCHEMA: Final = "literate-ai/project-guidance@1"
PROJECT_GUIDANCE_OPERATIONS: Final = frozenset(
    {"develop", "land", "release", "backport", "gc", "issue-close"}
)


class ProjectGuidanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    try:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _runtime_observations(
    root: Path, remote: str
) -> tuple[dict[str, object], TrackerInspect]:
    branch_result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    revision_result = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    status_result = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    remote_result = _git(root, "config", "--get", f"remote.{remote}.url")
    branch = (
        branch_result.stdout.strip()
        if branch_result is not None and branch_result.returncode == 0
        else None
    )
    clean = (
        not bool(status_result.stdout.splitlines())
        if status_result is not None and status_result.returncode == 0
        else None
    )
    revision = (
        revision_result.stdout.strip()
        if revision_result is not None and revision_result.returncode == 0
        else None
    )
    try:
        tracker_revision = (
            exact_git_revision(revision) if revision is not None else None
        )
    except ValueError:
        tracker_revision = None
    remotes = ()
    if remote_result is not None and remote_result.returncode == 0:
        url = remote_result.stdout.strip()
        if url:
            remotes = (GitRemote(name=remote, url=url),)
    tracker = inspect_remotes(remotes, revision=tracker_revision)
    return {
        "git": {
            "available": branch_result is not None,
            "branch": branch,
            "clean": clean,
            "commit": tracker_revision,
        },
        "forge": tracker.forge,
    }, tracker


def _requirement(identifier: str, text: str) -> dict[str, str]:
    return {"id": identifier, "text": text}


def _command(identifier: str, argv: tuple[str, ...] | list[str]) -> dict[str, object]:
    return {"id": identifier, "argv": list(argv)}


def _tracker_commands(
    operation: str,
    tracker: TrackerInspect,
    *,
    merge_method: str,
    labels: tuple[str, ...],
    target_branch: str,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    argv: list[dict[str, object]] = []
    skips: list[dict[str, str]] = []
    if tracker.forge not in {"github", "gitlab"}:
        skips.append(
            {
                "id": "forge-unsupported",
                "reason": f"configured remote forge is {tracker.forge}",
            }
        )
        return argv, skips

    if operation == "develop":
        argv.extend(
            (
                _command("issues", tracker.issue_list),
                _command("reviews", tracker.review_list),
                _command(
                    "peer-work",
                    ("litai", "project", "peer-work", "survey", "--when", "start"),
                ),
            )
        )
    elif operation == "land":
        # Name the target branch explicitly rather than trusting the forge's
        # repository default to match the project's policy.
        create = [
            *tracker.land_create,
            "--base" if tracker.forge == "github" else "--target-branch",
            target_branch,
        ]
        for label in labels:
            create.extend(("--label", label))
        merge = list(tracker.land_merge)
        if tracker.forge == "github":
            merge = [part for part in merge if part != "--merge"]
            merge.append(f"--{merge_method}")
        elif merge_method == "squash":
            merge.append("--squash")
        elif merge_method == "rebase":
            skips.append(
                {
                    "id": "forge-merge-method-unsupported",
                    "reason": "GitLab CLI has no safe rebase-merge projection",
                }
            )
            merge = []
        argv.extend(
            (
                _command(
                    "peer-work",
                    ("litai", "project", "peer-work", "survey", "--when", "end"),
                ),
                _command("review-create", create),
                _command("ci-status", tracker.ci_status),
            )
        )
        if merge:
            argv.append(_command("review-merge", merge))
    elif operation == "issue-close":
        argv.append(
            _command(
                "issue-close",
                (tracker.cli or "", "issue", "close", "ISSUE"),
            )
        )
    return argv, skips


def project_guidance(path: Path, *, operation: str) -> dict[str, object]:
    """Project committed policy and live observations into concise guidance."""

    if operation not in PROJECT_GUIDANCE_OPERATIONS:
        raise ProjectGuidanceError(
            "project.guidance_operation_invalid",
            "operation must be develop, land, release, backport, gc, or issue-close",
        )
    snapshot = ProjectConfigurationStore.discover(path)
    if snapshot is None:
        raise ProjectGuidanceError(
            "project.guidance_project_not_found", "no literate.project.json found"
        )
    definition = snapshot.definition
    policy = definition.repository_policy
    configuration: dict[str, object] = {
        "project_identity": definition.identity.uri,
        "default_branch": policy.default_branch,
        "development_posture": definition.agent_development_workflow,
    }
    requirements: list[dict[str, str]] = []
    argv: list[dict[str, object]] = []
    skips: list[dict[str, str]] = []

    if operation in {"develop", "land", "release", "backport"}:
        configuration.update(
            {
                "main_state": policy.main_state.value,
                "current_release": definition.version,
            }
        )
        if policy.pre_release_version is not None:
            configuration["pre_release_version"] = policy.pre_release_version
    if (
        operation in {"land", "release", "backport", "gc", "issue-close"}
        and policy.writers
    ):
        configuration["writers"] = list(policy.writers)
        requirements.append(
            _requirement(
                "authorized-writer",
                "The acting repository writer must be listed by policy.",
            )
        )

    runtime, tracker = _runtime_observations(snapshot.root, policy.remote)
    tracker_argv, tracker_skips = _tracker_commands(
        operation,
        tracker,
        merge_method=policy.merge_method.value,
        labels=policy.pull_request_labels,
        target_branch=policy.default_branch,
    )
    if operation in {"develop", "land", "issue-close"}:
        argv.extend(tracker_argv)
        skips.extend(tracker_skips)

    if operation == "develop":
        requirements.append(
            _requirement(
                "development-posture",
                "Use the "
                f"{definition.agent_development_workflow} verification posture.",
            )
        )
    elif operation == "land":
        configuration["merge_method"] = policy.merge_method.value
        requirements.extend(
            (
                _requirement(
                    "default-branch", f"Land through {policy.default_branch}."
                ),
                _requirement(
                    "merge-method", f"Use {policy.merge_method.value} merge semantics."
                ),
            )
        )
        if policy.pull_request_labels:
            configuration["pull_request_labels"] = list(policy.pull_request_labels)
            requirements.append(
                _requirement(
                    "pull-request-labels", "Apply every configured pull-request label."
                )
            )
    elif operation in {"release", "backport"}:
        from literate_ai.project_releases import (
            ProjectReleaseError,
            current_release_version,
            load_release_policy,
        )

        configuration["patch_authority"] = policy.patch_authority.value
        requirements.append(
            _requirement(
                "default-branch-first",
                f"Land fixes on {policy.default_branch} before backporting.",
            )
        )
        if policy.patch_authority.value == "strict":
            requirements.append(
                _requirement(
                    "patch-authority",
                    "Only release engineers may authorize patch content.",
                )
            )
        else:
            requirements.append(
                _requirement(
                    "patch-authority",
                    "Policy permits an authorized writer break-glass patch "
                    "with a reason.",
                )
            )
        try:
            release_root, release_policy = load_release_policy(snapshot.root)
            runtime["release"] = {
                "current_version": current_release_version(
                    release_root, release_policy
                ),
                "policy_identity": release_policy.identity,
            }
        except ProjectReleaseError as exc:
            runtime["release"] = {"available": False}
            skips.append({"id": exc.code, "reason": exc.message})
        if operation == "release":
            argv.extend(
                (
                    _command("release-state", ("litai", "release", "state")),
                    _command(
                        "release-plan", ("litai", "release", "plan", "--bump", "BUMP")
                    ),
                )
            )
        else:
            argv.extend(
                (
                    _command(
                        "backport-status",
                        (
                            "litai",
                            "release",
                            "backport-status",
                            "BRANCH",
                            "--against",
                            policy.default_branch,
                        ),
                    ),
                    _command(
                        "backport",
                        ("litai", "release", "backport", "COMMIT", "--to", "BRANCH"),
                    ),
                )
            )
    elif operation == "gc":
        configuration["branch_gc_minimum_age_days"] = policy.branch_gc_minimum_age_days
        requirements.append(
            _requirement(
                "minimum-age",
                "Collect only branches at least "
                f"{policy.branch_gc_minimum_age_days} days old.",
            )
        )
        argv.append(_command("gc-plan", ("litai", "project", "peer-work", "gc")))
    else:
        requirements.append(
            _requirement(
                "issue-accepted",
                "Close only after the issue's accepted work is landed or "
                "explicitly declined.",
            )
        )

    if runtime["git"]["available"] is False:  # type: ignore[index]
        skips.append(
            {"id": "git-unavailable", "reason": "Git observations are unavailable"}
        )

    result: dict[str, object] = {
        "schema": PROJECT_GUIDANCE_SCHEMA,
        "operation": operation,
        "facts": {"configuration": configuration, "runtime": runtime},
        "requirements": requirements,
        "argv": argv,
        "skips": skips,
    }
    result["identity"] = canonical_identity(result).uri
    return result


__all__ = [
    "PROJECT_GUIDANCE_OPERATIONS",
    "PROJECT_GUIDANCE_SCHEMA",
    "ProjectGuidanceError",
    "project_guidance",
]
