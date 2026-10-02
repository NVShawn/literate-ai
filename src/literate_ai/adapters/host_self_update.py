"""Prefix-installed GitHub Release self-update for the host `litai` launcher."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    run_with_tree_kill,
)
from literate_ai.adapters.user_paths import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HostInstallLayout,
)
from literate_ai.repository_urls import github_repository_coordinate
from literate_ai.version import DISTRIBUTION_VERSION

HOST_INSTALL_ENVIRONMENT = "LITAI_HOST_INSTALL"
PREFIX_ENVIRONMENT = "LITAI_PREFIX"
OPT_OUT_ENVIRONMENT = "LITAI_NO_SELF_UPDATE"
REEXEC_ENVIRONMENT = "LITAI_SELF_UPDATE_REEXEC"
WHEEL_NAME = re.compile(r"^literate_ai-.+-py3-none-any\.whl$")
SUCCESS_CACHE_SECONDS = 24 * 60 * 60
FAILURE_RETRY_SECONDS = 15 * 60
GITHUB_TIMEOUT_SECONDS = 10
PIP_TIMEOUT_SECONDS = 120
_MAXIMUM_MANIFEST_BYTES = 64 * 1024
_TRUTHY = frozenset({"1", "true", "yes", "on"})

POSIX_LAUNCHER = """\
#!/bin/sh
set -eu

launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix=$(CDPATH= cd -- "$launcher_dir/.." && pwd)
LITAI_PREFIX=$prefix
LITAI_HOST_INSTALL=1
export LITAI_PREFIX LITAI_HOST_INSTALL
if [ -n "${LITAI_EVIDENCE_RUN-}" ]; then
    exec "$prefix/share/literate-ai/venv/bin/python" -m literate_ai.step_harness \\
        --name launcher -- "$prefix/share/literate-ai/venv/bin/litai" "$@"
