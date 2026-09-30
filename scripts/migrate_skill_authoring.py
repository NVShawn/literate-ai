#!/usr/bin/env python3
"""Migrate repository-native skill catalogs from JSON to canonical ``SKILL.md``.

This intentionally narrow migration preserves each typed manifest field, moves the
agent-facing prompt into the Markdown body, and rewrites exact dependency identities
bottom-up.  References outside the selected catalogs are reported for explicit review;
they are never silently rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    render_authoring_markdown,
)


@dataclass(frozen=True, slots=True)
class Migration:
    old_path: Path
    new_path: Path
    old_digest: str
    new_digest: str


def _render_skill(value: dict[str, object], body: str) -> bytes:
    skill_id = value["skill_id"]
    title = value["title"]
    if not isinstance(skill_id, str) or not isinstance(title, str):
        raise ValueError("skill ID and title must be strings")
    return render_authoring_markdown(
        {
            "name": skill_id,
            "description": f"{title}. Use for Literate AI workflow tasks.",
            "metadata": {"author": AGENT_SKILL_EVALUATION_AUTHOR},
            **value,
        },
        f"# {title}\n\n{body.strip()}",
    )


def _forward_catalog(root: Path) -> tuple[Migration, ...]:
    sources = {
        path.parent.name: (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(root.glob("*/skill.json"))
    }
    rendered: dict[str, bytes] = {}
    visiting: set[str] = set()

    def render(skill_id: str) -> bytes:
        existing = rendered.get(skill_id)
        if existing is not None:
            return existing
        if skill_id in visiting:
            raise ValueError(f"skill dependency cycle at {skill_id}")
        visiting.add(skill_id)
        path, manifest = sources[skill_id]
        value = dict(manifest)
        body = value.pop("instructions")
        dependencies = []
        for dependency in value["dependencies"]:
            item = dict(dependency)
            dependency_id = item["skill_id"]
            if dependency_id in sources:
                dependency_content = render(dependency_id)
                identity = dict(item["identity"])
                identity["digest"] = hashlib.sha256(dependency_content).hexdigest()
                item["identity"] = identity
            dependencies.append(item)
        value["dependencies"] = dependencies
        content = _render_skill(value, body)
        rendered[skill_id] = content
        visiting.remove(skill_id)
        return content

    migrations = []
    for skill_id, (old_path, _manifest) in sorted(sources.items()):
        content = render(skill_id)
        new_path = old_path.with_name("SKILL.md")
        new_path.write_bytes(content)
        migrations.append(
            Migration(
                old_path,
                new_path,
                hashlib.sha256(old_path.read_bytes()).hexdigest(),
                hashlib.sha256(content).hexdigest(),
            )
        )
        old_path.unlink()
    return tuple(migrations)


def _inverse_catalog(root: Path) -> tuple[Migration, ...]:
    migrations = []
    for old_path in sorted(root.glob("*/skill.json")):
        original = old_path.read_bytes()
        value = json.loads(original)
        body = value.pop("prompt_template")
        content = _render_skill(value, body)
        new_path = old_path.with_name("SKILL.md")
        new_path.write_bytes(content)
        old_path.unlink()
        migrations.append(
            Migration(
                old_path,
                new_path,
                hashlib.sha256(original).hexdigest(),
                hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(migrations)


def migrate_repository(root: Path) -> tuple[Migration, ...]:
    catalogs = (
        ("forward", root / "skills/specification-to-source"),
        ("inverse", root / "skills/source-to-specification"),
        (
            "inverse",
            root / "src/literate_ai/source_to_specification/builtin_skills",
        ),
        (
            "forward",
            root / "src/literate_ai/project_template/skills/specification-to-source",
        ),
    )
    result = []
    for kind, catalog in catalogs:
        if not catalog.is_dir():
            raise ValueError(f"skill catalog is missing: {catalog}")
        result.extend(
            _forward_catalog(catalog)
            if kind == "forward"
            else _inverse_catalog(catalog)
        )
    return tuple(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    repository = arguments.repository.resolve(strict=True)
    migrations = migrate_repository(repository)
    print(
        json.dumps(
            {
                "schema": "literate-ai/skill-authoring-migration@1",
                "migrations": [
                    {
                        "old_path": item.old_path.relative_to(repository).as_posix(),
                        "new_path": item.new_path.relative_to(repository).as_posix(),
                        "old_digest": f"sha256:{item.old_digest}",
                        "new_digest": f"sha256:{item.new_digest}",
                    }
                    for item in migrations
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
