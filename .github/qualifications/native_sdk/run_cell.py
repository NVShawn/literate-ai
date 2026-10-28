"""Qualify a source-bound installed wheel on a native FreeBSD runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path


def command(name: str, argv: list[str], output: Path, timeout: int) -> None:
    started = time.monotonic()
    terminal = {"exit_code": None, "timed_out": False}
    try:
        with (
            (output / f"{name}.stdout").open("wb") as stdout,
            (output / f"{name}.stderr").open("wb") as stderr,
        ):
            result = subprocess.run(
                argv, stdout=stdout, stderr=stderr, timeout=timeout, check=False
            )
        terminal["exit_code"] = result.returncode
    except subprocess.TimeoutExpired:
        terminal["timed_out"] = True
        raise
    finally:
        (output / f"{name}.terminal.json").write_text(
            json.dumps({**terminal, "seconds": time.monotonic() - started}),
            encoding="utf-8",
        )
    if result.returncode:
        raise RuntimeError(
            f"{name} failed: exit {result.returncode}; see retained logs"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--test-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not sys.platform.startswith("freebsd") or platform.machine() not in {
        "amd64",
        "x86_64",
    }:
        parser.error("this cell requires native FreeBSD amd64")
    if os.geteuid() == 0:
        parser.error("the SDK qualification must run as an unprivileged account")
    output = args.output.resolve()
    output.mkdir(mode=0o700)
    metadata = json.loads((args.wheel_dir / "wheel.json").read_text(encoding="utf-8"))
    wheel = args.wheel_dir / metadata["wheel"]
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != metadata["sha256"]:
        raise ValueError("wheel changed after source-bound construction")
    if (
        hashlib.sha256((args.wheel_dir / "test-source.tar").read_bytes()).hexdigest()
        != metadata["test_source_sha256"]
    ):
        raise ValueError("test-data archive changed after source-bound construction")

    import literate_ai
    from literate_ai.adapters.standard_lifecycle_binding import (
        observe_installed_framework_distribution,
        observe_installed_framework_origin,
    )

    prefix = Path(sys.prefix).resolve(strict=True)
    module = Path(literate_ai.__file__).resolve(strict=True)
    if not module.is_relative_to(prefix) or "site-packages" not in module.parts:
        raise ValueError("qualification must import the non-editable installed wheel")
    distribution = observe_installed_framework_distribution()
    origin = observe_installed_framework_origin(distribution.distribution_version)
    if origin.git_revision != metadata["revision"]:
        raise ValueError("installed framework is not the selected source revision")
    record = {
        "schema": "literate-ai/native-sdk-qualification@1",
        "revision": origin.git_revision,
        "wheel": metadata,
        "distribution_identity": distribution.identity.uri,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "unprivileged": True,
        },
        "scope": "Installed public CLI SDK microfixture and installed owner controls",
        "execution_environment": "Ephemeral native FreeBSD VM on hosted Linux",
        "canonical_cli_test_doubles": False,
        "installed_owner_controls_may_use_test_doubles": True,
        "compiler_sdk_admitted": False,
        "product_release_admission": False,
        "passed": False,
    }
    (output / "platform.json").write_text(json.dumps(record, indent=2))
    project = output.parent / "consumer"
    command(
        "initialize",
        [
            sys.executable,
            "-I",
            "-m",
            "literate_ai.cli",
            "init",
            str(project),
            "--empty",
            "--no-tool-bootstrap",
            "--from",
            args.framework.resolve().as_uri() + "#v1.3.0",
            "--repository-fetch-total-seconds",
            "300",
            "--repository-fetch-no-progress-seconds",
            "60",
            "--repository-fetch-connect-seconds",
            "5",
        ],
        output,
        600,
    )
    harness = Path(__file__).resolve().parent
    command(
        "canonical-sdk",
        [
            sys.executable,
            "-I",
            str(harness / "source_transport.py"),
            "--fixture",
            str(args.fixture.resolve()),
            "--project",
            str(project),
            "--qualification-script",
            str(harness / "qualify_public_sdk.py"),
            "--output",
            str(output / "transport"),
        ],
        output,
        6600,
    )
    transport = json.loads((output / "transport/result.json").read_text())
    progress = json.loads(
        (output / "transport/qualification/progress.json").read_text()
    )
    if not transport["ok"] or transport["owned_server_terminal"] != 0:
        raise ValueError("owned authenticated source server did not close successfully")
    if not progress["canonical_installed_cli_qualified"]:
        raise ValueError("canonical installed public CLI did not qualify")

    # The source archive supplies tests/data only. Its src/ is never on sys.path.
    sys.path.insert(0, str(args.test_source.resolve()))
    names = [
        "tests.e2e.test_retained_sdk_public",
        "tests.critical.test_retained_project_sdks",
        "tests.critical.test_retained_project_runtime",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    if suite.countTestCases() < 40:
        raise ValueError("installed SDK owner inventory is incomplete")
    with (output / "installed-controls.stderr").open("w") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    imports = {}
    for name, value in sorted(sys.modules.items()):
        if name == "literate_ai" or name.startswith("literate_ai."):
            file = getattr(value, "__file__", None)
            if file:
                path = Path(file).resolve(strict=True)
                if not path.is_relative_to(prefix) or "site-packages" not in path.parts:
                    raise ValueError(
                        f"production import escaped installed prefix: {name}"
                    )
                imports[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    record.update(
        canonical_cli=progress,
        transport={
            key: transport[key]
            for key in (
                "resolved_commit",
                "source_tree",
                "source_snapshot",
                "files",
                "owned_server_terminal",
            )
        },
        installed_controls={
            "selected": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "production_modules_verified": len(imports),
        },
        passed=result.wasSuccessful() and not result.skipped,
    )
    (output / "production-imports.json").write_text(json.dumps(imports, indent=2))
    (output / "result.json").write_text(json.dumps(record, indent=2))
    if not record["passed"]:
        raise RuntimeError("native installed SDK owner controls failed or skipped")
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
