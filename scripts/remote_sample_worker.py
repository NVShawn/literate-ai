"""Bootstrap an isolated matrix checkout and run selected samples on one host."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform-flavor", required=True, choices=("linux", "macos", "windows")
    )
    parser.add_argument("--sample", action="append", default=[], metavar="GLOB")
    parser.add_argument("--flavor", action="append", default=[], metavar="SELECTOR")
    parser.add_argument(
        "--coding-cli", choices=("codex", "claude", "cursor-agent", "opencode")
    )
    parser.add_argument("--model")
    return parser


def _forwarded_live_arguments(
    coding_cli: str | None, model: str | None
) -> tuple[str, ...]:
    if (coding_cli is None) != (model is None):
        raise SystemExit("remote live selection requires both coding CLI and model")
    if coding_cli is None or model is None:
        return ()
    return ("--coding-cli", coding_cli, "--model", model)


def _windows_bazel_shell(environment: dict[str, str]) -> str:
    normalized = {key.casefold(): value for key, value in environment.items()}

    def value(name: str, default: str = "") -> str:
        return normalized.get(name.casefold(), default)

    configured = value("BAZEL_SH").strip()
    if configured:
        path = Path(configured)
        if path.is_absolute() and path.is_file():
            return str(path.resolve())
        raise SystemExit("BAZEL_SH must name an existing absolute bash.exe")

    candidates: list[Path] = []
    discovered = shutil.which("bash.exe", path=value("PATH"))
    if discovered:
        candidates.append(Path(discovered))
    for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = value(name).strip()
        if root:
            candidates.extend(
                (
                    Path(root) / "Git" / "bin" / "bash.exe",
                    Path(root) / "Git" / "usr" / "bin" / "bash.exe",
                )
            )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise SystemExit(
        "Windows Bazel tests require bash.exe; install Git for Windows or set "
        "BAZEL_SH to its absolute path"
    )


def _flavor_arguments(selectors: list[str]) -> tuple[str, ...]:
    return tuple(f"--flavor={selector}" for selector in selectors)


def _managed_python(environment: Path) -> Path:
    return environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )


def _usable_environment(environment: Path) -> bool:
    python = _managed_python(environment)
    if not python.is_file():
        return False
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import pip,sys; raise SystemExit(sys.prefix == sys.base_prefix)",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ,
    )
    return completed.returncode == 0


def _run_bootstrap(
    *,
    python: Path,
    bootstrap: Path,
    platform: str,
    coding_cli: str | None,
    flavors: tuple[str, ...] = (),
) -> dict[str, object]:
    command = [
        str(python),
        str(bootstrap),
        "--profile",
        "sample-worker",
        "--platform",
        platform,
        "--install-missing",
    ]
    if coding_cli is not None:
        command.extend(("--coding-cli", coding_cli))
    for flavor in flavors:
        command.extend(("--flavor", flavor))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    report: object | None = None
    report_index: int | None = None
    output_lines = completed.stdout.splitlines()
    for index in range(len(output_lines) - 1, -1, -1):
        try:
            candidate = json.loads(output_lines[index])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("schema") == "literate-ai/host-bootstrap-report@2"
        ):
            report = candidate
            report_index = index
            break
    if report is None or report_index is None:
        raise SystemExit("host bootstrap did not emit its required JSON report")
    diagnostic_lines = output_lines[:report_index] + output_lines[report_index + 1 :]
    if diagnostic_lines:
        print("\n".join(diagnostic_lines), file=sys.stderr)
    if (
        completed.returncode != 0
        or not isinstance(report, dict)
        or not report.get("passed")
    ):
        raise SystemExit("host bootstrap did not satisfy its selected Flavor closure")
    path_entries = report.get("path_entries")
    if not isinstance(path_entries, list) or any(
        not isinstance(item, str) or not item for item in path_entries
    ):
        raise SystemExit("host bootstrap emitted invalid path entries")
    os.environ["PATH"] = os.pathsep.join((*path_entries, os.environ.get("PATH", "")))
    return report


def main() -> int:
    args = _parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    if root.name != "literate-ai":
        raise SystemExit("remote checkout must be isolated in a literate-ai directory")
    bootstrap = (
        root / "skills" / "agent" / "detect-before-install" / "scripts" / "bootstrap.py"
    )
    base_report = _run_bootstrap(
        python=Path(sys.executable),
        bootstrap=bootstrap,
        platform=args.platform_flavor,
        coding_cli=args.coding_cli,
    )
    build_dir = Path(os.environ.get("BUILD_DIR", root / "generated")).resolve()
    object_dir = Path(os.environ.get("OBJ_DIR", root / "_build")).resolve()
    os.environ["BUILD_DIR"] = str(build_dir)
    os.environ["OBJ_DIR"] = str(object_dir)
    if args.platform_flavor == "windows":
        os.environ["BAZEL_SH"] = _windows_bazel_shell(dict(os.environ))
    environment = Path(
        os.environ.get(
            "LITAI_REMOTE_PYTHON_ENV", root.parent / "runtime" / "python-env"
        )
    ).resolve()
    if environment.exists() and not _usable_environment(environment):
        if environment.is_symlink() or not environment.is_dir():
            raise SystemExit("managed worker Python environment is unsafe")
        shutil.rmtree(environment)
    if not environment.exists():
        environment.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = _managed_python(environment)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-e", str(root)],
        check=True,
        env=os.environ,
    )
    plan_command = [
        str(python),
        str(root / "scripts" / "plan_sample_host_toolchains.py"),
        "--platform",
        args.platform_flavor,
    ]
    for pattern in args.sample or ["regenerative-roundtrip"]:
        plan_command.extend(("--sample", pattern))
    for selector in args.flavor:
        plan_command.extend(("--flavor", selector))
    planned = subprocess.run(
        plan_command,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    plan = json.loads(planned.stdout)
    flavor_values = plan.get("flavors")
    if not isinstance(flavor_values, list) or any(
        not isinstance(item, str) or not item for item in flavor_values
    ):
        raise SystemExit("sample host-toolchain plan is invalid")
    flavor_names = tuple(flavor_values)
    selected_report = _run_bootstrap(
        python=python,
        bootstrap=bootstrap,
        platform=args.platform_flavor,
        coding_cli=args.coding_cli,
        flavors=flavor_names,
    )
    (root.parent / "host-bootstrap.json").write_text(
        json.dumps(
            {
                "schema": "literate-ai/host-bootstrap-stages@1",
                "base": base_report,
                "plan": plan,
                "selected": selected_report,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = root.parent / "runtime"
    command = [
        str(python),
        str(root / "scripts" / "run_samples.py"),
        "--runtime-root",
        str(runtime),
        "--allow-host-execution",
        "--expected-platform",
        args.platform_flavor,
        *_forwarded_live_arguments(args.coding_cli, args.model),
    ]
    for pattern in args.sample or ["regenerative-roundtrip"]:
        command.extend(("--sample", pattern))
    command.extend(_flavor_arguments(args.flavor))
    return subprocess.run(command, cwd=root, check=False, env=os.environ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
