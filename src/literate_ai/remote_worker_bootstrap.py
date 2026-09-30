"""Fail-closed bootstrap for one exact Standard worker distribution.

This module intentionally uses only the Python standard library so a coordinator may
stage and invoke this file before trusting an ambient Literate AI installation.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform as host_platform
import re
import shutil
import signal
import subprocess
import tempfile
import venv
import zipfile
from contextlib import suppress
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

_MAX_WHEEL_BYTES = 256 * 1024 * 1024
_MAX_MEMBERS = 100_000
_IDENTITY_SCHEMA = "literate-ai/installed-framework-distribution@1"
_WHEELHOUSE_SCHEMA = "literate-ai/worker-wheelhouse-manifest@1"
_BOOTSTRAP_EVIDENCE_SCHEMA = "literate-ai/worker-bootstrap-evidence@2"
_BOOTSTRAP_EVIDENCE_FILE = ".literate-ai-worker-bootstrap.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIST_INFO = re.compile(r"^literate_ai-[A-Za-z0-9_.!+-]+\.dist-info$")
_WHEEL_FILENAME = re.compile(r"^[A-Za-z0-9_.!+-]+\.whl$")
_EXCLUDED_METADATA = frozenset(
    {
        "direct_url.json",
        "installer",
        "record",
        "requested",
        "uv_build.json",
        "uv_cache.json",
    }
)
# Keep aligned with `[project.scripts]` and
# `literate_ai.adapters.standard_lifecycle_binding._CONSOLE_LAUNCHER_NAMES`.
_CONSOLE_LAUNCHER_NAMES = frozenset(
    {
        "litai",
        "litai.exe",
        "litai-script.py",
        "litai-mcp",
        "litai-mcp.exe",
        "litai-mcp-script.py",
    }
)


class WorkerBootstrapError(RuntimeError):
    """Stable rejection from exact worker-distribution bootstrap."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# -- Self-contained process-tree kill --------------------------------------
#
# This module intentionally uses only the Python standard library (see the
# module docstring) so a coordinator can stage and invoke this one file
# before trusting an ambient literate-ai installation. It therefore cannot
# import the shared `terminate_process_tree`/`ProcessTreeOwnership` helpers
# from `literate_ai.adapters._processes` -- this reimplements the same
# posix-killpg / Windows-taskkill strategy those helpers use so that a
# `pip`/`node` subprocess timing out here cannot leave a helper tree alive
# holding locks or output pipes open. The Win32 Job Object primitive from
# `_processes.py` is not duplicated here (it needs meaningfully more ctypes
# machinery than is worth carrying in a bootstrap-only file); `taskkill
# /T /F` is a reasonable point-in-time fallback for this narrow use.


def _process_group_popen_options() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def _windows_taskkill_executable() -> Path | None:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return None
    try:
        root = Path(system_root).resolve(strict=True)
        candidate = (root / "System32" / "taskkill.exe").resolve(strict=True)
    except OSError:
        return None
    if candidate.parent != (root / "System32").resolve(strict=True):
        return None
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return candidate


def _terminate_process_tree(process: subprocess.Popen[object]) -> None:
    """Best-effort whole-process-tree kill, without any literate_ai import."""

    if os.name == "posix":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        return
    if os.name == "nt":
        executable = _windows_taskkill_executable()
        if executable is not None:
            with suppress(OSError):
                terminator = subprocess.Popen(
                    [str(executable), "/PID", str(process.pid), "/T", "/F"],
                    cwd=executable.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    terminator.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminator.kill()
                    with suppress(subprocess.TimeoutExpired):
                        terminator.wait(timeout=5)
    if process.poll() is None:
        with suppress(OSError):
            process.kill()


def _await_termination_or_give_up(process: subprocess.Popen[object]) -> None:
    try:
        process.communicate(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(OSError):
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=5)


def _run_with_tree_kill(
    args: object,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    text: bool = False,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    check: bool = False,
) -> subprocess.CompletedProcess[object]:
    """`subprocess.run(timeout=...)`-compatible call with whole-tree kill.

    On `TimeoutExpired`, kills the whole process tree/group (not just the
    direct child) before re-raising, so callers keep their existing
    exception handling unchanged.
    """

    popen_options = _process_group_popen_options()
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        **popen_options,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        _await_termination_or_give_up(process)
        raise
    result = subprocess.CompletedProcess(args, process.returncode, out, err)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, args, output=out, stderr=err
        )
    return result


def _canonical_identity(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + _digest(content)


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _wheel_record_digest(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(content).digest()
    ).rstrip(b"=").decode("ascii")


def _read_stable(path: Path) -> bytes:
    if path.is_symlink():
        raise WorkerBootstrapError(
            "worker.bootstrap_wheel_unsafe", "bootstrap wheel must not be a symlink"
        )
    try:
        before = path.stat()
        if not path.is_file() or before.st_size > _MAX_WHEEL_BYTES:
            raise OSError
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise WorkerBootstrapError(
            "worker.bootstrap_wheel_unavailable",
            "bootstrap wheel is unavailable or exceeds the size limit",
        ) from exc
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if len(content) != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in fields
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_wheel_changed",
            "bootstrap wheel changed while it was verified",
        )
    return content


