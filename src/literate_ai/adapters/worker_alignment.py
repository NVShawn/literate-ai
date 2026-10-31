"""Probe, inventory and align configured workers with one declared template.

A worker is aligned when it satisfies two sources of expectations:

* the repository's prerequisites, declared once in ``literate.worker-template.json``
  (commands per OS family, a minimum Python, and where Windows tools live); and
* the user's expectations: the live-test coding CLI and model from project-scoped
  ``test.json``, plus private ``worker-alignment.json`` naming the files each worker
  must hold (for example coding-CLI provider configuration and secret files, copied
  from user-chosen sources) and the commands that install missing tools.

Inspection is read-only. ``apply`` installs only commands that are missing, using
only commands the user declared, and replaces only files whose bytes differ, after
backing the old copy up. Secret contents are never printed; reports carry hash
prefixes only.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.ssh_transport import (
    BoundedSshProcessRunner,
    SshTransportError,
    powershell_command,
    powershell_home_path,
    powershell_literal,
    scp_arguments,
    ssh_arguments,
)
from literate_ai.contracts.execution_dispatch import ExecutionWorker

TEMPLATE_FILE = "literate.worker-template.json"
TEMPLATE_SCHEMA = "literate-ai/worker-template@1"
ALIGNMENT_SCHEMA = "literate-ai/worker-alignment@1"
REPORT_SCHEMA = "literate-ai/worker-alignment-report@1"
OS_FAMILIES = ("freebsd", "linux", "macos", "windows")
_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PROBE_TIMEOUT_SECONDS = 120
_MODEL_TIMEOUT_SECONDS = 300
_INSTALL_TIMEOUT_SECONDS = 1800
_MODEL_PROMPT = "Reply with exactly the word: ok"


class WorkerAlignmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(path: str, message: str) -> None:
    raise WorkerAlignmentError("worker_alignment.invalid", f"{path}: {message}")


@dataclass(frozen=True, slots=True)
class TemplateCommand:
    """One required command and, on Windows, where it may live off ``PATH``.

    ``extra_paths`` go before ``PATH``; ``fallback_paths`` go after it, so their
    tools never shadow a system or compiler tool of the same name.
    """

    name: str
    extra_paths: tuple[str, ...] = ()
    vswhere: str | None = None
    fallback_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformTemplate:
    commands: tuple[TemplateCommand, ...]
    python_commands: tuple[str, ...]
    python_minimum: tuple[int, ...]
    minimum_free_gib: int | None = None
    # Windows only: paths Microsoft Defender must not scan in real time.
    # ``{workspace}`` is the worker's configured workspace.
    defender_exclusions: tuple[str, ...] = ()
    # Windows only: executables whose file activity Defender must not scan.
    # ``{python}`` is the real base interpreter behind the first template Python.
    defender_process_exclusions: tuple[str, ...] = ()


def _narrow_exclusion(entry: str) -> bool:
    """Whether a Defender exclusion names one directory, never home or a root."""

    if entry == "{workspace}":
        return True
    if entry.startswith("~/"):
        parts = entry[2:].replace("\\", "/").split("/")
    elif re.fullmatch(r"[A-Za-z]:\\.+", entry):
        parts = entry[3:].replace("\\", "/").split("/")
    else:
        return False
    return all(part not in {"", ".", ".."} for part in parts)


# Executables a process exclusion must never name: shells, script hosts, and
# system processes whose file activity would then go entirely unscanned.
_DENIED_PROCESS_EXCLUSIONS = frozenset(
    {
        "cmd.exe",
        "conhost.exe",
        "cscript.exe",
        "explorer.exe",
        "mshta.exe",
        "powershell.exe",
        "pwsh.exe",
        "regsvr32.exe",
        "rundll32.exe",
        "svchost.exe",
        "wscript.exe",
    }
)


def _narrow_process_exclusion(entry: str) -> bool:
    """Whether a Defender process exclusion names one executable, never a pattern."""

    if entry == "{python}":
        return True
    if not re.fullmatch(r"[A-Za-z]:\\[^*?%<>|\":/\x00-\x1f]+\.exe", entry, re.I):
        return False
    parts = entry[3:].split("\\")
    return (
        all(part not in {"", ".", ".."} and part == part.strip() for part in parts)
        and parts[-1].casefold() not in _DENIED_PROCESS_EXCLUSIONS
    )


def _commands(value: object, path: str) -> tuple[TemplateCommand, ...]:
    if not isinstance(value, list) or not value:
        _fail(path, "must list required commands")
    parsed = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not {"name"} <= set(item) <= {
            "name",
            "extra_paths",
            "fallback_paths",
            "vswhere",
        }:
            _fail(
                f"{path}[{index}]", "is a command name or {name, extra_paths, vswhere}"
            )
        name = item["name"]
        if not isinstance(name, str) or not _COMMAND.fullmatch(name):
            _fail(f"{path}[{index}].name", "is not a plain command name")
        extra = item.get("extra_paths", [])
        fallback = item.get("fallback_paths", [])
        vswhere = item.get("vswhere")
        for key, directories in (("extra_paths", extra), ("fallback_paths", fallback)):
            if not isinstance(directories, list) or any(
                not isinstance(entry, str) or not entry for entry in directories
            ):
                _fail(f"{path}[{index}].{key}", "must be directory strings")
        if vswhere is not None and (not isinstance(vswhere, str) or not vswhere):
            _fail(f"{path}[{index}].vswhere", "must be a vswhere -find pattern")
        parsed.append(TemplateCommand(name, tuple(extra), vswhere, tuple(fallback)))
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class WorkerTemplate:
    """The repository's worker prerequisites, per OS family."""

    platforms: Mapping[str, PlatformTemplate]

    @classmethod
    def load(cls, path: Path) -> WorkerTemplate:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerAlignmentError(
                "worker_alignment.template_unavailable",
                f"cannot read the repository worker template {path}",
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "platforms"}
            or value["schema"] != TEMPLATE_SCHEMA
            or not isinstance(value["platforms"], dict)
            or not set(value["platforms"]) <= set(OS_FAMILIES)
        ):
            _fail("worker_template", f"must be {TEMPLATE_SCHEMA} with OS platforms")
        platforms = {}
        for family, raw in value["platforms"].items():
            path_name = f"worker_template.platforms.{family}"
            if not isinstance(raw, dict) or not {"commands", "python"} <= set(raw) <= {
                "commands",
                "python",
                "minimum_free_gib",
                "defender_exclusions",
                "defender_process_exclusions",
            }:
                _fail(path_name, "requires commands and python")
            exclusions = raw.get("defender_exclusions", [])
            if (
                family != "windows"
                and exclusions
                or not isinstance(exclusions, list)
                or any(
                    not isinstance(item, str) or not _narrow_exclusion(item)
                    for item in exclusions
                )
            ):
                _fail(
                    f"{path_name}.defender_exclusions",
                    "is Windows-only and lists {workspace} or a ~/ or absolute "
                    "directory below a drive root and home, without . or ..",
                )
            processes = raw.get("defender_process_exclusions", [])
            if (
                family != "windows"
                and processes
                or not isinstance(processes, list)
                or any(
                    not isinstance(item, str) or not _narrow_process_exclusion(item)
                    for item in processes
                )
            ):
                _fail(
                    f"{path_name}.defender_process_exclusions",
                    "is Windows-only and lists {python} or absolute .exe paths "
                    "without wildcards, variables, . or ..",
                )
            free = raw.get("minimum_free_gib")
            if free is not None and (type(free) is not int or not 0 < free < 100000):
                _fail(f"{path_name}.minimum_free_gib", "must be a positive integer")
            python = raw["python"]
            if (
                not isinstance(python, dict)
                or set(python) != {"commands", "minimum"}
                or not isinstance(python["commands"], list)
                or not python["commands"]
                or any(
                    not isinstance(item, str) or not _COMMAND.fullmatch(item)
                    for item in python["commands"]
                )
                or not isinstance(python["minimum"], str)
                or not re.fullmatch(r"\d+(\.\d+){0,2}", python["minimum"])
            ):
                _fail(f"{path_name}.python", "requires commands and a minimum version")
            platforms[family] = PlatformTemplate(
                _commands(raw["commands"], f"{path_name}.commands"),
                tuple(python["commands"]),
                tuple(int(part) for part in python["minimum"].split(".")),
                free,
                tuple(exclusions),
                tuple(processes),
            )
        return cls(platforms)


