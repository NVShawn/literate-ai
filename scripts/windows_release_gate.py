#!/usr/bin/env python3
"""Run the Windows release gate natively, without make or a POSIX shell.

`make release-check` needs a POSIX shell, which Windows workers do not have. This
gate runs exactly the steps hosted CI runs for Windows (the `windows-gates` and
`windows-tests` jobs in `.github/workflows/ci.yml`), so a Windows worker can
provide the same platform evidence. It installs into an isolated virtual
environment under `_build/` and never installs system tools: a missing tool is a
preflight failure naming what the worker operator must provide.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPORARY = ROOT.with_name(ROOT.name + ".tmp")
STEPS = ("preflight", "environment", "compile", "lint", "openspec", "wheel", "tests")
_COMPILED = (
    "tests/conformance/support/sample_runner.py",
    "scripts/run_samples.py",
    "tests/conformance/support/self_hosting_proof.py",
    "scripts/run_source_to_specification_fixtures.py",
)
_TEST_ROOTS = ("tests/e2e", "tests/smoke", "tests/critical", "tests/conformance")
_ADMITTED_RECEIVER_TEST = (
    "tests/smoke/test_remote_standard_toolchains.py::RemoteStandardToolchainTests::"
    "test_actual_admitted_receiver_supplies_discovery_and_live_profile_guard"
)


class GateFailure(RuntimeError):
    pass


def _windows() -> bool:
    return os.name == "nt"


def _environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if _windows() else "bin/python")


def _template_commands() -> list[dict[str, object]]:
    """This host's required commands from the repository worker template.

    `literate.worker-template.json` is the one declaration of worker
    prerequisites; `litai worker align` reads the same entries.
    """

    family = {"nt": "windows"}.get(os.name) or (
        "macos" if sys.platform == "darwin" else "linux"
    )
    template = json.loads((ROOT / "literate.worker-template.json").read_text())
    return [
        item if isinstance(item, dict) else {"name": item}
        for item in template["platforms"][family]["commands"]
    ]


def _fallback_tool_directories(commands: list[dict[str, object]]) -> list[str]:
    """Directories appended after PATH, so they never shadow system tools."""

    if not _windows():
        return []
    return [
        str(directory)
        for item in commands
        for directory in item.get("fallback_paths", [])  # type: ignore[union-attr]
        if Path(str(directory)).is_dir()
    ]


def _native_tool_directories(commands: list[dict[str, object]]) -> list[str]:
    """Directories, off PATH, where the template says required tools live."""

    if not _windows():
        return []
    candidates = [
        Path(str(directory))
        for item in commands
        for directory in item.get("extra_paths", [])  # type: ignore[union-attr]
    ]
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files) / "Microsoft Visual Studio/Installer/vswhere.exe"
    for item in commands:
        pattern = item.get("vswhere")
        if not pattern or not vswhere.is_file():
            continue
        found = subprocess.run(
            (str(vswhere), "-latest", "-products", "*", "-find", str(pattern)),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        if found:
            candidates.append(Path(found[0]).parent)
    return [str(item) for item in candidates if item.is_dir()]


def _run(argv: list[str], environment: dict[str, str], *, step: str) -> None:
    print(f"[windows-release-gate] {step}: {' '.join(argv)}", flush=True)
    started = time.monotonic()
    completed = subprocess.run(argv, cwd=ROOT, env=environment, check=False)
    elapsed = round(time.monotonic() - started, 1)
    if completed.returncode:
        raise GateFailure(
            f"{step} failed with exit status {completed.returncode} after {elapsed}s"
        )
    print(f"[windows-release-gate] {step}: passed in {elapsed}s", flush=True)


def _test_commands(
    python: Path,
    test_workers: str,
    *,
    temporary: Path | None = None,
) -> tuple[list[str], list[str]]:
    reports = temporary if temporary is not None else ROOT / "_build"
    test_temporary = temporary if temporary is not None else TEMPORARY
    # Match hosted CI: qualify the unchanged 60-second dependency observation
    # without contention, then run every other test once with the usual workers.
    return (
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "-rA",
            "-o",
            "addopts=",
            f"--junitxml={reports / 'windows-admitted-receiver.xml'}",
            _ADMITTED_RECEIVER_TEST,
        ],
        # A fixed base directory avoids pytest-current symlink cleanup failures.
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "-n",
            "0" if test_workers == "1" else test_workers,
            "--dist=loadfile",
            "--maxfail=1",
            f"--deselect={_ADMITTED_RECEIVER_TEST}",
            f"--basetemp={test_temporary / 'pytest'}",
            f"--junitxml={reports / 'windows-tests.xml'}",
            *_TEST_ROOTS,
        ],
    )


def _run_tests(
    python: Path,
    test_workers: str,
    environment: dict[str, str],
    *,
    temporary: Path | None = None,
) -> None:
    for command in _test_commands(python, test_workers, temporary=temporary):
        _run(command, environment, step="tests")


def _run_parallel_checks(
    commands: tuple[tuple[str, list[str]], ...],
    environment: dict[str, str],
    temporary: Path,
) -> None:
    """Run independent checks with owned trees and separate durable streams."""

    def tail(path: Path, limit: int) -> bytes:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read(limit)

    def log_record(path: Path) -> dict[str, object]:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"path": path.name, "bytes": path.stat().st_size, "sha256": digest}

    source = str(Path(__file__).resolve().parents[1] / "src")
    sys.path.insert(0, source)
    try:
        from literate_ai.adapters._processes import (
            create_process_tree_ownership,
            terminate_process_tree,
        )
    finally:
        sys.path.pop(0)
    jobs = []
    started = time.monotonic()
    with ExitStack() as streams:
        try:
            for step, command in commands:
                owner = create_process_tree_ownership()
                stdout = temporary / f"{step}.stdout.log"
                stderr = temporary / f"{step}.stderr.log"
                job = {
                    "step": step,
                    "owner": owner,
                    "process": None,
                    "started": time.monotonic(),
                    "stdout": stdout,
                    "stderr": stderr,
                }
                jobs.append(job)
                print(
                    f"[windows-release-gate] {step}: {' '.join(command)}",
                    flush=True,
                )
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=streams.enter_context(stdout.open("wb")),
                        stderr=streams.enter_context(stderr.open("wb")),
                        **owner.popen_options,
                    )
                except OSError as exc:
                    raise GateFailure(f"{step} could not start: {exc}") from exc
                job["process"] = process
                owner.bind(process.pid)
            pending = list(jobs)
            while pending:
                for job in tuple(pending):
                    status = job["process"].poll()
                    if status is None:
                        continue
                    job["seconds"] = time.monotonic() - job["started"]
                    pending.remove(job)
                    if status:
                        detail = (
                            tail(job["stderr"], 2000) + tail(job["stdout"], 5000)
                        ).decode("utf-8", errors="replace")
                        raise GateFailure(
                            f"{job['step']} failed with exit status {status}; "
                            f"retained logs in {temporary}\n{detail}"
                        )
                    print(
                        f"[windows-release-gate] {job['step']}: "
                        f"passed in {job['seconds']:.1f}s; logs in {temporary}",
                        flush=True,
                    )
                if pending:
                    time.sleep(0.1)
        finally:
            pending_error = sys.exception()
            cleanup_errors = []
            for job in jobs:
                process = job["process"]
                try:
                    if process is not None:
                        # Also collect descendants of an already exited root.
                        terminate_process_tree(
                            process, environment=environment, ownership=job["owner"]
                        )
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                except Exception as exc:
                    cleanup_errors.append(f"{job['step']}: {type(exc).__name__}: {exc}")
                finally:
                    try:
                        job["owner"].release()
                    except Exception as exc:
                        cleanup_errors.append(
                            f"{job['step']} ownership: {type(exc).__name__}: {exc}"
                        )
            try:
                streams.close()
            except OSError as exc:
                cleanup_errors.append(f"log close: {exc}")
            results = []
            for job in jobs:
                process = job["process"]
                results.append(
                    {
                        "step": job["step"],
                        "exit": None if process is None else process.returncode,
                        "seconds": job.get(
                            "seconds", time.monotonic() - job["started"]
                        ),
                        "logs": {
                            name: log_record(job[name])
                            for name in ("stdout", "stderr")
                            if job[name].is_file()
                        },
                    }
                )
            (temporary / "parallel-verification.json").write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/windows-parallel-verification@1",
                        "passed": bool(results)
                        and len(results) == len(commands)
                        and all(item["exit"] == 0 for item in results)
                        and not cleanup_errors,
                        "cleanup_errors": cleanup_errors,
                        "seconds": time.monotonic() - started,
                        "steps": results,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if cleanup_errors:
                detail = "; ".join(cleanup_errors)
                if pending_error is not None:
                    detail = f"{pending_error}; cleanup: {detail}"
                raise GateFailure(f"parallel verification cleanup failed: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        choices=STEPS,
        help="run only these steps (diagnostics; a release requires every step)",
    )
    parser.add_argument(
        "--environment",
        type=Path,
        default=ROOT / "_build/python-envs/windows-release-gate",
        help="isolated virtual environment for the gate",
    )
    parser.add_argument(
        "--test-workers",
        default=os.environ.get("PYTHON_TEST_WORKERS", "auto"),
        help="pytest processes: auto (CPU count) or a positive integer; 1 is serial",
    )
    args = parser.parse_args(argv)
    if args.test_workers != "auto" and (
        not args.test_workers.isascii()
        or not args.test_workers.isdecimal()
        or int(args.test_workers) < 1
    ):
        parser.error("--test-workers must be auto or a positive integer")
    selected = tuple(args.only or STEPS)
    environment = dict(os.environ)
    commands = _template_commands()
    environment["PATH"] = os.pathsep.join(
        [
            str(ROOT / "tools/openspec/node_modules/.bin"),
            *_native_tool_directories(commands),
            environment.get("PATH", ""),
            *_fallback_tool_directories(commands),
        ]
    )
    environment.pop("PYTHONPATH", None)
    temporary = None
    if _windows():
        # Keep smoke and test trees beside the checkout, under the worker
        # workspace that the template excludes from Defender scanning, but
        # outside the checkout: project discovery walks up from test fixtures
        # and would otherwise adopt this repository's own project manifest.
        # Each invocation starts empty without deleting an earlier run's failed
        # fixtures or evidence. Keep names short for Windows path limits.
        temporary = Path(tempfile.mkdtemp(prefix="wg-", dir=ROOT.parent))
        print(
            f"[windows-release-gate] retained temporary evidence: {temporary}",
            flush=True,
        )
        environment["TEMP"] = environment["TMP"] = str(temporary)
        environment["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
            item
            for item in (str(temporary), environment.get("GIT_CEILING_DIRECTORIES"))
            if item
        )
    python = _environment_python(args.environment.resolve())
    npm = shutil.which("npm.cmd" if _windows() else "npm", path=environment["PATH"])
    try:
        if "preflight" in selected:
            required = [str(item["name"]) for item in commands]
            missing = [
                tool
                for tool in required
                if shutil.which(tool, path=environment["PATH"]) is None
            ]
            if missing:
                raise GateFailure(
                    "required tools are unavailable on PATH: " + ", ".join(missing)
                )
            print("[windows-release-gate] preflight: passed", flush=True)
        if "environment" in selected:
            if not python.is_file():
                _run(
                    [sys.executable, "-m", "venv", str(args.environment)],
                    environment,
                    step="environment",
                )
            _run(
                [str(python), "-m", "pip", "install", "-q", "-e", ".[dev]"],
                environment,
                step="environment",
            )
            if npm is None:
                raise GateFailure("npm is unavailable on PATH")
            _run(
                [npm, "--prefix", "tools/openspec", "ci"],
                environment,
                step="environment",
            )
        if set(selected) - {"preflight", "environment"} and not python.is_file():
            raise GateFailure("gate environment is missing; run the environment step")
        if "compile" in selected:
            _run(
                [str(python), "-m", "compileall", "-q", "src", "tests"],
                environment,
                step="compile",
            )
            _run(
                [str(python), "-m", "py_compile", *_COMPILED],
                environment,
                step="compile",
            )
        if "lint" in selected:
            for arguments in (
                ["check", "src", "tests", "scripts"],
                ["format", "--check", "src", "tests", "scripts"],
            ):
                _run([str(python), "-m", "ruff", *arguments], environment, step="lint")
        if "openspec" in selected:
            if npm is None:
                raise GateFailure("npm is unavailable on PATH")
            for script in ("openspec:check", "documentation:check"):
                _run(
                    [npm, "--prefix", "tools/openspec", "run", script],
                    environment,
                    step="openspec",
                )
        wheel_command = None
        if "wheel" in selected:
            timing_root = ROOT / "_build"
            timing_root.mkdir(parents=True, exist_ok=True)
            timing_report = (
                Path(tempfile.mkdtemp(prefix="wg-", dir=timing_root))
                / "wheel-timings.json"
            )
            wheel_command = [
                str(python),
                "scripts/wheel_smoke.py",
                "--timing-report",
                str(timing_report),
            ]
        if wheel_command is not None and "tests" in selected:
            serial, remaining = _test_commands(
                python, args.test_workers, temporary=temporary
            )
            # The live deadline control completes without either competing ladder.
            _run(serial, environment, step="tests")
            _run_parallel_checks(
                (("wheel", wheel_command), ("tests", remaining)),
                environment,
                temporary,
            )
        elif wheel_command is not None:
            _run(wheel_command, environment, step="wheel")
        elif "tests" in selected:
            _run_tests(python, args.test_workers, environment, temporary=temporary)
    except GateFailure as exc:
        print(f"[windows-release-gate] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    print("[windows-release-gate] all selected steps passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