def _inspect_wheel(
    path: Path, expected_sha256: str, *, required_name: str | None
) -> dict[str, object]:
    if not _SHA256.fullmatch(expected_sha256):
        raise WorkerBootstrapError(
            "worker.bootstrap_digest_invalid",
            "expected wheel digest must be 64 lower-case hexadecimal characters",
        )
    content = _read_stable(Path(path))
    if _digest(content) != expected_sha256:
        raise WorkerBootstrapError(
            "worker.bootstrap_digest_mismatch",
            "bootstrap wheel bytes do not match the required digest",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise WorkerBootstrapError(
            "worker.bootstrap_wheel_invalid", "bootstrap input is not a valid wheel"
        ) from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if not infos or len(infos) > _MAX_MEMBERS or len(names) != len(set(names)):
            raise WorkerBootstrapError(
                "worker.bootstrap_wheel_invalid",
                "wheel members must be non-empty, bounded, and unique",
            )
        total = 0
        file_infos = []
        for info in infos:
            candidate = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            directory = info.is_dir()
            canonical_name = candidate.as_posix() + ("/" if directory else "")
            if (
                candidate.is_absolute()
                or not candidate.parts
                or canonical_name != info.filename
                or any(part in {"", ".", ".."} for part in candidate.parts)
                or any(":" in part for part in candidate.parts)
                or "\\" in info.filename
                or (mode & 0o170000) not in {0, 0o040000 if directory else 0o100000}
                or (directory and info.file_size != 0)
                or candidate.suffix.casefold() == ".pth"
            ):
                raise WorkerBootstrapError(
                    "worker.bootstrap_wheel_unsafe",
                    "wheel contains an unsafe or executable-at-startup member",
                )
            if directory:
                continue
            file_infos.append(info)
            total += info.file_size
            if total > _MAX_WHEEL_BYTES:
                raise WorkerBootstrapError(
                    "worker.bootstrap_wheel_invalid",
                    "wheel expanded payload exceeds the size limit",
                )
        # Component sorting keeps a parent adjacent to its first descendant and
        # case-equivalent subtrees together. Compare adjacent names without
        # allocating every full prefix of a potentially deep untrusted path.
        member_paths = sorted(
            ((PurePosixPath(info.filename).parts, info.is_dir()) for info in infos),
            key=lambda item: tuple(part.casefold() for part in item[0]),
        )
        for (previous, previous_directory), (current, _) in zip(
            member_paths, member_paths[1:], strict=False
        ):
            for left, right in zip(previous, current, strict=False):
                if left.casefold() != right.casefold():
                    break
                if left != right:
                    raise WorkerBootstrapError(
                        "worker.bootstrap_wheel_unsafe", "wheel member paths collide"
                    )
            else:
                if len(previous) == len(current) or not previous_directory:
                    raise WorkerBootstrapError(
                        "worker.bootstrap_wheel_unsafe", "wheel member paths collide"
                    )
        dist_infos = {
            PurePosixPath(name).parts[0]
            for name in names
            if PurePosixPath(name).parts
            and PurePosixPath(name).parts[0].endswith(".dist-info")
        }
        if len(dist_infos) != 1:
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid",
                "wheel must contain exactly one distribution metadata directory",
            )
        dist_info = next(iter(dist_infos))
        if required_name == "literate-ai" and not _DIST_INFO.fullmatch(dist_info):
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid",
                "wheel distribution metadata is not for literate-ai",
            )
        metadata_path = f"{dist_info}/METADATA"
        wheel_path = f"{dist_info}/WHEEL"
        record_path = f"{dist_info}/RECORD"
        if any(item not in names for item in (metadata_path, wheel_path, record_path)):
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid", "wheel metadata is incomplete"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        name = metadata.get("Name")
        version = metadata.get("Version")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            or not isinstance(version, str)
            or not version
            or any(character.isspace() for character in version)
        ):
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid",
                "wheel name or version is invalid",
            )
        canonical_name = _canonical_name(name)
        dist_info_name = dist_info.removesuffix(".dist-info").rsplit("-", 1)[0]
        if _canonical_name(dist_info_name) != canonical_name:
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid",
                "wheel metadata directory does not match its distribution name",
            )
        if required_name is not None and canonical_name != required_name:
            raise WorkerBootstrapError(
                "worker.bootstrap_metadata_invalid",
                "wheel name is not the required distribution",
            )
        rows = list(csv.reader(io.StringIO(archive.read(record_path).decode("utf-8"))))
        recorded = {row[0]: row[1:] for row in rows if len(row) == 3}
        if len(recorded) != len(rows) or set(recorded) != {
            info.filename for info in file_infos
        }:
            raise WorkerBootstrapError(
                "worker.bootstrap_record_invalid",
                "wheel RECORD does not cover every regular file exactly once",
            )
        for info in file_infos:
            digest, size = recorded[info.filename]
            if info.filename == record_path:
                if digest or size:
                    raise WorkerBootstrapError(
                        "worker.bootstrap_record_invalid",
                        "wheel RECORD must self-record with empty fields",
                    )
                continue
            payload = archive.read(info)
            if digest != _wheel_record_digest(payload) or size != str(len(payload)):
                raise WorkerBootstrapError(
                    "worker.bootstrap_record_invalid",
                    "wheel RECORD content does not match its member",
                )
    return {
        "filename": Path(path).name,
        "sha256": expected_sha256,
        "size": len(content),
        "distribution_name": canonical_name,
        "distribution_version": version,
        "requires_dist": sorted(metadata.get_all("Requires-Dist") or ()),
    }