@dataclass(frozen=True, slots=True)
class AlignedFile:
    source: Path
    destination: str
    secret: bool
    os_families: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallCommand:
    command: str
    os_family: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UserAlignment:
    """Private user expectations; never committed to a repository."""

    files: tuple[AlignedFile, ...] = ()
    installs: tuple[InstallCommand, ...] = ()

    @classmethod
    def load(cls, path: Path | None) -> UserAlignment:
        if path is None or not Path(path).exists():
            return cls()
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerAlignmentError(
                "worker_alignment.user_config_invalid",
                f"cannot read user worker alignment {path}",
            ) from exc
        if (
            not isinstance(value, dict)
            or not {"schema"} <= set(value) <= {"schema", "files", "installs"}
            or value["schema"] != ALIGNMENT_SCHEMA
        ):
            _fail("worker_alignment", f"must be {ALIGNMENT_SCHEMA}")
        files = []
        for index, item in enumerate(value.get("files", [])):
            item_path = f"worker_alignment.files[{index}]"
            if not isinstance(item, dict) or not {"source", "destination"} <= set(
                item
            ) <= {"source", "destination", "secret", "os_families"}:
                _fail(item_path, "requires source and destination")
            source = item["source"]
            destination = item["destination"]
            if not isinstance(source, str) or not (
                source.startswith("~/") or Path(source).is_absolute()
            ):
                _fail(f"{item_path}.source", "must be absolute or home-relative")
            if (
                not isinstance(destination, str)
                or not destination.startswith("~/")
                or ".." in Path(destination[2:]).parts
                or "\\" in destination
                or any(character in destination for character in "\0\r\n'\"$`")
            ):
                _fail(
                    f"{item_path}.destination",
                    "must be a plain home-relative path such as ~/.config/x",
                )
            families = item.get("os_families", list(OS_FAMILIES))
            if (
                not isinstance(families, list)
                or not families
                or not set(families) <= set(OS_FAMILIES)
            ):
                _fail(
                    f"{item_path}.os_families",
                    "must name freebsd, linux, macos or windows",
                )
            secret = item.get("secret", False)
            if type(secret) is not bool:
                _fail(f"{item_path}.secret", "must be a boolean")
            files.append(
                AlignedFile(
                    Path(source).expanduser(), destination, secret, tuple(families)
                )
            )
        installs = []
        for index, item in enumerate(value.get("installs", [])):
            item_path = f"worker_alignment.installs[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "command",
                "os_family",
                "argv",
            }:
                _fail(item_path, "requires command, os_family and argv")
            if (
                not isinstance(item["command"], str)
                or not _COMMAND.fullmatch(item["command"])
                or item["os_family"] not in OS_FAMILIES
                or not isinstance(item["argv"], list)
                or not item["argv"]
                or any(not isinstance(part, str) or not part for part in item["argv"])
            ):
                _fail(item_path, "needs a command name, an OS family and an argv")
            installs.append(
                InstallCommand(item["command"], item["os_family"], tuple(item["argv"]))
            )
        return cls(tuple(files), tuple(installs))


