"""Prove the prefix installation and initialized-project command contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from install_litai import install

from literate_ai.adapters.live_test_selection import (
    apply_live_test_selection,
    log_live_session_models,
    resolve_live_test_selection,
)
from literate_ai.adapters.models.coding_cli import CodingCliError
from literate_ai.evidence_ledger import (
    EvidenceNode,
    attach_run,
    retained_directory,
)

_SYNTHETIC_DISPATCHER = r"""from __future__ import annotations

import json
import platform
import sys

from literate_ai.contracts import (
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    canonical_identity,
    canonical_json_bytes,
)

request = ExecutionDispatchRequest.from_dict(json.load(sys.stdin))
artifact = request.artifact_reference
if request.action in {LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST}:
    artifact_identity = canonical_identity(
        {"schema": "synthetic-artifact@1", "authority": request.authority_identity.uri}
    )
    artifact = ContentReference(
        "artifact-export", f"cas:{artifact_identity.uri}", artifact_identity
    )
result = ExecutionDispatchResult(
    request.identity,
    request.worker_identity,
    "installed-synthetic-task",
    DispatchResultStatus.PASSED,
    ObservedExecutionEnvironment(
        {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(
            platform.system().lower(), "linux"
        ),
        "synthetic",
        platform.machine().lower() or "unknown",
        1,
        1,
        toolchain_identities=(canonical_identity({"toolchain": "synthetic"}),),
    ),
    0,
    artifact,
    canonical_identity(
        {"schema": "synthetic-dispatch-evidence@1", "request": request.identity.uri}
    ),
    "installed-command-worker-output\n"
    if request.action is LifecycleDispatchAction.RUN
    else "",
    "",
)
sys.stdout.buffer.write(canonical_json_bytes(result.to_dict()))
"""


@contextmanager
def _runtime_directory():
    root = Path(tempfile.mkdtemp(prefix="literate-install-smoke-"))
    with retained_directory(
        root,
        node_path="installed/project-smoke/runtime",
        operation="installed.project-smoke.runtime",
        role="installed-project-smoke-workspace",
    ) as retained:
        yield retained


def _run(
    *arguments: str, cwd: Path, environment: dict[str, str] | None = None
) -> dict[str, object]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-4000:]
        raise RuntimeError(
            f"installed-project command {Path(arguments[0]).name!r} failed with "
            f"status {completed.returncode}: {detail}"
        )
    return json.loads(completed.stdout)


def _path_without_source_graph(value: str) -> str:
    """Retain the host PATH except entries exposing a source-graph launcher."""

    retained = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        if any(
            shutil.which(command, path=entry) is not None
            for command in ("codegraph", "codegraph.cmd")
        ):
            continue
        retained.append(entry)
    return os.pathsep.join(retained)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-4000:]
        raise RuntimeError(
            f"installed-project Git command failed with status "
            f"{completed.returncode}: {detail}"
        )
    return completed.stdout.strip()


def _assert_installed_release_protocol(
    *,
    root: Path,
    launcher: Path,
    environment: dict[str, str],
    parent_source: str,
) -> dict[str, object]:
    """Prove the installed CLI against a real local Git remote without network I/O."""

    project = root / "release-project"
    _run(
        str(launcher),
        "--json",
        "init",
        str(project),
        "--from",
        parent_source,
        cwd=root,
        environment=environment,
    )
    policy_path = project / "literate.release.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["gate"] = {
        "argv": ["git", "diff", "--check", "HEAD^", "HEAD"],
        "timeout_seconds": 300,
    }
    policy["provider"] = None
    # This fixture proves the installed release state machine against a local bare
    # remote without network access. Canonical initialized projects require a live
    # contribution sweep, but that external GitHub/GitLab contract is outside this
    # deliberately offline fixture and cannot classify a filesystem remote.
    policy.pop("contributions", None)
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

    # RELEASE-012 authorization: every release operation resolves an actor via
    # _resolve_actor. With provider set to None (non-GitHub), it demands an explicit
    # --actor whose name is listed as a release engineer in the project's
    # release_engineer_source (default README.md#release-engineers). Author that source
    # so the smoke controls a known, authorized actor. The heading slug rule
    # (casefold, drop everything but [a-z0-9 -], spaces -> hyphens) turns
    # "Release Engineers" into "release-engineers", matching the default fragment; the
    # bullet must match _RELEASE_ENGINEER exactly: "- `NAME`" where NAME is
    # [A-Za-z0-9][A-Za-z0-9-]{0,38}.
    actor = "smoke-release-engineer"
    readme_path = project / "README.md"
    readme_path.write_text(
        f"# Installed Release Smoke\n\n## Release Engineers\n\n- `{actor}`\n",
        encoding="utf-8",
    )

    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "installed-release@example.invalid")
    _git(project, "config", "user.name", "Installed Release Smoke")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "Initialize installed release project")
    remote = root / "release-remote.git"
    _git(root, "init", "--bare", str(remote))
    _git(project, "remote", "add", "origin", str(remote))
    _git(project, "push", "-u", "origin", "main")

    release_root = project / "_build" / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    # The release-state machine (RELEASE-012) gates planning behind an explicit
    # pre-release declaration: main_state must be pre-release targeting the exact
    # major.minor being cut. A patch bump stays within the current major.minor, so
    # declare pre-release for the project's current version before planning.
    version_check = _run(
        str(launcher),
        "--json",
        "version",
        "check",
        cwd=project,
        environment=environment,
    )
    project_version = str(
        version_check.get("result", {}).get("project", {}).get("project_version", "")
    )
    major_minor = ".".join(project_version.split(".")[:2])
    _run(
        str(launcher),
        "--json",
        "release",
        "state",
        "set",
        "--mode",
        "pre-release",
        "--pre-release-version",
        major_minor,
        "--actor",
        actor,
        cwd=project,
        environment=environment,
    )
    _git(project, "add", "literate.project.json")
    _git(project, "commit", "-m", f"Declare pre-release state for {major_minor}")
    _git(project, "push", "origin", "main")
    plan = _run(
        str(launcher),
        "--json",
        "release",
        "plan",
        "--bump",
        "patch",
        cwd=project,
        environment=environment,
    )
    plan_result = plan.get("result")
    if not isinstance(plan_result, dict) or plan_result.get("source_clean") is not True:
        raise RuntimeError("installed release planning omitted a clean exact plan")
    plan_path = release_root / "plan.json"
    plan_path.write_text(
        json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _run(
        str(launcher),
        "--json",
        "release",
        "prepare",
        str(plan_path),
        "--actor",
        actor,
        cwd=project,
        environment=environment,
    )
    _git(project, "add", "literate.project.json", "CHANGELOG.md")
    _git(project, "commit", "-m", f"Prepare {plan_result['next_version']}")
    checked = _run(
        str(launcher),
        "--json",
        "release",
        "check",
        str(plan_path),
        "--actor",
        actor,
        cwd=project,
        environment=environment,
    )
    checked_result = checked.get("result")
    if (
        not isinstance(checked_result, dict)
        or checked_result.get("version") != plan_result["next_version"]
    ):
        raise RuntimeError("installed release check omitted exact prepared evidence")
    prepared_path = release_root / "prepared.json"
    published = _run(
        str(launcher),
        "--json",
        "release",
        "publish",
        str(prepared_path),
        "--authorize-external-write",
        "--actor",
        actor,
        cwd=project,
        environment=environment,
    )
    publication = published.get("result")
    if not isinstance(publication, dict):
        raise RuntimeError("installed release publish omitted its receipt")
    tag = str(plan_result["tag"])
    revision = str(publication.get("revision"))
    # RELEASE-012 cuts a patch on its release line, not the default branch: with
    # main_state pre-release targeting major.minor, `plan` binds the
    # release/<major>.<minor>.x line (create: true), `prepare` checks it out, and
    # `publish` pushes the tag and that line's HEAD -- never main. Assert the remote
    # line head and annotated tag both match the receipt revision; main stays at the
    # pre-release declaration commit.
    release_line = plan_result.get("release_line")
    if not isinstance(release_line, dict) or not isinstance(
        release_line.get("name"), str
    ):
        raise RuntimeError("installed release plan omitted its release line")
    release_branch = release_line["name"]
    if (
        _git(remote, "rev-parse", f"refs/tags/{tag}^{{}}") != revision
        or _git(remote, "rev-parse", f"refs/heads/{release_branch}") != revision
    ):
        raise RuntimeError("installed release remote state differs from its receipt")
    retried = _run(
        str(launcher),
        "--json",
        "release",
        "publish",
        str(prepared_path),
        "--authorize-external-write",
        "--actor",
        actor,
        cwd=project,
        environment=environment,
    )
    if retried.get("result") != publication:
        raise RuntimeError("installed release publication is not exactly idempotent")
    return publication


def _git_commit(repository: Path) -> None:
    commands = (
        ("-c", "init.defaultBranch=main", "init"),
        ("config", "user.name", "Literate AI installed smoke"),
        ("config", "user.email", "installed-smoke@example.invalid"),
        ("add", "."),
        ("commit", "-m", f"Initialize {repository.name}"),
    )
    for arguments in commands:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            env={**os.environ, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-4000:]
            raise RuntimeError(
                f"installed-project Git fixture failed with status "
                f"{completed.returncode}: {detail}"
            )


def _prove_repository_dag(
    launcher: Path,
    root: Path,
    environment: dict[str, str],
    parent_source: str,
) -> dict[str, object]:
    repositories: list[Path] = []
    parent: Path | None = None
    for name in ("lineage-root", "lineage-parent", "lineage-selected"):
        repository = root / name
        arguments = [str(launcher), "--json", "init", str(repository)]
        if parent is not None:
            arguments.extend(("--from", f"{parent.resolve().as_uri()}#HEAD"))
        else:
            arguments.extend(("--from", parent_source))
        initialized = _run(*arguments, cwd=root, environment=environment)
        if not initialized.get("ok"):
            raise RuntimeError(f"installed init failed for repository {name}")
        if parent is None:
            rooted = _run(
                str(launcher),
                "--json",
                "reparent",
                "none",
                "--project",
                str(repository),
                "--apply",
                cwd=root,
                environment=environment,
            )
            if not rooted.get("ok"):
                raise RuntimeError("installed reparent failed to establish a DAG root")
        _git_commit(repository)
        repositories.append(repository)
        parent = repository

    derived = root / "lineage-derived"
    initialized = _run(
        str(launcher),
        "--json",
        "init",
        str(derived),
        "--from",
        f"{repositories[-1].resolve().as_uri()}#HEAD",
        cwd=root,
        environment=environment,
    )
    validated = _run(
        str(launcher),
        "--json",
        "project",
        "validate",
        str(derived),
        cwd=root,
        environment=environment,
    )

    planned = _run(
        str(launcher),
        "--json",
        "plan",
        "samples/hello-component",
        cwd=derived,
        environment=environment,
    )
    result = initialized.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("installed repository-DAG init omitted its result")
    lineage = result.get("repository_lineage")
    catalogs = result.get("inherited_catalogs")
    if (
        not validated.get("ok")
        or not planned.get("ok")
        or not isinstance(lineage, dict)
        or lineage.get("node_count") != 3
        or not isinstance(catalogs, dict)
        or not isinstance(catalogs.get("item_count"), int)
        or catalogs["item_count"] < 1
    ):
        raise RuntimeError(
            "installed repository-DAG init did not preserve its complete lineage"
        )
    required = (
        "samples/hello-component/component.md",
        "flavors/lang-python/flavor.md",
        "skills/specification-to-source/python-portable-application/SKILL.md",
        ".literate/repository-parent.json",
        ".literate/repository-lineage.json",
        ".literate/imports.json",
    )
    missing = [relative for relative in required if not (derived / relative).is_file()]
    if missing:
        raise RuntimeError(
            "installed repository-DAG init omitted inherited authority: "
            + ", ".join(missing)
        )
    return {
        "lineage_nodes": lineage["node_count"],
        "inherited_items": catalogs["item_count"],
        "planned": True,
    }


def _assert_installed_model_scope_plan(
    *,
    project: Path,
    launcher: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    """Prove an installed launcher carries its pipeline model into node derivations."""

    selector = "installed-pipeline-model"
    planned = _run(
        str(launcher),
        "--json",
        "plan",
        "samples/hello-component",
        "--model",
        selector,
        cwd=project,
        environment=environment,
    )
    result = planned.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("installed model-scope plan omitted its result")
    raw_scopes = result.get("model_scopes")
    standard = result.get("standard_component_execution")
    if (
        not isinstance(raw_scopes, list)
        or not raw_scopes
        or not isinstance(standard, dict)
    ):
        raise RuntimeError("installed model-scope plan omitted node authority")
    scopes = {}
    for item in raw_scopes:
        if not isinstance(item, dict) or not isinstance(item.get("binding"), dict):
            raise RuntimeError("installed model-scope plan contains an invalid binding")
        binding = item["binding"]
        component_revision = item.get("component_revision")
        if binding.get("model_selector") != selector or not isinstance(
            component_revision, str
        ):
            raise RuntimeError("installed pipeline model did not reach every node")
        digest = hashlib.sha256(
            json.dumps(
                binding,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        scopes[component_revision] = {
            "schema": "urn:literate-ai:schema:v1:content-identity",
            "algorithm": "sha256",
            "digest": digest,
        }
    generation_plans = standard.get("generation_plans")
    if not isinstance(generation_plans, list) or len(generation_plans) != len(scopes):
        raise RuntimeError("installed model-scope plan omitted generation derivations")
    for item in generation_plans:
        if not isinstance(item, dict) or not isinstance(
            item.get("generation_key"), dict
        ):
            raise RuntimeError(
                "installed model-scope plan contains an invalid generation key"
            )
        raw_revision = item.get("component_revision")
        if isinstance(raw_revision, str):
            revision = raw_revision
        elif (
            isinstance(raw_revision, dict)
            and isinstance(raw_revision.get("algorithm"), str)
            and isinstance(raw_revision.get("digest"), str)
        ):
            revision = f"{raw_revision['algorithm']}:{raw_revision['digest']}"
        else:
            revision = None
        model_identity = item["generation_key"].get("model_identity")
        if not isinstance(revision, str) or model_identity != scopes.get(revision):
            raise RuntimeError(
                "installed generation key does not bind its resolved model scope: "
                + json.dumps(
                    {
                        "component_revision": revision,
                        "model_identity": model_identity,
                        "scope_identity": scopes.get(revision),
                    },
                    sort_keys=True,
                )
            )
    return {
        "selector": selector,
        "node_count": len(scopes),
        "generation_key_count": len(generation_plans),
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--coding-cli",
        metavar="CLI",
        help="override the live-test coding CLI from literate.test.json",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="override the live-test coding-CLI model from literate.test.json",
    )
    parser.add_argument(
        "--isolated-tool-bootstrap",
        action="store_true",
        help=(
            "hide ambient source-graph tools and prove init/validate/plan "
            "succeed without them"
        ),
    )
    arguments = parser.parse_args()

    repository = arguments.repository.resolve()
    parent_source = f"{repository.as_uri()}#{_git(repository, 'rev-parse', 'HEAD')}"
    with _runtime_directory() as directory:
        root = Path(directory)
        installed = install(prefix=root / ".local", source=repository)
        launcher = Path(installed["launcher"])
        project = root / "project"
        environment = dict(os.environ)
        environment["LITAI_NO_SELF_UPDATE"] = "1"
        environment["PATH"] = os.pathsep.join(
            (str(launcher.parent), environment.get("PATH", ""))
        )
        # The outer release gate may place this temporary project beneath its own
        # OBJ_DIR. Give the installed project sibling caches so neither cache is an
        # ancestor of project authority.
        environment["BUILD_DIR"] = str(root / "generated")
        environment["OBJ_DIR"] = str(root / "objects")
        if arguments.isolated_tool_bootstrap:
            environment["LITAI_TOOL_DIR"] = str(root / "managed-tools")
            environment["PATH"] = _path_without_source_graph(environment["PATH"])

        initialized = _run(
            str(launcher),
            "--json",
            "init",
            str(project),
            "--from",
            parent_source,
            cwd=root,
            environment=environment,
        )
        validated = _run(
            str(launcher),
            "--json",
            "project",
            "validate",
            str(project),
            cwd=root,
            environment=environment,
        )
        locked = _run(
            str(launcher),
            "--json",
            "lock",
            "samples/hello-component",
            "--check",
            cwd=project,
            environment=environment,
        )
        planned = _run(
            str(launcher),
            "--json",
            "plan",
            "samples/hello-component",
            cwd=project,
            environment=environment,
        )
        if not all(
            item.get("ok") for item in (initialized, validated, locked, planned)
        ):
            raise RuntimeError("installed-project command contract did not pass")
        model_scope_plan = _assert_installed_model_scope_plan(
            project=project,
            launcher=launcher,
            environment=environment,
        )
        validation_result = validated.get("result")
        if not isinstance(validation_result, dict):
            raise RuntimeError("installed validation omitted its project result")
        selectors = validation_result.get("default_flavor_selectors")
        if (
            not isinstance(selectors, list)
            or not selectors
            or any(
                not isinstance(selector, str) or not selector.startswith("+")
                for selector in selectors
            )
        ):
            raise RuntimeError("installed validation omitted declared default Flavors")
        flavors = validation_result.get("flavors")
        if not isinstance(flavors, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("coordinate"), str)
            for item in flavors
        ):
            raise RuntimeError("installed validation omitted its Flavor catalog")
        available_flavor_coordinates = {item["coordinate"] for item in flavors}
        missing_selected_flavors = [
            selector.removeprefix("+")
            for selector in selectors
            if selector.removeprefix("+") not in available_flavor_coordinates
        ]
        if missing_selected_flavors:
            raise RuntimeError(
                "installed init omitted selected Flavor definitions: "
                + ", ".join(missing_selected_flavors)
            )
        required_scaffold = (
            "literate.project.json",
            "literate.release.json",
            "SKILL.md",
            "AGENTS.md",
            "CHANGELOG.md",
            "docs/roadmap/active-work.md",
            "docs/user/test-matrix.md",
            "literate.test.example.json",
            "literate.workers.example.json",
            "samples/hello-component/component.md",
            "samples/hello-component/component.lock.json",
            "skills/specification-to-source/portable-application/SKILL.md",
            "skills/agent/SKILL.md",
            "skills/agent/record-user-directed-work/SKILL.md",
            "skills/agent/release-project/SKILL.md",
            "skills/agent/develop-in-production-workflow/SKILL.md",
            "workflows/production/staging/dev/workflow.md",
            "routing/production/staging/dev/routing.json",
            ".literate/initialization-origin.json",
            ".literate/initialization-baseline.json",
        )
        missing_scaffold = [
            relative
            for relative in required_scaffold
            if not (project / relative).is_file()
        ]
        if missing_scaffold:
            raise RuntimeError(
                "installed init omitted required scaffold: "
                + ", ".join(missing_scaffold)
            )
        release_publication = _assert_installed_release_protocol(
            root=root,
            launcher=launcher,
            environment=environment,
            parent_source=parent_source,
        )
        repository_dag = _prove_repository_dag(
            launcher,
            root,
            environment,
            parent_source,
        )
        tool_bootstrap = initialized.get("result", {}).get("tool_bootstrap")
        if arguments.isolated_tool_bootstrap:
            if (
                not isinstance(tool_bootstrap, dict)
                or tool_bootstrap.get("state") != "disabled"
            ):
                raise RuntimeError(
                    f"isolated init reported tool_bootstrap={tool_bootstrap!r}; "
                    "expected a mapping with state='disabled'"
                )
            if (project / ".codegraph" / "codegraph.db").is_file():
                raise RuntimeError(
                    "isolated init created a source-graph database without opting in"
                )

        installed_environment = Path(installed["environment"])
        installed_python = installed_environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        dispatcher = root / "synthetic-dispatcher.py"
        dispatcher.write_text(_SYNTHETIC_DISPATCHER, encoding="utf-8")
        worker_config = root / "synthetic-workers.json"
        worker_config.write_text(
            json.dumps(
                {
                    "schema": "urn:literate-ai:schema:v1:execution-worker-catalog",
                    "workers": [
                        {
                            "schema": "urn:literate-ai:schema:v1:execution-worker",
                            "worker_id": "synthetic",
                            "kind": "command",
                            "target_profile": "host",
                            "requirements": {
                                "schema": (
                                    "urn:literate-ai:schema:v1:execution-requirements"
                                ),
                                "os_family": None,
                                "os_version": None,
                                "cpu_architecture": None,
                                "minimum_cpu_cores": None,
                                "minimum_memory_mib": None,
                                "gpu": {
                                    "schema": (
                                        "urn:literate-ai:schema:v1:gpu-requirement"
                                    ),
                                    "vendor": None,
                                    "model": None,
                                    "minimum_count": None,
                                    "minimum_memory_mib": None,
                                    "capabilities": None,
                                },
                            },
                            "parameters": [],
                            "endpoint": None,
                            "workspace": None,
                            "command": [str(installed_python), str(dispatcher)],
                            "environment": [],
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        # build/test now require an explicit component whenever
        # repository_policy.default_component is null (as it is for a freshly
        # initialized project); name the starter sample, matching the run call below.
        command_built = _run(
            str(launcher),
            "--json",
            "build",
            "--project",
            ".",
            "--worker",
            "synthetic",
            "--worker-config",
            str(worker_config),
            "samples/hello-component",
            cwd=project,
            environment=environment,
        )
        command_tested = _run(
            str(launcher),
            "--json",
            "test",
            "--project",
            ".",
            "--worker",
            "synthetic",
            "--worker-config",
            str(worker_config),
            "samples/hello-component",
            cwd=project,
            environment=environment,
        )
        command_ran = _run(
            str(launcher),
            "--json",
            "run",
            "--project",
            ".",
            "--worker",
            "synthetic",
            "--worker-config",
            str(worker_config),
            "samples/hello-component",
            json.dumps([{"name": "LitAI", "messages": ["Build portable software"]}]),
            cwd=project,
            environment=environment,
        )
        command_build_result = command_built.get("result")
        command_test_result = command_tested.get("result")
        command_run_result = command_ran.get("result")
        if (
            not isinstance(command_build_result, dict)
            or not command_build_result.get("passed")
            or not isinstance(command_build_result.get("artifact"), str)
            or not command_build_result["artifact"].startswith("cas:")
            or not isinstance(command_test_result, dict)
            or not command_test_result.get("passed")
            or not isinstance(command_run_result, dict)
            or command_run_result.get("exit_status") != 0
            or command_run_result.get("stdout") != "installed-command-worker-output\n"
        ):
            raise RuntimeError(
                "installed command-worker build/test/run contract did not pass"
            )

        rebuilt: dict[str, object] | None = None
        if arguments.live:
            try:
                selection = resolve_live_test_selection(
                    coding_cli=arguments.coding_cli,
                    model=arguments.model,
                    environment=environment,
                    project_root=repository,
                )
            except CodingCliError as exc:
                raise RuntimeError(f"{exc.code}: {exc.message}") from exc
            apply_live_test_selection(environment, selection)
            log_live_session_models(selection)
            rebuilt = _run(
                str(launcher),
                "--json",
                "rebuild",
                "samples/hello-component",
                "--project",
                ".",
                "--allow-host-execution",
                "--update-receipt",
                "--model",
                selection.model,
                cwd=project,
                environment=environment,
            )
            result = rebuilt.get("result")
            if not isinstance(result, dict) or not result.get("passed"):
                raise RuntimeError("installed starter Component did not pass E2E")
            execution = result.get("execution_command")
            if (
                not result.get("receipt_committed")
                or not isinstance(execution, dict)
                or not isinstance(execution.get("argv"), list)
                or not execution["argv"]
                or not isinstance(execution.get("cwd"), str)
                or not isinstance(execution.get("environment"), dict)
                or any(
                    not isinstance(name, str) or not isinstance(value, str)
                    for name, value in execution["environment"].items()
                )
            ):
                raise RuntimeError(
                    "installed starter E2E lacks a committed receipt or run command"
                )
            application_environment = dict(environment)
            application_environment.update(execution["environment"])
            application = subprocess.run(
                # argv[1] is the JSON *array* of arguments, per the portable-application
                # skills; passing the bare object is what the contract forbids.
                (
                    *execution["argv"],
                    json.dumps(
                        [{"name": "LitAI", "messages": ["Build portable software"]}]
                    ),
                ),
                cwd=execution["cwd"],
                env=application_environment,
                check=False,
                text=True,
                capture_output=True,
            )
            try:
                observed_output = json.loads(application.stdout)
            except json.JSONDecodeError:
                observed_output = None
            if application.returncode != 0 or observed_output != {
                "greeting": "Hello, LitAI!",
                "recipient_id": "litai",
                "message_count": 1,
                "word_count": 3,
            }:
                diagnostic = json.dumps(
                    {
                        "argv": execution["argv"],
                        "returncode": application.returncode,
                        "stderr": application.stderr[-2000:],
                        "stdout": application.stdout[-2000:],
                    },
                    sort_keys=True,
                )
                raise RuntimeError(
                    "installed starter artifact did not produce its exact known "
                    f"output: {diagnostic}"
                )

        built: dict[str, object] | None = None
        tested: dict[str, object] | None = None
        ran: dict[str, object] | None = None
        if arguments.live:
            # The scaffolding path a new user actually takes: init, build, run.
            # It is separate from the rebuild proof above because a regression in the
            # friendly verbs would otherwise hide behind the gated lifecycle passing.
            built = _run(
                str(launcher),
                "--json",
                "build",
                "--project",
                ".",
                "--model",
                selection.model,
                "samples/hello-component",
                cwd=project,
                environment=environment,
            )
            build_result = built.get("result")
            if not isinstance(build_result, dict) or not build_result.get("passed"):
                raise RuntimeError("installed `litai build` did not pass")
            artifact = build_result.get("artifact")
            if not isinstance(artifact, str) or not Path(artifact).exists():
                raise RuntimeError("installed `litai build` kept no runnable artifact")

            tested = _run(
                str(launcher),
                "--json",
                "test",
                "--project",
                ".",
                "--model",
                selection.model,
                "samples/hello-component",
                cwd=project,
                environment=environment,
            )
            test_result = tested.get("result")
            if not isinstance(test_result, dict) or not test_result.get("passed"):
                raise RuntimeError("installed `litai test` did not pass")

            ran = _run(
                str(launcher),
                "--json",
                "run",
                "samples/hello-component",
                json.dumps(
                    [{"name": "LitAI", "messages": ["Build portable software"]}]
                ),
                cwd=project,
                environment=environment,
            )
            run_result = ran.get("result")
            if not isinstance(run_result, dict) or run_result.get("exit_status") != 0:
                raise RuntimeError("installed `litai run` did not exit zero")

        print(
            json.dumps(
                {
                    "ok": True,
                    "live": arguments.live,
                    "isolated_tool_bootstrap": arguments.isolated_tool_bootstrap,
                    "launcher": launcher.name,
                    "project_profile": validated["result"]["profile"],
                    "starter_lock_current": locked["result"]["current"],
                    "starter_planned": bool(
                        planned["result"]["standard_component_execution"][
                            "generation_plans"
                        ]
                    ),
                    "model_scope_plan": model_scope_plan,
                    "starter_rebuilt": rebuilt is not None,
                    "starter_output_verified": rebuilt is not None,
                    "starter_scaffold_verified": True,
                    "command_worker_built": True,
                    "command_worker_tested": True,
                    "command_worker_ran": True,
                    "release_publication_identity": release_publication.get("identity"),
                    "repository_dag": repository_dag,
                    "tool_bootstrap_state": (
                        tool_bootstrap.get("state")
                        if isinstance(tool_bootstrap, dict)
                        else None
                    ),
                    "built": bool(built),
                    "tested": bool(tested),
                    "ran": bool(ran),
                },
                sort_keys=True,
            )
        )
    return 0


def main() -> int:
    run = attach_run()
    if run is None:
        return _main()
    context = run.node(
        "installed/project-smoke",
        operation="installed.project-smoke",
        parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
    )
    with context as node:
        status = _main()
        if status and isinstance(node, EvidenceNode):
            node.fail(f"installed project smoke exited with status {status}")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