def verify_bootstrap_wheel(path: Path, expected_sha256: str) -> tuple[str, str]:
    """Verify exact wheel bytes and inert metadata before any install subprocess."""

    inspected = _inspect_wheel(Path(path), expected_sha256, required_name="literate-ai")
    return (
        str(inspected["distribution_name"]),
        str(inspected["distribution_version"]),
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_identity(manifest: dict[str, object]) -> str:
    return "sha256:" + _digest(_canonical_bytes(manifest))


def _required_unmarked_names(requirements: object) -> set[str]:
    if not isinstance(requirements, list) or any(
        not isinstance(item, str) for item in requirements
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "wheel requirements must be a list of strings",
        )
    result: set[str] = set()
    for requirement in requirements:
        assert isinstance(requirement, str)
        if ";" in requirement:
            continue
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match is None:
            raise WorkerBootstrapError(
                "worker.bootstrap_manifest_invalid",
                "wheel requirement metadata is malformed",
            )
        result.add(_canonical_name(match.group(1)))
    return result


def _host_target_compatible(target: dict[str, object]) -> bool:
    python_version = target["python_version"]
    if python_version is not None and python_version != (
        f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
    ):
        return False
    implementation = target["implementation"]
    if implementation is not None:
        current = "cp" if os.sys.implementation.name == "cpython" else "py"
        if implementation not in {current, "py"}:
            return False
    abi = target["abi"]
    if abi is not None:
        current_abi = f"cp{os.sys.version_info.major}{os.sys.version_info.minor}"
        if abi not in {current_abi, "abi3", "none"}:
            return False
    selected_platform = target["platform"]
    if selected_platform is None:
        return True
    machine = host_platform.machine().casefold().replace("-", "_")
    if os.name == "nt":
        return selected_platform.casefold() in {
            "win_amd64" if machine in {"amd64", "x86_64"} else f"win_{machine}",
        }
    if os.sys.platform == "darwin":
        return selected_platform.casefold().startswith("macosx_") and (
            selected_platform.casefold().endswith((machine, "universal2"))
        )
    return selected_platform.casefold().startswith(
        ("linux_", "manylinux_", "musllinux_")
    ) and selected_platform.casefold().endswith(machine)


def verify_worker_wheelhouse(
    wheelhouse: Path,
    manifest_path: Path,
    expected_closure_identity: str,
    *,
    require_host_compatible: bool = True,
) -> tuple[dict[str, object], str, Path]:
    """Verify one canonical, fully pinned wheelhouse before installer execution."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_closure_identity):
        raise WorkerBootstrapError(
            "worker.bootstrap_closure_identity_invalid",
            "expected dependency closure identity must be a SHA-256 URI",
        )
    root = Path(wheelhouse)
    selected_manifest = Path(manifest_path)
    try:
        if (
            root.is_symlink()
            or not root.is_dir()
            or selected_manifest.is_symlink()
            or selected_manifest.name != "worker-wheelhouse.json"
            or selected_manifest.parent.resolve(strict=True)
            != root.resolve(strict=True)
        ):
            raise OSError
        content = selected_manifest.read_bytes()
        manifest = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse manifest is unavailable or invalid",
        ) from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "framework",
        "framework_distribution_identity",
        "target",
        "wheels",
    }:
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse manifest fields are invalid",
        )
    if content != _canonical_bytes(manifest) + b"\n":
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_noncanonical",
            "worker wheelhouse manifest must use canonical JSON",
        )
    identity = _manifest_identity(manifest)
    if identity != expected_closure_identity:
        raise WorkerBootstrapError(
            "worker.bootstrap_closure_mismatch",
            "worker wheelhouse manifest does not match the required closure identity",
        )
    if (
        manifest["schema"] != _WHEELHOUSE_SCHEMA
        or not isinstance(manifest["framework"], str)
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(manifest["framework_distribution_identity"]),
        )
        or not isinstance(manifest["target"], dict)
        or not isinstance(manifest["wheels"], list)
        or not manifest["wheels"]
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse manifest authority is incomplete",
        )
    target = manifest["target"]
    assert isinstance(target, dict)
    if set(target) != {"abi", "implementation", "platform", "python_version"} or any(
        value is not None and (not isinstance(value, str) or not value)
        for value in target.values()
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse target is invalid",
        )
    if require_host_compatible and not _host_target_compatible(target):
        raise WorkerBootstrapError(
            "worker.bootstrap_target_incompatible",
            "worker wheelhouse target does not match this Python host",
        )
    entries = manifest["wheels"]
    assert isinstance(entries, list)
    filenames: list[str] = []
    names: list[str] = []
    inspected_entries: list[dict[str, object]] = []
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {
            "distribution_name",
            "distribution_version",
            "filename",
            "requires_dist",
            "sha256",
            "size",
        }:
            raise WorkerBootstrapError(
                "worker.bootstrap_manifest_invalid",
                "worker wheelhouse entry fields are invalid",
            )
        filename = raw["filename"]
        if (
            not isinstance(filename, str)
            or not _WHEEL_FILENAME.fullmatch(filename)
            or Path(filename).name != filename
            or not isinstance(raw["sha256"], str)
            or not _SHA256.fullmatch(raw["sha256"])
            or not isinstance(raw["size"], int)
            or isinstance(raw["size"], bool)
            or raw["size"] < 1
        ):
            raise WorkerBootstrapError(
                "worker.bootstrap_manifest_invalid",
                "worker wheelhouse entry is invalid",
            )
        candidate = root / filename
        inspected = _inspect_wheel(candidate, str(raw["sha256"]), required_name=None)
        if inspected != raw:
            raise WorkerBootstrapError(
                "worker.bootstrap_manifest_mismatch",
                "worker wheel does not match its manifest entry",
            )
        filenames.append(filename)
        names.append(str(raw["distribution_name"]))
        inspected_entries.append(inspected)
    if filenames != sorted(filenames) or len(filenames) != len(set(filenames)):
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse filenames must be sorted and unique",
        )
    allowed_files = set(filenames) | {selected_manifest.name}
    actual_files = set()
    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            raise WorkerBootstrapError(
                "worker.bootstrap_manifest_invalid",
                "worker wheelhouse contains an unverified entry",
            )
        actual_files.add(candidate.name)
    if actual_files != allowed_files:
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse files do not exactly match the manifest",
        )
    if len(names) != len(set(names)):
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse distributions must be unique",
        )
    available = set(names)
    for entry in inspected_entries:
        missing = _required_unmarked_names(entry["requires_dist"]) - available
        if missing:
            raise WorkerBootstrapError(
                "worker.bootstrap_dependency_missing",
                "worker wheelhouse omits a mandatory dependency",
            )
    framework = str(manifest["framework"])
    matches = [
        entry
        for entry in inspected_entries
        if entry["filename"] == framework
        and entry["distribution_name"] == "literate-ai"
    ]
    if len(matches) != 1:
        raise WorkerBootstrapError(
            "worker.bootstrap_manifest_invalid",
            "worker wheelhouse does not select one Literate AI wheel",
        )
    return manifest, identity, root / framework


def _pip_target_arguments(
    *,
    platform: str | None,
    python_version: str | None,
    implementation: str | None,
    abi: str | None,
) -> list[str]:
    result: list[str] = []
    for option, value in (
        ("--platform", platform),
        ("--python-version", python_version),
        ("--implementation", implementation),
        ("--abi", abi),
    ):
        if value is not None:
            result.extend((option, value))
    return result


def _target_marker_environment(
    *,
    platform: str | None,
    python_version: str | None,
    implementation: str | None,
) -> dict[str, str]:
    from packaging.markers import default_environment

    environment = default_environment()
    if python_version is not None:
        environment["python_version"] = python_version
        environment["python_full_version"] = python_version + ".0"
    if implementation is not None:
        environment["implementation_name"] = (
            "cpython" if implementation == "cp" else implementation
        )
        environment["platform_python_implementation"] = (
            "CPython" if implementation == "cp" else implementation
        )
    selected = (platform or "").casefold()
    machine = next(
        (
            candidate
            for candidate in ("x86_64", "aarch64", "arm64", "universal2")
            if selected.endswith(candidate)
        ),
        selected.rsplit("_", 1)[-1],
    )
    if selected.startswith("win_"):
        environment.update(
            {
                "os_name": "nt",
                "platform_machine": (
                    "AMD64"
                    if selected == "win_amd64"
                    else selected.removeprefix("win_")
                ),
                "platform_system": "Windows",
                "sys_platform": "win32",
            }
        )
    elif selected.startswith("macosx_"):
        environment.update(
            {
                "os_name": "posix",
                "platform_machine": machine,
                "platform_system": "Darwin",
                "sys_platform": "darwin",
            }
        )
    elif selected:
        environment.update(
            {
                "os_name": "posix",
                "platform_machine": machine,
                "platform_system": "Linux",
                "sys_platform": "linux",
            }
        )
    return environment


def _close_target_requirements(
    wheelhouse: Path,
    *,
    downloader: tuple[str, ...],
    framework_name: str,
    platform: str | None,
    python_version: str | None,
    implementation: str | None,
    abi: str | None,
) -> None:
    from packaging.requirements import InvalidRequirement, Requirement

    marker_environment = _target_marker_environment(
        platform=platform,
        python_version=python_version,
        implementation=implementation,
    )
    active_extras: dict[str, set[str]] = {"literate-ai": set()}
    processed: set[tuple[str, tuple[str, ...]]] = set()
    target_arguments = _pip_target_arguments(
        platform=platform,
        python_version=python_version,
        implementation=implementation,
        abi=abi,
    )
    for _ in range(_MAX_MEMBERS):
        entries = []
        for wheel in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name):
            content = _read_stable(wheel)
            entries.append(_inspect_wheel(wheel, _digest(content), required_name=None))
        by_name = {str(entry["distribution_name"]): entry for entry in entries}
        missing: set[str] = set()
        changed = False
        for name, extras in tuple(sorted(active_extras.items())):
            entry = by_name.get(name)
            if entry is None:
                continue
            state = (name, tuple(sorted(extras)))
            if state in processed:
                continue
            processed.add(state)
            for raw in entry["requires_dist"]:
                try:
                    requirement = Requirement(str(raw))
                except InvalidRequirement as exc:
                    raise WorkerBootstrapError(
                        "worker.wheelhouse_requirement_invalid",
                        "wheelhouse dependency metadata is invalid",
                    ) from exc
                selected_extras = extras | {""}
                if requirement.marker is not None and not any(
                    requirement.marker.evaluate(
                        {**marker_environment, "extra": selected_extra}
                    )
                    for selected_extra in selected_extras
                ):
                    continue
                if requirement.url is not None:
                    raise WorkerBootstrapError(
                        "worker.wheelhouse_requirement_invalid",
                        "worker dependencies must not use direct URLs",
                    )
                dependency_name = _canonical_name(requirement.name)
                dependency_extras = active_extras.setdefault(dependency_name, set())
                before = len(dependency_extras)
                dependency_extras.update(requirement.extras)
                changed = changed or len(dependency_extras) != before
                if dependency_name not in by_name:
                    missing.add(str(requirement).split(";", 1)[0].strip())
        if not missing:
            if not changed:
                if framework_name not in {str(entry["filename"]) for entry in entries}:
                    raise WorkerBootstrapError(
                        "worker.wheelhouse_framework_missing",
                        "resolved wheelhouse omits the exact framework wheel",
                    )
                return
            continue
        _download_wheels(
            (
                *downloader,
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--dest",
                os.fspath(wheelhouse),
                *target_arguments,
                *sorted(missing),
            )
        )
    raise WorkerBootstrapError(
        "worker.wheelhouse_dependency_oversized",
        "worker dependency closure exceeds the traversal bound",
    )


def _wheelhouse_downloader(parent: Path) -> tuple[Path, tuple[str, ...]]:
    """Provision an owned downloader without assuming pip is in the CLI runtime."""

    resolver = Path(
        tempfile.mkdtemp(prefix=".literate-ai-wheelhouse-resolver-", dir=parent)
    )
    try:
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(resolver)
        python = _python_launcher(resolver)
        if not python.is_file():
            raise OSError("resolver Python launcher is missing")
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(resolver, ignore_errors=True)
        raise WorkerBootstrapError(
            "worker.wheelhouse_downloader_unavailable",
            "wheelhouse export could not provision its isolated pip downloader; "
            "install this Python's venv and ensurepip support",
        ) from exc
    return resolver, (os.fspath(python), "-m", "pip")


def _download_wheels(command: tuple[str, ...]) -> None:
    try:
        _run_with_tree_kill(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
            env=_install_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerBootstrapError(
            "worker.wheelhouse_download_timeout",
            "binary wheel download exceeded the five-minute deadline",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise WorkerBootstrapError(
            "worker.wheelhouse_download_failed",
            "binary wheel download failed; verify package-index access and target "
            "wheel availability",
        ) from exc
    except OSError as exc:
        raise WorkerBootstrapError(
            "worker.wheelhouse_downloader_unavailable",
            "the isolated wheelhouse downloader could not be executed",
        ) from exc


def export_worker_wheelhouse(
    framework_wheel: Path,
    output: Path,
    *,
    framework_distribution_identity: str,
    platform: str | None = None,
    python_version: str | None = None,
    implementation: str | None = None,
    abi: str | None = None,
) -> tuple[Path, str]:
    """Resolve and pin an exact coordinator-owned binary dependency closure."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", framework_distribution_identity):
        raise WorkerBootstrapError(
            "worker.bootstrap_distribution_identity_invalid",
            "framework distribution identity must be a SHA-256 URI",
        )
    destination = Path(output)
    if destination.exists():
        raise WorkerBootstrapError(
            "worker.wheelhouse_output_exists",
            "wheelhouse output must not already exist",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent)
    )
    resolver: Path | None = None
    try:
        resolver, downloader = _wheelhouse_downloader(destination.parent)
        command = [
            *downloader,
            "download",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--dest",
            os.fspath(temporary),
        ]
        command.extend(
            _pip_target_arguments(
                platform=platform,
                python_version=python_version,
                implementation=implementation,
                abi=abi,
            )
        )
        command.append(os.fspath(Path(framework_wheel).resolve(strict=True)))
        _download_wheels(tuple(command))
        _close_target_requirements(
            temporary,
            downloader=downloader,
            framework_name=Path(framework_wheel).name,
            platform=platform,
            python_version=python_version,
            implementation=implementation,
            abi=abi,
        )
        entries = []
        for wheel in sorted(temporary.glob("*.whl"), key=lambda item: item.name):
            content = _read_stable(wheel)
            entries.append(_inspect_wheel(wheel, _digest(content), required_name=None))
        framework_name = Path(framework_wheel).name
        if not any(
            entry["filename"] == framework_name
            and entry["distribution_name"] == "literate-ai"
            for entry in entries
        ):
            raise WorkerBootstrapError(
                "worker.wheelhouse_framework_missing",
                "resolved wheelhouse does not contain the exact framework wheel",
            )
        manifest: dict[str, object] = {
            "schema": _WHEELHOUSE_SCHEMA,
            "framework": framework_name,
            "framework_distribution_identity": framework_distribution_identity,
            "target": {
                "abi": abi,
                "implementation": implementation,
                "platform": platform,
                "python_version": python_version,
            },
            "wheels": entries,
        }
        identity = _manifest_identity(manifest)
        manifest_path = temporary / "worker-wheelhouse.json"
        manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
        verify_worker_wheelhouse(
            temporary,
            manifest_path,
            identity,
            require_host_compatible=False,
        )
        os.replace(temporary, destination)
    except WorkerBootstrapError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise WorkerBootstrapError(
            "worker.wheelhouse_export_failed",
            "worker wheelhouse export failed",
        ) from exc
    finally:
        if resolver is not None:
            shutil.rmtree(resolver, ignore_errors=True)
    return destination / "worker-wheelhouse.json", identity


