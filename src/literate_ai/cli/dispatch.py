"""litai parser, command dispatch, and stdout envelope."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from argparse import Namespace
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any, TextIO

from literate_ai.adapters.cache import SourceCacheError
from literate_ai.adapters.host_self_update import maybe_host_self_update
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.mcp_runtime import (
    active_mcp_transport,
    discover_catalog_mcps,
    fan_out_author_event,
    load_project_channels,
    report_discovery,
)
from literate_ai.adapters.models import CodingCliError
from literate_ai.adapters.project_update_work_items import DEFAULT_QUEUE_PATH
from literate_ai.adapters.shared_cache_config import SharedCacheConfigurationError
from literate_ai.adapters.standard_project import StandardCommandProjectionError
from literate_ai.adapters.user_config import (
    UserConfigError,
    ensure_user_mcp_catalog,
    journal_mutagenic_event,
    load_user_mcp_catalog,
)
from literate_ai.contracts import PROJECT_TYPES
from literate_ai.diagnostics import (
    debug_diagnostics,
    debug_stage,
    progress_reporting,
    verbose_diagnostics,
)
from literate_ai.perf import PerformanceRecorder
from literate_ai.source_to_specification import SourceToSpecificationError

from .errors import (
    CLI_ERROR_MESSAGE_CHARS,
    CLI_ERROR_SCHEMA,
    CLI_RESULT_SCHEMA,
    HELP_SCHEMA,
    CliFailure,
    JsonArgumentParser,
    _json_text,
)
from .source_to_specification import add_spec_parser, spec_from_args

_GLOBAL_BOOL_FLAGS = frozenset({"--json", "-v", "--verbose", "--discover-mcps"})


class _BuildVersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        from literate_ai.build_version import cli_version_label

        print(f"{parser.prog} {cli_version_label()}")
        parser.exit()


_DEBUG_PATH_SUFFIXES = (".json", ".ndjson", ".log", ".txt", ".out")
_TOP_LEVEL_COMMANDS = frozenset(
    {
        "help",
        "version",
        "status",
        "doctor",
        "onboard",
        "init",
        "update",
        "reparent",
        "verify",
        "lock",
        "plan",
        "generate",
        "rebuild",
        "build",
        "test",
        "run",
        "package",
        "clean",
        "really-clean",
        "release",
        "catalog",
        "graph",
        "render",
        "component",
        "learn",
        "project",
        "cache",
        "skills",
        "config",
        "worker",
        "matrix",
        "perf",
        "profile",
        "spec",
        "prompt",
        "flavor",
        "design",
        "video",
    }
)
_CLI_CATALOG_EPILOG = """
SDLC catalog (verbs unchanged; grouped for flow):

  Adopt / template
    litai doctor
    litai onboard create | adopt
    litai onboard orchestrate plan | check | initialize
    litai onboard orchestrate refresh plan | check | apply
    litai status
    litai init
    litai init --convert [--plan]
    litai init PATH --from URL[#REVISION]
    litai update
    litai reparent URL[#REVISION]|none
    litai catalog copy
    litai flavor add

  Dev workflow
    litai orchestrate plan | run
    litai verify
    litai lock
    litai plan
    litai generate
    litai rebuild
    litai build | test | run
    litai package plan | build | verify
    litai video init | plan | build | verify
    litai clean | really-clean

  Release
    litai release plan | prepare | check | publish | verify-published
    litai release backport | backport-status | advance-default-branch
    litai release evidence explain | index | show | prune

  Authority / catalog
    litai catalog graph | migrate-flavor-names
    litai graph [show | rebalance]
    litai project validate | documentation-update | documentation-review
    litai project tracker inspect | peer-work | parent checkout | ci-plan
    litai project retained-cargo check | materialize | admit
    litai project test-receipt run-retained | update | check | require-current
    litai project test-receipt verify-evidence | publish-evidence
    litai project test-receipt require-current-evidence
    litai project source-intelligence sync | check
    litai project mac-contract
    litai component migrate
    litai learn
    litai design refine | explain | accept

  Operator
    litai config paths | migrate [--apply]
    litai worker verify-model | probe | wheelhouse export
    litai worker bootstrap | execute | execute-retained | acknowledge
    litai matrix
    litai perf show | chart
    litai profile
    litai cache publish
    litai skills evaluate
    litai prompt translate
    litai version check

  Inverse / aspirational
    litai spec format | validate | explain | status | derive | audit | review
    litai spec accept | qualify | refresh | conformance | coverage | diff
    litai spec attest | skills | scxml-review | merge

Global diagnostics: --json (stdout envelope), -v/--verbose (subprocess output),
--debug/--debug=FILE (SDLC stages, child argv, and spec maps on stderr or FILE).
"""


def _handle(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "orchestrate":
        from .repository_lifecycle import repository_lifecycle_from_args

        return repository_lifecycle_from_args(args)
    if args.command in {"status", "doctor"}:
        from .operator import operator_status_from_args

        return operator_status_from_args(args), 0
    if args.command == "onboard":
        if args.onboard_command == "orchestrate":
            from .orchestration import orchestration_from_args

            return orchestration_from_args(args), 0
        from .operator import onboard_from_args

        return onboard_from_args(args), 0
    if args.command in {"clean", "really-clean"}:
        from literate_ai.cache_directories import (
            CacheDirectoryError,
            clean_cache_directories,
            resolve_cache_directories,
        )

        project_root = Path(args.project).resolve(strict=True)
        try:
            directories = resolve_cache_directories(project_root)
            removed = clean_cache_directories(
                project_root,
                remove_generated_sources=args.command == "really-clean",
            )
        except CacheDirectoryError as exc:
            raise CliFailure("cache.clean_refused", str(exc)) from exc
        return {
            "schema": "literate-ai/cache-clean-result@1",
            "project": str(project_root),
            "build_dir": str(directories.build_dir),
            "obj_dir": str(directories.obj_dir),
            "removed": [str(path) for path in removed],
            "generated_sources_removed": args.command == "really-clean",
        }, 0
    if args.command == "version":
        from literate_ai.version_check import VersionCheckError, check_versions

        try:
            report = check_versions(
                project_path=None if args.no_project else Path(args.project),
                require_project=args.require_project,
                require_release_tag=args.require_release_tag,
            )
        except VersionCheckError as exc:
            raise CliFailure(exc.code, str(exc)) from exc
        return report, 0 if report["ok"] else 1
    if args.command == "learn":
        from .learning import learning_plan_from_args

        return learning_plan_from_args(args), 0
    if args.command == "design":
        from .design import design_from_args

        return design_from_args(args)
    if args.command == "release":
        from .release import release_from_args

        return release_from_args(args)
    if args.command == "package":
        from .package import package_from_args

        return package_from_args(args)
    if args.command == "flavor":
        from .flavor import flavor_add_from_args

        return flavor_add_from_args(args), 0
    if args.command == "matrix":
        from .target_matrix import target_matrix_from_args

        return target_matrix_from_args(args)
    if args.command == "perf":
        from .perf import perf_from_args

        return perf_from_args(args)
    if args.command == "worker":
        from .worker import worker_from_args

        return worker_from_args(args)
    if args.command == "config":
        from .config import config_from_args

        return config_from_args(args)
    if args.command == "work":
        from .work import work_from_args

        return work_from_args(args)
    if args.command == "video":
        from .video import video_from_args

        return video_from_args(args)
    if args.command == "document":
        from .document import document_from_args

        return document_from_args(args)
    if args.command == "init":
        from .project import init_project_from_args

        return init_project_from_args(args), 0
    if args.command == "cache":
        from .cache import cache_publish_from_args

        return cache_publish_from_args(args), 0
    if args.command == "skills":
        if args.skills_command == "evaluate":
            from .skills import skills_evaluate_from_args

            return skills_evaluate_from_args(args), 0
    if args.command == "prompt":
        if args.prompt_command == "translate":
            from .prompt import prompt_translate_from_args

            return prompt_translate_from_args(args), 0
    if args.command == "catalog":
        if args.catalog_command == "copy":
            from .catalog import catalog_copy_from_args

            return catalog_copy_from_args(args), 0
        if args.catalog_command == "graph":
            from .catalog import catalog_graph_from_args

            return catalog_graph_from_args(args), 0
        if args.catalog_command == "migrate-flavor-names":
            from .catalog import catalog_migrate_flavor_names_from_args

            return catalog_migrate_flavor_names_from_args(args), 0
    if args.command == "graph":
        from .catalog import authority_graph_from_args

        return authority_graph_from_args(args), 0
    if args.command == "render":
        from .render import render_dashboard_from_args, render_html_from_args

        return (
            render_dashboard_from_args(args)
            if args.render_command == "dashboard"
            else render_html_from_args(args)
        )
    if args.command == "build":
        from .build_run import build_from_args

        return build_from_args(args), 0
    if args.command == "test":
        from .build_run import test_from_args

        return test_from_args(args), 0
    if args.command == "run":
        from .build_run import run_from_args

        return run_from_args(args)
    if args.command == "verify":
        from .verify import verify_project_from_args

        return verify_project_from_args(args)
    if args.command == "update":
        from .project import update_project_from_args

        return update_project_from_args(args), 0
    if args.command == "reparent":
        from .project import reparent_project_from_args

        return reparent_project_from_args(args), 0
    if args.command == "lock":
        from .component_locks import component_lock_from_args

        return component_lock_from_args(args)
    if args.command == "component":
        from .component_authoring import component_authoring_from_args

        if args.component_command == "migrate":
            return component_authoring_from_args(args)
        raise CliFailure("cli.usage", "a component command is required")
    if args.command == "project":
        from .project import (
            documentation_review_from_args,
            documentation_update_from_args,
            mac_project_contract_from_args,
            parent_checkout_from_args,
            project_ci_plan_from_args,
            project_guidance_from_args,
            project_lifecycle_from_args,
            project_peer_work_from_args,
            project_source_intelligence_from_args,
            project_test_receipt_from_args,
            project_tracker_from_args,
            project_worktree_from_args,
            validate_project_from_args,
        )

        if args.project_command == "validate":
            return validate_project_from_args(args), 0
        if args.project_command == "ci-plan":
            return project_ci_plan_from_args(args), 0
        if args.project_command == "guidance":
            return project_guidance_from_args(args), 0
        if args.project_command == "lifecycle":
            return project_lifecycle_from_args(args), 0
        if args.project_command == "source-intelligence":
            return project_source_intelligence_from_args(args), 0
        if args.project_command == "documentation-review":
            return documentation_review_from_args(args), 0
        if args.project_command == "documentation-update":
            return documentation_update_from_args(args), 0
        if args.project_command == "mac-contract":
            return mac_project_contract_from_args(args), 0
        if args.project_command == "peer-work":
            return project_peer_work_from_args(args), 0
        if args.project_command == "worktree":
            return project_worktree_from_args(args), 0
        if args.project_command == "tracker":
            return project_tracker_from_args(args), 0
        if args.project_command == "parent":
            return parent_checkout_from_args(args), 0
        if args.project_command == "convert-stage":
            from .operator import conversion_authority_from_args

            return conversion_authority_from_args(args), 0
        if args.project_command == "retained-scope":
            from .operator import retained_scope_from_args

            return retained_scope_from_args(args), 0
        if args.project_command == "retained-harness":
            from .operator import retained_harness_from_args

            return retained_harness_from_args(args), 0
        if args.project_command == "retained-cargo":
            from .retained_cargo import retained_cargo_from_args

            return retained_cargo_from_args(args), 0
        if args.project_command == "test-checkpoint":
            from .test_checkpoints import test_checkpoint_from_args

            return test_checkpoint_from_args(args)
        return project_test_receipt_from_args(args), 0
    if args.command == "plan":
        from .generation import plan_from_args

        return plan_from_args(args), 0
    if args.command == "generate":
        from .generation import generate_from_args

        return generate_from_args(args), 0
    if args.command == "rebuild":
        from .rebuild import rebuild_from_args

        return rebuild_from_args(args), 0
    if args.command == "profile":
        from .profile import profile_from_args

        return profile_from_args(args), 0
    return spec_from_args(args)


def _add_repository_fetch_deadline_arguments(parser) -> None:
    parser.add_argument(
        "--repository-fetch-total-seconds",
        type=int,
        metavar="SECONDS",
        help=(
            "bounded total Git-fetch deadline (30..14400; default: 3600); "
            "the selected policy is identified in command evidence"
        ),
    )
    parser.add_argument(
        "--repository-fetch-no-progress-seconds",
        type=int,
        metavar="SECONDS",
        help=("bounded interval without forced Git progress (15..1800; default: 600)"),
    )
    parser.add_argument(
        "--repository-fetch-connect-seconds",
        type=int,
        metavar="SECONDS",
        help="bounded SSH connection attempt (5..120; default: 30)",
    )


def _parser() -> JsonArgumentParser:
    from literate_ai.contracts.known_test_failures import KnownTestFailureCause

    from .design import add_design_parser
    from .execution_workers import add_execution_worker_arguments
    from .flavor import add_flavor_parser
    from .package import add_package_parser
    from .release import add_release_parser

    parser = JsonArgumentParser(
        prog="litai",
        description=(
            "Release-engineering and SDLC harness for specification-led software. "
            "Front door: litai doctor, litai onboard create, and litai onboard "
            "adopt; then the evidence-gated dev workflow (status, verify, lock, plan, "
            "rebuild) and versioned release. Build-system Flavors may delegate "
            "native work to Bazel, Buck, CMake, or another authorized driver "
            "without making that build tool the specification authority."
        ),
        epilog=_CLI_CATALOG_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action=_BuildVersionAction,
        nargs=0,
        help="show build version and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete stable JSON envelope even on an interactive terminal",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show redacted subprocess commands and bounded output on stderr",
    )
    parser.add_argument(
        "--discover-mcps",
        action="store_true",
        help=(
            "probe operator-catalog MCP command hints for reachability "
            "(off by default; does not write tokens or mcps.json)"
        ),
    )
    parser.add_argument(
        "--debug",
        default=None,
        metavar="FILE",
        help=(
            "trace SDLC stages and redacted child argv to stderr (`-`) or FILE; "
            "also enable spec-to-source maps. Does not change stdout. Distinct from "
            "--verbose (child stdout/stderr)"
        ),
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=JsonArgumentParser
    )
    help_command = commands.add_parser(
        "help", help="show top-level or command-specific help"
    )
    help_command.add_argument(
        "topic",
        nargs="*",
        metavar="COMMAND",
        help="command path, for example: project validate",
    )
    version = commands.add_parser(
        "version", help="inspect distribution and protocol version agreement"
    )
    version_commands = version.add_subparsers(
        dest="version_command", required=True, parser_class=JsonArgumentParser
    )
    version_check = version_commands.add_parser("check")
    project_selection = version_check.add_mutually_exclusive_group()
    project_selection.add_argument("--project", default=".")
    project_selection.add_argument("--no-project", action="store_true")
    version_check.add_argument("--require-project", action="store_true")
    version_check.add_argument("--require-release-tag", action="store_true")
    status = commands.add_parser(
        "status",
        help=(
            "show project, conversion, lock, receipt, host-tool, and coding-CLI state"
        ),
    )
    status.add_argument(
        "--project", default=".", help="canonical project (default: current)"
    )
    commands.add_parser(
        "doctor", help="show project-independent host preflight and coding-CLI state"
    )
    lifecycle = commands.add_parser(
        "orchestrate", help="plan or execute independent child lifecycle commands"
    )
    lifecycle_commands = lifecycle.add_subparsers(
        dest="lifecycle_command", required=True, parser_class=JsonArgumentParser
    )
    for action in ("plan", "run"):
        lifecycle_action = lifecycle_commands.add_parser(action)
        lifecycle_action.add_argument(
            "operation", choices=("build", "package", "containerize")
        )
        lifecycle_action.add_argument("path", nargs="?", default=".")
        lifecycle_action.add_argument("--declaration", required=True)
        lifecycle_action.add_argument("--only", action="append", default=[])
        if action == "run":
            lifecycle_action.add_argument("--expected-plan-identity", required=True)
            lifecycle_action.add_argument("--acknowledge", action="store_true")
            lifecycle_action.add_argument("--resume")
    onboard = commands.add_parser(
        "onboard",
        help=("plan creation/adoption or inspect independent Gitlink orchestration"),
    )
    onboard_commands = onboard.add_subparsers(
        dest="onboard_command", required=True, parser_class=JsonArgumentParser
    )
    orchestration = onboard_commands.add_parser(
        "orchestrate",
        help="inspect or initialize root authority with independent Gitlinks",
    )
    orchestration_commands = orchestration.add_subparsers(
        dest="orchestration_command", required=True, parser_class=JsonArgumentParser
    )
    for operation in ("plan", "check", "initialize"):
        command = orchestration_commands.add_parser(operation)
        command.add_argument("path", nargs="?", default=".")
        command.add_argument("--declaration", required=True)
        command.add_argument("--project-id", required=operation == "initialize")
        command.add_argument("--project-version", required=operation == "initialize")
        if operation in {"check", "initialize"}:
            command.add_argument("--expected-plan-identity", required=True)
        if operation == "initialize":
            command.add_argument("--acknowledge", action="store_true")
    refresh = orchestration_commands.add_parser(
        "refresh",
        help="review, recheck, or apply published exact child pin updates",
    )
    refresh_commands = refresh.add_subparsers(
        dest="refresh_command", required=True, parser_class=JsonArgumentParser
    )
    for operation in ("plan", "check", "apply"):
        command = refresh_commands.add_parser(operation)
        command.add_argument("path", nargs="?", default=".")
        command.add_argument(
            "--request",
            required=True,
            help="canonical orchestration refresh request JSON",
        )
        _add_repository_fetch_deadline_arguments(command)
        if operation in {"check", "apply"}:
            command.add_argument("--expected-plan-identity", required=True)
        if operation == "apply":
            command.add_argument("--acknowledge", action="store_true")
    onboard_create = onboard_commands.add_parser(
        "create", help="plan or create a new canonical project"
    )
    onboard_adopt = onboard_commands.add_parser(
        "adopt", help="plan or adopt an existing source tree"
    )
    for onboard_parser in (onboard_create, onboard_adopt):
        onboard_parser.add_argument("path", nargs="?", default=".")
        onboard_parser.add_argument("--project-id")
        onboard_parser.add_argument(
            "--flavor", action="append", dest="flavors", default=[]
        )
        onboard_parser.add_argument(
            "--source-intelligence-provider",
            choices=("none", "codegraph-cli"),
            default="none",
        )
        onboard_parser.add_argument(
            "--from",
            dest="repository_from",
            metavar="URL[#REVISION]",
            help="inherit from this exact Literate-AI repository lineage",
        )
        _add_repository_fetch_deadline_arguments(onboard_parser)
        onboard_parser.add_argument(
            "--apply", action="store_true", help="apply the freshly revalidated plan"
        )
        onboard_parser.add_argument(
            "--acknowledge",
            action="store_true",
            help="acknowledge the reviewed plan before mutation",
        )
        onboard_parser.add_argument(
            "--expect-plan",
            metavar="SHA256",
            help="require the applied plan to match this reviewed plan identity",
        )
    onboard_create.add_argument("--empty", action="store_true")
    onboard_create.add_argument(
        "--type", dest="project_type", choices=sorted(PROJECT_TYPES)
    )
    onboard_create.add_argument(
        "--refine", metavar="REQUEST", help="refine this mission Markdown before apply"
    )
    onboard_adopt.add_argument("--default-branch")
    onboard_adopt.add_argument(
        "--root-plan",
        metavar="SELECTION.json",
        help="review and apply explicit monorepo Component ownership and commands",
    )
    onboard_adopt.add_argument("--run-baseline", action="store_true")
    onboard_adopt.add_argument("--allow-unready", action="store_true")
    onboard_adopt.add_argument("--baseline-timeout-seconds", type=int)
    onboard_adopt.add_argument("--baseline-diagnostic-chars", type=int)
    onboard_adopt.add_argument("--harness-workspace-link", action="append", default=[])
    add_design_parser(commands)
    add_release_parser(commands)
    add_package_parser(commands)
    add_flavor_parser(commands)
    perf = commands.add_parser(
        "perf",
        help=(
            "report recorded per-step timing (coding CLI, model, target, duration) "
            "from the disposable build directory"
        ),
    )
    perf_commands = perf.add_subparsers(dest="perf_command", required=True)
    perf_show = perf_commands.add_parser(
        "show", help="print a table of recorded performance spans"
    )
    perf_show.add_argument("--project", default=".", help="canonical project root")
    perf_show.add_argument(
        "--dir",
        help=(
            "read spans from this exact directory of *.jsonl files instead of the "
            "project's disposable build root; use this to report on a run archived "
            "before its build directory was cleaned"
        ),
    )
    perf_show.add_argument(
        "--group-by",
        choices=("stage", "target_id", "target_kind", "coding_cli", "model", "run_id"),
        default="stage",
        help="aggregate spans by this field (default: stage)",
    )
    perf_show.add_argument(
        "--stage", help="only include spans whose stage equals this exact value"
    )
    perf_show.add_argument(
        "--run-id", help="only include spans from this exact recorded run"
    )
    perf_chart = perf_commands.add_parser(
        "chart", help="render a dependency-free SVG bar chart of recorded spans"
    )
    perf_chart.add_argument("--project", default=".", help="canonical project root")
    perf_chart.add_argument(
        "--dir",
        help=(
            "read spans from this exact directory of *.jsonl files instead of the "
            "project's disposable build root; use this to report on a run archived "
            "before its build directory was cleaned"
        ),
    )
    perf_chart.add_argument(
        "--group-by",
        choices=("stage", "target_id", "target_kind", "coding_cli", "model", "run_id"),
        default="stage",
        help="aggregate spans by this field (default: stage)",
    )
    perf_chart.add_argument(
        "--stage", help="only include spans whose stage equals this exact value"
    )
    perf_chart.add_argument(
        "--run-id", help="only include spans from this exact recorded run"
    )
    perf_chart.add_argument(
        "--output", required=True, help="destination .svg path for the rendered chart"
    )
    matrix = commands.add_parser(
        "matrix",
        help=(
            "run a canonical concurrent Component target matrix without project copies"
        ),
    )
    matrix.add_argument("declaration", help="target-matrix declaration JSON")
    matrix.add_argument("--project", default=".", help="canonical project root")
    matrix.add_argument(
        "--evidence-root",
        required=True,
        help="matrix-scoped runtime, locks, cell receipts, and aggregate receipt",
    )
    matrix.add_argument(
        "--jobs", type=int, default=1, help="maximum concurrent matrix cells"
    )
    matrix.add_argument(
        "--cell-timeout",
        type=float,
        default=3600.0,
        help="maximum seconds allowed for each lifecycle cell",
    )
    matrix.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "reuse only accepted cell receipts whose complete identity remains bound "
            "to the current project authority and exact cell"
        ),
    )
    matrix.add_argument(
        "--allow-host-execution",
        action="store_true",
        help="acknowledge compilation and execution for every declared cell",
    )
    config = commands.add_parser(
        "config", help="inspect and migrate durable private user configuration"
    )
    config_commands = config.add_subparsers(
        dest="config_command", required=True, parser_class=JsonArgumentParser
    )
    config_paths = config_commands.add_parser(
        "paths", help="report platform-resolved user configuration and state paths"
    )
    config_paths.add_argument(
        "project", nargs="?", default=".", help="canonical project (default: current)"
    )
    config_migrate = config_commands.add_parser(
        "migrate",
        help="move recognized 0.8.x private assets into 0.9.0 user custody",
    )
    config_migrate.add_argument(
        "project",
        nargs="?",
        default=".",
        help=(
            "project root containing recognized 0.8.x assets "
            "(default: current directory)"
        ),
    )
    config_mcp = config_commands.add_parser(
        "mcp", help="read or atomically write the typed operator MCP catalog"
    )
    config_mcp_commands = config_mcp.add_subparsers(
        dest="mcp_command", required=True, parser_class=JsonArgumentParser
    )
    config_mcp_commands.add_parser("show", help="read the current typed MCP catalog")
    config_mcp_write = config_mcp_commands.add_parser(
        "write", help="replace the catalog with explicitly selected server ids"
    )
    config_mcp_write.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="ID[:USE]",
        help="server id and optional jira/slack/outlook/registry/other use",
    )
    config_channel = config_commands.add_parser(
        "channel-parse", help="validate an inbound channel trailer for one project"
    )
    config_channel.add_argument("message", type=Path, help="UTF-8 message file")
    config_channel.add_argument(
        "--project", default=".", help="expected canonical project"
    )
    work = commands.add_parser(
        "work", help="record and close typed active-roadmap work items"
    )
    work_commands = work.add_subparsers(
        dest="work_command", required=True, parser_class=JsonArgumentParser
    )
    work_record = work_commands.add_parser("record")
    work_record.add_argument("work_id")
    work_record.add_argument("--title", required=True)
    work_record.add_argument("--priority", required=True)
    work_record.add_argument("--owner", required=True)
    work_record.add_argument("--direction", required=True)
    work_record.add_argument("--conclusion", required=True)
    work_record.add_argument("--depends-on", action="append", default=[])
    work_record.add_argument("--implementation", action="append", required=True)
    work_record.add_argument("--evidence", action="append", required=True)
    work_record.add_argument("--project", default=".")
    work_close = work_commands.add_parser("close")
    work_close.add_argument("work_id")
    work_close.add_argument("--project", default=".")
    video = commands.add_parser(
        "video", help="author, rebuild, and verify narrated instructional courses"
    )
    video_commands = video.add_subparsers(
        dest="video_command", required=True, parser_class=JsonArgumentParser
    )
    for action in ("init", "plan", "build"):
        video_action = video_commands.add_parser(action)
        video_action.add_argument("manifest")
        if action == "build":
            video_action.add_argument("--output", required=True)
            video_action.add_argument(
                "--font",
                required=True,
                help="installed font file used for slide rendering",
            )
            video_action.add_argument(
                "--narration", choices=("say", "espeak", "recorded"), default="recorded"
            )
    video_verify = video_commands.add_parser("verify")
    video_verify.add_argument("receipt")
    video_verify.add_argument(
        "--manifest", help="also reject stale source and evidence"
    )
    document = commands.add_parser(
        "document", help="inspect and verify deterministic document artifacts"
    )
    document_commands = document.add_subparsers(
        dest="document_command", required=True, parser_class=JsonArgumentParser
    )
    document_verify = document_commands.add_parser("verify")
    document_verify.add_argument("--manifest", required=True)
    document_verify.add_argument("--component", required=True)
    config_migrate.add_argument(
        "--apply",
        action="store_true",
        help=(
            "perform the migration (without this flag the command is a pure dry-run)"
        ),
    )
    worker = commands.add_parser(
        "worker", help="inspect and refresh private execution-worker inventory"
    )
    worker_commands = worker.add_subparsers(
        dest="worker_command", required=True, parser_class=JsonArgumentParser
    )
    from .worker_registry import add_registry_arguments

    add_registry_arguments(worker_commands)
    from .worker_provisioning import add_provisioning_arguments

    add_provisioning_arguments(worker_commands)
    worker_health = worker_commands.add_parser(
        "health", help="inspect bounded storage health without dispatch or cleanup"
    )
    worker_health.add_argument(
        "--worker-id", required=True, help="one exact configured worker alias"
    )
    worker_health.add_argument(
        "--worker-config", help="private worker catalog (or LITAI_WORKER_CONFIG)"
    )
    worker_health.add_argument(
        "--health-config",
        required=True,
        help="private versioned worker capacity and path configuration",
    )
    worker_health.add_argument(
        "--alert-state",
        help=(
            "explicit private local history file for deduplicated alert "
            "transitions; its parent must exist"
        ),
    )
    worker_health.add_argument(
        "--job-identity",
        help="optional exact job SHA-256 identity; does not authorize dispatch",
    )
    worker_cleanup = worker_commands.add_parser(
        "cleanup", help="plan or apply exact authorized worker cleanup"
    )
    worker_cleanup_commands = worker_cleanup.add_subparsers(
        dest="worker_cleanup_command", required=True, parser_class=JsonArgumentParser
    )
    for name in ("plan", "apply"):
        cleanup_command = worker_cleanup_commands.add_parser(name)
        cleanup_command.add_argument("--worker-id", required=True)
        cleanup_command.add_argument("--worker-config")
        cleanup_command.add_argument("--health-config", required=True)
        if name == "apply":
            cleanup_command.add_argument("--authorization", required=True)
    worker_probe = worker_commands.add_parser(
        "probe",
        help="probe bounded OS, CPU, memory, and GPU facts for configured workers",
    )
    worker_selection = worker_probe.add_mutually_exclusive_group(required=True)
    worker_selection.add_argument(
        "--worker-id",
        action="append",
        default=[],
        help="configured worker identifier; repeat to probe a subset",
    )
    worker_selection.add_argument(
        "--all", action="store_true", help="probe every configured worker in parallel"
    )
    worker_probe.add_argument(
        "--worker-config", help="private worker catalog (or LITAI_WORKER_CONFIG)"
    )
    worker_probe.add_argument(
        "--output", help="ignored observation catalog (or LITAI_WORKER_OBSERVATIONS)"
    )
    worker_probe.add_argument(
        "--timeout-seconds", type=int, default=20, choices=range(1, 301)
    )
    worker_probe.add_argument(
        "--dry-run", action="store_true", help="return observations without writing"
    )
    worker_nvidia = worker_commands.add_parser(
        "resolve-nvidia",
        help="select an exact retained NVIDIA stack for one observed worker",
    )
    worker_nvidia.add_argument("--compatibility", required=True)
    worker_nvidia.add_argument("--worker-id", required=True)
    worker_nvidia.add_argument("--python-abi", required=True)
    worker_nvidia.add_argument("--observations")
    worker_nvidia.add_argument("--toolkit")
    worker_nvidia.add_argument("--package", action="append", default=[])
    worker_verify_model = worker_commands.add_parser(
        "verify-model",
        help=(
            "prove the live-qualification coding CLI + model in user test config "
            "(or --coding-cli/--model) actually resolves before trusting it"
        ),
    )
    worker_verify_model.add_argument(
        "--coding-cli",
        help="override the coding CLI from project-scoped user config for this check",
    )
    worker_verify_model.add_argument(
        "--model",
        help="override the model from project-scoped user config for this check",
    )
    worker_wheelhouse = worker_commands.add_parser(
        "wheelhouse",
        help="export one manifest-pinned offline Standard worker dependency closure",
    )
    worker_wheelhouse_commands = worker_wheelhouse.add_subparsers(
        dest="wheelhouse_command"
    )
    worker_wheelhouse_export = worker_wheelhouse_commands.add_parser("export")
    worker_wheelhouse_export.add_argument("--framework-wheel", required=True)
    worker_wheelhouse_export.add_argument("--distribution-identity", required=True)
    worker_wheelhouse_export.add_argument("--output", required=True)
    worker_wheelhouse_export.add_argument("--platform")
    worker_wheelhouse_export.add_argument("--python-version")
    worker_wheelhouse_export.add_argument("--implementation")
    worker_wheelhouse_export.add_argument("--abi")
    worker_bootstrap = worker_commands.add_parser(
        "bootstrap",
        help="install one exact verified Standard worker dependency closure",
    )
    bootstrap_source = worker_bootstrap.add_mutually_exclusive_group(required=True)
    bootstrap_source.add_argument("--wheel")
    bootstrap_source.add_argument("--wheelhouse")
    worker_bootstrap.add_argument("--wheel-sha256")
    worker_bootstrap.add_argument("--manifest")
    worker_bootstrap.add_argument("--closure-identity")
    worker_bootstrap.add_argument("--distribution-identity", required=True)
    worker_bootstrap.add_argument("--install-root", required=True)
    worker_bootstrap.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace one mismatched environment after verified staging",
    )
    worker_execute = worker_commands.add_parser(
        "execute",
        help="execute one controller-authored request in an isolated worker workspace",
    )
    worker_execute.add_argument("--worker-file", required=True)
    worker_execute.add_argument("--request", required=True)
    worker_execute.add_argument("--materialization", required=True)
    worker_execute.add_argument("--archive", required=True)
    worker_execute.add_argument("--workspace", required=True)
    worker_execute.add_argument("--cas-root", required=True)
    worker_execute.add_argument("--accepted-source-cache")
    worker_execute.add_argument("--evidence-output")
    worker_execute.add_argument("--cleanup-ticket")
    worker_execute_retained = worker_commands.add_parser(
        "execute-retained",
        help=(
            "execute one identity-bound retained harness attempt on a POSIX SSH worker"
        ),
    )
    worker_execute_retained.add_argument("--worker-file", required=True)
    worker_execute_retained.add_argument("--request", required=True)
    worker_execute_retained.add_argument("--inventory", required=True)
    worker_execute_retained.add_argument("--archive", required=True)
    worker_execute_retained.add_argument("--runtime-archive", required=True)
    worker_execute_retained.add_argument("--workspace", required=True)
    worker_acknowledge = worker_commands.add_parser(
        "acknowledge",
        help="acknowledge imported remote evidence and clean its exact attempt custody",
    )
    worker_acknowledge.add_argument("--cleanup-ticket", required=True)
    worker_acknowledge.add_argument("--manifest-identity", required=True)
    worker_acknowledge.add_argument("--bundle-identity", required=True)
    worker_acknowledge.add_argument("--acknowledgement-root", required=True)
    initialize = commands.add_parser(
        "init",
        help="initialize a canonical project",
        description=(
            "Initialize a new canonical project. With no --flavor selector, the "
            "starter uses Python, GNU Make, the host operating system, and pip-wheel "
            "packaging. Explicit "
            "selectors override those axis defaults. The target may be absent, "
            "empty, or contain only a regular .git directory and regular README.md; "
            "repository metadata and an existing README are preserved. Use "
            "--convert to adopt an existing repository by quarantining it into "
            "_legacy/ and wrapping its recorded operations; pass --convert --plan "
            "to inspect convert readiness without writing. Use --from "
            "URL[#REVISION] to resolve and inherit "
            "that repository's complete ancestor DAG without executing repository "
            "code. With no --from, inherit the installed framework at its highest "
            "published vX.Y.Z tag at or below this CLI version, never HEAD. "
            "Initialization also creates worker-catalog and test-matrix examples "
            "and reports their platform-resolved user configuration destinations."
        ),
    )
    initialize.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "new or repository-bootstrap-only project path (default: current directory)"
        ),
    )
    initialize.add_argument("--project-id")
    initialize.add_argument(
        "--default-branch",
        help=(
            "record this repository-policy default branch; convert otherwise "
            "derives it from origin/HEAD, the current upstream, or an unambiguous "
            "local-only branch"
        ),
    )
    initialize.add_argument(
        "--from",
        dest="repository_from",
        metavar="URL[#REVISION]",
        help=(
            "inherit from this Literate-AI repository and every declared ancestor; "
            "the complete exact lineage is resolved before project files are written"
        ),
    )
    _add_repository_fetch_deadline_arguments(initialize)
    initialize.add_argument("--profile", choices=("canonical",), default="canonical")
    initialize.add_argument(
        "--source-intelligence-provider",
        choices=("none", "codegraph-cli"),
        default="none",
        help=(
            "optional externally provisioned source-intelligence provider "
            "(default: none)"
        ),
    )
    initialize.add_argument(
        "--empty",
        action="store_true",
        help="create framework infrastructure without the portable starter Component",
    )
    initialize.add_argument(
        "--type",
        dest="project_type",
        choices=sorted(PROJECT_TYPES),
        metavar="TYPE",
        help=(
            "project shape before scaffolding (default: application). "
            "application and full-stack install the portable hello starter; "
            "library, component-only, document-generator, firmware, and "
            "open-ended install taxonomy without that starter. Flavor axes still "
            "use smart defaults unless --flavor is given."
        ),
    )
    initialize.add_argument(
        "--no-tool-bootstrap",
        action="store_true",
        help="fail instead of provisioning an explicitly selected missing tool",
    )
    initialize.add_argument(
        "--flavor",
        action="append",
        dest="flavors",
        metavar="SELECTOR",
        help=(
            "Flavor selector to install and use as a project default "
            "(e.g. python, macos, javascript); repeat for multiple. "
            "Known selectors: bazel, cmake, make, repo.sh (build system); cpp, "
            "javascript, python, rust (language); linux, macos, windows (OS); "
            "google-workspace, microsoft-365 (service)."
        ),
    )
    initialize.add_argument(
        "--convert",
        action="store_true",
        help=(
            "litai init --convert adopts an existing repository into literate-ai by "
            "quarantining pre-existing files into _legacy/ and wrapping recorded "
            "operations behind litai.harness.mk. Auto-detects language and "
            "build-system Flavors from the repo when no --flavor is given."
        ),
    )
    initialize.add_argument(
        "--plan",
        dest="convert_plan",
        action="store_true",
        help=(
            "with --convert, inspect the live tree and print convert readiness JSON "
            "without writing files, quarantining, or hashing the tree"
        ),
    )
    initialize.add_argument(
        "--run-baseline",
        action="store_true",
        help=(
            "with --convert, execute host-heavy recorded stages (such as repo.sh "
            "build) during init; default is to record those commands without running "
            "them"
        ),
    )
    initialize.add_argument(
        "--baseline-timeout-seconds",
        type=int,
        metavar="SECONDS",
        help=(
            "with --convert, bound each direct-baseline and wrapper-parity command "
            "(1..86400; default: 1800); the effective deadline is recorded in "
            "conversion evidence"
        ),
    )
    initialize.add_argument(
        "--baseline-diagnostic-chars",
        type=int,
        metavar="CHARS",
        help=(
            "with --convert, bound the combined stdout/stderr failure excerpt "
            "(512..65536; default: 8192); "
            "LITAI_CONVERT_DIAGNOSTIC_CHARS supplies the default for CI"
        ),
    )
    initialize.add_argument(
        "--harness-workspace-link",
        action="append",
        default=[],
        metavar="DESTINATION=SOURCE",
        help=(
            "with --convert, project an explicit external sibling directory beside "
            "each disposable retained-harness source root; repeat for multiple links"
        ),
    )
    initialize.add_argument(
        "--allow-unready",
        action="store_true",
        help=(
            "with --convert, proceed even when readiness is blocked-missing-catalog "
            "and record the catalog gaps in harness inventory"
        ),
    )
    cache = commands.add_parser(
        "cache",
        help=(
            "publish runtime source-cache entries into the project's committable "
            "target so a clone can reuse generated source"
        ),
    )
    cache_commands = cache.add_subparsers(
        dest="cache_command", required=True, parser_class=JsonArgumentParser
    )
    cache_publish = cache_commands.add_parser(
        "publish", help="copy runtime cache entries into the committable target"
    )
    cache_publish.add_argument("--project", default=".")
    cache_publish.add_argument(
        "--target", help="committable target id when the project declares several"
    )

    skills = commands.add_parser(
        "skills",
        help="behavioral diagnostics over the declared specification-to-source catalog",
    )
    skills_commands = skills.add_subparsers(
        dest="skills_command", required=True, parser_class=JsonArgumentParser
    )
    skills_evaluate = skills_commands.add_parser(
        "evaluate",
        help=(
            "run a synthetic prompt corpus against the real skill catalog and "
            "classify each outcome as correct/misroute/over_greedy/fragile_pass"
        ),
        description=(
            "Check whether a skill's own declared description would actually "
            "distinguish it for a given prompt (SKILL-ROUTING-001) -- distinct from "
            "`project validate`'s structural schema/DAG checks, which never look at "
            "whether selection would behave correctly."
        ),
    )
    skills_evaluate.add_argument("--project", default=".")
    skills_evaluate.add_argument(
        "corpus", help="path to a JSON array of {case_id, prompt, expected_skill_id}"
    )
    skills_evaluate.add_argument(
        "--fire-threshold",
        type=float,
        default=None,
        help="minimum overlap score for a skill to be considered selected",
    )
    skills_evaluate.add_argument(
        "--fragile-threshold",
        type=float,
        default=None,
        help="minimum overlap score for a correct pick to count as confidently, not "
        "fragilely, grounded",
    )

    prompt = commands.add_parser(
        "prompt",
        help="translate a direct user request into one bounded provider task",
    )
    prompt_commands = prompt.add_subparsers(
        dest="prompt_command", required=True, parser_class=JsonArgumentParser
    )
    prompt_translate = prompt_commands.add_parser(
        "translate",
        help="apply prompt-master to a direct request; bypass MAC task envelopes",
        description=(
            "Wrap skills/agent/prompt-master as a deterministic command. "
            "MAC-originated envelopes fail closed so Literate AI does not apply a "
            "second translation layer."
        ),
    )
    prompt_translate.add_argument("--project", default=".")
    prompt_translate.add_argument("--component", default=None)
    prompt_translate.add_argument(
        "--coding-cli",
        default=None,
        help="provider name recorded in the task envelope (not a model call)",
    )
    prompt_translate.add_argument(
        "--mac-envelope",
        action="store_true",
        help="declare that MAC already translated this task and refuse a second pass",
    )
    prompt_translate.add_argument(
        "--file",
        help="read the rough request from this UTF-8 file instead of REQUEST",
    )
    prompt_translate.add_argument(
        "request",
        nargs="*",
        help="rough direct-user request to translate",
    )

    catalog = commands.add_parser(
        "catalog",
        help="compose and inspect the project's catalog provenance DAG",
    )
    catalog_commands = catalog.add_subparsers(
        dest="catalog_command", required=True, parser_class=JsonArgumentParser
    )

    catalog_copy_parser = catalog_commands.add_parser(
        "copy",
        help="copy Flavor/skill/Component/MCP items from another literate-ai project",
        description=(
            "Copy named catalog items from a source literate-ai project into this "
            "project, recording provenance in .literate/imports.json and enforcing "
            "the DAG constraint (no cycles back to literate-ai root)."
        ),
    )
    catalog_copy_parser.add_argument(
        "source",
        help=(
            "source project: local path, local:/abs/path, git:<url>[#revision], "
            "or a git URL with optional #revision"
        ),
    )
    catalog_copy_parser.add_argument(
        "item",
        nargs="+",
        help=(
            "catalog items to copy as kind:name, for example flavor:openscad, "
            "mcp:loan-risk-tools, workflow:production/staging/dev, "
            "routing:production/staging/dev, or "
            "skill:specification-to-source/openscad-parametric-enclosure"
        ),
    )
    catalog_copy_parser.add_argument(
        "--project", default=".", help="destination project root"
    )
    catalog_copy_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace destination item if it already exists",
    )

    catalog_graph_parser = catalog_commands.add_parser(
        "graph",
        help="show the project's provenance DAG rooted at literate-ai",
    )
    catalog_graph_parser.add_argument(
        "--project", default=".", help="project root to inspect"
    )

    catalog_migrate_flavors = catalog_commands.add_parser(
        "migrate-flavor-names",
        help="plan or record the ADR 0010 canonical Flavor-name migration",
    )
    catalog_migrate_flavors.add_argument(
        "--project", default=".", help="project root to migrate"
    )
    catalog_migrate_flavors.add_argument(
        "--record",
        action="store_true",
        help="record the planned rewrites and directory moves (default is dry-run)",
    )

    graph = commands.add_parser(
        "graph",
        help="solve and export effective repository and catalog authority",
        description=(
            "Compose repository ancestry with local and inherited Components, Flavors, "
            "and skills; reject cycles and export the canonical provenance DAG."
        ),
    )
    graph.add_argument(
        "graph_action",
        nargs="?",
        choices=("show", "rebalance"),
        default="show",
        help="show the graph or report exact-identity upward-migration candidates",
    )
    graph.add_argument("--project", default=".", help="project root to inspect")
    graph.add_argument(
        "--format",
        choices=("json", "text", "mermaid", "dot", "svg"),
        default="text",
        help="deterministic graph representation",
    )
    graph.add_argument(
        "--output",
        help="write the selected representation to a new file",
    )
    graph.add_argument(
        "--kind",
        action="append",
        choices=("repository", "component", "flavor", "flavor-revision", "skill"),
        default=[],
        help="include only this node kind; repeat for a union",
    )
    graph.add_argument(
        "--provenance",
        action="append",
        default=[],
        help="include only entities originating in this project; repeat for a union",
    )
    graph.add_argument(
        "--ownership",
        choices=("local", "inherited"),
        help="include only locally owned or inherited nodes",
    )
    graph.add_argument(
        "--inheritance",
        choices=("inheritable", "private"),
        help="include only downstream-inheritable or private nodes",
    )
    graph.add_argument(
        "--edge-kind",
        action="append",
        default=[],
        help="include only this typed edge; repeat for a union",
    )

    render = commands.add_parser(
        "render", help="publish derived observability artifacts"
    )
    render_commands = render.add_subparsers(
        dest="render_command", required=True, parser_class=JsonArgumentParser
    )
    render_html = render_commands.add_parser(
        "html",
        help="render one provenance-bound single-file HTML view",
        description=(
            "Render using the actual non-editable framework wheel. Reuse verified "
            "cached bytes and their observation time; preserve foreign or edited "
            "output. No CDN fetch or browser is started by this command."
        ),
    )
    render_html.add_argument("--project", default=".", help="project to observe")
    render_html.add_argument(
        "--surface", default="authority-graph", help="registered JSON surface"
    )
    render_html.add_argument("--view", default="dag", help="registered view identifier")
    render_html.add_argument(
        "--view-version", default="1.0.0", help="exact view version"
    )
    render_html.add_argument(
        "--scope-kind", choices=("project", "component"), default="project"
    )
    render_html.add_argument(
        "--scope-identifier", help="scope identity (default: selected project ID)"
    )
    render_html.add_argument(
        "--output", default="graph.html", help="project-relative .html destination"
    )
    render_html.add_argument(
        "--cache-mode",
        choices=("off", "read-only", "write-only", "read-write"),
        default="read-write",
        help="cache access only; read-only may still publish the requested HTML file",
    )
    render_html.add_argument(
        "--external-asset-policy",
        choices=("inline-only", "pinned-cdn"),
        default="pinned-cdn",
        help="the DAG requires one SRI-pinned CDN library; inline-only refuses",
    )
    render_dashboard = render_commands.add_parser(
        "dashboard",
        help="compose exact rendered artifacts from one or more projects",
        description=(
            "Create an offline iframe shell over each selected project's declared "
            "and already-rendered HTML observability artifacts."
        ),
    )
    render_dashboard.add_argument(
        "--root", default=".", help="common directory containing every project"
    )
    render_dashboard.add_argument(
        "--project",
        action="append",
        required=True,
        help="project root to include; repeat to compose multiple projects",
    )
    render_dashboard.add_argument(
        "--output", default="dashboard.html", help="root-relative .html destination"
    )

    build = commands.add_parser(
        "build",
        help=(
            "build a Component from its current specifications and keep the runnable "
            "artifact; then use `litai run` to execute it"
        ),
    )
    build.add_argument("component", nargs="?", default=None)
    build.add_argument("--project", default=".")
    build.add_argument(
        "--target",
        default="host",
        help="generation/Flavor target profile (distinct from --worker)",
    )
    add_execution_worker_arguments(build)
    build.add_argument("--flavor", action="append", default=[])
    build.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    build.add_argument("--jobs", type=int, default=None)
    build.add_argument(
        "--force-regeneration",
        action="store_true",
        help="regenerate source even when an accepted cache entry matches",
    )
    build.add_argument(
        "--source-cache-entry",
        action="append",
        default=[],
        help=(
            "select one exact sha256 cache entry when a derivation has multiple "
            "candidates"
        ),
    )
    build.add_argument(
        "--update-receipt",
        action="store_true",
        help="commit the resulting project test receipt",
    )
    build.add_argument(
        "--from-accepted-source",
        action="store_true",
        help=(
            "for a Standard lifecycle, require exact verifier-admitted source-cache "
            "membership for every Component and never invoke a coding CLI"
        ),
    )
    test_command = commands.add_parser(
        "test",
        help=(
            "build a Component from current specifications and require its current "
            "generated tests and acceptance gates to pass"
        ),
    )
    test_command.add_argument("component", nargs="?", default=None)
    test_command.add_argument("--project", default=".")
    test_command.add_argument("--flavor", action="append", default=[])
    test_command.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    test_command.add_argument("--jobs", type=int, default=None)
    test_command.add_argument(
        "--target",
        default="host",
        help="generation/Flavor target profile (distinct from --worker)",
    )
    add_execution_worker_arguments(test_command)
    test_command.add_argument(
        "--force-regeneration",
        action="store_true",
        help="regenerate source even when an accepted cache entry matches",
    )
    test_command.add_argument(
        "--source-cache-entry",
        action="append",
        default=[],
        help=(
            "select one exact sha256 cache entry when a derivation has multiple "
            "candidates"
        ),
    )
    test_command.add_argument(
        "--update-receipt",
        action="store_true",
        help="commit the resulting project test receipt",
    )
    test_command.add_argument(
        "--from-accepted-source",
        action="store_true",
        help=(
            "for a Standard lifecycle, require exact verifier-admitted source-cache "
            "membership for every Component and never invoke a coding CLI"
        ),
    )
    run_command = commands.add_parser(
        "run",
        help="run a Component built by `litai build`, passing through any arguments",
    )
    run_command.add_argument("component", nargs="?", default=None)
    run_command.add_argument("--project", default=".")
    run_command.add_argument(
        "--target",
        default="host",
        help="generation/Flavor target profile (distinct from --worker)",
    )
    add_execution_worker_arguments(run_command)
    run_command.add_argument(
        "--flavor",
        action="append",
        default=[],
        help="repeat the exact build Flavor selectors when they were explicit",
    )
    run_command.add_argument(
        "--entrypoint",
        help=(
            "named entrypoint to execute from a multi-entrypoint Component; "
            "omission uses the Component's first declared entrypoint"
        ),
    )
    run_command.add_argument(
        "arguments",
        nargs="*",
        help="arguments passed through to the built application",
    )
    verify = commands.add_parser(
        "verify",
        help=(
            "check declared project authority; writes nothing, builds nothing, and "
            "reaches no model"
        ),
    )
    verify.add_argument("path", nargs="?", default=".")
    verify.add_argument(
        "--gate",
        dest="gates",
        action="append",
        default=[],
        help=(
            "run only this gate (repeatable). Default: all gates -- authority, locks, "
            "source-intelligence (always skipped when provider is none), "
            "html-observability (declared HTML currency), receipt. "
            "Verify never builds; that is rebuild's job"
        ),
    )
    update = commands.add_parser(
        "update",
        help=(
            "plan, and optionally apply, three-way updates from the complete recorded "
            "repository lineage and installed framework while preserving local "
            "authority"
        ),
        description=(
            "Re-resolve every recorded repository ancestor, compose prospective "
            "Components, Flavors, skills, workflows, and routing policies, and compare "
            "them plus framework-owned scaffold files with their previous provenance "
            "and current local bytes. "
            "Planning is read-only. --apply writes upstream-only and clean merges. "
            "New files require --adopt-added. Overlapping conflicts require "
            "--resolutions or an explicit inherited --take-upstream choice; "
            "--keep-local retains a retired dependency."
        ),
    )
    update.add_argument("path", nargs="?", default=".")
    update.add_argument(
        "--resolutions",
        metavar="FILE",
        help=(
            "with --apply, apply explicitly reviewed identity-bound "
            "conflict_reviews from update JSON"
        ),
    )
    update.add_argument(
        "--apply",
        action="store_true",
        help=(
            "apply upstream-only changes and clean three-way text merges; "
            "overlapping conflicts require explicitly reviewed resolutions"
        ),
    )
    update.add_argument(
        "--adopt-added",
        action="store_true",
        help="with --apply, also add files upstream introduced that this project lacks",
    )
    update.add_argument(
        "--take-upstream",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "with --apply, replace this planned inherited-catalog conflict with its "
            "upstream bytes; repeat for each explicitly reviewed path"
        ),
    )
    update.add_argument(
        "--keep-local",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "with --apply, preserve this planned inherited-catalog removal as local "
            "authority; repeat for each explicitly reviewed path"
        ),
    )
    update.add_argument(
        "--record-work-items",
        action="store_true",
        help=(
            "append advisory work items for upstream deltas to the project queue; "
            "the plan itself stays read-only"
        ),
    )
    update.add_argument(
        "--queue",
        default=DEFAULT_QUEUE_PATH,
        help=f"project-relative queue to append to (default: {DEFAULT_QUEUE_PATH})",
    )
    update.add_argument(
        "--unpin",
        action="store_true",
        help=(
            "when the recorded parent selector is an exact commit, change it to "
            "HEAD; requires a later --apply to write the selector"
        ),
    )
    update.add_argument(
        "--follow-ref",
        metavar="REF",
        help=(
            "explicitly change the recorded parent selector to this Git revision "
            "(HEAD, release/0.5.x, or a SHA). Does not write without --apply"
        ),
    )
    update.add_argument(
        "--allow-major",
        action="store_true",
        help=(
            "retarget the parent to the next newer release/X.Y.x line advertised "
            "by the parent repository; never a silent default"
        ),
    )
    update.add_argument(
        "--review-conflicts",
        action="store_true",
        help=(
            "ask the selected coding CLI to propose keep-local, take-upstream, or "
            "merge for each remaining conflict; writes nothing until a later --apply "
            "of the mechanical subset"
        ),
    )
    _add_repository_fetch_deadline_arguments(update)
    reparent = commands.add_parser(
        "reparent",
        help="plan or apply a cautious change to the complete repository parent chain",
        description=(
            "Resolve and validate the entire prospective parent DAG before changing "
            "authority. Pass a repository URL (optionally suffixed #REVISION), or "
            "pass the literal 'none' to become an explicit root project -- a divorce "
            "from the existing parent chain. Planning is read-only; --apply uses "
            "compare-and-swap and rolls back if project validation fails."
        ),
    )
    reparent.add_argument(
        "parent",
        metavar="URL[#REVISION]|none",
        help="new direct parent repository, or 'none' for an explicit root project",
    )
    reparent.add_argument(
        "--project",
        default=".",
        help="canonical project root (default: current directory)",
    )
    reparent.add_argument(
        "--apply",
        action="store_true",
        help=(
            "commit the already-resolved lineage authority after a final re-resolution"
        ),
    )
    _add_repository_fetch_deadline_arguments(reparent)
    learn = commands.add_parser(
        "learn", help="plan one read-only authority lesson from exact run evidence"
    )
    learn.add_argument("run", help="versioned learning-plan input JSON")
    lock = commands.add_parser(
        "lock", help="resolve or verify exact Component or repository locks"
    )
    lock.add_argument("component", nargs="?", default=".")
    lock.add_argument("--target", default="host")
    lock.add_argument(
        "--flavor",
        action="append",
        default=[],
        help=(
            "ordered +flavor/-flavor, +slot:flavor/-slot:flavor, or "
            "component://namespace/name::+selector override"
        ),
    )
    lock.add_argument(
        "--flavor-root",
        action="append",
        default=[],
        help="Flavor directory or catalog root (repeatable)",
    )
    lock_mode = lock.add_mutually_exclusive_group()
    lock_mode.add_argument(
        "--check", action="store_true", help="verify without writing"
    )
    lock_mode.add_argument(
        "--diff",
        action="store_true",
        help="report structured differences without writing",
    )
    lock_mode.add_argument(
        "--large-review",
        choices=("start", "status", "acknowledge", "apply", "cleanup"),
        help=(
            "explicitly review a genuine large lock transition in bounded "
            "identity-chained pages"
        ),
    )
    lock.add_argument(
        "--transaction-id",
        help="exact sha256 identity returned by large-review start",
    )
    lock.add_argument(
        "--page-identity",
        help="exact next page identity to acknowledge",
    )
    component = commands.add_parser(
        "component", help="author, migrate, and inspect Components"
    )
    component_commands = component.add_subparsers(
        dest="component_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    component_migrate = component_commands.add_parser(
        "migrate",
        help=(
            "project one Component or every Component in a canonical project from "
            "legacy component.json into readable component.md"
        ),
    )
    component_migrate.add_argument(
        "component", nargs="?", default=".", help="Component or canonical project root"
    )
    component_migration_mode = component_migrate.add_mutually_exclusive_group()
    component_migration_mode.add_argument(
        "--check", action="store_true", help="verify migration without writing"
    )
    component_migration_mode.add_argument(
        "--diff",
        action="store_true",
        help="report migration state without writing",
    )
    for clean_command, help_text in (
        ("clean", "remove generated objects and executables from OBJ_DIR"),
        (
            "really-clean",
            "remove OBJ_DIR and generated sources from BUILD_DIR",
        ),
    ):
        clean = commands.add_parser(clean_command, help=help_text)
        clean.add_argument("--project", default=".", help="canonical project root")

    project = commands.add_parser(
        "project", help="validate project authority, indexes, and test receipts"
    )
    project_commands = project.add_subparsers(
        dest="project_command", required=True, parser_class=JsonArgumentParser
    )
    project_validate = project_commands.add_parser("validate")
    project_validate.add_argument("path", nargs="?", default=".")
    project_guidance = project_commands.add_parser(
        "guidance", help="project configured policy for one repository operation"
    )
    project_guidance.add_argument(
        "--operation",
        required=True,
        choices=("develop", "land", "release", "backport", "gc", "issue-close"),
    )
    project_guidance.add_argument("path", nargs="?", default=".")
    project_lifecycle = project_commands.add_parser(
        "lifecycle",
        help="review and update project lifecycle authority",
    )
    project_lifecycle_commands = project_lifecycle.add_subparsers(
        dest="project_lifecycle_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    project_standard_rebind = project_lifecycle_commands.add_parser(
        "rebind-standard",
        help="plan or apply an exact installed-wheel Standard binding update",
    )
    project_standard_rebind.add_argument(
        "plan",
        nargs="?",
        help="reviewed JSON plan path (required with --apply)",
    )
    project_standard_rebind.add_argument("--project", default=".")
    project_standard_rebind.add_argument(
        "--output",
        help="write the plan to a new JSON file for review before apply",
    )
    project_standard_rebind.add_argument(
        "--apply",
        action="store_true",
        help="atomically apply the reviewed plan",
    )
    project_standard_rebind.add_argument(
        "--authorize-rebind",
        action="store_true",
        help="authorize changing Standard lifecycle and receipt-policy authority",
    )
    project_ci_plan = project_commands.add_parser(
        "ci-plan",
        help="detect test frameworks and emit a fail-closed shard/impact plan",
    )
    project_ci_plan.add_argument("path", nargs="?", default=".")
    project_ci_plan.add_argument(
        "--mode",
        choices=("shard", "impact", "compose"),
        default="compose",
        help="shard remaining tests, select by impact, or compose impact then shard",
    )
    project_source_intelligence = project_commands.add_parser(
        "source-intelligence",
        help="synchronize or check explicitly configured project source intelligence",
    )
    project_source_intelligence_commands = project_source_intelligence.add_subparsers(
        dest="source_intelligence_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    for source_intelligence_command in ("sync", "check"):
        source_intelligence_parser = project_source_intelligence_commands.add_parser(
            source_intelligence_command
        )
        source_intelligence_parser.add_argument("path", nargs="?", default=".")
        source_intelligence_parser.add_argument(
            "--binary",
            help="explicit provider command override for this invocation",
        )
    project_mac_contract = project_commands.add_parser(
        "mac-contract",
        help="project .mac/project.yaml from one resolved CycloneDX BOM",
    )
    project_mac_contract.add_argument(
        "resolved_bom", help="exact post-build CycloneDX 1.7 JSON BOM"
    )
    project_mac_contract.add_argument("--project", default=".")
    project_mac_contract.add_argument(
        "--platform",
        action="append",
        required=True,
        choices=("darwin", "linux", "windows", "wsl2"),
        help="MAC host family; repeat for every supported platform",
    )
    project_mac_contract.add_argument("--bootstrap-command", default="litai build")
    project_mac_contract.add_argument("--test-command", default="litai test")
    project_mac_contract.add_argument(
        "--bootstrap-creates", action="append", default=[]
    )
    project_documentation_update = project_commands.add_parser(
        "documentation-update",
        help="plan documentation drift against current project authority and evidence",
    )
    project_documentation_update.add_argument("path", nargs="?", default=".")
    project_documentation_update.add_argument("--apply", action="store_true")
    project_documentation_update.add_argument(
        "--allow-model-egress", action="store_true"
    )
    project_documentation_update.add_argument(
        "--model", help="explicit coding-CLI model for authorized reconciliation"
    )
    project_documentation_review = project_commands.add_parser("documentation-review")
    project_documentation_review.add_argument("path", nargs="?", default=".")
    project_documentation_review.add_argument(
        "--record",
        action="store_true",
        help="atomically record the exact validated authority-review marker",
    )
    project_peer_work = project_commands.add_parser(
        "peer-work",
        help="survey, mark, or safely collect peer branches and worktrees",
    )
    project_peer_work.add_argument(
        "peer_work_action",
        nargs="?",
        help="status, survey, mark, or gc; legacy invocations may put PATH here",
    )
    project_peer_work.add_argument("path", nargs="?", default=".")
    project_peer_work.add_argument(
        "--when",
        choices=("start", "end"),
        help="start lists green open PRs/MRs; end lists leftover worktrees",
    )
    project_peer_work.add_argument("--state", choices=("merged", "dead"))
    project_peer_work.add_argument("--branch")
    project_peer_work.add_argument("--actor")
    project_peer_work.add_argument("--reason")
    project_peer_work.add_argument("--push", action="store_true")
    project_peer_work.add_argument("--apply", action="store_true")
    project_peer_work.add_argument("--authorize-delete", action="store_true")
    project_worktree = project_commands.add_parser(
        "worktree",
        help="resolve the canonical project-scoped linked-worktree location",
    )
    project_worktree_commands = project_worktree.add_subparsers(
        dest="worktree_action",
        required=True,
        parser_class=JsonArgumentParser,
    )
    project_worktree_location = project_worktree_commands.add_parser("location")
    project_worktree_location.add_argument("path", nargs="?", default=".")
    project_tracker = project_commands.add_parser(
        "tracker",
        help="classify the issue tracker from Git remotes and name gh or glab",
    )
    project_tracker_commands = project_tracker.add_subparsers(
        dest="tracker_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    project_tracker_inspect = project_tracker_commands.add_parser(
        "inspect",
        help="read remotes from .git/config and name gh or glab",
    )
    project_tracker_inspect.add_argument("path", nargs="?", default=".")
    project_parent = project_commands.add_parser(
        "parent",
        help="check out parent repositories under parents/<id> in this project",
    )
    project_parent_commands = project_parent.add_subparsers(
        dest="parent_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    project_parent_checkout = project_parent_commands.add_parser(
        "checkout",
        help="clone or update a parent under parents/<id> with submodules and LFS",
    )
    project_parent_checkout.add_argument(
        "source",
        help="parent Git URL with optional #revision",
    )
    project_parent_checkout.add_argument("--project", default=".")
    _add_repository_fetch_deadline_arguments(project_parent_checkout)
    project_convert_stage = project_commands.add_parser(
        "convert-stage", help="show or explicitly advance adopted-project authority"
    )
    project_convert_stage_commands = project_convert_stage.add_subparsers(
        dest="convert_stage_command", required=True, parser_class=JsonArgumentParser
    )
    project_convert_stage_show = project_convert_stage_commands.add_parser("show")
    project_convert_stage_show.add_argument("--project", default=".")
    project_convert_stage_advance = project_convert_stage_commands.add_parser("advance")
    project_convert_stage_advance.add_argument(
        "--to", required=True, choices=("retained", "drafted", "qualified")
    )
    project_convert_stage_advance.add_argument("--project", default=".")
    project_retained_scope = project_commands.add_parser(
        "retained-scope", help="review and refresh retained source membership"
    )
    retained_scope_commands = project_retained_scope.add_subparsers(
        dest="retained_scope_command", required=True, parser_class=JsonArgumentParser
    )
    scope_refresh = retained_scope_commands.add_parser("refresh")
    scope_refresh.add_argument("--project", default=".")
    scope_refresh.add_argument("--apply", action="store_true")
    scope_refresh.add_argument("--acknowledge", action="store_true")
    scope_refresh.add_argument("--expected-plan-identity")
    scope_refresh.add_argument(
        "--run-component-baselines",
        action="store_true",
        help="execute stale refined Component harnesses in disposable source copies",
    )
    project_retained_harness = project_commands.add_parser(
        "retained-harness",
        help="review and replace an unqualified converted harness",
    )
    retained_harness_commands = project_retained_harness.add_subparsers(
        dest="retained_harness_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    harness_readmit = retained_harness_commands.add_parser("readmit")
    harness_readmit.add_argument("--project", default=".")
    harness_readmit.add_argument("--apply", action="store_true")
    harness_readmit.add_argument("--acknowledge", action="store_true")
    harness_readmit.add_argument("--expected-plan-identity")
    harness_readmit.add_argument("--worker-id", default="local")
    harness_readmit.add_argument("--worker-config")
    harness_readmit.add_argument("--timeout-seconds", type=int, default=1800)
    harness_readmit.add_argument(
        "--retain-stage",
        action="append",
        default=[],
        metavar="STAGE_ID",
        help=(
            "retain one exact stage from the current admitted inventory when fresh "
            "inspection does not rediscover it; repeat for multiple stages"
        ),
    )
    retained_cargo = project_commands.add_parser(
        "retained-cargo", help="check or provision a reviewed retained Cargo package"
    )
    retained_cargo_commands = retained_cargo.add_subparsers(
        dest="retained_cargo_command", required=True, parser_class=JsonArgumentParser
    )
    for operation in ("check", "materialize", "admit"):
        retained = retained_cargo_commands.add_parser(operation)
        retained.add_argument("--project", default=".")
        for option in (
            "binding",
            "plan",
            "gates",
            "reviewed-binding",
            "reviewed-gates",
            "provider-project",
            "provider-component",
            "provider-profile",
            "provider-id",
            "store-id",
        ):
            retained.add_argument("--" + option, required=True)
        for option in ("binding-size", "gate-size"):
            retained.add_argument("--" + option, type=int, required=True)
        retained.add_argument("--target", default="host")
        retained.add_argument("--model")
        archive_source = retained.add_mutually_exclusive_group(required=True)
        archive_source.add_argument("--archive")
        archive_source.add_argument(
            "--archive-https", help="explicit HTTPS evidence CAS base URL"
        )
        retained.add_argument(
            "--archive-token-env",
            help="environment variable containing the HTTPS bearer token",
        )
        retained.add_argument("--offline", action="store_true")
        if operation == "admit":
            for option in (
                "tests",
                "reviewed-tests",
                "retirement",
                "reviewed-retirement",
            ):
                retained.add_argument("--" + option, required=True)
            for option in ("tests-size", "retirement-size"):
                retained.add_argument("--" + option, type=int, required=True)
            retained.add_argument("--allow-host-execution", action="store_true")
            retained.add_argument(
                "--acknowledge-source-retirement", action="store_true"
            )
            retained.add_argument("--timeout-seconds", type=int, default=900)
    project_receipt = project_commands.add_parser("test-receipt")
    project_receipt_commands = project_receipt.add_subparsers(
        dest="test_receipt_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    for evidence_command in (
        "verify-evidence",
        "publish-evidence",
        "require-current-evidence",
    ):
        receipt_evidence = project_receipt_commands.add_parser(evidence_command)
        receipt_evidence.add_argument("plan")
        receipt_evidence.add_argument("--project", default=".")
        receipt_evidence.add_argument("--policy", required=True)
        receipt_evidence.add_argument("--revocations", required=True)
        if evidence_command == "publish-evidence":
            receipt_evidence.add_argument("--retention", action="append", required=True)
            receipt_evidence.add_argument("--bundle-store", required=True)
        elif evidence_command == "require-current-evidence":
            receipt_evidence.add_argument("--bundle-store", required=True)
        else:
            evidence_roots = receipt_evidence.add_mutually_exclusive_group(
                required=True
            )
            evidence_roots.add_argument("--retention", action="append")
            evidence_roots.add_argument("--current-map")
            receipt_evidence.add_argument("--bundle-store")
        for store_option in ("--store", "--monorepo-store", "--https-store"):
            receipt_evidence.add_argument(
                store_option, action="append", default=[], metavar="ID=LOCATION"
            )
    for receipt_command in ("update", "check"):
        receipt_parser = project_receipt_commands.add_parser(receipt_command)
        receipt_parser.add_argument("candidate")
        receipt_parser.add_argument("--project", default=".")
    receipt_retained = project_receipt_commands.add_parser(
        "run-retained",
        help="run a qualified retained harness and emit a finalized candidate",
    )
    receipt_retained.add_argument("candidate")
    receipt_retained.add_argument("--project", default=".")
    receipt_retained.add_argument("--evidence")
    receipt_retained.add_argument("--worker-id", default="local")
    receipt_retained.add_argument(
        "--worker-config",
        help=(
            "private worker catalog for a non-local --worker-id "
            "(default: LITAI_WORKER_CONFIG or the user configuration root)"
        ),
    )
    receipt_retained.add_argument("--timeout-seconds", type=int, default=1800)
    receipt_retained.add_argument("--known-failure-report")
    receipt_retained.add_argument(
        "--harness-workspace-link",
        action="append",
        default=[],
        metavar="DESTINATION=SOURCE",
        help=(
            "project an external sibling directory beside the disposable retained "
            "implementation; repeat exactly the destinations qualified at conversion"
        ),
    )
    receipt_current = project_receipt_commands.add_parser("require-current")
    receipt_current.add_argument("--project", default=".")
    project_test_checkpoint = project_commands.add_parser(
        "test-checkpoint",
        help="annotate, inspect, revalidate, clear, or import known test failures",
    )
    test_checkpoint_commands = project_test_checkpoint.add_subparsers(
        dest="test_checkpoint_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    for checkpoint_operation in (
        "inspect",
        "annotate",
        "revalidate",
        "clear",
        "import",
    ):
        checkpoint_parser = test_checkpoint_commands.add_parser(checkpoint_operation)
        checkpoint_parser.add_argument("--project", default=".")
        checkpoint_parser.add_argument("--state", required=True)
        checkpoint_parser.add_argument("--suite", default="python-unittest")
        if checkpoint_operation in {"annotate", "revalidate", "clear"}:
            checkpoint_parser.add_argument("--pin", required=True)
            checkpoint_parser.add_argument("--test-key", required=True)
        if checkpoint_operation == "annotate":
            checkpoint_parser.add_argument(
                "--cause",
                required=True,
                choices=tuple(item.value for item in KnownTestFailureCause),
            )
            context = checkpoint_parser.add_mutually_exclusive_group(required=True)
            context.add_argument("--context-identity")
            context.add_argument("--universal-authority")
            checkpoint_parser.add_argument("--failure-identity", required=True)
            checkpoint_parser.add_argument("--evidence-run-identity", required=True)
            checkpoint_parser.add_argument("--evidence-node-id", required=True)
            checkpoint_parser.add_argument("--reason", required=True)
            checkpoint_parser.add_argument("--expires-at")
            checkpoint_parser.add_argument("--tracker")
        if checkpoint_operation == "import":
            checkpoint_parser.add_argument("report")
            checkpoint_parser.add_argument("--release-evidence", action="store_true")

    plan = commands.add_parser(
        "plan", help="resolve an exact generation or repository plan"
    )
    plan.add_argument("specification")
    plan.add_argument("--recipe-id")
    plan.add_argument("--target", default="host")
    plan.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    plan.add_argument(
        "--flavor",
        action="append",
        default=[],
        help=(
            "ordered +flavor/-flavor or +slot:flavor/-slot:flavor selector; "
            "prefix component://namespace/name:: to target one Component; use "
            "--flavor=-python"
        ),
    )
    plan.add_argument(
        "--flavor-root",
        action="append",
        default=[],
        help="Flavor directory or catalog root (repeatable)",
    )

    generate = commands.add_parser(
        "generate", help="generate and index disposable source from specifications"
    )
    generate.add_argument("specification")
    generate.add_argument("--output", required=True)
    generate.add_argument("--recipe-id")
    generate.add_argument("--target", default="host")
    generate.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    generate.add_argument(
        "--flavor",
        action="append",
        default=[],
        help=(
            "ordered +flavor/-flavor or +slot:flavor/-slot:flavor selector; "
            "prefix component://namespace/name:: to target one Component; use "
            "--flavor=-python"
        ),
    )
    generate.add_argument(
        "--flavor-root",
        action="append",
        default=[],
        help="Flavor directory or catalog root (repeatable)",
    )
    generate.add_argument(
        "--admit",
        action="store_true",
        help=(
            "independently execute source tests and publish verifier-admitted "
            "source-only cache membership"
        ),
    )
    generate.add_argument(
        "--source-test-command",
        action="append",
        default=[],
        metavar="JSON_ARGV",
        help=(
            "verifier-owned source test command as a JSON argv array; repeatable "
            "and required with --admit"
        ),
    )
    generate.add_argument(
        "--source-portability",
        choices=("target-independent", "target-specific"),
        default="target-specific",
        help="declare whether generated source is portable across target profiles",
    )
    rebuild = commands.add_parser(
        "rebuild",
        help=(
            "reconcile after a litai update: re-run the coding agent over new plan "
            "entries, regenerate source where needed, then build. Use `litai build` "
            "for an ordinary build"
        ),
        description=(
            "Run the complete minimum lifecycle: compose/plan; resolve a source-cache "
            "hit or miss; generate source and current tests when needed; bind the "
            "source-tree identity; validate, classify, and authorize; invoke the "
            "selected native dependency resolver and build system; verify CycloneDX "
            "1.7 source and resolved dependency evidence; run generated tests and "
            "the application; "
            "perform independent acceptance; admit the workspace; and leave a compact "
            "candidate receipt. "
            "Packaging, publication, and deployment are optional project-declared "
            "extensions and are not claimed unless configured and receipted."
        ),
    )
    rebuild.add_argument(
        "specification",
        help=(
            "project-relative Component/specification entry point, or . for the project"
        ),
    )
    rebuild.add_argument("--project", default=".", help="canonical project root")
    rebuild.add_argument(
        "--target",
        default="host",
        help="generation/Flavor target profile",
    )
    rebuild.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    rebuild.add_argument(
        "--runtime-root",
        help=(
            "new or empty external runtime directory; Standard allocates a secure "
            "temporary directory when omitted"
        ),
    )
    rebuild.add_argument(
        "--candidate-receipt",
        help=("optional new external path for the compact passing receipt"),
    )
    rebuild.add_argument(
        "--build-dir",
        help=(
            "generated-source cache root; overrides the BUILD_DIR environment "
            "variable and the portable <project>/generated default"
        ),
    )
    rebuild.add_argument(
        "--obj-dir",
        help=(
            "object/tool cache root; overrides the OBJ_DIR environment variable "
            "and the portable <project>/_build default"
        ),
    )
    rebuild.add_argument(
        "--update-receipt",
        action="store_true",
        help="atomically commit the passing receipt configured by the project",
    )
    rebuild.add_argument(
        "--from-accepted-source",
        action="store_true",
        help=(
            "for a Standard lifecycle, require exact verifier-admitted source-cache "
            "membership for every Component and never invoke a coding CLI"
        ),
    )
    rebuild.add_argument(
        "--keep-runtime",
        action="store_true",
        help="retain an automatically allocated successful Standard runtime",
    )
    rebuild.add_argument(
        "--retained-project",
        help="complete retained input manifest; explicit original-source authority",
    )
    rebuild.add_argument(
        "--retained-project-profile",
        help="manifest-owned locked build/test/docs action profile",
    )
    rebuild.add_argument(
        "--retained-project-plan",
        action="store_true",
        help="validate retained project and inputs read-only; grants no admission",
    )
    rebuild.add_argument(
        "--authorize-retained-project",
        help="exact reviewed retained-project plan identity",
    )
    rebuild.add_argument(
        "--retained-source",
        help="exact local UTF-8 source tree to qualify, never regenerate",
    )
    rebuild.add_argument(
        "--retained-source-plan",
        action="store_true",
        help="read-only retained input review; grants no admission",
    )
    rebuild.add_argument(
        "--authorize-retained-source",
        help="exact sha256 identity returned by retained-source planning",
    )
    rebuild.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="cap parallel Component operations (default: admitted worker capacity)",
    )
    rebuild.add_argument(
        "--flavor",
        action="append",
        default=[],
        help=(
            "ordered explicit +flavor/-flavor or +slot:flavor/-slot:flavor selector; "
            "prefix component://namespace/name:: to target one Component; repeat to "
            "preserve precedence"
        ),
    )
    rebuild.add_argument(
        "--force-regeneration",
        action="store_true",
        help=(
            "bypass configured source-cache reads for every exact derivation; "
            "post-acceptance publication remains independently controlled"
        ),
    )
    rebuild.add_argument(
        "--source-cache-entry",
        action="append",
        default=[],
        help=(
            "select one exact sha256 cache entry when a derivation has multiple "
            "candidates; repeat for project-wide rebuilds. Supported for an "
            "external lifecycle driver only; a Standard driver rejects this and "
            "reuses already-accepted source deterministically via "
            "--from-accepted-source instead"
        ),
    )
    rebuild.add_argument(
        "--source-cache-root",
        action="append",
        default=[],
        metavar="BINDING=/ABSOLUTE/PATH",
        help=(
            "bind one configured operator-named cache root to a host path; repeat "
            "for every operator-bound root. Supported for an external lifecycle "
            "driver only; a Standard driver rejects this and keeps its "
            "BUILD_DIR-backed cache custody"
        ),
    )
    rebuild.add_argument(
        "--allow-host-execution",
        action="store_true",
        help="acknowledge compilation and execution of generated code on this host",
    )

    profile = commands.add_parser(
        "profile",
        help="run a workflow with structured timing instrumentation and report results",
        description=(
            "Run a literate-ai command under active structured logging and produce a "
            "machine-readable timing report. Records per-operation and per-subprocess "
            "durations, hotspots, and total pipeline time. Output is JSON. "
            "Set LITAI_LOG_DIR to override the log directory."
        ),
    )
    profile.add_argument(
        "specification",
        help=(
            "project-relative Component/specification entry point, or . for the project"
        ),
    )
    profile.add_argument("--project", default=".", help="canonical project root")
    profile.add_argument(
        "--subcommand",
        dest="profile_subcommand",
        default="rebuild",
        choices=["rebuild", "build", "test", "generate"],
        help="underlying command to profile (default: rebuild)",
    )
    profile.add_argument(
        "--log-dir",
        dest="profile_log_dir",
        default=None,
        help="directory for the raw NDJSON trace (default: logs/ under project root)",
    )
    profile.add_argument(
        "--target", default="host", help="generation/Flavor target profile"
    )
    profile.add_argument(
        "--model",
        help="pipeline-default coding-CLI model; narrower scopes may override it",
    )
    profile.add_argument(
        "--runtime-root", help="new or empty external runtime directory"
    )
    profile.add_argument(
        "--candidate-receipt", help="optional new external path for the passing receipt"
    )
    profile.add_argument("--update-receipt", action="store_true")
    profile.add_argument(
        "--from-accepted-source",
        action="store_true",
        help="require verifier-admitted source for a Standard lifecycle",
    )
    profile.add_argument("--keep-runtime", action="store_true")
    profile.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="cap parallel Component operations (default: admitted worker capacity)",
    )
    profile.add_argument("--flavor", action="append", default=[])
    profile.add_argument("--force-regeneration", action="store_true")
    profile.add_argument(
        "--source-cache-entry",
        action="append",
        default=[],
        help=(
            "external lifecycle driver only; a Standard driver rejects this and "
            "reuses already-accepted source deterministically via "
            "--from-accepted-source instead"
        ),
    )
    profile.add_argument(
        "--source-cache-root",
        action="append",
        default=[],
        metavar="BINDING=/ABSOLUTE/PATH",
        help=(
            "external lifecycle driver only; a Standard driver rejects this and "
            "keeps its BUILD_DIR-backed cache custody"
        ),
    )
    profile.add_argument("--allow-host-execution", action="store_true")

    add_spec_parser(commands)
    return parser


def _help_topic_parser(
    parser: argparse.ArgumentParser, topic: Sequence[str]
) -> argparse.ArgumentParser:
    """Resolve an exact command path without executing any command parser."""

    selected = parser
    resolved: list[str] = []
    for command in topic:
        subcommands: dict[str, argparse.ArgumentParser] | None = None
        for action in selected._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and all(
                isinstance(value, argparse.ArgumentParser) for value in choices.values()
            ):
                subcommands = choices
                break
        if subcommands is None or command not in subcommands:
            prefix = " ".join(resolved) or "litai"
            available = ""
            if subcommands:
                available = "; available: " + ", ".join(sorted(subcommands))
            raise CliFailure(
                "cli.help_topic_unknown",
                f"unknown help topic after {prefix}: {command}{available}",
            )
        selected = subcommands[command]
        resolved.append(command)
    return selected


def _is_global_token(item: str) -> bool:
    return (
        item in _GLOBAL_BOOL_FLAGS or item == "--debug" or item.startswith("--debug=")
    )


def _looks_like_debug_path(token: str) -> bool:
    if token in _TOP_LEVEL_COMMANDS:
        return False
    if token == "-":
        return True
    if token.startswith("-"):
        return False
    lowered = token.casefold()
    return "/" in token or "\\" in token or lowered.endswith(_DEBUG_PATH_SUFFIXES)


def _normalize_help_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    """Make ``COMMAND [SUBCOMMAND ...] help`` the scoped-help verb form."""

    values = tuple(arguments)
    non_global = tuple(value for value in values if not _is_global_token(value))
    if len(non_global) >= 2 and non_global[-1] == "help" and non_global[0] != "help":
        global_options = tuple(value for value in values if _is_global_token(value))
        return (*global_options, "help", *non_global[:-1])
    return values


def _normalize_global_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    """Accept global diagnostics before or after a verb, stopping at ``--``.

    ``--debug`` is rewritten to ``--debug=-`` (stderr) or ``--debug=PATH`` so
    argparse never steals the following command verb as a file argument.
    """

    globals_: list[str] = []
    remainder: list[str] = []
    passthrough = False
    values = list(arguments)
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--":
            passthrough = True
            remainder.append(value)
            index += 1
            continue
        if passthrough:
            remainder.append(value)
            index += 1
            continue
        if value in _GLOBAL_BOOL_FLAGS:
            globals_.append(value)
            index += 1
            continue
        if value == "--debug" or value.startswith("--debug="):
            if value == "--debug":
                nxt = values[index + 1] if index + 1 < len(values) else None
                if nxt is not None and _looks_like_debug_path(nxt):
                    globals_.append(f"--debug={nxt}")
                    index += 2
                    continue
                globals_.append("--debug=-")
                index += 1
                continue
            globals_.append(value if value != "--debug=" else "--debug=-")
            index += 1
            continue
        remainder.append(value)
        index += 1
    return (*globals_, *remainder)


def _update_moving_counts(counts: object) -> dict[str, int]:
    if not isinstance(counts, dict):
        return {}
    moving: dict[str, int] = {}
    for key in ("conflict", "mergeable", "upstream-added", "upstream-only"):
        value = counts.get(key)
        if isinstance(value, int) and value:
            moving[key] = value
    return moving


def _update_settled_count(counts: object) -> int:
    if not isinstance(counts, dict):
        return 0
    return sum(
        value
        for key, value in counts.items()
        if key in {"unchanged", "already-current", "local-only", "preserved-dynamic"}
        and isinstance(value, int)
    )


def _human_update_conflict_lines(files: object, diffs: object) -> list[str]:
    """Print ours/theirs conflict markers; fall back to paths when diffs are absent."""

    by_path: dict[str, dict[str, object]] = {}
    if isinstance(diffs, list):
        for item in diffs:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                by_path[item["path"]] = item
    conflicts: list[dict[str, object]] = []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("classification") == "conflict":
                conflicts.append(item)
    lines: list[str] = []
    for item in conflicts:
        path = item.get("path")
        if not isinstance(path, str):
            continue
        diff = by_path.get(path, item)
        unified = diff.get("unified_diff")
        if isinstance(unified, str) and unified.strip():
            lines.append(unified.rstrip("\n"))
        else:
            lines.append(f"    conflict: {path}")
    return lines


def _human_update_movement_lines(
    counts: object, files: object, *, heading: str, diffs: object = None
) -> list[str]:
    moving = _update_moving_counts(counts)
    lines = [heading]
    if not moving:
        lines.append("  Nothing in this half has moved.")
    else:
        for key in ("conflict", "mergeable", "upstream-added", "upstream-only"):
            if moving.get(key):
                lines.append(f"  {moving[key]} {key}")
        lines.extend(_human_update_conflict_lines(files, diffs))
    settled = _update_settled_count(counts)
    lines.append(f"  {settled} file(s) need no decision.")
    return lines


def _human_update_text(result: dict[str, Any]) -> str:
    """Summarize an update plan; the per-file identity table is machine material."""

    if result.get("schema") == "literate-ai/composite-project-update@1":
        return _human_composite_update_text(result)

    counts = result.get("counts") or {}
    moving = _update_moving_counts(counts)
    lines = [f"Literate AI update: {result.get('mode', 'plan')}"]
    if not moving:
        lines.append("Nothing upstream has moved.")
    else:
        lines.append("Upstream movement:")
        for key in ("conflict", "mergeable", "upstream-added", "upstream-only"):
            if moving.get(key):
                lines.append(f"  {moving[key]} {key}")
        lines.extend(
            _human_update_conflict_lines(
                result.get("files"), result.get("conflict_diffs")
            )
        )
    lines.append(f"{_update_settled_count(counts)} file(s) need no decision.")
    return _human_update_footer(result, lines)


def _human_composite_update_text(result: dict[str, Any]) -> str:
    lines = [f"Literate AI update: {result.get('mode', 'plan')}"]
    pinned = [
        item
        for item in result.get("parent_selectors") or []
        if isinstance(item, dict) and item.get("pinned")
    ]
    if pinned:
        for item in pinned:
            lines.append(
                f"Parent is pinned to exact commit {item.get('requested_revision')}."
            )
        lines.append(
            "This cannot follow later commits. Use --unpin, --follow-ref REF, "
            "or --allow-major."
        )
    follow = result.get("follow")
    if isinstance(follow, dict):
        lines.append(
            f"Parent selector change: {follow.get('from')} -> {follow.get('to')}."
        )
        if follow.get("applied"):
            lines.append("Parent selector change applied.")
        elif result.get("mode") != "applied":
            lines.append("Use --apply to write the parent-selector change.")
    raw_framework = result.get("framework")
    framework = raw_framework if isinstance(raw_framework, dict) else {}
    raw_lineage = result.get("repository_lineage")
    lineage = raw_lineage if isinstance(raw_lineage, dict) else {}
    lines.extend(
        _human_update_movement_lines(
            framework.get("counts"),
            framework.get("files"),
            heading="Framework templates:",
            diffs=framework.get("conflict_diffs"),
        )
    )
    lines.extend(
        _human_update_movement_lines(
            lineage.get("counts"),
            lineage.get("files"),
            heading="Inherited catalogs:",
            diffs=lineage.get("conflict_diffs"),
        )
    )
    framework_moving = _update_moving_counts(framework.get("counts"))
    lineage_moving = _update_moving_counts(lineage.get("counts"))
    if (
        not pinned
        and not isinstance(follow, dict)
        and not framework_moving
        and not lineage_moving
        and not result.get("changed")
    ):
        lines.append("Nothing upstream has moved.")
    return _human_update_footer(result, lines)


def _human_update_footer(result: dict[str, Any], lines: list[str]) -> str:
    repository = result.get("repository_lineage")
    repository_applied = (
        repository.get("applied") if isinstance(repository, dict) else None
    )
    if isinstance(repository_applied, dict):
        lines.append(
            "Inherited catalogs: "
            f"applied {len(repository_applied.get('applied') or [])}, "
            f"adopted {len(repository_applied.get('adopted') or [])}, "
            "took upstream for "
            f"{len(repository_applied.get('taken_upstream') or [])}."
        )
        kept_local = repository_applied.get("kept_local") or []
        if kept_local:
            lines.append(f"Kept {len(kept_local)} retired path(s) as local authority.")
        for key, paths in sorted((repository_applied.get("refused") or {}).items()):
            lines.append(f"  refused {len(paths)} {key}")
    applied = result.get("applied")
    if not isinstance(applied, dict):
        framework = result.get("framework")
        if isinstance(framework, dict) and isinstance(framework.get("applied"), dict):
            applied = framework["applied"]
    if isinstance(applied, dict):
        lines.append(
            f"Applied {len(applied.get('applied') or [])}, "
            f"adopted {len(applied.get('adopted') or [])}."
        )
        for key, paths in sorted((applied.get("refused") or {}).items()):
            lines.append(f"  refused {len(paths)} {key}")
    recorded = result.get("work_items_recorded")
    if isinstance(recorded, dict):
        added = recorded.get("recorded") or []
        lines.append(
            f"Recorded {len(added)} work item(s) in {recorded.get('queue_path')}."
            if added
            else "No new work items; the queue already covers this."
        )
    if result.get("apply_supported") is False and not isinstance(applied, dict):
        lines.append("This is a plan. Use --apply to write the safe subset.")
    elif (
        result.get("schema") == "literate-ai/composite-project-update@1"
        and result.get("mode") != "applied"
        and result.get("apply_supported") is True
    ):
        lines.append("This is a plan. Use --apply to write the safe subset.")
    reviews = result.get("conflict_reviews")
    if isinstance(reviews, list) and reviews:
        lines.append("Conflict reviews (plan-only, not written):")
        for item in reviews:
            if not isinstance(item, dict):
                continue
            lines.append(f"  {item.get('path')}: {item.get('decision')}")
    lines.append("Unchanged and already-current files are omitted.")
    return "\n".join(lines) + "\n"


def _human_verify_text(result: dict[str, Any]) -> str:
    """Render each gate on its own line; one failure must not hide the others."""

    lines = [
        "Literate AI verify: " + ("passed" if result.get("ok") else "FAILED"),
        f"Project: {result.get('project')}",
    ]
    for item in result.get("gates") or []:
        if not isinstance(item, dict):
            continue
        mark = {"pass": "  ok  ", "fail": " FAIL ", "skipped": " skip "}.get(
            str(item.get("state")), " ???  "
        )
        lines.append(f"{mark} {item.get('gate'):<20} {item.get('detail', '')}")
    return "\n".join(lines) + "\n"


def _human_build_text(result: dict[str, Any]) -> str:
    """Say whether it passed and how to run it; that is the whole first-run need."""

    summary = result.get("test_summary") or {}
    lines = [
        "Literate AI build: " + ("passed" if result.get("passed") else "FAILED"),
        f"Component: {result.get('component')}",
    ]
    if isinstance(summary, dict) and summary.get("total"):
        lines.append(
            f"Tests: {summary.get('passed', 0)}/{summary.get('total', 0)} passed"
        )
    lines.append(f"Artifact: {result.get('artifact')}")
    if result.get("run"):
        lines.append(f"Run it with: {result['run']}")
    return "\n".join(lines) + "\n"


def _human_run_text(result: dict[str, Any]) -> str:
    """The application's own output already went to stdout; add only provenance."""

    status = result.get("exit_status")
    lines = [f"Literate AI run: {result.get('component')} exited {status}"]
    source = result.get("arguments_source")
    if source and source != "supplied":
        lines.append(f"Arguments came from the declared {source}.")
    return "\n".join(lines) + "\n"


def _human_operator_status_text(result: dict[str, Any]) -> str:
    from .operator import human_operator_status_text

    return human_operator_status_text(result)


def _human_onboard_text(result: dict[str, Any]) -> str:
    if result.get("schema") in {
        "literate-ai/orchestration-plan@1",
        "literate-ai/orchestration-check@1",
        "literate-ai/orchestration-initialization-plan@1",
        "literate-ai/orchestration-initialization-check@1",
        "literate-ai/orchestration-initialization@1",
        "literate-ai/orchestration-refresh-plan@1",
        "literate-ai/orchestration-refresh-check@1",
        "literate-ai/orchestration-refresh-apply@1",
    }:
        from .orchestration import human_orchestration_text

        return human_orchestration_text(result)
    from .operator import human_onboard_text

    return human_onboard_text(result)


def _human_conversion_authority_text(result: dict[str, Any]) -> str:
    from .operator import human_conversion_authority_text

    return human_conversion_authority_text(result)


def _human_test_text(result: dict[str, Any]) -> str:
    summary = result.get("test_summary")
    lines = [
        f"Literate AI test: {result.get('component')} "
        f"{'passed' if result.get('passed') else 'failed'}"
    ]
    if isinstance(summary, dict):
        lines.append(
            f"Tests: {summary.get('passed', 0)} passed, "
            f"{summary.get('failed', 0)} failed, "
            f"{summary.get('skipped', 0)} skipped"
        )
    return "\n".join(lines) + "\n"


def _command_name(arguments: Sequence[str]) -> str:
    positional = tuple(item for item in arguments if not _is_global_token(item))
    if len(positional) >= 2 and positional[0] in {
        "perf",
        "project",
        "package",
        "release",
        "spec",
        "version",
        "worker",
        "config",
        "catalog",
        "flavor",
        "render",
        "prompt",
        "skills",
        "design",
    }:
        return f"{positional[0]}.{positional[1]}"
    return positional[0] if positional else "unknown"


def _interactive(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _display_command(argv: Sequence[object]) -> str:
    values = tuple(str(item) for item in argv)
    return subprocess.list2cmdline(values) if os.name == "nt" else shlex.join(values)


def _human_rebuild_text(result: dict[str, Any]) -> str:
    """Render the successful Standard path without discarding JSON evidence."""

    if result.get("schema") != "literate-ai/project-rebuild@1":
        return "Literate AI rebuild completed. Use --json for complete evidence.\n"
    lines = [f"Literate AI rebuild: {'passed' if result.get('passed') else 'failed'}"]
    lines.append(f"Project: {result.get('project_id', 'unknown')}")
    dag = result.get("dag")
    if isinstance(dag, dict):
        lines.append("DAG:")
        layers = dag.get("layers", [])
        if isinstance(layers, list):
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                components = layer.get("components", [])
                rendered = []
                if isinstance(components, list):
                    for component in components:
                        if isinstance(component, dict):
                            rendered.append(
                                f"{component.get('coordinate', 'unknown')} "
                                f"[{component.get('source', 'unknown')}]"
                            )
                lines.append(f"  {layer.get('index', '?')}: " + ", ".join(rendered))
        edges = dag.get("edges", [])
        if isinstance(edges, list) and edges:
            lines.append("Edges:")
            for edge in edges:
                if isinstance(edge, dict):
                    lines.append(
                        f"  {edge.get('provider', 'unknown')} -> "
                        f"{edge.get('consumer', 'unknown')} "
                        f"({edge.get('kind', 'unknown')})"
                    )
    cache = result.get("build_cache")
    if isinstance(cache, dict):
        lines.append(
            "Build cache: "
            f"{cache.get('hits', 0)} hit(s), {cache.get('misses', 0)} miss(es)"
        )
    repair = result.get("repair")
    if isinstance(repair, dict):
        lines.append(
            f"Repair: {repair.get('status', 'unknown')} "
            f"({repair.get('attempts', 0)} attempt(s))"
        )
    summary = result.get("test_summary")
    if isinstance(summary, dict):
        lines.append(
            "Tests: "
            f"{summary.get('passed', 0)} passed, "
            f"{summary.get('failed', 0)} failed, "
            f"{summary.get('skipped', 0)} skipped"
        )
    lines.append(f"Artifact: {result.get('artifact', 'unavailable')}")
    execution = result.get("execution_command")
    if isinstance(execution, dict) and isinstance(execution.get("argv"), list):
        lines.append(f"Execute: {_display_command(execution['argv'])}")
        lines.append(f"Working directory: {execution.get('cwd', '.')}")
    lines.append(
        "Receipt: "
        + (
            "committed and current"
            if result.get("receipt_committed")
            else "generated but not committed"
        )
    )
    lines.append("Use --json for complete content-identified evidence.")
    return "\n".join(lines) + "\n"


_HUMAN_RENDERERS = {
    "status": _human_operator_status_text,
    "doctor": _human_operator_status_text,
    "onboard": _human_onboard_text,
    "project.convert-stage": _human_conversion_authority_text,
    "rebuild": _human_rebuild_text,
    "update": _human_update_text,
    "verify": _human_verify_text,
    "build": _human_build_text,
    "test": _human_test_text,
    "run": _human_run_text,
}


def _perf_build_root(args: argparse.Namespace) -> Path | None:
    """Anchor performance telemetry to the invoked project's own OBJ_DIR.

    A relative ``OBJ_DIR`` must resolve against the project this exact
    command targets, not this process's ambient working directory --
    otherwise a command invoked with ``--project`` pointing elsewhere (or a
    test harness with a different cwd) writes telemetry into the wrong tree.
    Once a usable project root is known, every fallback stays anchored to
    that root too: never hand off to the recorder's own environment-only
    default, which would re-read an unanchored relative ``OBJ_DIR`` and
    reintroduce exactly the leak this function exists to prevent. Falls
    back to no explicit root only when no project was declared at all;
    performance telemetry must never fail, slow, or redirect the command it
    is only observing.
    """

    project = getattr(args, "project", None)
    if project is None:
        return None
    try:
        project_root = Path(project).resolve(strict=True)
    except OSError:
        return None
    try:
        from literate_ai.cache_directories import (
            CacheDirectoryError,
            resolve_cache_directories,
        )

        return resolve_cache_directories(project_root).obj_dir
    except CacheDirectoryError:
        return project_root / "_build"


def _run_operator_mcp_discovery(args: Namespace, errors: TextIO) -> None:
    if not getattr(args, "discover_mcps", False):
        return
    from literate_ai.contracts.user_mcp import EMPTY_USER_MCP_CATALOG

    try:
        catalog = load_user_mcp_catalog()
    except UserConfigError:
        catalog = EMPTY_USER_MCP_CATALOG
    ids = discover_catalog_mcps(
        catalog,
        enabled=True,
        transport=active_mcp_transport(),
    )
    report_discovery(ids, enabled=True, stderr=errors)


def _fan_out_operator_mcp_event(journal_path: Path | None, errors: TextIO) -> None:
    if journal_path is None:
        return
    from literate_ai.contracts.channel_events import ChannelEvent

    try:
        event = ChannelEvent.from_dict(
            json.loads(journal_path.read_text(encoding="utf-8"))
        )
        catalog = load_user_mcp_catalog()
    except (OSError, UnicodeError, json.JSONDecodeError, UserConfigError, TypeError):
        return
    fan_out_author_event(
        event,
        catalog,
        load_project_channels(),
        transport=active_mcp_transport(),
        stderr=errors,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Execute one command with interactive prose or stable JSON envelopes."""

    raw = sys.argv[1:] if argv is None else list(argv)
    arguments = _normalize_help_arguments(_normalize_global_arguments(raw))
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    command = _command_name(arguments)
    # Even acknowledged initialization owns only its reviewed root additions;
    # implicit host configuration, telemetry and event writes are out of scope.
    scoped_orchestration = (
        command == "orchestrate"
        or command == "onboard"
        and arguments[arguments.index("onboard") + 1 :][:1] == ("orchestrate",)
    )
    scoped_retained_cargo = command == "project.retained-cargo"
    scoped_worker_health = command == "worker.health"
    scoped_orchestration = scoped_orchestration or scoped_worker_health
    scoped_evidence = (
        command.startswith("project.")
        and "test-receipt" in arguments
        and any(
            name in arguments
            for name in (
                "verify-evidence",
                "publish-evidence",
                "require-current-evidence",
            )
        )
    )
    if (
        command not in {"verify", "lock", "plan", "update"}
        and not scoped_orchestration
        and not scoped_evidence
        and not scoped_retained_cargo
    ):
        maybe_host_self_update(raw)
    try:
        parser = _parser()
        args = parser.parse_args(arguments)
        if args.command == "update":
            maybe_host_self_update(raw, check_now=True)
        scoped_orchestration = (
            scoped_orchestration
            or scoped_retained_cargo
            or (
                args.command == "project"
                and getattr(args, "project_command", None) == "test-receipt"
                and getattr(args, "test_receipt_command", None)
                in {"verify-evidence", "publish-evidence", "require-current-evidence"}
            )
        )
        if args.command in {"lock", "plan", "verify"}:
            from .repository_locks import repository_root

            selected = (
                getattr(args, "component", None)
                or getattr(args, "specification", None)
                or getattr(args, "path", ".")
            )
            try:
                args._repository_root = repository_root(Path(selected))
            except CliFailure:
                if args.command != "verify":
                    raise
                # Verification owns its malformed-project gate report, not routing.
                args._repository_root = None
            scoped_orchestration = args._repository_root is not None
            if args.command in {"lock", "plan"} and not scoped_orchestration:
                maybe_host_self_update(raw)
        if scoped_orchestration and (
            getattr(args, "discover_mcps", False)
            or getattr(args, "debug", None) not in {None, "-"}
        ):
            raise CliFailure(
                "worker.health_read_only_options"
                if scoped_worker_health
                else "orchestration.read_only_options",
                "read-only inspection does not admit MCP discovery "
                "or debug-file output",
            )
        if (
            args.command not in {"config", "help", "verify"}
            and not scoped_orchestration
        ):
            try:
                ensure_user_mcp_catalog(
                    json_mode=bool(args.json),
                    stdin=sys.stdin,
                    stderr=errors,
                )
            except UserConfigError as exc:
                raise CliFailure(exc.code, exc.message) from exc
        if not scoped_orchestration:
            _run_operator_mcp_discovery(args, errors)
        if args.command == "help":
            topic = tuple(args.topic)
            help_text = _help_topic_parser(parser, topic).format_help()
            if args.json:
                output.write(
                    _json_text(
                        {
                            "schema": CLI_RESULT_SCHEMA,
                            "ok": True,
                            "command": "help",
                            "result": {
                                "schema": HELP_SCHEMA,
                                "topic": list(topic),
                                "text": help_text,
                            },
                        }
                    )
                )
            else:
                output.write(help_text)
            return 0
        recorder = PerformanceRecorder(build_root=_perf_build_root(args))
        perf_target = next(
            (
                getattr(args, name)
                for name in ("component", "specification", "project")
                if getattr(args, name, None) is not None
            ),
            command,
        )
        performance_span = (
            nullcontext({})
            if (
                getattr(args, "project_command", None) == "retained-scope"
                or scoped_orchestration
                or args.command in ("render", "verify")
                or (
                    command == "onboard"
                    and getattr(args, "root_plan", None) is not None
                )
            )
            else recorder.span(
                f"cli.{command}",
                target_kind="command",
                target_id=str(perf_target),
                model=getattr(args, "model", None),
            )
        )
        with (
            performance_span as perf_outcome,
            verbose_diagnostics(bool(args.verbose), errors),
            debug_diagnostics(
                getattr(args, "debug", None),
                json_mode=bool(args.json),
                stderr=errors,
            ),
            progress_reporting(
                errors if not args.json and _interactive(errors) else None
            ),
        ):
            with debug_stage(f"cli.{command}", command=command):
                result, status = _handle(args)
            if isinstance(result, dict) and result.get("coding_cli") is not None:
                perf_outcome["coding_cli"] = result["coding_cli"]
        renderer = _HUMAN_RENDERERS.get(command)
        if command in {
            "worker.list",
            "worker.show",
            "worker.add",
            "worker.update",
            "worker.remove",
            "worker.test",
        }:
            from .worker_registry import human_registry_result

            renderer = human_registry_result
        if command == "worker.health":
            from .worker_health import human_worker_health

            renderer = human_worker_health
        if getattr(args, "_repository_root", None) is not None and command in {
            "lock",
            "plan",
        }:
            from .repository_locks import human_repository_text

            renderer = human_repository_text
        if renderer is None and command.startswith("perf."):
            from .perf import PERF_HUMAN_RENDERERS

            renderer = PERF_HUMAN_RENDERERS.get(command)
        if renderer is not None and not args.json and _interactive(output):
            output.write(renderer(result))
        else:
            output.write(
                _json_text(
                    {
                        "schema": CLI_RESULT_SCHEMA,
                        "ok": True,
                        "command": command,
                        "result": result,
                    }
                )
            )
        if status == 0 and not scoped_orchestration:
            read_only_lock = command == "lock" and (
                bool(getattr(args, "check", False))
                or bool(getattr(args, "diff", False))
            )
            journal_path = journal_mutagenic_event(
                command,
                outcome="ok",
                lock_check=read_only_lock,
                stderr=errors,
            )
            _fan_out_operator_mcp_event(journal_path, errors)
        return status
    except (
        CliFailure,
        CodingCliError,
        LocalStandardLifecycleError,
        SourceCacheError,
        SharedCacheConfigurationError,
        SourceToSpecificationError,
        StandardCommandProjectionError,
        UserConfigError,
    ) as exc:
        code = exc.code
        message = exc.message
        message_limit = getattr(exc, "message_limit", CLI_ERROR_MESSAGE_CHARS)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        code = "cli.invalid_input"
        message = str(exc) or "invalid CLI input"
        message_limit = CLI_ERROR_MESSAGE_CHARS
    if "--json" not in arguments and _interactive(errors):
        errors.write(
            f"Literate AI {command} failed: {code}\n{message[:message_limit]}\n"
        )
    else:
        errors.write(
            _json_text(
                {
                    "schema": CLI_ERROR_SCHEMA,
                    "ok": False,
                    "command": command,
                    "error": {"code": code, "message": message[:message_limit]},
                }
            )
        )
    return 2


__all__ = ["main"]