fi
exec "$prefix/share/literate-ai/venv/bin/litai" "$@"
"""

WINDOWS_LAUNCHER = (
    "@echo off\n"
    "setlocal\n"
    'set "LITAI_PREFIX=%~dp0.."\n'
    'set "LITAI_HOST_INSTALL=1"\n'
    "if defined LITAI_EVIDENCE_RUN (\n"
    '  "%LITAI_PREFIX%\\share\\literate-ai\\venv\\Scripts\\python.exe"'
    " -m literate_ai.step_harness --name launcher -- "
    '"%LITAI_PREFIX%\\share\\literate-ai\\venv\\Scripts\\litai.exe" %*\n'
    "  exit /b %ERRORLEVEL%\n"
    ")\n"
    '"%LITAI_PREFIX%\\share\\literate-ai\\venv\\Scripts\\litai.exe" %*\n'
    "exit /b %ERRORLEVEL%\n"
)


@dataclass(frozen=True, slots=True)
class HostSelfUpdateEnrollment:
    prefix: Path
    environment: Path
    launcher: Path
    manifest: Path
    staging_root: Path


@dataclass(frozen=True, slots=True)
class HostSelfUpdatePlan:
    tag: str
    version: str
    wheel_name: str
    wheel_url: str


FetchJson = Callable[[str, Mapping[str, str], int], bytes]
FetchFile = Callable[[str, Mapping[str, str], int, Path], None]
CommandRunner = Callable[..., object]
ExecFn = Callable[[Sequence[str], Mapping[str, str]], None]


def _flag(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().casefold() in _TRUTHY


def _contained(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _symlink_in_custody(*paths: Path) -> bool:
    return any(path.is_symlink() for path in paths)


def host_install_manifest_document(
    *, prefix: Path, environment: Path, launcher: Path
) -> dict[str, object]:
    return {
        "schema": HOST_INSTALL_MANIFEST_SCHEMA,
        "prefix": str(prefix),
        "environment": str(environment),
        "launcher": str(launcher),
        "self_update": True,
    }


def write_host_install_manifest(
    manifest: Path, *, prefix: Path, environment: Path, launcher: Path
) -> None:
    payload = (
        json.dumps(
            host_install_manifest_document(
                prefix=prefix, environment=environment, launcher=launcher
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_name(manifest.name + ".tmp")
    if manifest.is_symlink() or temporary.is_symlink():
        raise OSError("installation manifest path cannot be a symbolic link")
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    os.replace(temporary, manifest)


def resolve_host_self_update_enrollment(
    *, environ: Mapping[str, str], executable: Path
) -> HostSelfUpdateEnrollment | None:
    if not _flag(environ, HOST_INSTALL_ENVIRONMENT):
        return None
    if (
        _flag(environ, OPT_OUT_ENVIRONMENT)
        or _flag(environ, "CI")
        or _flag(environ, "GITHUB_ACTIONS")
        or environ.get("LITAI_EVIDENCE_RUN", "").strip()
        or _flag(environ, REEXEC_ENVIRONMENT)
    ):
        return None
    raw_prefix = environ.get(PREFIX_ENVIRONMENT, "").strip()
    if not raw_prefix:
        return None
    prefix = Path(raw_prefix)
    if not prefix.is_absolute():
        return None
    try:
        prefix = prefix.resolve()
        executable = executable.resolve()
    except OSError:
        return None
    layout = HostInstallLayout.for_prefix(prefix)
    environment = Path(layout.environment)
    launcher = Path(layout.launcher)
    manifest = Path(layout.manifest)
    if _symlink_in_custody(Path(layout.application_root), environment, manifest):
        return None
    if not manifest.is_file() or manifest.stat().st_size > _MAXIMUM_MANIFEST_BYTES:
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema") != HOST_INSTALL_MANIFEST_SCHEMA:
        return None
    if document.get("self_update") is not True:
        return None
    if document.get("prefix") != str(prefix):
        return None
    if document.get("environment") != str(environment):
        return None
    if document.get("launcher") != str(launcher):
        return None
    if not _contained(executable, environment):
        return None
    return HostSelfUpdateEnrollment(
        prefix,
        environment,
        launcher,
        manifest,
        Path(layout.application_root) / "self-update",
    )


def select_github_release_wheel(
    payload: Mapping[str, object], *, installed_version: str
) -> HostSelfUpdatePlan | None:
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    raw_tag = tag.strip()
    version_text = raw_tag[1:] if raw_tag[:1] in {"v", "V"} else raw_tag
    parsed = _parsed_versions(version_text, installed_version)
    if parsed is None:
        return None
    latest, installed = parsed
    if latest <= installed or latest.is_prerelease or latest.is_devrelease:
        return None
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if (
            isinstance(name, str)
            and isinstance(url, str)
            and WHEEL_NAME.fullmatch(name)
            and name == f"literate_ai-{latest}-py3-none-any.whl"
        ):
            return HostSelfUpdatePlan(raw_tag, version_text, name, url)
    return None


def cache_allows_github_check(
    cache: Mapping[str, object],
    *,
    now: float,
    staged_newer: bool = False,
) -> bool:
    if staged_newer:
        checked_at = cache.get("checked_at")
        if (
            isinstance(checked_at, (int, float))
            and now - float(checked_at) < SUCCESS_CACHE_SECONDS
        ):
            return False
        if not isinstance(checked_at, (int, float)):
            return False
    checked_at = cache.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return True
    age = now - float(checked_at)
    if cache.get("ok") is True:
        return age >= SUCCESS_CACHE_SECONDS
    return age >= FAILURE_RETRY_SECONDS


def _pip_environment(environ: Mapping[str, str]) -> dict[str, str]:
    environment = {
        name: value
        for name, value in environ.items()
        if not name.casefold().startswith("python")
    }
    # Pip must refresh the same bytecode cache used by the re-executed launcher.
    # Otherwise an equal-size, equal-timestamp upgrade can execute stale code.
    # Keep import-path/interpreter overrides isolated; only cache custody is shared.
    if "PYTHONPYCACHEPREFIX" in environ:
        environment["PYTHONPYCACHEPREFIX"] = environ["PYTHONPYCACHEPREFIX"]
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _venv_python(environment: Path) -> Path:
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _read_cache(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_cache(path: Path, document: Mapping[str, object]) -> None:
    _atomic_write(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n",
    )


def _github_headers(environ: Mapping[str, str]) -> dict[str, str]:
    headers = {"User-Agent": "literate-ai", "Accept": "application/vnd.github+json"}
    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or ""
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def github_releases_latest_url() -> str | None:
    """Resolve the release API from immutable installed-distribution provenance."""

    # Prefix installation imports this module before pip has installed runtime
    # dependencies such as ``packaging``. Keep the standard binding import behind the
    # background update path so manifest bootstrap remains dependency-free.
    from literate_ai.adapters.standard_lifecycle_binding import (
        StandardLifecycleBindingError,
        observe_installed_framework_origin,
    )

    try:
        origin = observe_installed_framework_origin(DISTRIBUTION_VERSION)
    except StandardLifecycleBindingError:
        return None
    coordinate = github_repository_coordinate(origin.repository_url)
    if coordinate is None:
        return None
    owner, repository = coordinate
    return f"https://api.github.com/repos/{owner}/{repository}/releases/latest"


def _default_fetch_json(url: str, headers: Mapping[str, str], timeout: int) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _default_fetch_file(
    url: str, headers: Mapping[str, str], timeout: int, destination: Path
) -> None:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(response.read())
        os.replace(temporary, destination)


def _staged_plan(enrollment: HostSelfUpdateEnrollment) -> HostSelfUpdatePlan | None:
    cache = _read_cache(enrollment.staging_root / "cache.json")
    name = cache.get("wheel_name")
    version = cache.get("version")
    url = cache.get("wheel_url")
    tag = cache.get("tag")
    if not all(isinstance(item, str) and item for item in (name, version, url, tag)):
        return None
    wheel = enrollment.staging_root / str(name)
    if not wheel.is_file():
        return None
    return HostSelfUpdatePlan(str(tag), str(version), str(name), str(url))


def _parsed_versions(candidate: str, installed: str):
    """Parse PEP 440 versions without importing packaging at module load."""

    try:
        from packaging.version import InvalidVersion, Version
    except ModuleNotFoundError:
        return None
    try:
        return Version(candidate), Version(installed)
    except InvalidVersion:
        return None


def _is_newer(candidate: str, installed: str) -> bool:
    parsed = _parsed_versions(candidate, installed)
    return parsed is not None and parsed[0] > parsed[1]


def try_exclusive_lock(path: Path) -> int | None:
    """Return a lock fd when acquired; None when the lock is busy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def release_exclusive_lock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def stage_github_wheel(
    enrollment: HostSelfUpdateEnrollment,
    *,
    fetch_json: FetchJson = _default_fetch_json,
    fetch_file: FetchFile = _default_fetch_file,
    now: float,
    installed_version: str,
    environ: Mapping[str, str],
    releases_url: str | None = None,
    force_check: bool = False,
) -> None:
    cache_path = enrollment.staging_root / "cache.json"
    cache = _read_cache(cache_path)
    staged = _staged_plan(enrollment)
    staged_newer = staged is not None and _is_newer(staged.version, installed_version)
    if not force_check and not cache_allows_github_check(
        cache, now=now, staged_newer=staged_newer
    ):
        return
    headers = _github_headers(environ)
    try:
        selected_url = releases_url or github_releases_latest_url()
        if selected_url is None:
            raise ValueError("installed distribution has no public GitHub origin")
        raw = fetch_json(selected_url, headers, GITHUB_TIMEOUT_SECONDS)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("release payload is not an object")
        plan = select_github_release_wheel(payload, installed_version=installed_version)
        if plan is None:
            _write_cache(
                cache_path,
                {
                    "checked_at": now,
                    "ok": True,
                    "tag": None,
                    "version": installed_version,
                },
            )
            return
        destination = enrollment.staging_root / plan.wheel_name
        fetch_file(plan.wheel_url, headers, GITHUB_TIMEOUT_SECONDS, destination)
        _write_cache(
            cache_path,
            {
                "checked_at": now,
                "ok": True,
                "tag": plan.tag,
                "version": plan.version,
                "wheel_name": plan.wheel_name,
                "wheel_url": plan.wheel_url,
            },
        )
    except (
        OSError,
        urllib.error.URLError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TimeoutError,
    ):
        _write_cache(cache_path, {"checked_at": now, "ok": False})