def _site_packages(environment: Path) -> Path:
    candidates = [environment / "Lib" / "site-packages"]
    candidates.extend(sorted((environment / "lib").glob("python*/site-packages")))
    matches = [path for path in candidates if path.is_dir()]
    if len(matches) != 1:
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "isolated environment has no unique site-packages directory",
        )
    return matches[0]


def _logical_path(
    recorded: str, site_packages: Path, environment: Path
) -> tuple[str, Path] | None:
    candidate = PurePosixPath(recorded)
    if candidate.is_absolute() or any(part in {"", "."} for part in candidate.parts):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid", "installed RECORD path is invalid"
        )
    try:
        located = site_packages.joinpath(*candidate.parts).resolve(strict=True)
    except FileNotFoundError:
        portable_parts = list(candidate.parts)
        while portable_parts and portable_parts[0] == "..":
            portable_parts.pop(0)
        located = site_packages.joinpath(*portable_parts).resolve(strict=True)
    try:
        located.relative_to(environment.resolve(strict=True))
    except ValueError as exc:
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed RECORD path escapes the isolated environment",
        ) from exc
    parts = candidate.parts
    lowered = tuple(part.casefold() for part in parts)
    if candidate.suffix.casefold() in {".pyc", ".pyo"}:
        return None
    if candidate.suffix.casefold() == ".pth" or any(
        part.startswith("__editable__") for part in lowered
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed distribution contains editable or startup path machinery",
        )
    if "literate_ai" in parts:
        index = parts.index("literate_ai")
        return PurePosixPath(*parts[index:]).as_posix(), located
    for index in range(len(parts) - 2):
        if lowered[index : index + 3] == ("share", "literate-ai", "schemas"):
            return PurePosixPath(*parts[index:]).as_posix(), located
    for index, part in enumerate(parts):
        if part.endswith(".dist-info") and index + 1 < len(parts):
            if parts[-1].casefold() in _EXCLUDED_METADATA:
                return None
            return PurePosixPath(part, *parts[index + 1 :]).as_posix(), located
    if candidate.name.casefold() in _CONSOLE_LAUNCHER_NAMES:
        return None
    raise WorkerBootstrapError(
        "worker.bootstrap_install_invalid",
        "installed distribution contains an unsupported payload location",
    )


