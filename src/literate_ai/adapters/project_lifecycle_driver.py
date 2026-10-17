"""Public filesystem/process adapter for an external project lifecycle driver."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.builders import BuildError, run_bounded_process
from literate_ai.adapters.live_test_selection import configured_test_coding_cli
from literate_ai.adapters.models import CodingCliError, lifecycle_driver_environment
from literate_ai.contracts import (
    ContentIdentity,
    ProjectLifecycleDriver,
    canonical_identity,
)
from literate_ai.diagnostics import report_progress
from literate_ai.evidence_ledger import EvidenceNode, attach_run
from literate_ai.projects import LoadedProject

PROJECT_LIFECYCLE_ENVIRONMENT_SCHEMA = "literate-ai/project-lifecycle-environment@1"
PROJECT_LIFECYCLE_IMPLEMENTATION_SCHEMA = (
    "literate-ai/project-lifecycle-implementation@1"
)
_MAXIMUM_DRIVER_IMPLEMENTATION_BYTES = 32 * 1024 * 1024

_CREDENTIAL_VALUE_ENVIRONMENT_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "CODEX_API_KEY",
        "CURSOR_API_KEY",
        "LITAI_INHERITED_SESSION_AUTH_KEY",
        "OPENAI_API_KEY",
    }
)


class ProjectLifecycleDriverAdapterError(ValueError):
    """Stable failure from external lifecycle-driver binding or execution."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def lifecycle_driver_environment_identity_material(
    environment: Mapping[str, str],
) -> dict[str, object]:
    """Bind semantic values while recording credential values by presence only."""

    credential_keys = tuple(
        key for key in sorted(environment) if key in _CREDENTIAL_VALUE_ENVIRONMENT_KEYS
    )
    return {
        "schema": PROJECT_LIFECYCLE_ENVIRONMENT_SCHEMA,
        "values": {
            key: environment[key]
            for key in sorted(environment)
            if key not in _CREDENTIAL_VALUE_ENVIRONMENT_KEYS
        },
        "credential_key_presence": list(credential_keys),
    }


_DRIVER_FAILURE_SIGNALS = (
    " failed:",
    "generation errors=",
    "build errors=",
    "Error:",
    "error:",
)
_DRIVER_EXCERPT_SIGNAL_LIMIT = 4
_DRIVER_EXCERPT_TAIL_LINES = 8
_DRIVER_EXCERPT_BYTE_LIMIT = 4000


def _driver_failure_excerpt(diagnostic: str) -> str:
    """Summarize a driver failure without discarding the line that explains it.

    A driver's most informative line is often its own failure summary near the start of
    a long Python traceback, so a plain tail excerpt reliably keeps the least useful
    frames and drops the actual cause.
    """

    lines = [line.strip() for line in diagnostic.splitlines() if line.strip()]
    if not lines:
        return ""
    signals = [
        line
        for line in lines
        if any(marker in line for marker in _DRIVER_FAILURE_SIGNALS)
    ][:_DRIVER_EXCERPT_SIGNAL_LIMIT]
    selected: list[str] = []
    for line in (*signals, *lines[-_DRIVER_EXCERPT_TAIL_LINES:]):
        if line not in selected:
            selected.append(line)
    return "\n".join(selected)[-_DRIVER_EXCERPT_BYTE_LIMIT:].strip()


def _retain_driver_diagnostic(
    diagnostic: str, *, project_root: Path | None = None
) -> str | None:
    """Retain the complete driver output so a bounded excerpt is never the only copy."""

    if not diagnostic.strip():
        return None
    run = attach_run(project_root)
    if run is not None:
        context = run.node(
            "lifecycle/driver/failure",
            operation="lifecycle.driver.diagnostic",
            parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
        )
        with context as node:
            if isinstance(node, EvidenceNode):
                node.attach_text("driver.log", diagnostic, role="driver-diagnostic")
                path = node.directory / "driver.log"
                report_progress(
                    f"Retained lifecycle driver diagnostic: {path.resolve()}"
                )
                node.fail("lifecycle driver failed")
                if path.is_file():
                    return str(path.resolve())
    try:
        descriptor, path = tempfile.mkstemp(
            prefix="litai-driver-failure-", suffix=".log"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(diagnostic)
    except OSError:
        # Diagnostics are best effort; never mask the driver failure with a write error.
        return None
    report_progress(f"Retained lifecycle driver diagnostic: {Path(path).resolve()}")
    return path


def _require_no_symlink_path(root: Path, target: Path, *, label: str) -> None:
    relative = target.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.path_unsafe", f"{label} cannot traverse a symbolic link"
            )


