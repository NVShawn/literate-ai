"""Fan selected E2E samples from exact working-tree or Git sources over SSH."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.execution_dispatch import (
    ExecutionDispatchAdapterError,
    load_execution_worker_catalog,
)
from literate_ai.adapters.live_test_selection import (
    LIVE_MODEL_ENVIRONMENT,
    REMOTE_LIVE_GATE_ENVIRONMENT,
    apply_live_test_selection,
    log_live_session_models,
    posix_export_prefix,
    resolve_live_test_selection,
)
from literate_ai.adapters.models.coding_cli import CodingCliError
from literate_ai.adapters.ssh_transport import (
    scp_arguments as bounded_scp_arguments,
)
from literate_ai.adapters.ssh_transport import (
    ssh_arguments as bounded_ssh_arguments,
)
from literate_ai.adapters.test_matrix_config import (
    GLOBAL_TEST_MATRIX_SCHEMA,
    TestMatrixConfigError,
    load_global_test_matrix,
)
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_test_config_path,
    resolve_worker_config_path,
)
from literate_ai.contracts import (
    ContractValidationError,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.evidence_ledger import EvidenceNode, attach_run, record_retained_output
from literate_ai.remote_source_guard import (
    extract_source_archive,
    git_tree_identity,
    source_paths_identity,
    source_tree_identity,
)

SCHEMA = GLOBAL_TEST_MATRIX_SCHEMA
FANOUT_CHECKPOINT_VERSION = 2
DEFAULT_TARGET_TIMEOUT_SECONDS = 3600
_HOST = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$")
_PATH = re.compile(r"^(?:~/|/)[A-Za-z0-9._/-]+$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}[A-Za-z0-9]$|^[A-Za-z0-9]$")
_EXCLUDED_ROOTS = {".codegraph", ".git", "_build"}
_EXCLUDED_TREE_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_ARCHIVE_DIGEST_CODE = (
    "import hashlib,sys; "
    "actual=hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest(); "
    "sys.exit('source archive SHA-256 mismatch') "
    "if actual != sys.argv[2] else None"
)
_FILE_DIGEST_CODE = (
    "import hashlib,sys; "
    "actual=hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest(); "
    "sys.exit('remote source guard digest mismatch') "
    "if actual != sys.argv[2] else None"
)
_GIT_GUARD_CODE = (
    "import hashlib,pathlib,subprocess,sys; "
    'content=subprocess.run(["git","-c","core.hooksPath="+sys.argv[1],'
    '"-c","core.autocrlf=false","-c","core.eol=lf",'
    '"-c","core.symlinks=true","-C",sys.argv[2],"show",'
    'sys.argv[3]+":src/literate_ai/remote_source_guard.py"],check=True,'
    "capture_output=True).stdout; "
    "actual=hashlib.sha256(content).hexdigest(); "
    'sys.exit("remote source guard digest mismatch") '
    "if actual != sys.argv[5] else pathlib.Path(sys.argv[4]).write_bytes(content)"
)
_REMOTE_DIAGNOSTIC_TIMEOUT_SECONDS = 15
_POSIX_PYTHON_FALLBACKS = (
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
)
_ACTIVE_FLAVOR_SELECTORS: tuple[str, ...] = ()


def _posix_flavor_arguments() -> str:
    return " ".join(
        shlex.quote(f"--flavor={selector}") for selector in _ACTIVE_FLAVOR_SELECTORS
    )


def _powershell_flavor_arguments() -> str:
    return " ".join(
        "'--flavor=" + selector.replace("'", "''") + "'"
        for selector in _ACTIVE_FLAVOR_SELECTORS
    )


def _selected_platforms(selectors: tuple[str, ...]) -> frozenset[str]:
    selected: set[str] = set()
    for raw in selectors:
        selector = raw.removeprefix("+")
        if selector in {
            "os.*",
            "flavor://samples/os-*",
            "flavor://literate-ai/os-*",
        }:
            return frozenset({"linux", "macos", "windows"})
        for name in ("linux", "macos", "windows"):
            if selector in {
                f"os-{name}",
                f"flavor://samples/os-{name}",
                f"flavor://literate-ai/os-{name}",
            }:
                selected.add(name)
    if selected:
        return frozenset(selected)
    host = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(
        platform.system().lower()
    )
    if host is None:
        raise ValueError("sample fanout controller host OS is unsupported")
    return frozenset({host})


def _posix_python_discovery(
    fallbacks: tuple[str, ...] = _POSIX_PYTHON_FALLBACKS,
) -> str:
    """Prefer PATH, then probe bounded package-manager locations."""

    fallback_arguments = " ".join(shlex.quote(item) for item in fallbacks)
    return (
        "{ python_command=''; old_ifs=$IFS; IFS=:; "
        "for directory in $PATH; do for name in python3 python; do "
        "candidate=${directory:-.}/$name; "
        'if [ -x "$candidate" ] && "$candidate" -c '
        "'import sys; raise SystemExit(sys.version_info < (3, 11))' "
        ">/dev/null 2>&1; then python_command=$candidate; break 2; fi; "
        "done; done; IFS=$old_ifs; "
        'if [ -z "$python_command" ]; then for candidate in '
        f"{fallback_arguments}; do "
        'if [ -x "$candidate" ] && "$candidate" -c '
        "'import sys; raise SystemExit(sys.version_info < (3, 11))' "
        ">/dev/null 2>&1; then python_command=$candidate; break; fi; done; fi; "
        'if [ -z "$python_command" ]; then '
        "echo 'Python 3.11+ is required' >&2; exit 127; fi; }"
    )


@dataclass(frozen=True, slots=True)
class Worker:
    definition: ExecutionWorker
    platform_flavor: str
    source_mode: str = "working-tree"

    @property
    def worker_id(self) -> str:
        return self.definition.worker_id

    @property
    def destination(self) -> str:
        if self.definition.endpoint is None or self.definition.workspace is None:
            raise ValueError(
                f"worker {self.worker_id!r} does not declare an SSH destination"
            )
        return f"{self.definition.endpoint}:{self.definition.workspace}"

    @property
    def platform_target(self) -> str:
        prefix = "flavor://literate-ai/os-"
        return (
            self.platform_flavor.removeprefix(prefix)
            if self.platform_flavor.startswith(prefix)
            else self.platform_flavor
        )

    @property
    def host_and_base(self) -> tuple[str, str]:
        if self.definition.kind is not ExecutionWorkerKind.SSH:
            raise ValueError(
                f"worker {self.worker_id!r} must be an SSH worker for sample fan-out"
            )
        host = self.definition.endpoint
        base = self.definition.workspace
        if host is None or base is None:
            raise ValueError(
                f"worker {self.worker_id!r} does not declare an SSH destination"
            )
        parts = base.removeprefix("~/").removeprefix("/").split("/")
        if (
            not _HOST.fullmatch(host)
            or not _PATH.fullmatch(base)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(
                f"worker {self.worker_id!r} destination must be "
                "username@host:~/path or a POSIX-style OpenSSH "
                "username@host:/absolute/path (never a drive-qualified path)"
            )
        return host, base


@dataclass(frozen=True, slots=True)
class GitSource:
    repository_url: str
    revision: str
    advertised_ref: str
    source_identity: str
    guard_digest: str
    history_depth: int | None = 1


def _posix_live_exports() -> str:
    overlay = {REMOTE_LIVE_GATE_ENVIRONMENT: "1"}
    cli = os.environ.get("CODING_CLI", "").strip()
    model = os.environ.get(LIVE_MODEL_ENVIRONMENT, "").strip()
    if cli:
        overlay["CODING_CLI"] = cli
    if model:
        overlay[LIVE_MODEL_ENVIRONMENT] = model
    return posix_export_prefix(overlay)


def _windows_live_exports() -> tuple[str, ...]:
    assignments = [
        f"$env:{REMOTE_LIVE_GATE_ENVIRONMENT} = '1'",
    ]
    cli = os.environ.get("CODING_CLI", "").strip()
    model = os.environ.get(LIVE_MODEL_ENVIRONMENT, "").strip()
    if cli:
        assignments.append(f"$env:CODING_CLI = {_powershell_literal(cli)}")
    if model:
        assignments.append(
            f"$env:{LIVE_MODEL_ENVIRONMENT} = {_powershell_literal(model)}"
        )
    return tuple(assignments)


def _posix_live_arguments(coding_cli: str | None, model: str | None) -> str:
    if (coding_cli is None) != (model is None):
        raise ValueError("remote live selection requires both coding CLI and model")
    if coding_cli is None or model is None:
        return ""
    return " ".join(
        (
            "--coding-cli",
            shlex.quote(coding_cli),
            "--model",
            shlex.quote(model),
        )
    )


def _windows_live_arguments(coding_cli: str | None, model: str | None) -> str:
    if (coding_cli is None) != (model is None):
        raise ValueError("remote live selection requires both coding CLI and model")
    if coding_cli is None or model is None:
        return ""
    return " ".join(
        (
            "--coding-cli",
            _powershell_literal(coding_cli),
            "--model",
            _powershell_literal(model),
        )
    )


def _git_source(
    repository: Path,
    *,
    timeout_seconds: int = DEFAULT_TARGET_TIMEOUT_SECONDS,
    history_depth: int | None = 1,
) -> GitSource:
    """Bind remote Git targets to one clean, pushed local revision."""

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return completed.stdout.strip()

    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError(
            "Git-backed test targets require a clean working tree so every host "
            "tests the same committed authority"
        )
    repository_url = git("remote", "get-url", "origin")
    revision = git("rev-parse", "HEAD")
    if (
        not repository_url
        or repository_url.startswith("-")
        or any(character in repository_url for character in "\r\n")
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision)
    ):
        raise ValueError("Git-backed test source is missing a safe origin or revision")
    remote_revision = git("ls-remote", "origin")
    advertised_refs = sorted(
        {
            fields[1].removesuffix("^{}")
            for line in remote_revision.splitlines()
            if len(fields := line.split(maxsplit=1)) == 2 and fields[0] == revision
        }
    )
    if history_depth is not None and not 1 <= history_depth <= 1_000_000:
        raise ValueError("repository history depth must be between 1 and 1000000")
    if not advertised_refs:
        containing = git(
            "for-each-ref",
            "--format=%(refname)",
            "--contains",
            revision,
            "refs/remotes/origin",
        ).splitlines()
        if not containing:
            raise ValueError(
                "Git-backed test revision is not reachable from an origin ref; "
                "push it before fan-out"
            )
        advertised_refs = [revision]
    guard_content = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "show",
            f"{revision}:src/literate_ai/remote_source_guard.py",
        ],
        check=True,
        capture_output=True,
        timeout=timeout_seconds,
    ).stdout
    return GitSource(
        repository_url,
        revision,
        advertised_refs[0],
        git_tree_identity(repository, revision),
        hashlib.sha256(guard_content).hexdigest(),
        history_depth,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=(
            Path(os.environ["LITAI_TEST_CONFIG"])
            if os.environ.get("LITAI_TEST_CONFIG")
            else None
        ),
        help=(
            "user-owned worker matrix (default: LITAI_TEST_CONFIG or "
            "the project-scoped user configuration path)"
        ),
    )
    parser.add_argument(
        "--worker-config",
        type=Path,
        default=(
            Path(os.environ["LITAI_WORKER_CONFIG"])
            if os.environ.get("LITAI_WORKER_CONFIG")
            else None
        ),
        help=(
            "user-owned execution workers (default: LITAI_WORKER_CONFIG or "
            "the resolved user configuration root)"
        ),
    )
    parser.add_argument("--sample", action="append", default=[], metavar="GLOB")
    parser.add_argument("--flavor", action="append", default=[], metavar="SELECTOR")
    parser.add_argument("--worker", action="append", default=[], metavar="ID")
    parser.add_argument(
        "--coding-cli",
        metavar="CLI",
        help="override the live-test coding CLI from project-scoped user config",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="override the live-test coding-CLI model from project-scoped user config",
    )
    parser.add_argument("--jobs", type=int)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TARGET_TIMEOUT_SECONDS,
        help="maximum wall time for each target (default: 3600)",
    )
    parser.add_argument(
        "--retain-workspaces",
        action="store_true",
        help=(
            "retain successful remote workspaces; failed workspaces are always retained"
        ),
    )
    history = parser.add_mutually_exclusive_group()
    history.add_argument(
        "--repository-history-depth",
        type=int,
        default=1,
        metavar="COMMITS",
        help=(
            "fetch this bounded exact-revision history depth for Git-backed "
            "workers (default: 1)"
        ),
    )
    history.add_argument(
        "--full-repository-history",
        action="store_true",
        help=(
            "explicitly unshallow Git-backed worker checkouts for "
            "history-sensitive jobs"
        ),
    )
    return parser


def _configuration(
    path: Path, worker_config: Path
) -> tuple[tuple[str, ...], tuple[Worker, ...]]:
    try:
        matrix = load_global_test_matrix(path)
    except TestMatrixConfigError as exc:
        raise ValueError(exc.message) from exc
    if not worker_config.is_file():
        return matrix.default_samples, ()
    try:
        catalog = load_execution_worker_catalog(worker_config)
    except ExecutionDispatchAdapterError as exc:
        raise ValueError(str(exc)) from exc
    workers: list[Worker] = []
    for selected in matrix.workers:
        try:
            definition = catalog.worker(selected.worker_id)
        except ContractValidationError as exc:
            raise ValueError(str(exc)) from exc
        worker = Worker(definition, selected.platform_flavor, selected.source_mode)
        if worker.definition.requirements.os_family != worker.platform_target:
            raise ValueError(
                f"worker {worker.worker_id!r} requirements must declare its "
                "matrix platform Flavor"
            )
        _ = worker.host_and_base
        workers.append(worker)
    if len({item.worker_id for item in workers}) != len(workers):
        raise ValueError("global test matrix worker IDs must be unique")
    destinations: set[tuple[str, str, str]] = set()
    for worker in workers:
        host, base = worker.host_and_base
        username, hostname = host.rsplit("@", 1)
        canonical_base = base.rstrip("/")
        if worker.platform_target == "windows":
            username = username.casefold()
            canonical_base = canonical_base.casefold()
        destination = (username, hostname.casefold(), canonical_base)
        if destination in destinations:
            raise ValueError("global test matrix worker destinations must be unique")
        destinations.add(destination)
    return matrix.default_samples, tuple(workers)


def _working_tree_excluded(relative: Path) -> bool:
    return relative.parts[0] in _EXCLUDED_ROOTS or any(
        part in _EXCLUDED_TREE_NAMES for part in relative.parts
    )


def _archive(root: Path, target: Path) -> str:

    selected_paths = _git_visible_paths(root)
    before = (
        source_tree_identity(root)
        if selected_paths is None
        else source_paths_identity(root, selected_paths)
    )
    with tarfile.open(target, "w:gz") as archive:
        if selected_paths is not None:
            for relative_text in selected_paths:
                relative = Path(relative_text)
                archive.add(
                    root.joinpath(*relative.parts),
                    arcname=Path("literate-ai") / relative,
                    recursive=False,
                )
        else:
            for current, directory_names, file_names in os.walk(
                root, topdown=True, followlinks=False
            ):
                directory = Path(current)
                retained_directories: list[str] = []
                entries: list[Path] = []
                for name in sorted(directory_names):
                    path = directory / name
                    relative = path.relative_to(root)
                    if _working_tree_excluded(relative):
                        continue
                    entries.append(path)
                    if not path.is_symlink():
                        retained_directories.append(name)
                directory_names[:] = retained_directories
                entries.extend(directory / name for name in sorted(file_names))
                for path in entries:
                    relative = path.relative_to(root)
                    if not _working_tree_excluded(relative):
                        archive.add(
                            path,
                            arcname=Path("literate-ai") / relative,
                            recursive=False,
                        )
    after = (
        source_tree_identity(root)
        if selected_paths is None
        else source_paths_identity(root, selected_paths)
    )
    final_paths = None if selected_paths is None else _git_visible_paths(root)
    if before != after or final_paths != selected_paths:
        raise ValueError("working tree changed while its source archive was captured")
    with tempfile.TemporaryDirectory(prefix="litai-archive-verification-") as temporary:
        extracted = Path(temporary)
        extract_source_archive(target, extracted / "materialized", before)
        archived = source_tree_identity(extracted / "materialized" / "literate-ai")
    if archived != before:
        raise ValueError("source archive does not contain the captured working tree")
    return before


def _git_visible_paths(root: Path) -> tuple[str, ...] | None:
    git = shutil.which("git")
    if git is None:
        if root.joinpath(".git").exists():
            raise ValueError("Git is required to capture a Git working tree safely")
        return None
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )

    def invoke(*arguments: str) -> bytes | None:
        completed = subprocess.run(
            [
                git,
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(root),
                *arguments,
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
        )
        return completed.stdout if completed.returncode == 0 else None

    top_level = invoke("rev-parse", "--show-toplevel")
    if top_level is None:
        if root.joinpath(".git").exists():
            raise ValueError("Git working-tree inventory failed")
        return None
    try:
        discovered_root = Path(os.fsdecode(top_level).strip()).resolve(strict=True)
        requested_root = root.resolve(strict=True)
    except OSError:
        raise ValueError("Git working-tree root could not be resolved safely") from None
    if discovered_root != requested_root:
        raise ValueError("working-tree source must be the Git repository root")
    visible = invoke("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    deleted = invoke("ls-files", "-z", "--deleted")
    if visible is None or deleted is None:
        raise ValueError("Git-visible working-tree inventory failed")
    deleted_paths = {
        Path(os.fsdecode(raw)).as_posix() for raw in deleted.split(b"\0") if raw
    }
    result: set[str] = set()
    for raw in visible.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        normalized = relative.as_posix()
        if (
            normalized in deleted_paths
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or _working_tree_excluded(relative)
        ):
            continue
        result.add(normalized)
    return tuple(sorted(result))


def _archive_identity(archive: Path) -> str:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return f"sha256:{digest}"


def _archive_digest(identity: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise ValueError("working-tree archive identity must be an exact SHA-256")
    return identity.removeprefix("sha256:")


def _source_identity(
    target: Worker,
    archive_source_identity: str | None,
    git_source: GitSource | None,
) -> str:
    if target.source_mode == "git":
        if git_source is None:
            raise ValueError("Git-backed target lacks an exact source revision")
        return git_source.source_identity
    if archive_source_identity is None:
        raise ValueError("working-tree target lacks an exact archive identity")
    return archive_source_identity


def _worker_log(report_root: Path, worker_id: str) -> Path:
    if not _WORKER_ID.fullmatch(worker_id):
        raise ValueError("test target ID is not a safe report basename")
    return report_root / f"{worker_id}.log"


def _remaining_timeout(deadline: float, timeout_seconds: int) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("target execution", timeout_seconds)
    # Whole-second resolution only: monotonic() arithmetic can round a hair past
    # timeout_seconds on floating-point deadlines, and no caller here schedules
    # subprocess timeouts to millisecond or finer precision anyway.
    return max(1, min(timeout_seconds, int(remaining)))


def _run_path(base: str, run_id: str) -> str:
    return f"{base.rstrip('/')}/runs/{run_id}"


def _git_run_path(base: str, run_id: str) -> str:
    return f"{base.rstrip('/')}.litai-runs/{run_id}"


def _ssh_arguments(target: Worker, command: str, timeout_seconds: float) -> list[str]:
    host, _base = target.host_and_base
    return list(
        bounded_ssh_arguments(
            host,
            command,
            timeout_seconds,
            transport=target.definition.transport,
            login_shell=target.platform_target != "windows",
        )
    )


def _scp_arguments(source: Path, destination: str, timeout_seconds: float) -> list[str]:
    endpoint, separator, remote = destination.partition(":")
    if not separator:
        raise ValueError("SCP destination must be endpoint:path")
    return list(bounded_scp_arguments(source, endpoint, remote, timeout_seconds))


def _worker_timeout(timeout_seconds: int) -> int:
    grace = min(15, max(2, timeout_seconds // 10))
    return max(1, timeout_seconds - grace)


def _posix_path(path: str) -> str:
    if path.startswith("~/"):
        return f'"$HOME/{path.removeprefix("~/")}"'
    return shlex.quote(path)


def _powershell_root(path: str) -> str:
    if path.startswith("~/"):
        suffix = path.removeprefix("~/").replace("'", "''")
        return f"Join-Path $HOME '{suffix}'"
    return "'" + path.replace("'", "''") + "'"


def _powershell_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _compressed_powershell_command(script: str) -> str:
    """Keep scripts below cmd.exe's ceiling without outer-shell interpolation.

    Windows OpenSSH may be configured with either cmd.exe or PowerShell as its
    default shell. A nested ``-Command \"$variable=...\"`` survives cmd.exe but an
    outer PowerShell expands those variables before the child sees them. Keep the
    compact decoder variable-free so the same argv is literal under both shells.
    """

    payload = base64.b64encode(
        gzip.compress(script.encode("utf-8"), compresslevel=9, mtime=0)
    ).decode("ascii")
    loader = (
        "& ([ScriptBlock]::Create((New-Object IO.StreamReader("
        "(New-Object IO.Compression.GzipStream("
        "(New-Object IO.MemoryStream(,[Convert]::FromBase64String("
        f"'{payload}'))),[IO.Compression.CompressionMode]::Decompress)),"
        "[Text.Encoding]::UTF8,[bool]1)).ReadToEnd()))"
    )
    return f'powershell.exe -NoProfile -NonInteractive -Command "{loader}"'


def _linux_command(
    remote_path: str,
    patterns: tuple[str, ...],
    platform: str,
    archive_transport_identity: str,
    source_identity: str,
    guard_digest: str,
    worker_timeout_seconds: int,
    *,
    cache_path: str | None = None,
    retain_workspace: bool = False,
    coding_cli: str | None = None,
    model: str | None = None,
) -> str:
    root = _posix_path(remote_path)
    incoming = _posix_path(remote_path + ".incoming")
    cache_root = _posix_path(cache_path or remote_path + ".litai-cache")
    archive_digest = shlex.quote(_archive_digest(archive_transport_identity))
    expected_source = shlex.quote(source_identity)
    expected_guard = shlex.quote(guard_digest)
    pattern_arguments = " ".join(
        f"--sample {shlex.quote(pattern)}" for pattern in patterns
    )
    flavor_arguments = _posix_flavor_arguments()
    live_arguments = _posix_live_arguments(coding_cli, model)
    command = " && ".join(
        (
            f"root={root}",
            f"incoming={incoming}",
            f"cache_root={cache_root}",
            'export BUILD_DIR="$cache_root/generated"',
            'export OBJ_DIR="$cache_root/_build"',
            _posix_live_exports(),
            'export LITAI_REMOTE_PYTHON_ENV="$cache_root/remote-python-env"',
            _posix_python_discovery(),
            (
                f'"$python_command" -c {shlex.quote(_ARCHIVE_DIGEST_CODE)} '
                f'"$incoming/repo.tar.gz" {archive_digest}'
            ),
            (
                f'"$python_command" -c {shlex.quote(_FILE_DIGEST_CODE)} '
                f'"$incoming/remote_source_guard.py" {expected_guard}'
            ),
            (
                '"$python_command" "$incoming/remote_source_guard.py" '
                'extract-archive --archive "$incoming/repo.tar.gz" '
                f'--destination "$root" --expected {expected_source}'
            ),
            'rm -rf -- "$incoming"',
            'cd "$root/literate-ai"',
            (
                f'"$python_command" -c {shlex.quote(_FILE_DIGEST_CODE)} '
                f"src/literate_ai/remote_source_guard.py {expected_guard}"
            ),
            (
                '"$python_command" src/literate_ai/remote_source_guard.py supervise '
                f"--timeout-seconds {worker_timeout_seconds} "
                ' --status "$root/litai-status.json" -- "$python_command" '
                f"scripts/remote_sample_worker.py --platform-flavor {platform} "
                f"{live_arguments} {pattern_arguments} {flavor_arguments}"
            ),
            (
                "{ printf '\\nLITAI_REMOTE_STATUS='; "
                'tr -d "\\r\\n" < "$root/litai-status.json"; printf "\\n"; }'
            ),
        )
    )
    if retain_workspace:
        return command
    return f'{command} && cd / && rm -rf -- "$root"'


def _windows_command(
    remote_path: str,
    patterns: tuple[str, ...],
    platform: str,
    archive_transport_identity: str,
    source_identity: str,
    guard_digest: str,
    worker_timeout_seconds: int,
    *,
    cache_path: str | None = None,
    retain_workspace: bool = False,
    coding_cli: str | None = None,
    model: str | None = None,
) -> str:
    archive_digest = _powershell_literal(_archive_digest(archive_transport_identity))
    expected_source = _powershell_literal(source_identity)
    expected_guard = _powershell_literal(guard_digest)
    pattern_arguments = " ".join(
        "--sample '" + pattern.replace("'", "''") + "'" for pattern in patterns
    )
    flavor_arguments = _powershell_flavor_arguments()
    live_arguments = _windows_live_arguments(coding_cli, model)
    statements = [
        "$ErrorActionPreference = 'Stop'",
        f"$root = {_powershell_root(remote_path)}",
        f"$incoming = {_powershell_root(remote_path + '.incoming')}",
        f"$cacheRoot = {_powershell_root(cache_path or '~/.litai')}",
        "$env:BUILD_DIR = Join-Path $cacheRoot 'sources'",
        "$env:OBJ_DIR = Join-Path $cacheRoot '_build'",
        "$env:LITAI_REMOTE_PYTHON_ENV = Join-Path $cacheRoot 'python'",
        # The configured Windows worker is a disposable VM; Codex must remain
        # noninteractive while the VM, rather than its host sandbox, contains it.
        "$env:LITAI_CODEX_SANDBOX = 'danger-full-access'",
        *_windows_live_exports(),
        f"$archiveSha256 = {archive_digest}",
        (
            "$python = $null; foreach ($directory in ($env:Path -split ';')) { "
            "foreach ($name in @('python3.exe', 'python.exe')) { "
            "$searchDirectory = if ($directory) { $directory } else { '.' }; "
            "$candidate = Join-Path $searchDirectory $name; "
            "if (Test-Path $candidate) { "
            '& $candidate -c "import sys; raise SystemExit(sys.version_info < '
            '(3, 11))" 2>$null; if ($LASTEXITCODE -eq 0) { '
            "$python = $candidate; break } } }; if ($python) { break } }"
        ),
        "if (-not $python) { throw 'Python 3.11+ is required' }",
        (
            f'& $python -c "{_ARCHIVE_DIGEST_CODE}" '
            "(Join-Path $incoming 'repo.tar.gz') $archiveSha256"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            f'& $python -c "{_FILE_DIGEST_CODE}" '
            f"(Join-Path $incoming 'remote_source_guard.py') {expected_guard}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "& $python (Join-Path $incoming 'remote_source_guard.py') "
            "extract-archive --archive (Join-Path $incoming 'repo.tar.gz') "
            f"--destination $root --expected {expected_source}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        "Remove-Item -LiteralPath $incoming -Recurse -Force",
        "Set-Location (Join-Path $root 'literate-ai')",
        (
            f'& $python -c "{_FILE_DIGEST_CODE}" '
            f"src/literate_ai/remote_source_guard.py {expected_guard}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "& $python src/literate_ai/remote_source_guard.py supervise "
            f"--timeout-seconds {worker_timeout_seconds} "
            "--status (Join-Path $root 'litai-status.json') -- $python "
            f"scripts/remote_sample_worker.py --platform-flavor {platform} "
            f"{live_arguments} {pattern_arguments} {flavor_arguments}"
        ),
        "$workerExit = $LASTEXITCODE",
        "if ($workerExit -ne 0) { exit $workerExit }",
        (
            "$status = (Get-Content -Raw "
            "(Join-Path $root 'litai-status.json')).Trim(); "
            'Write-Output "LITAI_REMOTE_STATUS=$status"'
        ),
    ]
    if not retain_workspace:
        statements.extend(
            (
                "Set-Location $HOME",
                "Remove-Item -LiteralPath $root -Recurse -Force",
            )
        )
    statements.append("exit 0")
    script = "; ".join(statements)
    return _powershell_command(script)


def _linux_git_command(
    checkout_path: str,
    run_id: str,
    patterns: tuple[str, ...],
    platform: str,
    source: GitSource,
    worker_timeout_seconds: int,
    *,
    retain_workspace: bool = False,
    coding_cli: str | None = None,
    model: str | None = None,
) -> str:
    repo = _posix_path(checkout_path)
    url = shlex.quote(source.repository_url)
    revision = shlex.quote(source.revision)
    advertised_ref = shlex.quote(source.advertised_ref)
    expected_source = shlex.quote(source.source_identity)
    expected_guard = shlex.quote(source.guard_digest)
    object_format = "sha256" if len(source.revision) == 64 else "sha1"
    git_fetch = (
        'if [ -f "$repo/.git/shallow" ]; then '
        "git -c core.hooksPath=/dev/null -c core.autocrlf=false "
        '-c core.eol=lf -c core.symlinks=true -C "$repo" '
        'fetch --no-tags --force --progress --unshallow origin "$source_ref"; '
        "else git -c core.hooksPath=/dev/null -c core.autocrlf=false "
        '-c core.eol=lf -c core.symlinks=true -C "$repo" '
        'fetch --no-tags --force --progress origin "$source_ref"; fi'
        if source.history_depth is None
        else "git -c core.hooksPath=/dev/null -c core.autocrlf=false "
        '-c core.eol=lf -c core.symlinks=true -C "$repo" '
        f"fetch --no-tags --force --progress --depth={source.history_depth} "
        'origin "$source_ref"'
    )
    pattern_arguments = " ".join(
        f"--sample {shlex.quote(pattern)}" for pattern in patterns
    )
    flavor_arguments = _posix_flavor_arguments()
    live_arguments = _posix_live_arguments(coding_cli, model)
    command = " && ".join(
        (
            f"repo={repo}",
            f"source_url={url}",
            f"revision={revision}",
            f"source_ref={advertised_ref}",
            f"object_format={object_format}",
            "export GIT_NO_REPLACE_OBJECTS=1",
            (
                'if [ ! -e "$repo" ]; then mkdir -p -- "$repo"; fi; '
                'if [ ! -d "$repo/.git" ]; then '
                "git -c core.hooksPath=/dev/null -c core.autocrlf=false "
                "-c core.eol=lf -c core.symlinks=true init "
                '--object-format="$object_format" -- "$repo"; fi'
            ),
            'test -d "$repo/.git"',
            (
                'test "$(git -C "$repo" rev-parse --show-object-format)" '
                '= "$object_format"'
            ),
            (
                'if ! actual_url=$(git -C "$repo" remote get-url origin '
                '2>/dev/null); then git -C "$repo" remote add origin '
                '"$source_url"; else test "$actual_url" = "$source_url"; fi'
            ),
            (
                f"if {git_fetch}; then :; else "
                "echo 'exact-revision fetch failed; retry with "
                "--repository-history-depth COMMITS or "
                "--full-repository-history' >&2; exit 1; fi"
            ),
            (
                "actual=$(git -c core.hooksPath=/dev/null "
                "-c core.autocrlf=false -c core.eol=lf -c core.symlinks=true "
                '-C "$repo" rev-parse "FETCH_HEAD^{commit}")'
            ),
            'test "$actual" = "$revision"',
            f'run_root="$repo.litai-runs/{run_id}"',
            'cache_root="$repo.litai-cache"',
            'export BUILD_DIR="$cache_root/generated"',
            'export OBJ_DIR="$cache_root/_build"',
            _posix_live_exports(),
            'export LITAI_REMOTE_PYTHON_ENV="$cache_root/remote-python-env"',
            'test ! -e "$run_root"',
            'mkdir -p "$repo.litai-runs"',
            'mkdir -- "$run_root"',
            _posix_python_discovery(),
            (
                f'"$python_command" -c {shlex.quote(_GIT_GUARD_CODE)} '
                '"/dev/null" "$repo" "$revision" '
                f'"$run_root/remote_source_guard.py" {expected_guard}'
            ),
            (
                '"$python_command" "$run_root/remote_source_guard.py" '
                'materialize-git --repository "$repo" --revision "$revision" '
                f'--destination "$run_root/literate-ai" --expected {expected_source}'
            ),
            'cd "$run_root/literate-ai"',
            (
                f'"$python_command" -c {shlex.quote(_FILE_DIGEST_CODE)} '
                f"src/literate_ai/remote_source_guard.py {expected_guard}"
            ),
            (
                '"$python_command" src/literate_ai/remote_source_guard.py supervise '
                f"--timeout-seconds {worker_timeout_seconds} "
                '--status "$run_root/litai-status.json" -- "$python_command" '
                f"scripts/remote_sample_worker.py --platform-flavor {platform} "
                f"{live_arguments} {pattern_arguments} {flavor_arguments}"
            ),
            (
                "{ printf '\\nLITAI_REMOTE_STATUS='; "
                'tr -d "\\r\\n" < "$run_root/litai-status.json"; printf "\\n"; }'
            ),
        )
    )
    if retain_workspace:
        return command
    return " && ".join(
        (
            command,
            "cd /",
            'rm -rf -- "$run_root"',
        )
    )


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_git_command(
    checkout_path: str,
    run_id: str,
    patterns: tuple[str, ...],
    platform: str,
    source: GitSource,
    worker_timeout_seconds: int,
    *,
    retain_workspace: bool = False,
    coding_cli: str | None = None,
    model: str | None = None,
) -> str:
    pattern_arguments = " ".join(
        "--sample '" + pattern.replace("'", "''") + "'" for pattern in patterns
    )
    flavor_arguments = _powershell_flavor_arguments()
    live_arguments = _windows_live_arguments(coding_cli, model)
    revision = _powershell_literal(source.revision)
    url = _powershell_literal(source.repository_url)
    advertised_ref = _powershell_literal(source.advertised_ref)
    expected_source = _powershell_literal(source.source_identity)
    expected_guard = _powershell_literal(source.guard_digest)
    bootstrap_code = _powershell_literal(
        base64.b64encode(_GIT_GUARD_CODE.encode("utf-8")).decode("ascii")
    )
    object_format = "sha256" if len(source.revision) == 64 else "sha1"
    git_options = (
        '-c "core.hooksPath=NUL" -c "core.autocrlf=false" '
        '-c "core.eol=lf" -c "core.symlinks=true"'
    )
    git_fetch = (
        "if (Test-Path (Join-Path $repo '.git/shallow')) { "
        f"& git {git_options} -C $repo fetch --no-tags --force --progress "
        "--unshallow origin $sourceRef } else { "
        f"& git {git_options} -C $repo fetch --no-tags --force --progress "
        "origin $sourceRef }"
        if source.history_depth is None
        else f"& git {git_options} -C $repo fetch --no-tags --force --progress "
        f"--depth={source.history_depth} origin $sourceRef"
    )
    statements = [
        "$ErrorActionPreference = 'Stop'",
        f"$repo = {_powershell_root(checkout_path)}",
        f"$sourceUrl = {url}",
        f"$revision = {revision}",
        f"$sourceRef = {advertised_ref}",
        f"$objectFormat = '{object_format}'",
        "$env:GIT_NO_REPLACE_OBJECTS = '1'",
        (
            "if (-not (Test-Path $repo)) { "
            "New-Item -ItemType Directory -Force -Path $repo | Out-Null }; "
            "if (-not (Test-Path (Join-Path $repo '.git'))) { "
            f'& git {git_options} init "--object-format=$objectFormat" -- $repo; '
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }"
        ),
        (
            "if (-not (Test-Path (Join-Path $repo '.git'))) { "
            "throw 'destination is not a Git checkout' }"
        ),
        "$actualObjectFormat = (& git -C $repo rev-parse --show-object-format).Trim()",
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "if ($actualObjectFormat -cne $objectFormat) { "
            "throw 'repository object format does not match revision' }"
        ),
        "$remoteNames = @(& git -C $repo remote)",
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "if (-not ($remoteNames -ccontains 'origin')) { "
            "& git -C $repo remote add origin $sourceUrl; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } } else { "
            "$actualUrlOutput = & git -C $repo remote get-url origin; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            "$actualUrl = ($actualUrlOutput | Out-String).Trim(); "
            "if ($actualUrl -cne $sourceUrl) { "
            "throw 'remote origin does not match' } }"
        ),
        git_fetch,
        (
            "if ($LASTEXITCODE -ne 0) { throw 'exact-revision fetch failed; "
            "retry with --repository-history-depth COMMITS or "
            "--full-repository-history' }"
        ),
        (
            f"$actual = (& git {git_options} -C $repo "
            'rev-parse "FETCH_HEAD^{commit}").Trim()'
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        "if ($actual -cne $revision) { throw 'fetched revision does not match' }",
        f'$runRoot = "$repo.litai-runs/{run_id}"',
        "$cacheRoot = Join-Path $HOME '.litai'",
        "$env:BUILD_DIR = Join-Path $cacheRoot 'sources'",
        "$env:OBJ_DIR = Join-Path $cacheRoot '_build'",
        "$env:LITAI_REMOTE_PYTHON_ENV = Join-Path $cacheRoot 'python'",
        # The configured Windows worker is a disposable VM; Codex must remain
        # noninteractive while the VM, rather than its host sandbox, contains it.
        "$env:LITAI_CODEX_SANDBOX = 'danger-full-access'",
        *_windows_live_exports(),
        "if (Test-Path $runRoot) { throw 'run root already exists' }",
        ('New-Item -ItemType Directory -Force -Path "$repo.litai-runs" | Out-Null'),
        "New-Item -ItemType Directory -Path $runRoot | Out-Null",
        (
            "$python = $null; foreach ($directory in ($env:Path -split ';')) { "
            "foreach ($name in @('python3.exe', 'python.exe')) { "
            "$searchDirectory = if ($directory) { $directory } else { '.' }; "
            "$candidate = Join-Path $searchDirectory $name; "
            "if (Test-Path $candidate) { "
            '& $candidate -c "import sys; raise SystemExit(sys.version_info < '
            '(3, 11))" 2>$null; if ($LASTEXITCODE -eq 0) { '
            "$python = $candidate; break } } }; if ($python) { break } }"
        ),
        "if (-not $python) { throw 'Python 3.11+ is required' }",
        "$bootstrapPath = Join-Path $runRoot 'bootstrap_remote_source_guard.py'",
        (
            "[IO.File]::WriteAllBytes($bootstrapPath, "
            f"[Convert]::FromBase64String({bootstrap_code}))"
        ),
        (
            "& $python $bootstrapPath NUL $repo "
            "$revision (Join-Path $runRoot 'remote_source_guard.py') "
            f"{expected_guard}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "& $python (Join-Path $runRoot 'remote_source_guard.py') "
            "materialize-git --repository $repo --revision $revision "
            "--destination (Join-Path $runRoot 'literate-ai') "
            f"--expected {expected_source}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        "Set-Location (Join-Path $runRoot 'literate-ai')",
        (
            f'& $python -c "{_FILE_DIGEST_CODE}" '
            f"src/literate_ai/remote_source_guard.py {expected_guard}"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        (
            "& $python src/literate_ai/remote_source_guard.py supervise "
            f"--timeout-seconds {worker_timeout_seconds} "
            "--status (Join-Path $runRoot 'litai-status.json') -- $python "
            f"scripts/remote_sample_worker.py --platform-flavor {platform} "
            f"{live_arguments} {pattern_arguments} {flavor_arguments}"
        ),
        "$workerExit = $LASTEXITCODE",
        "if ($workerExit -ne 0) { exit $workerExit }",
        (
            "$status = (Get-Content -Raw "
            "(Join-Path $runRoot 'litai-status.json')).Trim(); "
            'Write-Output "LITAI_REMOTE_STATUS=$status"'
        ),
    ]
    if not retain_workspace:
        statements.extend(
            (
                "Set-Location $HOME",
                (
                    "if (Test-Path $runRoot) { "
                    "Remove-Item -LiteralPath $runRoot -Recurse -Force }"
                ),
            )
        )
    statements.append("exit 0")
    script = "; ".join(statements)
    return _compressed_powershell_command(script)


def _workspace_paths(target: Worker, base: str, run_id: str) -> tuple[str, str | None]:
    if target.source_mode == "git":
        return _git_run_path(base, run_id), None
    final = _run_path(base, run_id)
    return final, final + ".incoming"


def _probe_workspace(
    target: Worker, host: str, base: str, run_id: str
) -> dict[str, object]:
    final, incoming = _workspace_paths(target, base, run_id)
    if target.platform_target == "windows":
        incoming_expression = (
            "$null" if incoming is None else _powershell_root(incoming)
        )
        script = "; ".join(
            (
                "$ErrorActionPreference = 'Stop'",
                f"$root = {_powershell_root(final)}",
                f"$incoming = {incoming_expression}",
                "$state = 'absent'",
                "$active = $null",
                (
                    "if (Test-Path $root) { $state = 'present'; $active = $root } "
                    "elseif ($incoming -and (Test-Path $incoming)) { "
                    "$state = 'staging'; $active = $incoming }"
                ),
                'Write-Output "LITAI_WORKSPACE_STATE=$state"',
                'if ($active) { Write-Output "LITAI_WORKSPACE_ROOT=$active" }',
                (
                    "if ($active -and (Test-Path (Join-Path $active "
                    "'litai-status.json'))) { $status = Get-Content -Raw "
                    "(Join-Path $active 'litai-status.json'); "
                    'Write-Output "LITAI_REMOTE_STATUS=$($status.Trim())" }'
                ),
            )
        )
        command = _powershell_command(script)
    else:
        root = _posix_path(final)
        incoming_value = "''" if incoming is None else _posix_path(incoming)
        command = "; ".join(
            (
                f"root={root}",
                f"incoming={incoming_value}",
                "state=absent",
                "active=''",
                (
                    'if [ -e "$root" ]; then state=present; active=$root; '
                    'elif [ -n "$incoming" ] && [ -e "$incoming" ]; then '
                    "state=staging; active=$incoming; fi"
                ),
                "printf 'LITAI_WORKSPACE_STATE=%s\\n' \"$state\"",
                (
                    'if [ -n "$active" ]; then printf '
                    "'LITAI_WORKSPACE_ROOT=%s\\n' \"$active\"; fi"
                ),
                (
                    'if [ -n "$active" ] && [ -f "$active/litai-status.json" ]; '
                    "then printf 'LITAI_REMOTE_STATUS='; "
                    'tr -d "\\r\\n" < "$active/litai-status.json"; printf "\\n"; fi'
                ),
            )
        )
    try:
        completed = subprocess.run(
            _ssh_arguments(target, command, _REMOTE_DIAGNOSTIC_TIMEOUT_SECONDS),
            check=False,
            capture_output=True,
            text=True,
            timeout=_REMOTE_DIAGNOSTIC_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "workspace_root": None,
            "workspace_state": "unknown",
            "remote_status": None,
        }
    state = "unknown"
    workspace_root: str | None = None
    remote_status: dict[str, object] | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("LITAI_WORKSPACE_STATE="):
            candidate = line.partition("=")[2]
            if candidate in {"absent", "present", "staging"}:
                state = candidate
        elif line.startswith("LITAI_WORKSPACE_ROOT="):
            workspace_root = line.partition("=")[2]
        elif line.startswith("LITAI_REMOTE_STATUS="):
            try:
                value = json.loads(line.partition("=")[2])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                remote_status = value
    return {
        "workspace_root": workspace_root,
        "workspace_state": state,
        "remote_status": remote_status,
    }


def _materialized_identity(output: str) -> str | None:
    prefix = "LITAI_SOURCE_TREE_IDENTITY="
    values = [
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    ]
    if len(values) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", values[0]):
        return None
    return values[0]


def _remote_status(output: str) -> dict[str, object] | None:
    prefix = "LITAI_REMOTE_STATUS="
    values: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line.removeprefix(prefix))
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == "literate-ai/remote-worker-status@1"
            and value.get("state") in {"running", "passed", "failed", "timed-out"}
        ):
            values.append(value)
    return values[0] if len(values) == 1 else None


def _remote_result(
    *,
    target: Worker,
    source_identity: str,
    source_revision: str | None,
    transport_identity: str | None,
    completed: subprocess.CompletedProcess[str],
    observation: dict[str, object],
    log: Path,
    retain_workspace: bool,
) -> dict[str, object]:
    remote_status = observation["remote_status"] or _remote_status(completed.stdout)
    timed_out = (
        isinstance(remote_status, dict) and remote_status.get("state") == "timed-out"
    )
    materialized_identity = _materialized_identity(completed.stdout)
    verified = materialized_identity == source_identity
    passed = completed.returncode == 0 and verified
    failure_kind = None
    if not passed:
        if timed_out:
            failure_kind = "timeout"
        elif completed.returncode == 0:
            failure_kind = "source-verification"
        else:
            failure_kind = "target-command"
    state = observation["workspace_state"]
    result: dict[str, object] = {
        "id": target.worker_id,
        "destination": target.destination,
        "platform_flavor": target.platform_flavor,
        "source_mode": target.source_mode,
        "source_identity": source_identity,
        "source_revision": source_revision,
        "transport_identity": transport_identity,
        "materialized_source_identity": materialized_identity,
        "returncode": completed.returncode,
        "passed": passed,
        "failure_kind": failure_kind,
        "retention_requested": retain_workspace,
        "workspace": observation["workspace_root"],
        "workspace_state": state,
        "workspace_retained": (
            True
            if state in {"present", "staging"}
            else False
            if state == "absent"
            else None
        ),
        "remote_status": remote_status,
        "log": str(log),
    }
    return result


def _run_worker(
    target: Worker,
    archive: Path | None,
    archive_source_identity: str | None,
    archive_transport_identity: str | None,
    guard_digest: str,
    run_id: str,
    patterns: tuple[str, ...],
    report_root: Path,
    git_source: GitSource | None,
    retain_workspace: bool,
    timeout_seconds: int,
    coding_cli: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    host, base = target.host_and_base
    remote_path = _run_path(base, run_id)
    source_identity = _source_identity(target, archive_source_identity, git_source)
    if target.source_mode == "git":
        if git_source is None:
            raise ValueError("Git-backed target lacks an exact source revision")
        command = (
            _windows_git_command(
                base,
                run_id,
                patterns,
                target.platform_target,
                git_source,
                _worker_timeout(timeout_seconds),
                retain_workspace=retain_workspace,
                coding_cli=coding_cli,
                model=model,
            )
            if target.platform_target == "windows"
            else _linux_git_command(
                base,
                run_id,
                patterns,
                target.platform_target,
                git_source,
                _worker_timeout(timeout_seconds),
                retain_workspace=retain_workspace,
                coding_cli=coding_cli,
                model=model,
            )
        )
        completed = subprocess.run(
            _ssh_arguments(
                target, command, _remaining_timeout(deadline, timeout_seconds)
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=_remaining_timeout(deadline, timeout_seconds),
        )
        log = _worker_log(report_root, target.worker_id)
        log.write_text(
            completed.stdout + "\n--- stderr ---\n" + completed.stderr,
            encoding="utf-8",
        )
        return _remote_result(
            target=target,
            source_identity=source_identity,
            source_revision=git_source.revision,
            transport_identity=None,
            completed=completed,
            observation=_probe_workspace(target, host, base, run_id),
            log=log,
            retain_workspace=retain_workspace,
        )
    if archive is None:
        raise ValueError("working-tree target lacks a source archive")
    if archive_transport_identity is None:
        raise ValueError("working-tree target lacks an archive transport identity")
    if _archive_identity(archive) != archive_transport_identity:
        raise ValueError("working-tree archive changed after identity capture")
    if host.endswith("@localhost"):
        actual_platform = {
            "darwin": "macos",
            "linux": "linux",
            "windows": "windows",
        }.get(platform.system().lower())
        if actual_platform != target.platform_target:
            raise ValueError(
                f"local target expected {target.platform_target}, got {actual_platform}"
            )
        run_root = (
            Path.home() / remote_path.removeprefix("~/")
            if remote_path.startswith("~/")
            else Path(remote_path)
        )
        run_root.parent.mkdir(parents=True, exist_ok=True)
        extract_source_archive(archive, run_root, source_identity)
        materialized = source_tree_identity(run_root / "literate-ai")
        if materialized != source_identity:
            raise ValueError("materialized source tree identity mismatch")
        status_path = run_root / "litai-status.json"
        guard = run_root / "literate-ai" / "scripts" / "remote_source_guard.py"
        if hashlib.sha256(guard.read_bytes()).hexdigest() != guard_digest:
            raise ValueError("materialized remote source guard identity mismatch")
        completed = subprocess.run(
            [
                sys.executable,
                str(guard),
                "supervise",
                "--timeout-seconds",
                str(_worker_timeout(timeout_seconds)),
                "--status",
                str(status_path),
                "--",
                sys.executable,
                str(run_root / "literate-ai" / "scripts" / "remote_sample_worker.py"),
                "--platform-flavor",
                target.platform_target,
                *(
                    ("--coding-cli", coding_cli, "--model", model)
                    if coding_cli is not None and model is not None
                    else ()
                ),
                *(
                    argument
                    for pattern in patterns
                    for argument in ("--sample", pattern)
                ),
                *(f"--flavor={selector}" for selector in _ACTIVE_FLAVOR_SELECTORS),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "BUILD_DIR": str(run_root.parent.parent / "cache" / "generated"),
                "OBJ_DIR": str(run_root.parent.parent / "cache" / "_build"),
                "LITAI_REMOTE_PYTHON_ENV": str(
                    run_root.parent.parent / "cache" / "remote-python-env"
                ),
            },
            timeout=_remaining_timeout(deadline, timeout_seconds),
        )
        log = _worker_log(report_root, target.worker_id)
        log.write_text(
            (
                f"LITAI_SOURCE_TREE_IDENTITY={materialized}\n"
                + completed.stdout
                + "\n--- stderr ---\n"
                + completed.stderr
            ),
            encoding="utf-8",
        )
        returncode = completed.returncode
        failure_kind = None if returncode == 0 else "worker"
        status = (
            json.loads(status_path.read_text(encoding="utf-8"))
            if status_path.is_file()
            else None
        )
        if returncode == 0 and not retain_workspace:
            try:
                shutil.rmtree(run_root)
            except OSError as exc:
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"\nlocal workspace cleanup failed: {exc}\n")
                returncode = 1
                failure_kind = "cleanup"
        return {
            "id": target.worker_id,
            "destination": target.destination,
            "platform_flavor": target.platform_flavor,
            "source_mode": target.source_mode,
            "source_identity": source_identity,
            "source_revision": None,
            "transport_identity": archive_transport_identity,
            "materialized_source_identity": materialized,
            "returncode": returncode,
            "passed": returncode == 0,
            "failure_kind": (
                "timeout"
                if isinstance(status, dict) and status.get("state") == "timed-out"
                else failure_kind
            ),
            "retention_requested": retain_workspace,
            "workspace": str(run_root) if run_root.exists() else None,
            "workspace_state": "present" if run_root.exists() else "absent",
            "workspace_retained": run_root.exists(),
            "remote_status": status,
            "log": str(log),
        }
    final, incoming = _workspace_paths(target, base, run_id)
    assert incoming is not None
    mkdir = (
        _powershell_command(
            f"$ErrorActionPreference = 'Stop'; "
            f"$root = {_powershell_root(final)}; "
            f"$incoming = {_powershell_root(incoming)}; "
            "$parent = Split-Path -Parent $root; "
            "New-Item -ItemType Directory -Force -Path $parent | Out-Null; "
            "if ((Test-Path $root) -or (Test-Path $incoming)) { "
            "throw 'remote run destination already exists' }; "
            "New-Item -ItemType Directory -Path $incoming | Out-Null"
        )
        if target.platform_target == "windows"
        else " && ".join(
            (
                f"root={_posix_path(final)}",
                f"incoming={_posix_path(incoming)}",
                "parent=${root%/*}",
                'mkdir -p -- "$parent"',
                'test ! -e "$root"',
                'test ! -e "$incoming"',
                'mkdir -- "$incoming"',
            )
        )
    )
    preflight_timeout = _remaining_timeout(deadline, timeout_seconds)
    subprocess.run(
        _ssh_arguments(target, mkdir, preflight_timeout),
        check=True,
        capture_output=True,
        text=True,
        timeout=_remaining_timeout(deadline, timeout_seconds),
    )
    subprocess.run(
        _scp_arguments(
            archive,
            f"{host}:{incoming}/repo.tar.gz",
            _remaining_timeout(deadline, timeout_seconds),
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=_remaining_timeout(deadline, timeout_seconds),
    )
    guard_path = (
        Path(__file__).resolve().parents[1] / "src/literate_ai/remote_source_guard.py"
    )
    if hashlib.sha256(guard_path.read_bytes()).hexdigest() != guard_digest:
        raise ValueError("remote source guard changed before upload")
    subprocess.run(
        _scp_arguments(
            guard_path,
            f"{host}:{incoming}/remote_source_guard.py",
            _remaining_timeout(deadline, timeout_seconds),
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=_remaining_timeout(deadline, timeout_seconds),
    )
    command = (
        _windows_command(
            remote_path,
            patterns,
            target.platform_target,
            archive_transport_identity,
            source_identity,
            guard_digest,
            _worker_timeout(timeout_seconds),
            cache_path=base.rstrip("/") + "/cache",
            retain_workspace=retain_workspace,
            coding_cli=coding_cli,
            model=model,
        )
        if target.platform_target == "windows"
        else _linux_command(
            remote_path,
            patterns,
            target.platform_target,
            archive_transport_identity,
            source_identity,
            guard_digest,
            _worker_timeout(timeout_seconds),
            cache_path=base.rstrip("/") + "/cache",
            retain_workspace=retain_workspace,
            coding_cli=coding_cli,
            model=model,
        )
    )
    completed = subprocess.run(
        _ssh_arguments(target, command, _remaining_timeout(deadline, timeout_seconds)),
        check=False,
        capture_output=True,
        text=True,
        timeout=_remaining_timeout(deadline, timeout_seconds),
    )
    log = _worker_log(report_root, target.worker_id)
    log.write_text(
        completed.stdout + "\n--- stderr ---\n" + completed.stderr,
        encoding="utf-8",
    )
    return _remote_result(
        target=target,
        source_identity=source_identity,
        source_revision=None,
        transport_identity=archive_transport_identity,
        completed=completed,
        observation=_probe_workspace(target, host, base, run_id),
        log=log,
        retain_workspace=retain_workspace,
    )


def _record_worker(
    target: Worker,
    archive: Path | None,
    archive_source_identity: str | None,
    archive_transport_identity: str | None,
    guard_digest: str,
    run_id: str,
    patterns: tuple[str, ...],
    report_root: Path,
    git_source: GitSource | None,
    retain_workspace: bool,
    timeout_seconds: int,
    coding_cli: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    try:
        return _run_worker(
            target,
            archive,
            archive_source_identity,
            archive_transport_identity,
            guard_digest,
            run_id,
            patterns,
            report_root,
            git_source,
            retain_workspace,
            timeout_seconds,
            coding_cli,
            model,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log = _worker_log(report_root, target.worker_id)
        timed_out = isinstance(exc, subprocess.TimeoutExpired)
        detail = (
            f"target exceeded its {timeout_seconds}-second wall-time limit"
            if timed_out
            else str(exc)
        )
        log.write_text(
            f"fan-out failed before sample completion: {detail}\n", encoding="utf-8"
        )
        try:
            source_identity: str | None = _source_identity(
                target, archive_source_identity, git_source
            )
        except ValueError:
            source_identity = None
        host, base = target.host_and_base
        observation = (
            {
                "workspace_root": None,
                "workspace_state": "unknown",
                "remote_status": None,
            }
            if host.endswith("@localhost")
            else _probe_workspace(target, host, base, run_id)
        )
        state = observation["workspace_state"]
        return {
            "id": target.worker_id,
            "destination": target.destination,
            "platform_flavor": target.platform_flavor,
            "source_mode": target.source_mode,
            "source_identity": source_identity,
            "source_revision": (
                git_source.revision
                if target.source_mode == "git" and git_source is not None
                else None
            ),
            "transport_identity": (
                archive_transport_identity
                if target.source_mode == "working-tree"
                else None
            ),
            "materialized_source_identity": None,
            "returncode": 1,
            "passed": False,
            "failure_kind": "timeout" if timed_out else "preflight",
            "retention_requested": retain_workspace,
            "workspace": observation["workspace_root"],
            "workspace_state": state,
            "workspace_retained": (
                True
                if state in {"present", "staging"}
                else False
                if state == "absent"
                else None
            ),
            "remote_status": observation["remote_status"],
            "log": str(log),
        }


def _execute_workers(
    workers: tuple[Worker, ...],
    *,
    archive: Path | None,
    archive_source_identity: str | None,
    archive_transport_identity: str | None,
    guard_digest: str,
    run_id: str,
    patterns: tuple[str, ...],
    report_root: Path,
    git_source: GitSource | None,
    jobs: int | None,
    retain_workspace: bool,
    timeout_seconds: int,
    coding_cli: str | None = None,
    model: str | None = None,
    resumed_results: dict[str, dict[str, object]] | None = None,
    record_success: Callable[[dict[str, object]], None] | None = None,
) -> tuple[dict[str, object], ...]:
    """Run workers concurrently, stopping new scheduling after the first failure."""

    results = dict(resumed_results or {})
    remaining = [worker for worker in workers if worker.worker_id not in results]
    maximum_workers = min(jobs or len(workers), len(remaining)) if remaining else 0
    failed = False

    def invoke(worker: Worker) -> dict[str, object]:
        return _record_worker(
            worker,
            archive,
            archive_source_identity,
            archive_transport_identity,
            guard_digest,
            run_id,
            patterns,
            report_root,
            git_source,
            retain_workspace,
            timeout_seconds,
            coding_cli,
            model,
        )

    if maximum_workers:
        with ThreadPoolExecutor(max_workers=maximum_workers) as executor:
            active: dict[object, Worker] = {}
            next_index = 0

            def schedule() -> None:
                nonlocal next_index
                while (
                    not failed
                    and len(active) < maximum_workers
                    and next_index < len(remaining)
                ):
                    worker = remaining[next_index]
                    next_index += 1
                    active[executor.submit(invoke, worker)] = worker

            schedule()
            while active:
                done, _ = wait_for_futures(active, return_when=FIRST_COMPLETED)
                ordered = sorted(done, key=lambda future: workers.index(active[future]))
                for future in ordered:
                    worker = active.pop(future)
                    result = future.result()
                    results[worker.worker_id] = result
                    if result.get("passed") is True:
                        if record_success is not None:
                            record_success(result)
                    else:
                        failed = True
                schedule()

            for worker in remaining[next_index:]:
                results[worker.worker_id] = {
                    "id": worker.worker_id,
                    "destination": worker.destination,
                    "platform_flavor": worker.platform_flavor,
                    "source_mode": worker.source_mode,
                    "passed": False,
                    "failure_kind": "not-run-after-failure",
                }
    return tuple(results[worker.worker_id] for worker in workers)


def _fanout_checkpoint_identity(
    patterns: tuple[str, ...],
    workers: tuple[Worker, ...],
    flavor_selectors: tuple[str, ...] = (),
    history_depth: int | None = 1,
    *,
    source_bindings: tuple[tuple[str, str, str | None], ...],
    coding_cli: str,
    model: str,
) -> str:
    value = {
        "patterns": patterns,
        "flavor_selectors": flavor_selectors,
        "repository_history_depth": history_depth,
        "source_bindings": source_bindings,
        "live_selection": {
            "coding_cli": coding_cli,
            "model": model,
        },
        "workers": [
            (
                worker.worker_id,
                worker.destination,
                worker.platform_flavor,
                worker.source_mode,
            )
            for worker in workers
        ],
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_fanout_checkpoint(
    path: Path, identity: str, worker_ids: set[str]
) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("v") != FANOUT_CHECKPOINT_VERSION
        or value.get("p") != identity
        or not isinstance(value.get("ok"), list)
    ):
        return {}
    results: dict[str, dict[str, object]] = {}
    for result in value["ok"]:
        if (
            not isinstance(result, dict)
            or result.get("passed") is not True
            or not isinstance(result.get("id"), str)
            or result["id"] not in worker_ids
            or result["id"] in results
        ):
            return {}
        results[result["id"]] = result
    return results


def _store_fanout_checkpoint(
    path: Path, identity: str, results: dict[str, dict[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "v": FANOUT_CHECKPOINT_VERSION,
        "p": identity,
        "ok": [results[key] for key in sorted(results)],
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fanout_report(
    *,
    run_id: str,
    patterns: tuple[str, ...],
    results: tuple[dict[str, object], ...],
    retain_workspace: bool,
    timeout_seconds: int,
    resumed_worker_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "schema": "literate-ai/remote-sample-fanout-report@5",
        "run_id": run_id,
        "sample_patterns": list(patterns),
        "source_authority": "per-worker",
        "retention": "all" if retain_workspace else "failures",
        "target_timeout_seconds": timeout_seconds,
        "resumed_workers": list(resumed_worker_ids),
        "authoritative": not resumed_worker_ids,
        "passed": all(item["passed"] for item in results),
        "workers": list(results),
    }


def _record_worker_evidence(
    results: tuple[dict[str, object], ...], report_root: Path
) -> None:
    run = attach_run()
    if run is None:
        for result in results:
            log = _worker_log(report_root, str(result.get("id", "unknown")))
            if result.get("workspace_retained"):
                record_retained_output(
                    log,
                    node_path=f"fanout/workers/{result.get('id', 'unknown')}/log",
                    operation="fanout.worker",
                    role="worker-log",
                )
        return
    for result in results:
        worker_id = str(result.get("id", "unknown"))
        destination = str(result.get("destination", ""))
        endpoint_digest = (
            "sha256:" + hashlib.sha256(destination.encode("utf-8")).hexdigest()
        )
        context = run.node(
            f"fanout/workers/{worker_id}",
            operation="fanout.worker",
            pins={"worker_id": worker_id, "endpoint_digest": endpoint_digest},
        )
        with context as node:
            if not isinstance(node, EvidenceNode):
                continue
            node.add_output(
                role="worker-log",
                path=_worker_log(report_root, worker_id),
                media_type="text/plain",
            )
            workspace = result.get("workspace")
            if result.get("workspace_retained") and isinstance(workspace, str):
                node.add_output(
                    role="worker-workspace",
                    path=workspace,
                    media_type="inode/directory",
                    retention="host-only",
                )
            if not result.get("passed"):
                node.fail(
                    f"worker {worker_id} failed: "
                    f"{result.get('failure_kind') or 'unknown failure'}"
                )


def _record_fanout_unavailable(reason: str) -> None:
    run = attach_run()
    if run is None:
        return
    context = run.node(
        "fanout/workers",
        operation="fanout.workers",
        parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
    )
    with context as node:
        if isinstance(node, EvidenceNode):
            node.mark_unavailable(reason)


def main() -> int:
    global _ACTIVE_FLAVOR_SELECTORS
    args = _parser().parse_args()
    _ACTIVE_FLAVOR_SELECTORS = tuple(args.flavor)
    repository = Path(__file__).resolve().parents[1]
    try:
        test_config = resolve_test_config_path(
            project_root=repository,
            explicit=args.config,
        )
        worker_config = resolve_worker_config_path(
            project_root=repository,
            explicit=args.worker_config,
        )
    except UserAssetPathError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from exc
    default_patterns, configured_workers = _configuration(test_config, worker_config)
    patterns = tuple(args.sample) or default_patterns
    if args.jobs is not None and args.jobs < 1:
        raise SystemExit("--jobs must be a positive integer")
    if args.timeout_seconds < 1:
        raise SystemExit("--timeout-seconds must be a positive integer")
    selected_ids = set(args.worker)
    selected_platforms = _selected_platforms(_ACTIVE_FLAVOR_SELECTORS)
    workers = tuple(
        item
        for item in configured_workers
        if (not selected_ids or item.worker_id in selected_ids)
        and item.platform_target in selected_platforms
    )
    if not configured_workers and not selected_ids:
        reason = "no worker fleet is configured"
        _record_fanout_unavailable(reason)
        print(
            json.dumps(
                {
                    "schema": "literate-ai/remote-sample-fanout-report@5",
                    "sample_patterns": list(patterns),
                    "source_authority": "per-worker",
                    "retention": "failures",
                    "target_timeout_seconds": args.timeout_seconds,
                    "resumed_workers": [],
                    "authoritative": False,
                    "passed": True,
                    "workers": [],
                    "unavailable_reason": reason,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        selection = resolve_live_test_selection(
            coding_cli=args.coding_cli,
            model=args.model,
            environment=os.environ,
            project_root=repository,
            require_opencode=args.coding_cli is None,
            require_openai_api_key=False,
            ignore_environment_pins=True,
        )
    except CodingCliError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from exc
    apply_live_test_selection(os.environ, selection)
    log_live_session_models(selection)
    if not workers or selected_ids - {item.worker_id for item in configured_workers}:
        raise SystemExit("selected test worker is not present in the global matrix")
    remote_workers = tuple(
        worker
        for worker in workers
        if not worker.host_and_base[0].endswith("@localhost")
    )
    required_commands = set()
    if remote_workers:
        required_commands.add("ssh")
    if any(worker.source_mode == "working-tree" for worker in remote_workers):
        required_commands.add("scp")
    for command in sorted(required_commands):
        if shutil.which(command) is None:
            raise SystemExit(f"{command} is required for remote sample fan-out")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    git_source = (
        _git_source(
            repository,
            timeout_seconds=args.timeout_seconds,
            history_depth=(
                None if args.full_repository_history else args.repository_history_depth
            ),
        )
        if any(worker.source_mode == "git" for worker in workers)
        else None
    )
    object_root = Path(os.environ.get("OBJ_DIR", repository / "_build")).resolve()
    checkpoint_path = object_root / "test-matrix" / "checkpoint.json"
    report_root = object_root / "test-matrix" / run_id
    report_root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="litai-test-matrix-") as temporary:
        archive = (
            Path(temporary) / "repo.tar.gz"
            if any(worker.source_mode == "working-tree" for worker in workers)
            else None
        )
        archive_source_identity = (
            _archive(repository, archive) if archive is not None else None
        )
        archive_transport_identity = (
            _archive_identity(archive) if archive is not None else None
        )
        source_bindings = tuple(
            (
                worker.worker_id,
                _source_identity(worker, archive_source_identity, git_source),
                (
                    git_source.revision
                    if worker.source_mode == "git" and git_source is not None
                    else None
                ),
            )
            for worker in workers
        )
        checkpoint_identity = _fanout_checkpoint_identity(
            patterns,
            workers,
            _ACTIVE_FLAVOR_SELECTORS,
            None if args.full_repository_history else args.repository_history_depth,
            source_bindings=source_bindings,
            coding_cli=selection.coding_cli,
            model=selection.model,
        )
        resumed_results = _load_fanout_checkpoint(
            checkpoint_path,
            checkpoint_identity,
            {worker.worker_id for worker in workers},
        )
        checkpoint_results = dict(resumed_results)

        def record_success(result: dict[str, object]) -> None:
            checkpoint_results[str(result["id"])] = result
            _store_fanout_checkpoint(
                checkpoint_path, checkpoint_identity, checkpoint_results
            )

        guard_digest = hashlib.sha256(
            (repository / "src/literate_ai/remote_source_guard.py").read_bytes()
        ).hexdigest()
        results = _execute_workers(
            workers,
            archive=archive,
            archive_source_identity=archive_source_identity,
            archive_transport_identity=archive_transport_identity,
            guard_digest=guard_digest,
            run_id=run_id,
            patterns=patterns,
            report_root=report_root,
            git_source=git_source,
            jobs=args.jobs,
            retain_workspace=args.retain_workspaces,
            timeout_seconds=args.timeout_seconds,
            coding_cli=selection.coding_cli,
            model=selection.model,
            resumed_results=resumed_results,
            record_success=record_success,
        )
    _record_worker_evidence(results, report_root)
    report = _fanout_report(
        run_id=run_id,
        patterns=patterns,
        results=results,
        retain_workspace=args.retain_workspaces,
        timeout_seconds=args.timeout_seconds,
        resumed_worker_ids=tuple(sorted(resumed_results)),
    )
    report_path = report_root / "report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({**report, "report": str(report_path)}, sort_keys=True))
    if not report["passed"]:
        return 1
    checkpoint_path.unlink(missing_ok=True)
    if resumed_results:
        print(
            "fan-out repair cycle completed from provisional checkpoints; "
            "checkpoint reset, rerun from the beginning for release evidence",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