def installed_distribution_identity(environment: Path) -> str:
    """Observe installed files without importing or executing the distribution."""

    site_packages = _site_packages(Path(environment))
    dist_infos = sorted(site_packages.glob("literate_ai-*.dist-info"))
    if len(dist_infos) != 1:
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "isolated environment lacks one Literate AI distribution",
        )
    dist_info = dist_infos[0]
    metadata = BytesParser().parsebytes((dist_info / "METADATA").read_bytes())
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid", "installed metadata is incomplete"
        )
    rows = list(
        csv.reader((dist_info / "RECORD").read_text(encoding="utf-8").splitlines())
    )
    if (
        not rows
        or len(rows) > _MAX_MEMBERS
        or len({row[0] for row in rows}) != len(rows)
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed RECORD inventory is empty, oversized, or ambiguous",
        )
    members: list[dict[str, object]] = []
    total_size = 0
    for row in rows:
        if len(row) != 3:
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid", "installed RECORD is malformed"
            )
        resolved = _logical_path(row[0], site_packages, Path(environment))
        if resolved is None:
            continue
        logical, located = resolved
        if located.is_symlink() or not located.is_file():
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed payload contains an unsafe member",
            )
        content = located.read_bytes()
        total_size += len(content)
        if total_size > _MAX_WHEEL_BYTES:
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed distribution exceeds the payload size limit",
            )
        members.append(
            {
                "path": logical,
                "size": len(content),
                "digest": "sha256:" + _digest(content),
            }
        )
    members.sort(key=lambda item: str(item["path"]))
    if not members or len({item["path"] for item in members}) != len(members):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed payload inventory is empty or ambiguous",
        )
    return _canonical_identity(
        {
            "schema": _IDENTITY_SCHEMA,
            "distribution_name": _canonical_name(name),
            "distribution_version": version,
            "members": members,
        }
    )