def spawn_self_update_worker(
    enrollment: HostSelfUpdateEnrollment,
    *,
    argv_executable: Path,
    popen: Callable[..., object] | None = None,
) -> None:
    import subprocess

    lock = try_exclusive_lock(enrollment.staging_root / "worker.lock")
    if lock is None:
        return
    ownership = create_process_tree_ownership()
    try:
        enrollment.staging_root.mkdir(parents=True, exist_ok=True)
        command = (
            str(argv_executable),
            "-m",
            "literate_ai.adapters.host_self_update",
            "--stage",
            str(enrollment.prefix),
        )
        environment = dict(os.environ)
        environment[HOST_INSTALL_ENVIRONMENT] = "1"
        environment[PREFIX_ENVIRONMENT] = str(enrollment.prefix)
        extra: dict[str, object] = {"env": environment, **ownership.popen_options}
        with open(enrollment.staging_root / "worker.log", "ab") as log_file:
            extra["stdout"] = log_file
            extra["stderr"] = log_file
            extra["stdin"] = subprocess.DEVNULL
            if popen is None:
                subprocess.Popen(command, **extra)
            else:
                popen(command, **extra)
    except OSError as exc:
        print(
            f"litai: self-update staging worker failed to start: {exc}", file=sys.stderr
        )
    finally:
        ownership.release()
        release_exclusive_lock(lock)


