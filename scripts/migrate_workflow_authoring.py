#!/usr/bin/env python3
"""Convert a prose-bearing generation workflow JSON file to workflow.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from literate_ai.application.planning import (
    GENERATION_WORKFLOW_MARKDOWN_SCHEMA,
    GENERATION_WORKFLOW_SCHEMA,
)
from literate_ai.contracts.authoring_markdown import render_authoring_markdown


def migrate(path: Path) -> Path:
    if path.suffix != ".json" or not path.is_file() or path.is_symlink():
        raise ValueError(f"expected a direct regular workflow JSON file: {path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") != GENERATION_WORKFLOW_SCHEMA:
        raise ValueError(f"unsupported generation workflow: {path}")
    target = path.with_suffix(".md")
    if target.exists():
        raise ValueError(f"refusing ambiguous dual authority: {target}")
    stages = value.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError(f"generation workflow has no stages: {path}")
    authored_stages = []
    instruction_sections = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict) or "instructions" not in stage:
            raise ValueError(f"invalid generation workflow stage {index}: {path}")
        instruction = stage["instructions"]
        if not isinstance(instruction, str):
            raise ValueError(f"invalid generation workflow instruction {index}: {path}")
        authored_stages.append(
            {key: item for key, item in stage.items() if key != "instructions"}
        )
        if instruction.strip():
            instruction_sections.extend(
                (f"## Stage: {stage['stage_id']}", "", instruction.strip(), "")
            )
    frontmatter = {
        "schema": GENERATION_WORKFLOW_MARKDOWN_SCHEMA,
        "workflow_id": value["workflow_id"],
        "version": value["version"],
        "stages": authored_stages,
    }
    title = str(value["workflow_id"]).replace("-", " ").title()
    body = "\n".join(
        (
            f"# {title}",
            "",
            "This workflow defines the ordered model and guarded lifecycle stages.",
            "",
            *instruction_sections,
        )
    ).rstrip()
    target.write_bytes(render_authoring_markdown(frontmatter, body))
    path.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()
    for path in arguments.paths:
        print(migrate(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