def _python_launcher(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _litai_launcher(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "litai.cmd"
    return environment / "bin" / "litai"


def _install_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("PYTHON") or name.startswith("PIP_"):
            environment.pop(name)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _write_portable_launchers(environment: Path) -> None:
    launcher = (
        "import os\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parent\n"
        "os.environ.setdefault('LITAI_TOOL_DIR', str(root / 'tools'))\n"
        "from literate_ai.cli.dispatch import main\n"
        "raise SystemExit(main())\n"
    )
    (environment / "launcher.py").write_text(launcher, encoding="utf-8")
    posix = environment / "bin" / "litai"
    posix.parent.mkdir(parents=True, exist_ok=True)
    posix.write_text(
        "#!/bin/sh\n"
        'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "$script_dir/python" "$script_dir/../launcher.py" "$@"\n',
        encoding="utf-8",
    )
    posix.chmod(0o755)
    windows = environment / "Scripts" / "litai.cmd"
    windows.parent.mkdir(parents=True, exist_ok=True)
    windows.write_bytes(b'@"%~dp0python.exe" "%~dp0..\\launcher.py" %*\r\n')


def _install_offline(
    environment: Path,
    arguments: tuple[str, ...],
) -> None:
    try:
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(environment)
        _run_with_tree_kill(
            (
                str(_python_launcher(environment)),
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                *arguments,
            ),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
            env=_install_environment(),
        )
        _write_portable_launchers(environment)
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise


def _installed_distribution_record_identity(
    environment: Path, expected: dict[str, object]
) -> str:
    site_packages = _site_packages(environment)
    matches: list[Path] = []
    for candidate in site_packages.glob("*.dist-info"):
        metadata_path = candidate / "METADATA"
        if not metadata_path.is_file():
            continue
        metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        name = metadata.get("Name")
        version = metadata.get("Version")
        if (
            isinstance(name, str)
            and _canonical_name(name) == expected["distribution_name"]
            and version == expected["distribution_version"]
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed dependency metadata does not match the wheelhouse",
        )
    dist_info = matches[0]
    record = dist_info / "RECORD"
    try:
        rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed dependency RECORD is unavailable",
        ) from exc
    if (
        not rows
        or len(rows) > _MAX_MEMBERS
        or any(len(row) != 3 for row in rows)
        or len({row[0] for row in rows}) != len(rows)
    ):
        raise WorkerBootstrapError(
            "worker.bootstrap_install_invalid",
            "installed dependency RECORD is malformed",
        )
    members: list[dict[str, object]] = []
    root = environment.resolve(strict=True)
    for recorded, digest, size in rows:
        candidate = PurePosixPath(recorded)
        if candidate.is_absolute() or any(
            part in {"", "."} for part in candidate.parts
        ):
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed dependency RECORD path is unsafe",
            )
        located = site_packages.joinpath(*candidate.parts)
        if candidate.suffix.casefold() in {".pyc", ".pyo"} and not located.exists():
            continue
        try:
            try:
                resolved = located.resolve(strict=True)
            except FileNotFoundError:
                portable_parts = list(candidate.parts)
                while portable_parts and portable_parts[0] == "..":
                    portable_parts.pop(0)
                resolved = site_packages.joinpath(*portable_parts).resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed dependency RECORD path escapes the environment",
            ) from exc
        if resolved.is_symlink() or not resolved.is_file():
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed dependency payload contains an unsafe member",
            )
        content = resolved.read_bytes()
        if digest and digest != _wheel_record_digest(content):
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed dependency RECORD digest does not match: "
                f"{expected['distribution_name']}:{recorded}",
            )
        if size and size != str(len(content)):
            raise WorkerBootstrapError(
                "worker.bootstrap_install_invalid",
                "installed dependency RECORD size does not match",
            )
        members.append(
            {
                "path": recorded,
                "size": len(content),
                "digest": "sha256:" + _digest(content),
            }
        )
    members.sort(key=lambda item: str(item["path"]))
    return _canonical_identity(
        {
            "schema": "literate-ai/installed-worker-distribution@1",
            "distribution_name": expected["distribution_name"],
            "distribution_version": expected["distribution_version"],
            "members": members,
        }
    )


