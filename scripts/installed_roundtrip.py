"""Run credentialed regenerative conformance from an exact installed wheel."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from install_litai import install

from literate_ai.evidence_ledger import (
    EvidenceNode,
    attach_run,
    retained_directory,
)


@contextmanager
def _runtime_directory():
    root = Path(tempfile.mkdtemp(prefix="literate-installed-roundtrip-"))
    with retained_directory(
        root,
        node_path="installed/roundtrip/runtime",
        operation="installed.roundtrip.runtime",
        role="installed-roundtrip-workspace",
    ) as retained:
        yield retained


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--languages",
        help="optional unique comma-separated round-trip language subset",
    )
    parser.add_argument("--skip-composed", action="store_true")
    arguments = parser.parse_args()

    repository = arguments.repository.resolve(strict=True)
    with _runtime_directory() as root:
        installed = install(prefix=root / ".local", source=repository)
        environment_root = Path(installed["environment"])
        scripts = environment_root / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        environment = dict(os.environ)
        environment["LITAI_NO_SELF_UPDATE"] = "1"
        environment.pop("PYTHONPATH", None)
        environment["LITERATE_AI_RUN_LIVE_ROUNDTRIP"] = "1"
        if arguments.languages:
            environment["LITERATE_AI_LIVE_ROUNDTRIP_LANGUAGES"] = arguments.languages
        tests = ["tests.conformance.test_live_bidirectional_roundtrip"]
        if not arguments.skip_composed:
            environment["LITERATE_AI_RUN_LIVE_COMPOSED_ROUNDTRIP"] = "1"
            tests.append("tests.conformance.test_live_composed_roundtrip")
        completed = subprocess.run(
            (str(python), "-m", "unittest", *tests),
            cwd=repository,
            env=environment,
            check=False,
        )
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, completed.args)
        return completed.returncode


def main() -> int:
    run = attach_run()
    if run is None:
        return _main()
    context = run.node(
        "installed/roundtrip",
        operation="installed.roundtrip",
        parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
    )
    with context as node:
        status = _main()
        if status and isinstance(node, EvidenceNode):
            node.fail(f"installed roundtrip exited with status {status}")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
