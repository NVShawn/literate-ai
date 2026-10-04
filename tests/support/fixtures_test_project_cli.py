from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_project_cli``."""


import io
import json
import re
import shutil
from pathlib import Path

from literate_ai.cli import main
from tests.unit.root_parent_adapter import root_parent_for_fixture_project

REPO_ROOT = Path(__file__).resolve().parents[2]


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    with root_parent_for_fixture_project(arguments):
        status = main(arguments, stdout=output, stderr=errors)
    content = output.getvalue() if status == 0 else errors.getvalue()
    if not content:
        content = output.getvalue() or errors.getvalue()
    try:
        return status, json.loads(content)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON CLI output ({status=}): {content!r}") from exc


def copy_generation_catalogs(target: Path) -> None:
    shutil.copytree(
        REPO_ROOT / "skills" / "specification-to-source",
        target / "skills" / "specification-to-source",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        REPO_ROOT / "skills" / "agent" / "swift-toolchain-prerequisite",
        target / "skills" / "agent" / "swift-toolchain-prerequisite",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        REPO_ROOT / "skills" / "agent" / "select-nvidia-accelerated-stack",
        target / "skills" / "agent" / "select-nvidia-accelerated-stack",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        REPO_ROOT / "workflows" / "sample-host.md",
        target / "workflows" / "sample-host.md",
    )
    shutil.copy2(
        REPO_ROOT / "routing" / "sample-host.json",
        target / "routing" / "sample-host.json",
    )


def copy_hello_component(target: Path) -> Path:
    component = target / "samples" / "hello-component"
    if not component.is_dir():
        shutil.copytree(REPO_ROOT / "samples" / "hello-component", component)
    for generated_lock in component.glob("component.*.json"):
        generated_lock.unlink()
    return component


def refresh_authority_review(target: Path) -> None:
    manifest = json.loads((target / "literate.project.json").read_text())
    documents = sorted(
        path
        for root in manifest["documentation_roots"]
        for path in (target / root).rglob("*.md")
    )
    assert documents
    marker = re.compile(
        r"<!--\s*literate-ai:authority-reviewed sha256:[0-9a-f]{64}\s*-->"
    )
    selected = next(
        (
            path
            for path in documents
            if marker.search(path.read_text(encoding="utf-8"))
            or "<!-- literate-ai:authority-review-pending -->"
            in path.read_text(encoding="utf-8")
        ),
        documents[0],
    )
    content = selected.read_text(encoding="utf-8")
    if marker.search(content):
        content = marker.sub("<!-- literate-ai:authority-review-pending -->", content)
    elif "<!-- literate-ai:authority-review-pending -->" not in content:
        content += "\n<!-- literate-ai:authority-review-pending -->\n"
    selected.write_text(content, encoding="utf-8")
    status, envelope = invoke("project", "documentation-review", str(target))
    assert status == 0, envelope
    expected = envelope["result"]["expected_marker"]
    selected.write_text(
        selected.read_text(encoding="utf-8").replace(
            "<!-- literate-ai:authority-review-pending -->", expected
        ),
        encoding="utf-8",
    )