def _write_bootstrap_evidence(
    environment: Path,
    *,
    distribution_identity: str,
    dependency_closure_identity: str,
    installed: list[dict[str, object]],
) -> None:
    evidence = {
        "schema": _BOOTSTRAP_EVIDENCE_SCHEMA,
        "distribution_identity": distribution_identity,
        "dependency_closure_identity": dependency_closure_identity,
        "installed_distributions": installed,
        "capabilities": [],
    }
    (environment / _BOOTSTRAP_EVIDENCE_FILE).write_bytes(
        _canonical_bytes(evidence) + b"\n"
    )


def _existing_bootstrap_matches(
    environment: Path,
    *,
    distribution_identity: str,
    dependency_closure_identity: str,
    wheels: list[object],
) -> list[dict[str, object]] | None:
    path = environment / _BOOTSTRAP_EVIDENCE_FILE
    try:
        content = path.read_bytes()
        evidence = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not (
        isinstance(evidence, dict)
        and content == _canonical_bytes(evidence) + b"\n"
        and evidence.get("schema") == _BOOTSTRAP_EVIDENCE_SCHEMA
        and evidence.get("distribution_identity") == distribution_identity
        and evidence.get("dependency_closure_identity") == dependency_closure_identity
        and isinstance(evidence.get("installed_distributions"), list)
        and evidence.get("capabilities") == []
    ):
        return None
    try:
        observed = []
        for entry in wheels:
            if not isinstance(entry, dict):
                return None
            installed_identity = (
                distribution_identity
                if entry["distribution_name"] == "literate-ai"
                else _installed_distribution_record_identity(environment, entry)
            )
            observed.append(
                {
                    "distribution_name": entry["distribution_name"],
                    "distribution_version": entry["distribution_version"],
                    "installed_identity": installed_identity,
                }
            )
        observed.sort(key=lambda item: str(item["distribution_name"]))
        if (
            installed_distribution_identity(environment) == distribution_identity
            and observed == evidence["installed_distributions"]
        ):
            return observed
        return None
    except (OSError, WorkerBootstrapError):
        return None


def bootstrap_worker_wheelhouse(
    wheelhouse: Path,
    manifest_path: Path,
    *,
    expected_closure_identity: str,
    expected_distribution_identity: str,
    install_root: Path,
    replace_existing: bool = False,
) -> tuple[Path, list[dict[str, object]]]:
    """Install and verify one exact offline Standard worker dependency closure."""

    manifest, closure_identity, framework_wheel = verify_worker_wheelhouse(
        wheelhouse, manifest_path, expected_closure_identity
    )
    if manifest["framework_distribution_identity"] != expected_distribution_identity:
        raise WorkerBootstrapError(
            "worker.bootstrap_distribution_mismatch",
            "wheelhouse framework authority differs from the project pin",
        )
    target = Path(install_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    wheels = manifest["wheels"]
    assert isinstance(wheels, list)
    replacing = False
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise WorkerBootstrapError(
                "worker.bootstrap_existing_mismatch",
                "existing isolated distribution root is unsafe",
            )
        existing = _existing_bootstrap_matches(
            target,
            distribution_identity=expected_distribution_identity,
            dependency_closure_identity=closure_identity,
            wheels=wheels,
        )
        if existing is not None:
            return _litai_launcher(target), existing
        if not replace_existing:
            raise WorkerBootstrapError(
                "worker.bootstrap_existing_mismatch",
                "existing isolated distribution does not match project authority",
            )
        replacing = True
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.bootstrap-", dir=target.parent)
    )
    shutil.rmtree(temporary)
    try:
        _install_offline(
            temporary,
            (
                "--no-index",
                "--find-links",
                str(Path(wheelhouse).resolve(strict=True)),
                str(framework_wheel.resolve(strict=True)),
            ),
        )
        _write_portable_launchers(temporary)
        if installed_distribution_identity(temporary) != expected_distribution_identity:
            raise WorkerBootstrapError(
                "worker.bootstrap_distribution_mismatch",
                "installed distribution does not match project-pinned identity",
            )
        installed = []
        for entry in wheels:
            assert isinstance(entry, dict)
            installed_identity = (
                expected_distribution_identity
                if entry["distribution_name"] == "literate-ai"
                else _installed_distribution_record_identity(temporary, entry)
            )
            installed.append(
                {
                    "distribution_name": entry["distribution_name"],
                    "distribution_version": entry["distribution_version"],
                    "installed_identity": installed_identity,
                }
            )
        installed.sort(key=lambda item: str(item["distribution_name"]))
        _write_bootstrap_evidence(
            temporary,
            distribution_identity=expected_distribution_identity,
            dependency_closure_identity=closure_identity,
            installed=installed,
        )
        if not replacing:
            os.replace(temporary, target)
        else:
            backup = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.replaced-", dir=target.parent)
            )
            backup.rmdir()
            os.replace(target, backup)
            try:
                os.replace(temporary, target)
            except BaseException:
                os.replace(backup, target)
                raise
            else:
                shutil.rmtree(backup)
    except WorkerBootstrapError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise WorkerBootstrapError(
            "worker.bootstrap_install_failed",
            "isolated wheelhouse installation failed",
        ) from exc
    return _litai_launcher(target), installed