@dataclass(slots=True)
class Finding:
    kind: str
    name: str
    state: str
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "state": self.state,
            **({"detail": self.detail} if self.detail else {}),
            **({"remediation": self.remediation} if self.remediation else {}),
        }


@dataclass(slots=True)
class WorkerReport:
    worker_id: str
    os_family: str | None
    status: str = "aligned"
    findings: list[Finding] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "os_family": self.os_family,
            "status": self.status,
            "findings": [item.to_dict() for item in self.findings],
            "applied": list(self.applied),
        }


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _windows(worker: ExecutionWorker) -> bool:
    return worker.requirements.os_family == "windows"


def _exclusion_path(worker: ExecutionWorker, entry: str) -> str:
    """A PowerShell expression for one declared Defender exclusion."""

    if entry == "{workspace}":
        entry = worker.workspace or "~/.litai"
    return f"[IO.Path]::GetFullPath({powershell_home_path(entry)}).TrimEnd('\\')"


def _process_exclusion_path(platform: PlatformTemplate, entry: str) -> str:
    """PowerShell assigning ``$px`` one declared process exclusion, or ``$null``.

    ``{python}`` runs the template Python isolated (``-I -S``) so no site hook can
    print text that would be taken for the path, and in UTF-8 mode so a non-ASCII
    install path survives the pipe. The result must be a rooted, existing ``.exe``
    that is not a Microsoft Store alias, whose image Defender would never match.
    """

    if entry != "{python}":
        resolved = f"$px = {powershell_literal(entry)}"
    else:
        # Python 3.11+ always has sys._base_executable; no quotes survive PowerShell
        # 5.1 native-argument passing, so the program uses none.
        python = powershell_literal(platform.python_commands[0])
        resolved = (
            "$oe = [Console]::OutputEncoding; "
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            f"$px = & {python} -I -S -X utf8 -c "
            "'import os,sys;print(os.path.realpath(sys._base_executable))' 2>$null "
            "| Select-Object -First 1; [Console]::OutputEncoding = $oe"
        )
    return (
        f"{resolved}; if ($px) {{ $px = ([string]$px).Trim() }}; "
        "if (-not ($px -and [IO.Path]::IsPathRooted($px) -and $px -like '*.exe' "
        "-and $px -notlike '*\\WindowsApps\\*' -and "
        "(Test-Path -LiteralPath $px -PathType Leaf))) { $px = $null }"
    )


