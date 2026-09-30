#!/usr/bin/env python3
"""Add direct Agent Skill discovery metadata without projecting skill authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai.contracts import canonical_identity
from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    parse_authoring_markdown,
    project_typed_agent_skill,
    render_authoring_markdown,
)


@dataclass(frozen=True, slots=True)
class Change:
    path: Path
    old_digest: str
    new_digest: str
    original: bytes
    content: bytes
    inputs: tuple[tuple[Path, str], ...] = ()


def _discovery(value: dict[str, object], body: str) -> tuple[dict[str, object], str]:
    skill_id = value["skill_id"]
    title = value["title"]
    if not isinstance(skill_id, str) or not isinstance(title, str):
        raise ValueError("typed skill ID and title must be strings")
    return (
        {
            "name": skill_id,
            "description": f"{title}. Use for Literate AI workflow tasks.",
            "metadata": {"author": AGENT_SKILL_EVALUATION_AUTHOR},
            **value,
        },
        f"# {title}\n\n{body.strip()}",
    )


def _catalog(
    root: Path, *, forward: bool, overrides: dict[Path, bytes] | None = None
) -> tuple[Change, ...]:
    sources: dict[str, tuple[Path, dict[str, object], str, bytes]] = {}
    authoring: dict[str, tuple[dict[str, object], bytes]] = {}
    for path in sorted(root.rglob("SKILL.md")):
        if path.is_symlink():
            raise ValueError(f"skill authority cannot be a symlink: {path}")
        original = path.read_bytes()
        selected = (overrides or {}).get(path, original)
        value, body = parse_authoring_markdown(selected, source=path.as_posix())
        discovery = dict(value)
        value, body = project_typed_agent_skill(value, body, source=path.as_posix())
        skill_id = value.get("skill_id")
        if not isinstance(skill_id, str) or skill_id in sources:
            raise ValueError(f"invalid or duplicate skill ID: {path}")
        sources[skill_id] = (path, value, body, original)
        authoring[skill_id] = (discovery, selected)

    rendered: dict[str, bytes] = {}
    visiting: set[str] = set()

    def render(skill_id: str) -> bytes:
        existing = rendered.get(skill_id)
        if existing is not None:
            return existing
        if skill_id in visiting:
            raise ValueError(f"skill dependency cycle at {skill_id}")
        visiting.add(skill_id)
        _path, original_value, body, _original = sources[skill_id]
        value = dict(original_value)
        if forward:
            dependencies = []
            raw_dependencies = value.get("dependencies")
            if not isinstance(raw_dependencies, list):
                raise ValueError(
                    f"forward skill dependencies must be a list: {skill_id}"
                )
            for dependency in raw_dependencies:
                if not isinstance(dependency, dict):
                    raise ValueError(f"invalid forward skill dependency: {skill_id}")
                item = dict(dependency)
                dependency_id = item.get("skill_id")
                if dependency_id in sources:
                    dependency_content = render(str(dependency_id))
                    identity = dict(item["identity"])
                    identity["digest"] = hashlib.sha256(dependency_content).hexdigest()
                    item["identity"] = identity
                    item["version"] = sources[str(dependency_id)][1]["version"]
                dependencies.append(item)
            value["dependencies"] = dependencies
        frontmatter, canonical_body = _discovery(value, body)
        authored, selected = authoring[skill_id]
        for key in ("name", "description", "metadata"):
            if key in authored:
                frontmatter[key] = authored[key]
        content = (
            selected
            if value == original_value
            and "name" in authored
            and "description" in authored
            else render_authoring_markdown(frontmatter, canonical_body)
        )
        rendered[skill_id] = content
        visiting.remove(skill_id)
        return content

    changes = []
    for skill_id, (path, _value, _body, original) in sorted(sources.items()):
        content = render(skill_id)
        if content == original:
            continue
        changes.append(
            Change(
                path,
                hashlib.sha256(original).hexdigest(),
                hashlib.sha256(content).hexdigest(),
                original,
                content,
            )
        )
    return tuple(changes)


def plan_repository(root: Path, *, mirrors: tuple[str, ...] = ()) -> tuple[Change, ...]:
    root = root.resolve(strict=True)
    overrides: dict[Path, bytes] = {}
    for name in mirrors:
        # Mirroring is explicit because some initialized templates intentionally differ.
        if (
            not name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
        ):
            raise ValueError("mirror must be a relative skill catalog name")
        source = root / "skills/specification-to-source" / name / "SKILL.md"
        target = (
            root
            / "src/literate_ai/project_template/skills/specification-to-source"
            / name
            / "SKILL.md"
        )
        for path in (source, target):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("mirror path escapes repository authority")
        if not target.is_file():
            raise ValueError(f"mirror has no existing template: {name}")
        overrides[target] = source.read_bytes()
    catalogs = (
        (root / "skills/specification-to-source", True),
        (root / "skills/source-to-specification", False),
        (root / "src/literate_ai/source_to_specification/builtin_skills", False),
        (
            root / "src/literate_ai/project_template/skills/specification-to-source",
            True,
        ),
    )
    changes = []
    for catalog, forward in catalogs:
        changes.extend(_catalog(catalog, forward=forward, overrides=overrides))
    inputs = tuple(
        (path, hashlib.sha256(path.read_bytes()).hexdigest())
        for catalog, _forward in catalogs
        for path in sorted(catalog.rglob("SKILL.md"))
    )
    return tuple(replace(item, inputs=inputs) for item in changes)


def plan_identity(root: Path, changes: tuple[Change, ...]) -> str:
    return canonical_identity(
        {
            "changes": [
                {
                    "path": item.path.relative_to(root).as_posix(),
                    "old": item.old_digest,
                    "new": item.new_digest,
                }
                for item in changes
            ],
            "inputs": [
                {"path": path.relative_to(root).as_posix(), "identity": digest}
                for path, digest in (changes[0].inputs if changes else ())
            ],
        }
    ).uri


def apply_changes(changes: tuple[Change, ...]) -> None:
    for path, digest in changes[0].inputs if changes else ():
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"skill update input is stale: {path}")
    for item in changes:
        if item.path.is_symlink() or item.path.read_bytes() != item.original:
            raise ValueError(f"skill update plan is stale: {item.path}")

    def replace_file(path: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".skill-update-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    written: list[Change] = []
    try:
        for item in changes:
            if item.path.read_bytes() != item.original:
                raise ValueError(f"skill update plan is stale: {item.path}")
            replace_file(item.path, item.content)
            written.append(item)
    except Exception:
        for item in reversed(written):
            if item.path.read_bytes() == item.content:
                replace_file(item.path, item.original)
        raise


def normalize_repository(root: Path) -> tuple[Change, ...]:
    changes = plan_repository(root)
    apply_changes(changes)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="report drift without writes; exit 1 if changes are needed",
    )
    mode.add_argument(
        "--plan", action="store_true", help="emit a non-writing update plan"
    )
    parser.add_argument(
        "--mirror",
        action="append",
        default=[],
        help="explicit forward-skill catalog path to mirror into the template",
    )
    parser.add_argument(
        "--expected-plan-identity",
        help="reject application if the reviewed plan changed",
    )
    arguments = parser.parse_args()
    repository = arguments.repository.resolve(strict=True)
    changes = plan_repository(repository, mirrors=tuple(arguments.mirror))
    identity = plan_identity(repository, changes)
    if (
        arguments.expected_plan_identity is not None
        and identity != arguments.expected_plan_identity
    ):
        parser.error("skill update plan changed since review")
    if not arguments.check and not arguments.plan:
        apply_changes(changes)
    print(
        json.dumps(
            {
                "schema": "literate-ai/skill-discovery-normalization@1",
                "plan_identity": identity,
                "writes": not arguments.check and not arguments.plan,
                "changes": [
                    {
                        "path": item.path.relative_to(repository).as_posix(),
                        "old_digest": f"sha256:{item.old_digest}",
                        "new_digest": f"sha256:{item.new_digest}",
                    }
                    for item in changes
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1 if arguments.check and changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