def apply_staged_wheel(
    enrollment: HostSelfUpdateEnrollment,
    *,
    argv: Sequence[str],
    installed_version: str,
    runner: CommandRunner,
    exec_fn: ExecFn,
    stderr: TextIO,
    environ: Mapping[str, str],
) -> None:
    plan = _staged_plan(enrollment)
    if plan is None or not _is_newer(plan.version, installed_version):
        return
    lock = try_exclusive_lock(enrollment.staging_root / "apply.lock")
    if lock is None:
        return
    try:
        python = _venv_python(enrollment.environment)
        wheel = enrollment.staging_root / plan.wheel_name
        completed = runner(
            str(python),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--upgrade",
            "--force-reinstall",
            "--no-index",
            "--no-deps",
            str(wheel),
            timeout=PIP_TIMEOUT_SECONDS,
            env=_pip_environment(environ),
        )
        status = getattr(completed, "returncode", completed)
        if status not in {0, None}:
            detail = _command_output(completed)
            suffix = f": {detail}" if detail else ""
            print(
                "litai: self-update to "
                f"{plan.version} failed (pip status {status}){suffix}",
                file=stderr,
            )
            return
        _refresh_launcher(enrollment)
        write_host_install_manifest(
            enrollment.manifest,
            prefix=enrollment.prefix,
            environment=enrollment.environment,
            launcher=enrollment.launcher,
        )
        print(
            "litai: updated "
            f"{enrollment.prefix} from {installed_version} to {plan.version}",
            file=stderr,
        )
        child_env = dict(environ)
        child_env[REEXEC_ENVIRONMENT] = "1"
        child_env[HOST_INSTALL_ENVIRONMENT] = "1"
        child_env[PREFIX_ENVIRONMENT] = str(enrollment.prefix)
        exec_fn((str(enrollment.launcher), *argv), child_env)
    except (OSError, TimeoutError) as exc:
        print(f"litai: self-update failed: {exc}", file=stderr)
    finally:
        release_exclusive_lock(lock)