def bootstrap_worker_distribution(
    wheel: Path,
    *,
    expected_wheel_sha256: str,
    expected_distribution_identity: str,
    install_root: Path,
    replace_existing: bool = False,
) -> Path:
    """Install one digest-pinned wheel and verify its project-pinned payload."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_distribution_identity):
        raise WorkerBootstrapError(
            "worker.bootstrap_distribution_identity_invalid",
            "expected distribution identity must be a SHA-256 URI",
        )
    inspected = _inspect_wheel(
        Path(wheel), expected_wheel_sha256, required_name="literate-ai"
    )
    if inspected["requires_dist"]:
        raise WorkerBootstrapError(
            "worker.bootstrap_dependency_closure_required",
            "framework wheel declares dependencies; a verified wheelhouse is required",
        )
    target = Path(install_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    replacing = False
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise WorkerBootstrapError(
                "worker.bootstrap_existing_mismatch",
                "existing isolated distribution root is unsafe",
            )
        try:
            existing_identity = installed_distribution_identity(target)
        except WorkerBootstrapError:
            existing_identity = None
        if existing_identity == expected_distribution_identity:
            return _litai_launcher(target)
        if not replace_existing:
            raise WorkerBootstrapError(
                "worker.bootstrap_existing_mismatch",
                "existing isolated distribution does not match project authority",
            )
        replacing = True
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.bootstrap-", dir=target.parent)
    )
    shutil.rmtree(temporary)
    try:
        _install_offline(
            temporary,
            (
                "--no-index",
                "--no-deps",
                str(Path(wheel).resolve(strict=True)),
            ),
        )
        if installed_distribution_identity(temporary) != expected_distribution_identity:
            raise WorkerBootstrapError(
                "worker.bootstrap_distribution_mismatch",
                "installed distribution does not match project-pinned identity",
            )
        if not replacing:
            os.replace(temporary, target)
        else:
            backup = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.replaced-", dir=target.parent)
            )
            backup.rmdir()
            os.replace(target, backup)
            try:
                os.replace(temporary, target)
            except BaseException:
                os.replace(backup, target)
                raise
            else:
                shutil.rmtree(backup)
    except WorkerBootstrapError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise WorkerBootstrapError(
            "worker.bootstrap_install_failed",
            "isolated wheel installation failed",
        ) from exc
    return _litai_launcher(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheel", type=Path)
    source.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--closure-identity")
    parser.add_argument("--distribution-identity", required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--replace-existing", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        installed: list[dict[str, object]] = []
        if args.wheelhouse is not None:
            if args.manifest is None or args.closure_identity is None:
                raise WorkerBootstrapError(
                    "worker.bootstrap_manifest_required",
                    "wheelhouse bootstrap requires manifest and closure identity",
                )
            launcher, installed = bootstrap_worker_wheelhouse(
                args.wheelhouse,
                args.manifest,
                expected_closure_identity=args.closure_identity,
                expected_distribution_identity=args.distribution_identity,
                install_root=args.install_root,
                replace_existing=args.replace_existing,
            )
        else:
            if args.wheel_sha256 is None:
                raise WorkerBootstrapError(
                    "worker.bootstrap_digest_invalid",
                    "single-wheel bootstrap requires a wheel digest",
                )
            launcher = bootstrap_worker_distribution(
                args.wheel,
                expected_wheel_sha256=args.wheel_sha256,
                expected_distribution_identity=args.distribution_identity,
                install_root=args.install_root,
                replace_existing=args.replace_existing,
            )
    except WorkerBootstrapError as exc:
        print(
            json.dumps(
                {
                    "schema": "literate-ai/worker-bootstrap-error@1",
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=__import__("sys").stderr,
        )
        return 2
    result: dict[str, object] = {
        "schema": (
            "literate-ai/worker-bootstrap-result@2"
            if args.wheelhouse is not None
            else "literate-ai/worker-bootstrap-result@1"
        ),
        "ok": True,
        "distribution_identity": args.distribution_identity,
        "launcher": str(launcher),
    }
    if args.wheelhouse is not None:
        result["dependency_closure_identity"] = args.closure_identity
        result["installed_distributions"] = installed
        result["capabilities"] = []
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
