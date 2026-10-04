"""Shared test fixtures extracted from test_catalog_import."""

import json
from pathlib import Path


def make_project(directory: Path, project_id: str, *, with_origin: bool = True) -> Path:
    """Create a minimal literate-ai project structure."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "literate.project.json").read_bytes()
    )
    manifest["project_id"] = project_id
    (directory / "literate.project.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    (directory / ".literate").mkdir(exist_ok=True)
    if with_origin:
        origin = {
            "distribution_name": "literate-ai",
            "distribution_version": "0.2.0",
            "git_revision": "abc" * 13 + "a",
            "repository_url": "git@github.com:test/literate-ai.git",
            "schema": "urn:literate-ai:schema:v1:project-initialization-origin",
        }
        (directory / ".literate" / "initialization-origin.json").write_text(
            json.dumps(origin) + "\n", encoding="utf-8"
        )
    return directory