def _probe_script(
    worker: ExecutionWorker,
    platform: PlatformTemplate | None,
    commands: Sequence[str],
    files: Sequence[AlignedFile],
) -> str:
    """One read-only script printing tagged lines: cmd, python, file, os."""

    python = () if platform is None else platform.python_commands
    if _windows(worker):
        extras = (
            {} if platform is None else {item.name: item for item in platform.commands}
        )
        lines = ["$ErrorActionPreference = 'SilentlyContinue'"]
        for name in commands:
            lines.append(
                f"$c = Get-Command {powershell_literal(name)} -CommandType "
                "Application | Select-Object -First 1; $p = $c.Source"
            )
            item = extras.get(name)
            for directory in (
                () if item is None else (*item.extra_paths, *item.fallback_paths)
            ):
                candidate = powershell_literal(f"{directory}\\{name}.exe")
                lines.append(
                    f"if (-not $p -and (Test-Path -LiteralPath {candidate})) "
                    f"{{ $p = {candidate} }}"
                )
            if item is not None and item.vswhere:
                lines.append(
                    "$vw = Join-Path ${env:ProgramFiles(x86)} "
                    "'Microsoft Visual Studio\\Installer\\vswhere.exe'; "
                    "if (-not $p -and (Test-Path -LiteralPath $vw)) { $p = & $vw "
                    "-latest -products * -find "
                    f"{powershell_literal(item.vswhere)} | Select-Object -First 1 }}"
                )
            lines.append(f"if ($p) {{ 'cmd {name} ' + $p }} else {{ 'cmd {name} -' }}")
        for name in python:
            lines.append(
                f"$v = & {powershell_literal(name)} -c "
                "\"import sys;print('%d.%d.%d' % sys.version_info[:3])\" 2>$null; "
                f"if ($v) {{ 'python {name} ' + $v }}"
            )
        for index, item in enumerate(files):
            target = powershell_home_path(item.destination)
            lines.append(
                f"$f = {target}; if (Test-Path -LiteralPath $f -PathType Leaf) "
                "{ 'file " + str(index) + " ' + (Get-FileHash -Algorithm SHA256 "
                "-LiteralPath $f).Hash.ToLower() } else { 'file " + str(index) + " -' }"
            )
        exclusions = () if platform is None else platform.defender_exclusions
        if exclusions:
            # Without Defender, or with real-time protection off, there is
            # nothing to exclude; any other error fails closed as unreadable.
            # Reading exclusions needs administrator rights: Defender then
            # throws or returns an "N/A: Must be an administrator" placeholder.
            # Only literal exclusions of the path or a parent count, because
            # Defender expands %VARIABLES% in its own context.
            lines.append(
                "$state = 'unreadable'; if (-not (Get-Command Get-MpComputerStatus "
                "-ErrorAction SilentlyContinue)) { $state = 'none' } else { try { "
                "if (-not (Get-MpComputerStatus -ErrorAction Stop)"
                ".RealTimeProtectionEnabled) { $state = 'none' } else { $mp = "
                "Get-MpPreference -ErrorAction Stop; $raw = @($mp.ExclusionPath | "
                "Where-Object { $_ }); if (-not ($raw | Where-Object { $_ -like "
                "'N/A*' })) { $state = 'readable' } } } catch {} }; $have = @(); "
                "if ($state -eq 'readable') { $have = @($raw | ForEach-Object { "
                "$_.TrimEnd('\\').ToLowerInvariant() }) }"
            )
            for index, entry in enumerate(exclusions):
                lines.append(
                    f"$x = {_exclusion_path(worker, entry)}.ToLowerInvariant(); "
                    f"if ($state -ne 'readable') {{ 'excl {index} ' + $state }} "
                    "elseif ($have | Where-Object { $x -eq $_ -or $x.StartsWith($_ "
                    f"+ '\\') }}) {{ 'excl {index} present' }} else {{ 'excl "
                    f"{index} absent' }}"
                )
        if platform is not None and platform.defender_process_exclusions:
            # The same fail-closed reading as path exclusions; a process
            # exclusion matches one executable path exactly (case-insensitive).
            lines.append(
                "$pstate = 'unreadable'; if (-not (Get-Command Get-MpComputerStatus "
                "-ErrorAction SilentlyContinue)) { $pstate = 'none' } else { try { "
                "if (-not (Get-MpComputerStatus -ErrorAction Stop)"
                ".RealTimeProtectionEnabled) { $pstate = 'none' } else { $pmp = "
                "Get-MpPreference -ErrorAction Stop; $praw = @($pmp.ExclusionProcess"
                " | Where-Object { $_ }); if (-not ($praw | Where-Object { $_ -like "
                "'N/A*' })) { $pstate = 'readable' } } } catch {} }; $phave = @(); "
                "if ($pstate -eq 'readable') { $phave = @($praw | ForEach-Object { "
                "$_.ToLowerInvariant() }) }"
            )
            for index, entry in enumerate(platform.defender_process_exclusions):
                lines.append(
                    f"{_process_exclusion_path(platform, entry)}; "
                    f"if ($pstate -ne 'readable') {{ 'pexcl {index} ' + $pstate }} "
                    f"elseif (-not $px) {{ 'pexcl {index} unresolved' }} "
                    "elseif ($phave -contains $px.ToLowerInvariant())"
                    f" {{ 'pexcl {index} present' }} else {{ 'pexcl {index} absent' }}"
                )
        lines.append(
            "$d = Get-PSDrive -Name ((Split-Path -Qualifier $HOME).TrimEnd(':')); "
            "'free ' + [int64]($d.Free / 1KB)"
        )
        lines.append("'os windows ' + $env:PROCESSOR_ARCHITECTURE")
        lines.append("$global:LASTEXITCODE = 0")
        return powershell_command("; ".join(lines))
    lines = []
    for name in commands:
        quoted = shlex.quote(name)
        lines.append(
            f'p=$(command -v {quoted} 2>/dev/null) && echo "cmd {name} $p" '
            f'|| echo "cmd {name} -"'
        )
    for name in python:
        quoted = shlex.quote(name)
        lines.append(
            f"v=$({quoted} -c 'import sys;print(\"%d.%d.%d\" % sys.version_info[:3])'"
            f' 2>/dev/null) && echo "python {name} $v"'
        )
    for index, item in enumerate(files):
        target = '"$HOME"/' + shlex.quote(item.destination[2:])
        lines.append(
            f'if [ -f {target} ]; then echo "file {index} $( (sha256sum 2>/dev/null'
            f' || shasum -a 256 2>/dev/null || sha256 -q) < {target} | cut -c1-64)"; '
            f'else echo "file {index} -"; fi'
        )
    lines.append('echo "free $(df -Pk "$HOME" | awk \'NR==2 {print $4}\')"')
    lines.append('echo "os $(uname -s) $(uname -m)"')
    return "; ".join(lines) + "; true"