def _refresh_launcher(enrollment: HostSelfUpdateEnrollment) -> None:
    temporary = enrollment.launcher.with_name(enrollment.launcher.name + ".tmp")
    if enrollment.launcher.is_symlink() or temporary.is_symlink():
        raise OSError("launcher path cannot be a symbolic link")
    if os.name == "nt":
        temporary.write_text(WINDOWS_LAUNCHER, encoding="utf-8", newline="\r\n")
    else:
        temporary.write_text(POSIX_LAUNCHER, encoding="utf-8")
        temporary.chmod(0o755)
    os.replace(temporary, enrollment.launcher)


def _command_output(completed: object) -> str:
    detail = (
        getattr(completed, "stderr", None) or getattr(completed, "stdout", None) or ""
    )
    if isinstance(detail, bytes):
        return detail.decode("utf-8", "replace").strip()
    return str(detail).strip()


def _default_runner(*arguments: str, timeout: int, env: Mapping[str, str]) -> object:
    return run_with_tree_kill(
        arguments,
        timeout=float(timeout),
        env=env,
        check=False,
    )


def _default_exec(command: Sequence[str], environ: Mapping[str, str]) -> None:
    if os.name == "nt":
        import subprocess

        completed = subprocess.call(list(command), env=dict(environ))
        raise SystemExit(completed)
    os.execvpe(command[0], list(command), dict(environ))


def maybe_host_self_update(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    executable: Path | None = None,
    stderr: TextIO | None = None,
    now: float | None = None,
    runner: CommandRunner | None = None,
    exec_fn: ExecFn | None = None,
    spawn: Callable[..., object] | None = None,
    check_now: bool = False,
) -> None:
    configured = os.environ if environ is None else environ
    errors = sys.stderr if stderr is None else stderr
    python = Path(sys.executable) if executable is None else executable
    try:
        enrollment = resolve_host_self_update_enrollment(
            environ=configured, executable=python
        )
        if enrollment is None:
            if check_now and not _flag(configured, REEXEC_ENVIRONMENT):
                print(
                    "litai: CLI self-update is not enabled for this invocation; "
                    "continuing with project update",
                    file=errors,
                )
            return
        if check_now:
            import time

            lock = try_exclusive_lock(enrollment.staging_root / "worker.lock")
            if lock is None:
                print("litai: self-update check already in progress", file=errors)
                return
            try:
                stage_github_wheel(
                    enrollment,
                    now=time.time() if now is None else now,
                    installed_version=DISTRIBUTION_VERSION,
                    environ=configured,
                    force_check=True,
                )
            finally:
                release_exclusive_lock(lock)
            cache = _read_cache(enrollment.staging_root / "cache.json")
            if cache.get("ok") is not True:
                print(
                    "litai: release check unavailable; continuing with installed CLI",
                    file=errors,
                )
        apply_staged_wheel(
            enrollment,
            argv=argv,
            installed_version=DISTRIBUTION_VERSION,
            runner=runner or _default_runner,
            exec_fn=exec_fn or _default_exec,
            stderr=errors,
            environ=configured,
        )
        if not check_now:
            spawn_self_update_worker(
                enrollment,
                argv_executable=python,
                popen=spawn,
            )
    except Exception as exc:  # noqa: BLE001 — fail open
        print(f"litai: self-update skipped: {exc}", file=errors)


def _stage_cli(prefix: str) -> int:
    import time

    layout = HostInstallLayout.for_prefix(Path(prefix))
    enrollment = HostSelfUpdateEnrollment(
        Path(prefix).resolve(),
        Path(layout.environment),
        Path(layout.launcher),
        Path(layout.manifest),
        Path(layout.application_root) / "self-update",
    )
    lock = try_exclusive_lock(enrollment.staging_root / "worker.lock")
    if lock is None:
        return 0
    try:
        stage_github_wheel(
            enrollment,
            now=time.time(),
            installed_version=DISTRIBUTION_VERSION,
            environ=os.environ,
        )
    finally:
        release_exclusive_lock(lock)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 2 and arguments[0] == "--stage":
        return _stage_cli(arguments[1])
    print(
        "usage: python -m literate_ai.adapters.host_self_update --stage PREFIX",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
