"""Shared test fixtures extracted from test_retained_harness_receipts."""

from __future__ import annotations

import io
import json
from pathlib import Path

from literate_ai.adapters.project_initialization import (
    detect_repo_flavors,
    host_platform_selector,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    ProjectInitializationOrigin,
)
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="ssh://git.example.test/operator/literate-ai.git",
        git_revision="c" * 40,
        distribution_name="literate-ai",
        distribution_version="0.9.0",
    )


def _adapter() -> FilesystemProjectInitializationAdapter:
    return FilesystemProjectInitializationAdapter(
        initialization_origin_provider=_origin,
        standard_binding_provider=lambda: None,
    )


def _selectors(target: Path) -> tuple[str, ...]:
    return (
        *(f"+{selector}" for selector in detect_repo_flavors(target)),
        host_platform_selector(),
    )


def _invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = main(arguments, stdout=stdout, stderr=stderr)
    content = stdout.getvalue() if status == 0 else stderr.getvalue()
    return status, json.loads(content)


def _legacy_project(target: Path, test_summary: str = "Ran 2 tests") -> None:
    target.mkdir()
    (target / "app.py").write_text("print('retained')\n", encoding="utf-8")
    (target / "Makefile").write_text(
        "build:\n"
        "\tmkdir -p build\n"
        "\tprintf 'artifact\\n' > build/app\n"
        "test:\n"
        f"\t@printf '{test_summary}\\n'\n"
        "\t@printf 'OK\\n'\n"
        "package:\n"
        "\tmkdir -p dist\n"
        "\tprintf 'package\\n' > dist/app.txt\n",
        encoding="utf-8",
    )