def _remote_command(worker: ExecutionWorker, argv: Sequence[str]) -> str:
    if _windows(worker):
        return powershell_command(
            "& " + " ".join(powershell_literal(item) for item in argv)
        )
    return shlex.join(argv)


class WorkerAligner:
    """Inspect and optionally align workers; one runner per transport call."""

    def __init__(
        self,
        template: WorkerTemplate,
        user: UserAlignment,
        *,
        coding_cli: str | None,
        model: str | None,
        cwd: Path,
        runner_factory: Callable[[], BoundedSshProcessRunner] = BoundedSshProcessRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.template = template
        self.user = user
        self.coding_cli = coding_cli
        self.model = model
        self.cwd = cwd
        self.runner_factory = runner_factory
        self.clock = clock

    def _run(self, worker: ExecutionWorker, command: str, timeout: int):
        assert worker.endpoint is not None
        return self.runner_factory().run(
            ssh_arguments(
                worker.endpoint,
                "/bin/sh -c " + shlex.quote(command)
                if worker.requirements.os_family == "freebsd"
                else command,
                timeout,
                transport=worker.transport,
                login_shell=worker.requirements.os_family not in {"freebsd", "windows"},
            ),
            cwd=self.cwd,
            timeout_seconds=timeout,
        )

    def _commands(self, platform: PlatformTemplate | None) -> list[str]:
        names = [] if platform is None else [item.name for item in platform.commands]
        if self.coding_cli and self.coding_cli not in names:
            names.append(self.coding_cli)
        return names

    def inspect(
        self, worker: ExecutionWorker, *, check_model: bool = True
    ) -> tuple[WorkerReport, dict[str, str | None]]:
        family = worker.requirements.os_family
        report = WorkerReport(worker.worker_id, family)
        platform = self.template.platforms.get(family or "")
        if platform is None and self.template.platforms:
            report.findings.append(
                Finding(
                    "platform",
                    str(family),
                    "unknown",
                    "the repository template declares no prerequisites for this "
                    "OS family; set the worker's os_family requirement",
                )
            )
        files = [item for item in self.user.files if (family or "") in item.os_families]
        commands = self._commands(platform)
        try:
            completed = self._run(
                worker,
                _probe_script(worker, platform, commands, files),
                _PROBE_TIMEOUT_SECONDS,
            )
        except SshTransportError as exc:
            report.status = "unreachable"
            report.findings.append(
                Finding("transport", worker.worker_id, "unreachable", exc.message)
            )
            return report, {}
        output = completed.stdout.decode("utf-8", errors="replace")
        found: dict[str, str | None] = {}
        pythons: dict[str, str] = {}
        hashes: dict[int, str | None] = {}
        exclusions: dict[int, str] = {}
        process_exclusions: dict[int, str] = {}
        free_kib: int | None = None
        observed_os = None
        for line in output.splitlines():
            if line.startswith("free ") and line[5:].strip().isdigit():
                free_kib = int(line[5:].strip())
            if line.startswith("os "):
                parts_os = line.strip().split()
                observed_os = parts_os[1] if len(parts_os) > 1 else None
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "cmd":
                found[parts[1]] = None if parts[2] == "-" else parts[2]
            elif len(parts) == 3 and parts[0] == "python":
                pythons[parts[1]] = parts[2].strip()
            elif len(parts) == 3 and parts[0] == "file" and parts[1].isdigit():
                hashes[int(parts[1])] = None if parts[2] == "-" else parts[2].strip()
            elif len(parts) == 3 and parts[0] == "excl" and parts[1].isdigit():
                exclusions[int(parts[1])] = parts[2].strip()
            elif len(parts) == 3 and parts[0] == "pexcl" and parts[1].isdigit():
                process_exclusions[int(parts[1])] = parts[2].strip()
        if completed.returncode or not found and commands:
            report.status = "unreachable"
            report.findings.append(
                Finding(
                    "transport",
                    worker.worker_id,
                    "probe-failed",
                    f"inventory probe exited {completed.returncode}",
                )
            )
            return report, found
        if family == "freebsd" and observed_os != "FreeBSD":
            report.findings.append(
                Finding(
                    "platform",
                    "freebsd",
                    "mismatch",
                    "the native inventory did not identify a FreeBSD kernel",
                )
            )
        installs = {
            item.command for item in self.user.installs if item.os_family == family
        }
        for name in commands:
            if found.get(name):
                continue
            report.findings.append(
                Finding(
                    "command",
                    name,
                    "missing",
                    remediation=(
                        "declared install runs with --apply"
                        if name in installs
                        else "add an install to worker-alignment.json, or run the "
                        "detect-before-install bootstrap with --install-missing"
                    ),
                )
            )
        if platform is not None:
            usable = [
                (name, version)
                for name, version in pythons.items()
                if tuple(int(part) for part in version.split(".")[:3])
                >= platform.python_minimum
            ]
            if not usable:
                minimum = ".".join(str(part) for part in platform.python_minimum)
                report.findings.append(
                    Finding(
                        "python",
                        "/".join(platform.python_commands),
                        "missing",
                        f"no Python {minimum}+ found (saw {pythons or 'none'})",
                        "run the detect-before-install bootstrap",
                    )
                )
        if (
            platform is not None
            and platform.minimum_free_gib is not None
            and free_kib is not None
            and free_kib < platform.minimum_free_gib * 1024 * 1024
        ):
            report.findings.append(
                Finding(
                    "disk",
                    "home volume",
                    "low",
                    f"{free_kib // (1024 * 1024)} GiB free; the template requires "
                    f"{platform.minimum_free_gib} GiB",
                    "inspect with `litai worker health` and free space with "
                    "`litai worker cleanup`; align never deletes worker data",
                )
            )
        if platform is not None and _windows(worker):
            for index, entry in enumerate(platform.defender_exclusions):
                state = exclusions.get(index, "unreadable")
                if state in {"present", "none"}:
                    continue
                report.findings.append(
                    Finding(
                        "defender-exclusion",
                        entry,
                        "missing" if state == "absent" else "unchecked",
                        "real-time scanning slows builds and test trees here"
                        if state == "absent"
                        else "Defender exclusions are unreadable without "
                        "administrator rights",
                        "added with --apply"
                        if state == "absent"
                        else "connect as an administrator, then rerun",
                    )
                )
            for index, entry in enumerate(platform.defender_process_exclusions):
                state = process_exclusions.get(index, "unreadable")
                if state in {"present", "none"}:
                    continue
                report.findings.append(
                    Finding(
                        "defender-process-exclusion",
                        entry,
                        "missing" if state == "absent" else "unchecked",
                        "real-time scanning slows every file this executable opens"
                        if state == "absent"
                        else "the template Python did not resolve to an executable"
                        if state == "unresolved"
                        else "Defender exclusions are unreadable without "
                        "administrator rights",
                        "added with --apply"
                        if state == "absent"
                        else "run the detect-before-install bootstrap"
                        if state == "unresolved"
                        else "connect as an administrator, then rerun",
                    )
                )
        for index, item in enumerate(files):
            try:
                local = _digest(item.source.read_bytes())
            except OSError:
                report.findings.append(
                    Finding(
                        "file",
                        item.destination,
                        "source-missing",
                        f"declared source {item.source} is unreadable",
                    )
                )
                continue
            remote = hashes.get(index)
            if remote != local:
                report.findings.append(
                    Finding(
                        "file",
                        item.destination,
                        "missing" if remote is None else "stale",
                        f"worker {(remote or '-')[:12]} source {local[:12]}",
                        "synced with --apply (old copy backed up)",
                    )
                )
        if (
            check_model
            and self.coding_cli
            and self.model
            and found.get(self.coding_cli)
        ):
            self._check_model(worker, report)
        if report.findings and report.status == "aligned":
            report.status = "misaligned"
        return report, found

    def _check_model(self, worker: ExecutionWorker, report: WorkerReport) -> None:
        if self.coding_cli != "opencode":
            report.findings.append(
                Finding(
                    "model",
                    str(self.model),
                    "unchecked",
                    f"remote model checks support opencode, not {self.coding_cli}",
                )
            )
            return
        try:
            help_result = self._run(
                worker,
                _remote_command(worker, ("opencode", "run", "--help")),
                _PROBE_TIMEOUT_SECONDS,
            )
            if help_result.returncode:
                report.findings.append(
                    Finding(
                        "model",
                        str(self.model),
                        "failed",
                        "opencode run help probe failed before model check",
                    )
                )
                return
            # Alignment intentionally checks the worker's own provider setup.
            # V2 must launch a private server so process-bound keys are visible.
            standalone = bool(
                re.search(
                    rb"(?m)^[ \t]*--standalone(?:[ =\t,]|$)",
                    help_result.stdout + b"\n" + help_result.stderr,
                )
            )
            argv = (
                "opencode",
                "run",
                *(("--standalone",) if standalone else ()),
                "-m",
                str(self.model),
                _MODEL_PROMPT,
            )
            completed = self._run(
                worker, _remote_command(worker, argv), _MODEL_TIMEOUT_SECONDS
            )
        except SshTransportError as exc:
            report.findings.append(
                Finding("model", str(self.model), "failed", exc.message)
            )
            return
        text = re.sub(
            r"\x1b\[[0-9;]*m", "", completed.stdout.decode("utf-8", errors="replace")
        )
        if completed.returncode or not re.search(r"(?m)^\s*ok\s*$", text):
            report.findings.append(
                Finding(
                    "model",
                    str(self.model),
                    "failed",
                    f"{self.coding_cli} exited {completed.returncode} without the "
                    "expected reply",
                    "check the provider configuration and secret files, then rerun",
                )
            )

    def apply(self, worker: ExecutionWorker, report: WorkerReport) -> None:
        """Install missing declared commands and sync differing files."""

        family = worker.requirements.os_family
        missing = {
            item.name
            for item in report.findings
            if item.kind == "command" and item.state == "missing"
        }
        for install in self.user.installs:
            if install.os_family != family or install.command not in missing:
                continue
            completed = self._run(
                worker,
                _remote_command(worker, install.argv),
                _INSTALL_TIMEOUT_SECONDS,
            )
            report.applied.append(
                f"install {install.command}: exit {completed.returncode}"
            )
        platform = self.template.platforms.get(family or "")
        excluded = {
            item.name
            for item in report.findings
            if item.kind == "defender-exclusion" and item.state == "missing"
        }
        for entry in () if platform is None else platform.defender_exclusions:
            if entry not in excluded:
                continue
            completed = self._run(
                worker,
                powershell_command(
                    "Add-MpPreference -ExclusionPath "
                    f"({_exclusion_path(worker, entry)}) -ErrorAction Stop; "
                    "$global:LASTEXITCODE = 0"
                ),
                _PROBE_TIMEOUT_SECONDS,
            )
            report.applied.append(
                f"exclude {entry} from Defender: exit {completed.returncode}"
            )
        excluded_processes = {
            item.name
            for item in report.findings
            if item.kind == "defender-process-exclusion" and item.state == "missing"
        }
        for entry in () if platform is None else platform.defender_process_exclusions:
            if entry not in excluded_processes or platform is None:
                continue
            completed = self._run(
                worker,
                powershell_command(
                    f"{_process_exclusion_path(platform, entry)}; "
                    "if (-not $px) { throw 'process exclusion did not resolve' }; "
                    "Add-MpPreference -ExclusionProcess $px -ErrorAction Stop; "
                    "'excluded ' + $px; $global:LASTEXITCODE = 0"
                ),
                _PROBE_TIMEOUT_SECONDS,
            )
            excluded_path = next(
                (
                    line[len("excluded ") :].strip()
                    for line in completed.stdout.decode("utf-8", "replace").splitlines()
                    if line.startswith("excluded ")
                ),
                "",
            )
            report.applied.append(
                f"exclude process {entry}"
                + (f" ({excluded_path})" if excluded_path else "")
                + f" from Defender: exit {completed.returncode}"
            )
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        stale = {
            item.name
            for item in report.findings
            if item.kind == "file" and item.state in {"missing", "stale"}
        }
        for item in self.user.files:
            if item.destination not in stale or family not in item.os_families:
                continue
            self._sync_file(worker, item, stamp)
            report.applied.append(
                f"sync {item.destination}" + (" (secret)" if item.secret else "")
            )

    def _sync_file(self, worker: ExecutionWorker, item: AlignedFile, stamp: str):
        relative = item.destination[2:]
        backup = f"{relative}.bak-{stamp}"
        if _windows(worker):
            target = powershell_home_path(item.destination)
            # A failed backup must stop the sync before the copy overwrites it.
            prepare = powershell_command(
                f"$f = {target}; "
                "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $f) "
                "-ErrorAction Stop | Out-Null; if (Test-Path -LiteralPath $f) { "
                f"Copy-Item -LiteralPath $f -Destination "
                f"{powershell_home_path('~/' + backup)} -Force -ErrorAction Stop }}; "
                "$global:LASTEXITCODE = 0"
            )
        else:
            target = '"$HOME"/' + shlex.quote(relative)
            directory = str(Path(relative).parent)
            parent = '"$HOME"/' + shlex.quote(directory)
            prepare = (
                f"set -e; mkdir -p {parent}; "
                # Restrict a secret's own directory, never $HOME itself.
                + (f"chmod 700 {parent}; " if item.secret and directory != "." else "")
                + f"if [ -f {target} ]; then cp -p {target} "
                + '"$HOME"/'
                + shlex.quote(backup)
                + "; fi"
            )
        if self._run(worker, prepare, _PROBE_TIMEOUT_SECONDS).returncode:
            raise WorkerAlignmentError(
                "worker_alignment.sync_failed",
                f"cannot prepare {item.destination} on {worker.worker_id}",
            )
        assert worker.endpoint is not None
        copied = self.runner_factory().run(
            scp_arguments(
                item.source,
                worker.endpoint,
                relative,
                _PROBE_TIMEOUT_SECONDS,
                transport=worker.transport,
            ),
            cwd=self.cwd,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        if copied.returncode:
            raise WorkerAlignmentError(
                "worker_alignment.sync_failed",
                f"cannot copy {item.destination} to {worker.worker_id}",
            )
        if not item.secret:
            return
        if _windows(worker):
            # Owner-only: drop inherited ACEs and grant only the current user.
            restrict = powershell_command(
                "; ".join(
                    f"if (Test-Path -LiteralPath ({path})) {{ icacls ({path}) "
                    '/inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null; '
                    "if ($LASTEXITCODE) { exit $LASTEXITCODE } }"
                    for path in (
                        powershell_home_path(item.destination),
                        powershell_home_path("~/" + backup),
                    )
                )
                + "; $global:LASTEXITCODE = 0"
            )
        else:
            restrict = (
                f'chmod 600 "$HOME"/{shlex.quote(relative)} && '
                f'{{ [ ! -e "$HOME"/{shlex.quote(backup)} ] || '
                f'chmod 600 "$HOME"/{shlex.quote(backup)}; }}'
            )
        if self._run(worker, restrict, _PROBE_TIMEOUT_SECONDS).returncode:
            raise WorkerAlignmentError(
                "worker_alignment.sync_failed",
                f"cannot restrict {item.destination} to its owner on "
                f"{worker.worker_id}",
            )

    def align(
        self,
        workers: Sequence[ExecutionWorker],
        *,
        apply: bool,
        check_model: bool = True,
    ) -> dict[str, object]:
        def one(worker: ExecutionWorker) -> dict[str, object]:
            report, _ = self.inspect(worker, check_model=check_model)
            if apply and report.status == "misaligned":
                try:
                    self.apply(worker, report)
                except (WorkerAlignmentError, SshTransportError) as exc:
                    report.applied.append(f"failed: {exc}")
                applied = report.applied
                # Report what is true after the changes, not what was planned.
                report, _ = self.inspect(worker, check_model=check_model)
                report.applied = applied
            return report.to_dict()

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(workers)))) as pool:
            reports = list(pool.map(one, workers))
        return {
            "schema": REPORT_SCHEMA,
            "applied": apply,
            "coding_cli": self.coding_cli,
            "model": self.model,
            "aligned": all(item["status"] == "aligned" for item in reports),
            "workers": reports,
        }
