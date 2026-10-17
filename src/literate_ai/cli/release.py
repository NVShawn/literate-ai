"""CLI presentation adapter for project release phases."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from literate_ai.diagnostics import report_progress
from literate_ai.evidence_ledger import (
    EvidenceRun,
    explain_run,
    latest_run,
    load_run,
    prune_runs,
)
from literate_ai.project_releases import (
    RELEASE_TARGETS,
    ProjectReleaseError,
    advance_default_branch_version,
    backport_commits,
    backport_status,
    check_release,
    create_release_candidate,
    create_release_plan,
    current_release_version,
    load_release_policy,
    merge_release_pull_request,
    prepare_release,
    publish_release,
    qualify_release,
    release_state,
    set_release_state,
    verify_published_release,
)


def _evidence_run(args: argparse.Namespace) -> EvidenceRun:
    from .errors import CliFailure

    project = Path(args.project)
    run = load_run(project, args.run) if args.run else latest_run(project)
    if run is None:
        raise CliFailure(
            "release.evidence_unavailable",
            "no usable evidence ledger run is available",
        )
    return run


def release_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from literate_ai.diagnostics import debug_stage

    with debug_stage(f"release.{args.release_command}"):
        return _release_from_args_body(args)


def _release_from_args_body(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        if args.release_command == "contributions":
            from literate_ai.release_contributions import (
                ReleaseContributionsError,
                disposition_release_contribution,
                require_release_contributions_ready,
                sweep_release_contributions,
            )

            try:
                if args.contributions_command == "disposition":
                    return (
                        disposition_release_contribution(
                            Path(args.project),
                            kind=args.kind,
                            number=args.number,
                            release=args.version,
                            decision=args.decision,
                            milestone=args.milestone,
                            reason=args.reason,
                            branches=tuple(args.branch),
                            authorize_external_write=args.authorize_external_write,
                        ),
                        0,
                    )
                selected_version = args.version
                release_policy = None
                if selected_version is None or args.current_milestone is None:
                    try:
                        release_root, release_policy = load_release_policy(
                            Path(args.project)
                        )
                    except ProjectReleaseError as exc:
                        # Explicit-version standalone sweeps remain available when
                        # no policy exists. A malformed/unreadable policy is an error.
                        if selected_version is None or not isinstance(
                            exc.__cause__, FileNotFoundError
                        ):
                            raise
                if selected_version is None:
                    assert release_policy is not None
                    selected_version = current_release_version(
                        release_root, release_policy
                    )
                selected_milestone = args.current_milestone
                if selected_milestone is None:
                    configured = (
                        None if release_policy is None else release_policy.contributions
                    )
                    selected_milestone = (
                        selected_version
                        if configured is None
                        else configured.current_milestone.replace(
                            "{version}", selected_version
                        )
                    )
                result = sweep_release_contributions(
                    Path(args.project),
                    release=selected_version,
                    current_milestone=selected_milestone,
                    remote=args.remote,
                    default_branch=args.default_branch,
                )
                if args.require_ready:
                    require_release_contributions_ready(result)
                return result, 0
            except ReleaseContributionsError as exc:
                from .errors import CliFailure

                raise CliFailure(exc.code, exc.message) from exc
        if args.release_command == "plan":
            transition = "explicit" if args.version is not None else args.bump
            return (
                create_release_plan(
                    Path(args.project),
                    transition=transition,
                    explicit_version=args.version,
                ),
                0,
            )
        if args.release_command == "prepare":
            return (
                prepare_release(
                    Path(args.project),
                    Path(args.plan),
                    actor=args.actor,
                    break_glass_reason=args.break_glass_reason,
                ),
                0,
            )
        if args.release_command == "check":
            result = check_release(
                Path(args.project),
                Path(args.plan),
                output=Path(args.output),
                target=args.target,
                actor=args.actor,
                break_glass_reason=args.break_glass_reason,
            )
            evidence = result.get("evidence")
            if isinstance(evidence, dict) and isinstance(evidence.get("root"), str):
                report_progress(f"Release evidence root: {evidence['root']}")
            return result, 0
        if args.release_command == "publish":
            return (
                publish_release(
                    Path(args.project),
                    Path(args.prepared),
                    authorize_external_write=args.authorize_external_write,
                    actor=args.actor,
                    break_glass_reason=args.break_glass_reason,
                ),
                0,
            )
        if args.release_command == "verify-published":
            return (
                verify_published_release(Path(args.project), Path(args.prepared)),
                0,
            )
        if args.release_command == "advance-default-branch":
            transition = "explicit" if args.version is not None else args.bump
            return (
                advance_default_branch_version(
                    Path(args.project),
                    branch=args.branch,
                    transition=transition,
                    explicit_version=args.version,
                ),
                0,
            )
        if args.release_command == "backport":
            return (
                backport_commits(
                    Path(args.project),
                    commits=tuple(args.commit),
                    to_branch=args.to,
                    create_from=args.create_from,
                    actor=args.actor,
                    break_glass_reason=args.break_glass_reason,
                ),
                0,
            )
        if args.release_command == "qualify":
            return qualify_release(Path(args.project), output=Path(args.output)), 0
        if args.release_command == "rc":
            return (
                create_release_candidate(
                    Path(args.project),
                    version=args.version,
                    actor=args.actor,
                    authorize_external_write=args.authorize_external_write,
                    qualification=(
                        Path(args.qualification) if args.qualification else None
                    ),
                ),
                0,
            )
        if args.release_command == "state":
            if args.state_command == "set":
                return (
                    set_release_state(
                        Path(args.project),
                        mode=args.mode,
                        pre_release_version=args.pre_release_version,
                        actor=args.actor,
                    ),
                    0,
                )
            return release_state(Path(args.project)), 0
        if args.release_command == "merge-pr":
            return (
                merge_release_pull_request(
                    Path(args.project),
                    number=args.number,
                    actor=args.actor,
                    authorize_external_write=args.authorize_external_write,
                    qualification=(
                        Path(args.qualification) if args.qualification else None
                    ),
                ),
                0,
            )
        if args.release_command == "backport-status":
            return (
                backport_status(
                    Path(args.project),
                    branch=args.branch,
                    against=args.against,
                ),
                0,
            )
        if args.release_command == "evidence":
            if args.evidence_command == "prune":
                return (
                    prune_runs(
                        Path(args.project),
                        keep=args.keep,
                        include_failed=args.include_failed,
                    ),
                    0,
                )
            run = _evidence_run(args)
            if args.evidence_command == "index":
                path = run.write_index()
                return {
                    "run_id": run.run_id,
                    "index": str(path) if path is not None else None,
                }, 0
            if args.evidence_command == "show":
                index = run.reduced()
                node = next(
                    (
                        item
                        for item in index.get("nodes", [])
                        if isinstance(item, dict) and item.get("node_id") == args.node
                    ),
                    None,
                )
                if node is None:
                    from .errors import CliFailure

                    raise CliFailure(
                        "release.evidence_node_missing",
                        f"evidence node is unavailable: {args.node}",
                    )
                return node, 0
            if args.evidence_command == "explain":
                return explain_run(run), 0
    except ProjectReleaseError as exc:
        if args.release_command == "check":
            run = latest_run(Path(args.project))
            if run is not None:
                explanation = explain_run(run)
                render = explanation.get("render", [])
                if isinstance(render, list):
                    report_progress(
                        "Release evidence explanation:\n"
                        + "\n".join(str(line) for line in render)
                    )
        from .errors import CliFailure

        raise CliFailure(exc.code, exc.message) from exc
    from .errors import CliFailure

    raise CliFailure("cli.usage", "a release command is required")


def add_release_parser(commands: argparse._SubParsersAction) -> None:
    release = commands.add_parser(
        "release",
        help="plan, prepare, check, and explicitly publish a project release",
        description=(
            "Execute the project-owned release policy as separate evidence-bound "
            "phases. Planning is read-only; preparation changes only declared local "
            "authority; publication requires explicit external-write authorization."
        ),
    )
    release_commands = release.add_subparsers(
        dest="release_command",
        required=True,
    )

    contributions = release_commands.add_parser(
        "contributions",
        help="inventory or disposition release-relevant tracker and Git work",
    )
    contribution_commands = contributions.add_subparsers(
        dest="contributions_command",
        required=True,
    )
    contribution_sweep = contribution_commands.add_parser(
        "sweep",
        help="read issues, reviews, branches, and worktrees without changing them",
    )
    contribution_sweep.add_argument("--project", default=".")
    contribution_sweep.add_argument("--version")
    contribution_sweep.add_argument("--current-milestone")
    contribution_sweep.add_argument("--remote", default="origin")
    contribution_sweep.add_argument("--default-branch", default="main")
    contribution_sweep.add_argument(
        "--require-ready",
        action="store_true",
        help="fail when work is unclassified or included work remains open",
    )
    contribution_disposition = contribution_commands.add_parser(
        "disposition",
        help="set one milestone and append its machine-readable scope comment",
    )
    contribution_disposition.add_argument("--project", default=".")
    contribution_disposition.add_argument(
        "--kind", choices=("issue", "review"), required=True
    )
    contribution_disposition.add_argument("--number", type=int, required=True)
    contribution_disposition.add_argument("--version", required=True)
    contribution_disposition.add_argument(
        "--decision", choices=("include", "defer"), required=True
    )
    contribution_disposition.add_argument("--milestone", required=True)
    contribution_disposition.add_argument("--reason", required=True)
    contribution_disposition.add_argument("--branch", action="append", default=[])
    contribution_disposition.add_argument(
        "--authorize-external-write", action="store_true"
    )

    plan = release_commands.add_parser(
        "plan",
        help="create a read-only content-identified release plan",
        description=(
            "When the policy names default_branch, plan from that trunk only "
            "to cut a missing release/<major>.<minor>.x line, or from the "
            "existing line for a patch."
        ),
    )
    plan.add_argument("--project", default=".")
    selection = plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--bump", choices=("patch", "minor", "major"))
    selection.add_argument("--version", help="explicit canonical Semantic Version")

    prepare = release_commands.add_parser(
        "prepare",
        help="apply only the version and changelog changes declared by a plan",
        description=(
            "When the plan's release_line.create is true, create that branch "
            "from the plan revision, check it out, then write declared paths."
        ),
    )
    prepare.add_argument("plan", help="release-plan JSON or litai JSON result")
    prepare.add_argument("--project", default=".")
    prepare.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )
    prepare.add_argument("--break-glass-reason", default=None)

    check = release_commands.add_parser(
        "check", help="run the declared gate for one exact clean prepared commit"
    )
    check.add_argument("plan", help="release-plan JSON or litai JSON result")
    check.add_argument("--project", default=".")
    check.add_argument(
        "--output",
        default="_build/release/prepared.json",
        help="prepared-release record (default: _build/release/prepared.json)",
    )
    check.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )
    check.add_argument("--break-glass-reason", default=None)
    check.add_argument(
        "--target",
        choices=sorted(RELEASE_TARGETS),
        default=None,
        help=(
            "explicit release-gate execution target; overrides the default, "
            "which is tiered when the release policy declares qualification "
            "(this host, then workers, then CI), else the project's declared "
            "ci_targets preference, else the invoking machine "
            "(gitlab is recognized but not yet supported)"
        ),
    )

    publish = release_commands.add_parser(
        "publish",
        help="publish an exact checked release through Git and its optional provider",
    )
    publish.add_argument("prepared", help="prepared-release JSON")
    publish.add_argument("--project", default=".")
    publish.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )
    publish.add_argument("--break-glass-reason", default=None)
    publish.add_argument(
        "--authorize-external-write",
        action="store_true",
        help="authorize tag, push, and configured provider writes",
    )

    verify_published = release_commands.add_parser(
        "verify-published",
        help=(
            "prove the remote tag, release line, and optional provider "
            "match one prepared identity"
        ),
        description=(
            "Read-only remote inspection. Never creates, moves, deletes, or "
            "force-pushes a tag."
        ),
    )
    verify_published.add_argument("prepared", help="prepared-release JSON")
    verify_published.add_argument("--project", default=".")

    advance = release_commands.add_parser(
        "advance-default-branch",
        help=("advance the checked-out branch's version past every released tag"),
        description=(
            "A release cut on a release-line release/x.y.x branch is "
            "never merged back, so the default branch's own version can lag "
            "behind every release that shipped. Run this with --branch's "
            "worktree already checked out clean; it finds the highest version "
            "among every released tag and the branch's own current version, "
            "then advances past it. Writes files only -- never commits or "
            "pushes; review and commit the diff yourself."
        ),
    )
    advance.add_argument("--project", default=".")
    advance.add_argument(
        "--branch",
        required=True,
        help="the already-checked-out branch to advance (e.g. main)",
    )
    advance_selection = advance.add_mutually_exclusive_group(required=True)
    advance_selection.add_argument("--bump", choices=("patch", "minor", "major"))
    advance_selection.add_argument(
        "--version", help="explicit canonical Semantic Version"
    )

    backport = release_commands.add_parser(
        "backport",
        help="cherry-pick already-landed commits onto a release branch",
        description=(
            "Fix-forward-then-cherry-pick: the commit(s) must already exist "
            "(typically on the trunk branch); this replays them onto "
            "--to, creating that branch from --from first if needed. Runs in "
            "an isolated worktree; the current checkout is left untouched. "
            "Never pushes."
        ),
    )
    backport.add_argument(
        "commit", nargs="+", help="commit(s) to cherry-pick, oldest first"
    )
    backport.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )
    backport.add_argument("--break-glass-reason", default=None)

    rc = release_commands.add_parser(
        "rc", help="create and push an annotated RC tag from exact green main HEAD"
    )
    rc.add_argument("--project", default=".")
    rc.add_argument(
        "--version",
        required=True,
        help="canonical RC such as 0.8.0-rc.1 (semver) or 0.8.0rc1 (PEP 440)",
    )
    rc.add_argument("--actor", default=None, help="assert authenticated GitHub login")
    rc.add_argument("--authorize-external-write", action="store_true")
    rc.add_argument(
        "--qualification",
        default=None,
        help=(
            "exact-HEAD record from `litai release qualify`; when its host and "
            "worker tiers cover every platform and action, CI is not required"
        ),
    )

    qualify = release_commands.add_parser(
        "qualify",
        help=(
            "cover the declared platform x action matrix at exact HEAD, "
            "preferring this host, then workers.json workers, then CI"
        ),
    )
    qualify.add_argument("--project", default=".")
    qualify.add_argument(
        "--output",
        default="_build/release/qualification.json",
        help="qualification record (default: _build/release/qualification.json)",
    )

    state = release_commands.add_parser("state", help="inspect or set release state")
    state_commands = state.add_subparsers(dest="state_command")
    state.add_argument("--project", default=".")
    state_set = state_commands.add_parser("set", help="atomically set release state")
    state_set.add_argument("--project", default=".")
    state_set.add_argument("--mode", choices=("free", "pre-release"), required=True)
    state_set.add_argument(
        "--pre-release-version",
        default=None,
        help="required major.minor target for pre-release mode",
    )
    state_set.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )

    merge_pr = release_commands.add_parser(
        "merge-pr", help="guard and merge a GitHub release-line pull request"
    )
    merge_pr.add_argument("number", type=int, help="GitHub pull request number")
    merge_pr.add_argument("--project", default=".")
    merge_pr.add_argument(
        "--actor", default=None, help="assert authenticated GitHub login"
    )
    merge_pr.add_argument("--authorize-external-write", action="store_true")
    merge_pr.add_argument(
        "--qualification",
        default=None,
        help=(
            "record from `litai release qualify` for the PR head; when it covers "
            "every platform and action without CI, pending checks do not block"
        ),
    )
    backport.add_argument("--project", default=".")
    backport.add_argument(
        "--to", required=True, help="release branch to cherry-pick onto"
    )
    backport.add_argument(
        "--from",
        dest="create_from",
        default=None,
        help="ref to create --to from if it does not exist yet (e.g. a release tag)",
    )

    backport_status_parser = release_commands.add_parser(
        "backport-status",
        help="list commits on --against not yet cherry-picked onto a release branch",
    )
    backport_status_parser.add_argument("branch", help="release branch to check")
    backport_status_parser.add_argument("--project", default=".")
    backport_status_parser.add_argument(
        "--against", default="HEAD", help="trunk ref to compare against (default: HEAD)"
    )

    evidence = release_commands.add_parser(
        "evidence",
        help="inspect and reclaim release evidence ledger runs",
    )
    evidence_commands = evidence.add_subparsers(
        dest="evidence_command",
        required=True,
    )
    explain = evidence_commands.add_parser(
        "explain", help="explain the root-to-failure path of a run"
    )
    explain.add_argument("--project", default=".")
    explain.add_argument("--run", default=None, help="run id or run root")
    index = evidence_commands.add_parser(
        "index", help="reduce a run ledger into index.json"
    )
    index.add_argument("--project", default=".")
    index.add_argument("--run", default=None, help="run id or run root")
    show = evidence_commands.add_parser(
        "show", help="show one complete reduced evidence node"
    )
    show.add_argument("node", help="node id, for example n0001")
    show.add_argument("--project", default=".")
    show.add_argument("--run", default=None, help="run id or run root")
    evidence_prune = evidence_commands.add_parser(
        "prune", help="reclaim old evidence runs"
    )
    evidence_prune.add_argument("--project", default=".")
    evidence_prune.add_argument("--run", default=None, help="run id or run root")
    evidence_prune.add_argument(
        "--keep", type=int, default=1, help="retain this many eligible runs"
    )
    evidence_prune.add_argument(
        "--include-failed",
        action="store_true",
        help="allow failed runs to be reclaimed",
    )


__all__ = ["add_release_parser", "release_from_args"]
