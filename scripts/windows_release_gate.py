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
import json
import os
import shutil
import subprocess
import sys
import time
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
    args = parser.parse_args(argv)
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
    if _windows():
        # Keep smoke and test trees beside the checkout, under the worker
        # workspace that the template excludes from Defender scanning, but
        # outside the checkout: project discovery walks up from test fixtures
        # and would otherwise adopt this repository's own project manifest.
        # Each gate starts empty; git never discovers this checkout from them.
        temporary = TEMPORARY
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True, exist_ok=True)
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
        if "wheel" in selected:
            _run([str(python), "scripts/wheel_smoke.py"], environment, step="wheel")
        if "tests" in selected:
            # A fixed base directory keeps pytest from maintaining numbered
            # directories behind a `pytest-current` symlink, whose session-end
            # cleanup raised WinError 1463 on the worker and hid the summary.
            # The JUnit report keeps per-test results even if the session dies.
            _run(
                [
                    str(python),
                    "-m",
                    "pytest",
                    "-q",
                    f"--basetemp={TEMPORARY / 'pytest'}",
                    f"--junitxml={ROOT / '_build/windows-tests.xml'}",
                    *_TEST_ROOTS,
                ],
                environment,
                step="tests",
            )
    except GateFailure as exc:
        print(f"[windows-release-gate] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    print("[windows-release-gate] all selected steps passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