def lifecycle_driver_implementation_identity(
    project: LoadedProject, driver: ProjectLifecycleDriver
) -> ContentIdentity:
    """Hash the exact project tree trusted to implement an external driver.

    The directory scan below walks the raw filesystem, not git's tracked tree, so it
    deliberately excludes dot-prefixed entries (``.DS_Store``, ``.claude/``, editor
    swap files, tool caches, ...) in addition to ``__pycache__``/``.pyc``/``.pyo``.
    Those entries are commonly hidden from ``git status``/``git diff`` by a *global*
    ``core.excludesFile`` rather than this repository's own ``.gitignore``, so they
    are invisible to `scripts/review_lifecycle_driver.py`'s git-diff-based drift
    report even though they would otherwise silently change this digest whenever
    such a file happens to exist under a declared implementation path (see issue
    #125: the reported TCB digest churn without any git-visible change).
    """

    members: list[dict[str, object]] = []
    seen: set[str] = set()
    total_bytes = 0
    # A Standard-bound driver (#215) has no project-local implementation paths:
    # its trusted implementation is the installed framework distribution itself.
    # Return that distribution identity as the implementation pin instead of
    # iterating a nonexistent ``implementation_paths`` list.
    if not hasattr(driver, "implementation_paths"):
        distribution = getattr(driver, "framework_distribution_identity", None)
        if isinstance(distribution, ContentIdentity):
            return canonical_identity(
                {
                    "schema": PROJECT_LIFECYCLE_IMPLEMENTATION_SCHEMA,
                    "standard_distribution": distribution.uri,
                }
            )
        raise ProjectLifecycleDriverAdapterError(
            "rebuild.driver_implementation_unavailable",
            "lifecycle driver has no implementation paths or distribution identity",
        )
    for declared in driver.implementation_paths:
        candidate = project.root.joinpath(*Path(declared).parts)
        _require_no_symlink_path(
            project.root, candidate, label="lifecycle driver implementation"
        )
        try:
            path = candidate.resolve(strict=True)
            if not path.is_relative_to(project.root):
                raise OSError
            if path.is_file():
                files = (path,)
            elif path.is_dir():
                discovered: list[Path] = []
                for descendant in sorted(path.rglob("*")):
                    if descendant.is_symlink():
                        raise OSError
                    relative_parts = descendant.relative_to(path).parts
                    if (
                        "__pycache__" in relative_parts
                        or any(part.startswith(".") for part in relative_parts)
                        or descendant.suffix in {".pyc", ".pyo"}
                    ):
                        continue
                    if descendant.is_dir():
                        continue
                    if not descendant.is_file():
                        raise OSError
                    discovered.append(descendant)
                files = tuple(discovered)
            else:
                raise OSError
            if not files:
                raise OSError
        except OSError as exc:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_implementation_unavailable",
                "a pinned lifecycle driver implementation root is unavailable",
            ) from exc
        for implementation_file in files:
            relative = implementation_file.relative_to(project.root).as_posix()
            if relative in seen:
                raise ProjectLifecycleDriverAdapterError(
                    "rebuild.driver_implementation_overlap",
                    "lifecycle driver implementation roots overlap",
                )
            seen.add(relative)
            try:
                size = implementation_file.stat().st_size
                total_bytes += size
                if total_bytes > _MAXIMUM_DRIVER_IMPLEMENTATION_BYTES:
                    raise ProjectLifecycleDriverAdapterError(
                        "rebuild.driver_implementation_too_large",
                        "lifecycle driver implementation exceeds the byte limit",
                    )
                digest = hashlib.sha256()
                with implementation_file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except ProjectLifecycleDriverAdapterError:
                raise
            except OSError as exc:
                raise ProjectLifecycleDriverAdapterError(
                    "rebuild.driver_implementation_unavailable",
                    "a pinned lifecycle driver implementation file is unavailable",
                ) from exc
            members.append(
                {
                    "path": relative,
                    "size": size,
                    "identity": f"sha256:{digest.hexdigest()}",
                }
            )
    members.sort(key=lambda item: str(item["path"]))
    for argument in driver.argv[1:]:
        if argument.startswith("{"):
            continue
        referenced = project.root.joinpath(*Path(argument).parts)
        if referenced.is_symlink() or not referenced.is_file():
            continue
        try:
            relative = (
                referenced.resolve(strict=True).relative_to(project.root).as_posix()
            )
        except (OSError, ValueError) as exc:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_implementation_unavailable",
                "a lifecycle driver argv file is outside project authority",
            ) from exc
        if relative not in seen:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_implementation_incomplete",
                "a project-relative lifecycle driver argv file is outside the pinned "
                "implementation closure",
            )
    return canonical_identity(
        {
            "schema": PROJECT_LIFECYCLE_IMPLEMENTATION_SCHEMA,
            "members": members,
        }
    )


