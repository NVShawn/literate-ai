"""Install a private Literate AI runtime and stable prefix-relative launcher."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from literate_ai.adapters.host_install import (  # noqa: E402
    HostInstallError,
    apply_host_install_path,
    ensure_host_install_dependencies,
)
from literate_ai.adapters.host_self_update import (  # noqa: E402
    write_host_install_manifest,
)
from literate_ai.adapters.user_paths import (  # noqa: E402
    HostInstallLayout,
    resolve_host_paths,
)
from literate_ai.bootstrap.host_install_requirements import (  # noqa: E402
    HOST_INSTALL_SBOM_RELATIVE_PATH,
)


def _install_environment() -> dict[str, str]:
    """Keep repository and package-manager policy out of the private runtime.

    ``make`` deliberately redirects repository bytecode through
    ``PYTHONPYCACHEPREFIX``.  Letting that setting reach ``pip install`` makes pip
    record private-runtime bytecode beneath the repository object directory, so the
    installed distribution is neither self-contained nor portable.  Python startup
    hooks can similarly select code outside the host-install contract. Package-index
    and proxy policy remains ambient: unlike offline worker bootstrap, this host
    installer is allowed to use the operator's configured Python package source.
    """

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.casefold().startswith("python")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run(*arguments: str) -> None:
    completed = subprocess.run(
        arguments,
        check=False,
        env=_install_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"installation command {Path(arguments[0]).name!r} failed with status "
            f"{completed.returncode}"
        )


def _require_clean_git_checkout(source: Path) -> None:
    """Fail fast with an actionable message before a doomed, noisy pip build.

    ``tools/build_backend/literate_ai_build_backend.py`` refuses to build a
    wheel from a dirty Git checkout (it must embed an exact, reproducible
    repository URL and revision), but that refusal happens deep inside a pip
    subprocess and surfaces only as a generic "installation command 'python'
    failed with status 1" wrapped around a buried traceback. Check the same
    condition here, first, so installing from an in-progress checkout tells
    the operator exactly what to do instead of a script failure to diagnose.
    """

    if not (source / ".git").exists():
        return
    status = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        # Not a working Git checkout (bare repo, corrupt worktree, git
        # unavailable, ...) -- let the pip build itself decide what to do.
        return
    if status.stdout.strip():
        raise RuntimeError(
            "Literate AI source has uncommitted or untracked changes:\n"
            f"{status.stdout}"
            "\nCommit or stash them before installing from this checkout (a "
            "distribution build from Git requires a clean, exact revision), "
            "or install from a clean clone or source tarball instead."
        )


def _venv_python_is_usable(python: Path) -> bool:
    """Probe whether a venv's interpreter can actually run.

    A venv's own ``pyvenv.cfg`` pins it to the exact base interpreter path it
    was created from. If that base interpreter is later removed or replaced
    (e.g. a Homebrew Python point-release upgrade prunes the old Cellar
    version), the venv's ``python`` binary still exists on disk but dies at
    dyld load time. ``python.is_file()`` alone can't tell a live venv from
    this kind of orphaned one, so run it instead.
    """

    try:
        probe = subprocess.run(
            (str(python), "-c", "import sys"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return probe.returncode == 0


def install(*, prefix: Path, source: Path) -> dict[str, str]:
    prefix = prefix.expanduser().resolve()
    source = source.resolve()
    if not (source / "pyproject.toml").is_file():
        raise RuntimeError(f"Literate AI source has no pyproject.toml: {source}")
    _require_clean_git_checkout(source)

    layout = HostInstallLayout.for_prefix(prefix)
    environment = Path(layout.environment)
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    if python.is_file() and not _venv_python_is_usable(python):
        print(
            f"install_litai: existing runtime at {environment} is unusable "
            "(its interpreter no longer runs, likely because the base Python "
            "it was created from was removed); recreating it",
            file=sys.stderr,
        )
        shutil.rmtree(environment)
    if not python.is_file():
        environment.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    _run(
        str(python),
        "-m",
        "pip",
        "--disable-pip-version-check",
        "install",
        "--upgrade",
        "--force-reinstall",
        str(source),
    )

    bin_dir = Path(layout.launcher).parent
    bin_dir.mkdir(parents=True, exist_ok=True)
    repository_scripts = source / "scripts"
    if sys.platform == "win32":
        launcher = Path(layout.launcher)
        shutil.copy2(repository_scripts / "litai-launcher.cmd", launcher)
    else:
        launcher = Path(layout.launcher)
        shutil.copy2(repository_scripts / "litai-launcher", launcher)
        launcher.chmod(0o755)
    write_host_install_manifest(
        Path(layout.manifest),
        prefix=prefix,
        environment=environment,
        launcher=launcher,
    )
    return {
        "prefix": str(prefix),
        "environment": str(environment),
        "launcher": str(launcher),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix", type=Path, default=Path(resolve_host_paths().install_root)
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--host-sbom-root",
        type=Path,
        help="override the repository's tuple-specific host-install SBOM directory",
    )
    parser.add_argument(
        "--host-dependencies-only",
        action="store_true",
        help="verify or install declared native dependencies without installing litai",
    )
    arguments = parser.parse_args()
    source = arguments.source.resolve()
    sbom_root = arguments.host_sbom_root
    if sbom_root is None:
        sbom_root = source.joinpath(*HOST_INSTALL_SBOM_RELATIVE_PATH.parts)
    try:
        host_report = ensure_host_install_dependencies(sbom_root=sbom_root)
        apply_host_install_path(host_report)
        if arguments.host_dependencies_only:
            print(json.dumps(host_report.to_dict(), sort_keys=True))
            return 0
        result = install(prefix=arguments.prefix, source=source)
    except (HostInstallError, RuntimeError) as exc:
        print(f"install_litai: {exc}", file=sys.stderr)
        return 2
    result["host_dependency_sbom"] = str(host_report.sbom_path)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
