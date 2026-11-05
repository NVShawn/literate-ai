"""Real noneditable public Rust library journey; fixture inputs are not grants.

Run with the actual installed prefix Python -I -B. The repository root supplies
test authoring helpers only; no source production directory is added to imports.
Missing provisioned native tools/crates report an explicit unqualified cell.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path


def main():
    if not __debug__ or not sys.flags.isolated or len(sys.argv) != 3:
        raise RuntimeError("isolated installed Python, fresh root and exact F required")
    import literate_ai
    from literate_ai.adapters.standard_lifecycle_binding import (
        observe_installed_framework_distribution,
    )
    from literate_ai.remote_worker_bootstrap import installed_distribution_identity

    prefix = Path(sys.prefix).resolve(strict=True)
    expected = sys.argv[2]

    def require_installed():
        if (
            not Path(literate_ai.__file__).resolve(strict=True).is_relative_to(prefix)
            or installed_distribution_identity(prefix) != expected
            or observe_installed_framework_distribution().identity.uri != expected
        ):
            raise RuntimeError("exact noneditable installed framework required")
        for name, module in tuple(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if name.startswith("literate_ai.") and filename:
                if not Path(filename).resolve(strict=True).is_relative_to(prefix):
                    raise RuntimeError("source framework import refused")

    require_installed()
    supplied = Path(sys.argv[1]).absolute()
    if os.path.lexists(supplied):
        raise RuntimeError("preserve previous installed Rust fixture")
    root = supplied.parent.resolve(strict=True) / supplied.name
    if os.name != "posix" or not shutil.which("make"):
        print(
            json.dumps(
                {
                    "qualified": False,
                    "reason": "POSIX external-agent fixture unavailable",
                }
            )
        )
        return
    if not shutil.which("cargo") or not shutil.which("rustc"):
        print(
            json.dumps({"qualified": False, "reason": "native Cargo/Rust unavailable"})
        )
        return
    # This exposes qualified TEST SUPPORT only. Every framework import is already
    # installed and is checked again after the actual public lifecycle completes.
    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository))
    from tests.support.fixtures_public_rust_harness import author_public_project
    from tests.support.fixtures_rust_harness_vendor import CRATES
    from tests.support.fixtures_rust_library_harness import prepare_seed

    require_installed()
    cache = Path.home() / ".cargo/registry/cache"
    archives = None
    if cache.is_dir():
        with os.scandir(cache) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 64:
                    raise RuntimeError(
                        "bounded explicit fixture cache selection required"
                    )
                if entry.is_dir(follow_symlinks=False):
                    candidate = Path(entry.path)
                    if all(
                        (candidate / (name + ".crate")).is_file() for name in CRATES
                    ):
                        archives = candidate
                        break
    if archives is None:
        print(
            json.dumps(
                {
                    "qualified": False,
                    "reason": "exact provisioned upstream crates unavailable",
                }
            )
        )
        return
    from literate_ai.adapters.builders._process import run_bounded_process
    from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
    from literate_ai.adapters.builders.rust import discover_rust_toolchain
    from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
    from literate_ai.projects import PinnedInputClosure

    values = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "PATH",
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "HOME",
            "USERPROFILE",
            "TMPDIR",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "NO_COLOR",
        }
    }
    # Probes select an already installed toolchain; they do not provision one.
    observed_rust = discover_rust_toolchain({**values, "RUSTUP_AUTO_INSTALL": "0"})
    native_rust = Path(observed_rust.compiler_executable)
    native_cargo = native_rust.with_name("cargo")
    if not native_cargo.is_file():
        print(
            json.dumps(
                {"qualified": False, "reason": "direct native profile unavailable"}
            )
        )
        return
    cargo = LocalComponentToolBinding.from_observed_toolchain(
        discover_cargo_toolchain(values, pinned_command=(str(native_cargo),))
    )
    rustc = LocalComponentToolBinding.from_observed_toolchain(
        discover_rust_toolchain({**values, "RUSTC": shlex.quote(str(native_rust))})
    )
    root.mkdir(mode=0o700)
    (root / "home").mkdir(mode=0o700)
    (root / "seed").mkdir(mode=0o700)
    values.update(
        CARGO=shlex.quote(cargo.command[0]),
        RUSTC=shlex.quote(rustc.command[0]),
        BUILD_DIR=str(root / "build"),
        OBJ_DIR=str(root / "obj"),
        CODING_CLI="claude",
        LITAI_STATE_DIR=str(root / "state"),
        LITAI_CONFIG_DIR=str(root / "config"),
        HOME=str(root / "home"),
        USERPROFILE=str(root / "home"),
    )
    prepare_seed(root / "seed", archives, cargo, values)
    evidence = root / "cli-evidence"
    evidence.mkdir(mode=0o700)
    calls = []

    def invoke(arguments):
        calls.append(tuple(arguments))
        record = evidence / f"call-{len(calls)}"
        result = None
        error = None
        try:
            with record.with_suffix(".stdout").open("xb", buffering=0) as output:
                result = run_bounded_process(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-m",
                        "literate_ai.cli",
                        "--json",
                        *arguments,
                    ],
                    cwd=root,
                    environment=values,
                    timeout_seconds=600,
                    stdout_limit_bytes=8 * 1024**2,
                    stderr_limit_bytes=4 * 1024**2,
                    terminate_descendants=True,
                    stdout_sink=output,
                    trace=False,
                    error_prefix="installed.rust-library-public-cli",
                )
        except BaseException as exc:
            error = {"type": type(exc).__name__, "detail": str(exc)[:4096]}
            raise
        finally:
            if result is not None:
                record.with_suffix(".stderr").write_bytes(result.stderr)
            record.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "arguments": arguments,
                        "exit": None if result is None else result.returncode,
                        "error": error,
                        "stderr_returned": result is not None,
                    }
                )
            )
        # Canonical stdout sinks intentionally return empty result.stdout.
        captured = PinnedInputClosure(
            maximum_files=1,
            maximum_file_bytes=8 * 1024**2,
            maximum_total_bytes=8 * 1024**2,
        ).pin(
            record.with_suffix(".stdout"), boundary=evidence, label="actual-cli-stdout"
        )
        raw = captured or result.stderr
        return result.returncode, json.loads(raw)

    # With no synthetic driver/origin arguments the helper MUST use the actual
    # installed initialization observers. Source-only observation seams are not
    # accepted for this journey.
    project, component, fixture, tools = author_public_project(
        root,
        root / "seed",
        cargo,
        rustc,
        invoke=invoke,
    )
    values["PATH"] = str(tools) + os.pathsep + values.get("PATH", "")
    status, result = invoke(
        [
            "rebuild",
            component.relative_to(project).as_posix(),
            "--project",
            str(project),
            "--allow-host-execution",
            "--update-receipt",
        ]
    )
    if status != 0 or result.get("ok") is not True:
        raise RuntimeError(
            "actual installed public Rust rebuild refused; retained raw evidence"
        )
    if (
        result["result"].get("passed") is not True
        or result["result"].get("receipt_committed") is not True
        or not (project / "verification/current.json").is_file()
    ):
        raise RuntimeError("actual installed acceptance and committed receipt required")
    require_installed()
    receipt_inputs = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=8 * 1024**2,
        maximum_total_bytes=8 * 1024**2,
    )
    receipt_bytes = receipt_inputs.pin(
        project / "verification/current.json",
        boundary=project,
        label="actual-installed-Rust-receipt",
    )
    receipt_inputs.require_unchanged()
    print(
        json.dumps(
            {
                "qualified": True,
                "scope": "installed-public-library-offline-serde-json",
                "framework_distribution": expected,
                "public_cli_calls": len(calls),
                "native_cargo_identity": cargo.toolchain_identity.uri,
                "native_rust_identity": rustc.toolchain_identity.uri,
                "oracle_identity": fixture.oracle.identity.uri,
                "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "receipt_committed": True,
                "synthetic_framework_observers": False,
                "ordinary_rustup_isolated_home": "not-qualified",
                "whole_ADR0056": "not-qualified",
            }
        )
    )


if __name__ == "__main__":
    main()