def _resolve_executable(
    driver: ProjectLifecycleDriver, environment: Mapping[str, str]
) -> tuple[Path, ContentIdentity]:
    configured = sys.executable if driver.argv[0] == "{python}" else driver.argv[0]
    discovered = shutil.which(configured, path=environment.get("PATH"))
    if discovered is None:
        raise ProjectLifecycleDriverAdapterError(
            "rebuild.driver_unavailable",
            "configured lifecycle driver executable is unavailable",
        )
    invocation = Path(os.path.abspath(discovered))
    try:
        launcher = invocation.resolve(strict=True)
        if (
            not invocation.is_file()
            or not os.access(invocation, os.X_OK)
            or not launcher.is_file()
        ):
            raise OSError
        content = launcher.read_bytes()
    except OSError as exc:
        raise ProjectLifecycleDriverAdapterError(
            "rebuild.driver_unavailable",
            "configured lifecycle driver executable is not a readable regular file",
        ) from exc
    return invocation, canonical_identity(
        {
            "schema": "literate-ai/project-lifecycle-executable-binding@1",
            "invocation": str(invocation),
            "launcher": str(launcher),
            "launcher_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
    )


@dataclass(frozen=True, slots=True)
class BoundExternalProjectLifecycleDriver:
    """A project-authorized external driver bound to exact host capabilities."""

    project: LoadedProject
    driver: ProjectLifecycleDriver
    executable: Path
    executable_identity: ContentIdentity
    implementation_identity: ContentIdentity
    environment: Mapping[str, str]
    environment_identity_material: Mapping[str, object]

    def require_executable_unchanged(self) -> None:
        try:
            launcher = self.executable.resolve(strict=True)
            if (
                not self.executable.is_file()
                or not os.access(self.executable, os.X_OK)
                or not launcher.is_file()
            ):
                raise OSError
            actual = canonical_identity(
                {
                    "schema": "literate-ai/project-lifecycle-executable-binding@1",
                    "invocation": str(self.executable),
                    "launcher": str(launcher),
                    "launcher_digest": (
                        "sha256:" + hashlib.sha256(launcher.read_bytes()).hexdigest()
                    ),
                }
            )
        except OSError as exc:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_changed",
                "lifecycle driver executable became unavailable during rebuild",
            ) from exc
        if actual != self.executable_identity:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_changed",
                "lifecycle driver executable changed during rebuild",
            )

    def current_implementation_identity(self) -> ContentIdentity:
        return lifecycle_driver_implementation_identity(self.project, self.driver)

    def command(
        self,
        *,
        specification: Path,
        runtime_root: Path,
        candidate_receipt: Path,
        project_revision: ContentIdentity,
        request_identity: ContentIdentity,
        flavor_selectors: Sequence[str],
        allow_host_execution: bool = True,
    ) -> tuple[str, ...]:
        if flavor_selectors and not self.driver.accepts_flavor_selectors:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.flavors_unsupported",
                "configured lifecycle driver does not accept explicit Flavor selectors",
            )
        replacements = {
            "{allow_host_execution}": (
                "--allow-host-execution" if allow_host_execution else ""
            ),
            "{candidate_receipt}": str(candidate_receipt),
            "{lifecycle_request_identity}": request_identity.uri,
            "{project}": str(self.project.root),
            "{project_revision_identity}": project_revision.uri,
            "{python}": str(self.executable),
            "{runtime_root}": str(runtime_root),
            "{specification}": str(specification),
        }
        result: list[str] = []
        for argument in self.driver.argv:
            if argument == "{flavor_args}":
                for selector in flavor_selectors:
                    result.extend(("--flavor", selector))
            else:
                expanded = replacements.get(argument, argument)
                if expanded:
                    result.append(expanded)
        result[0] = str(self.executable)
        if len(result) > 256:
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_argv_limit",
                "expanded lifecycle driver command exceeds the argument limit",
            )
        return tuple(result)

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        try:
            completed = run_bounded_process(
                argv,
                cwd=self.project.root,
                environment=self.environment if environment is None else environment,
                timeout_seconds=(
                    self.driver.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                stdout_limit_bytes=64 * 1024,
                stderr_limit_bytes=64 * 1024,
                error_prefix="rebuild.driver",
            )
        except BuildError as exc:
            if exc.code.endswith("_timeout"):
                raise ProjectLifecycleDriverAdapterError(
                    "rebuild.driver_timeout",
                    "configured lifecycle driver exceeded its authorized timeout",
                ) from exc
            if exc.code.endswith("_launch_failed"):
                raise ProjectLifecycleDriverAdapterError(
                    "rebuild.driver_unavailable",
                    "configured lifecycle driver could not be started",
                ) from exc
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_output_invalid",
                "configured lifecycle driver exceeded or could not close its bounded "
                "diagnostic streams",
            ) from exc
        if completed.returncode != 0:
            diagnostic = completed.stderr or completed.stdout
            decoded = diagnostic.decode("utf-8", errors="replace")
            detail = _driver_failure_excerpt(decoded)
            suffix = f": {detail}" if detail else ""
            retained = _retain_driver_diagnostic(
                decoded,
                project_root=self.project.root,
            )
            location = f"; complete driver output: {retained}" if retained else ""
            raise ProjectLifecycleDriverAdapterError(
                "rebuild.driver_failed",
                "configured lifecycle driver failed with exit status "
                f"{completed.returncode}{suffix}{location}",
            )


def _configured_coding_cli(
    project: LoadedProject,
    driver: ProjectLifecycleDriver,
    source_environment: Mapping[str, str],
) -> str | None:
    """Name the CLI the driver's live selection will pair with its model.

    Resolve from only the keys the driver receives, so both read the same test
    configuration; PATH order must not pick a CLI the configured model is not for.
    """

    if str(source_environment.get("CODING_CLI", "")).strip():
        return None
    environment = {
        key: source_environment[key]
        for key in driver.environment_keys
        if key in source_environment
    }
    return configured_test_coding_cli(
        environment=environment, project_root=project.root
    )


def bind_external_project_lifecycle_driver(
    project: LoadedProject,
    driver: ProjectLifecycleDriver,
    source_environment: Mapping[str, str],
) -> BoundExternalProjectLifecycleDriver:
    """Bind one configured external driver to exact project and host authority."""

    try:
        environment = lifecycle_driver_environment(
            source_environment,
            driver.environment_keys,
            workspace=project.root,
            default_coding_cli=_configured_coding_cli(
                project, driver, source_environment
            ),
        )
    except CodingCliError as exc:
        raise ProjectLifecycleDriverAdapterError(exc.code, exc.message) from exc
    executable, executable_identity = _resolve_executable(driver, environment)
    implementation_identity = lifecycle_driver_implementation_identity(project, driver)
    if implementation_identity != driver.implementation_identity:
        raise ProjectLifecycleDriverAdapterError(
            "rebuild.driver_implementation_mismatch",
            "lifecycle driver implementation does not match the project-authorized "
            "content identity",
        )
    return BoundExternalProjectLifecycleDriver(
        project=project,
        driver=driver,
        executable=executable,
        executable_identity=executable_identity,
        implementation_identity=implementation_identity,
        environment=dict(environment),
        environment_identity_material=lifecycle_driver_environment_identity_material(
            environment
        ),
    )


__all__ = [
    "BoundExternalProjectLifecycleDriver",
    "PROJECT_LIFECYCLE_ENVIRONMENT_SCHEMA",
    "PROJECT_LIFECYCLE_IMPLEMENTATION_SCHEMA",
    "ProjectLifecycleDriverAdapterError",
    "bind_external_project_lifecycle_driver",
    "lifecycle_driver_environment_identity_material",
    "lifecycle_driver_implementation_identity",
]
