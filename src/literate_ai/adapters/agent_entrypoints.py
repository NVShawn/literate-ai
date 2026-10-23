"""Read-only discovery of owned coding-agent delegates to project onboarding."""

from __future__ import annotations

import hashlib
import posixpath
import re

import yaml

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.adapters.repository_orchestration import (
    _read_document,
)
from literate_ai.contracts import canonical_identity
from literate_ai.projects import (
    CANONICAL_AGENT_SHIMS,
    LoadedProject,
    project_owns_agent_shim,
)

_MAX_BYTES = 1024 * 1024
_MAX_YAML_NODES = 4096
_MAX_YAML_DEPTH = 32


class _AuditYamlLoader(yaml.SafeLoader):
    """Bound parsing and forbid expansion through shared container aliases."""

    def __init__(self, stream):
        super().__init__(stream)
        self._audit_nodes = 0
        self._audit_depth = 0

    def compose_node(self, parent, index):
        self._audit_nodes += 1
        if self._audit_nodes > _MAX_YAML_NODES or self._audit_depth >= _MAX_YAML_DEPTH:
            raise yaml.YAMLError("agent-entrypoint YAML exceeds parser bounds")
        if self.check_event(yaml.AliasEvent):
            anchor = self.peek_event().anchor
            if not isinstance(self.anchors.get(anchor), yaml.ScalarNode):
                raise yaml.YAMLError("agent-entrypoint YAML container alias refused")
        self._audit_depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._audit_depth -= 1


def _parse_yaml(text: str):
    loader = _AuditYamlLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _read(project: LoadedProject, relative: str) -> bytes:
    path = project.root.joinpath(*relative.split("/"))
    require_safe_directory(path.parent, allow_missing=True)
    if not path.parent.exists():
        raise FileNotFoundError(relative)
    return _read_document(
        path,
        label=relative,
        error_suffix="agent_entrypoint_invalid",
        maximum_bytes=_MAX_BYTES,
    )


def _visible(text: str) -> str:
    # Advance once through comments, including an unfinished final comment.
    fragments = []
    cursor = 0
    while True:
        start = text.find("<!--", cursor)
        if start < 0:
            fragments.append(text[cursor:])
            break
        fragments.append(text[cursor:start])
        end = text.find("-->", start + 4)
        if end < 0:
            break
        cursor = end + 3
    text = "".join(fragments)
    result = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker[1]
        elif not line.startswith(("    ", "\t")):
            result.append(line)
    return "\n".join(result)


def _has_reference(text: str, references: set[str]) -> bool:
    return any(
        re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w/-]|\.+[\w/-])", text)
        for path in references
    )


def _delegates(text: str, relative: str, skill: str) -> bool:
    references = {skill, posixpath.relpath(skill, posixpath.dirname(relative) or ".")}
    if relative == "agents/openai.yaml":
        value = _parse_yaml(text)
        interface = value.get("interface") if isinstance(value, dict) else None
        prompt = (
            interface.get("default_prompt") if isinstance(interface, dict) else None
        )
        return (
            isinstance(prompt, str)
            and bool(re.search(r"\$literate-ai(?![\w-])", prompt))
            and (skill == "SKILL.md" or _has_reference(prompt, references))
        )
    visible = _visible(text)
    return _has_reference(visible, references)


def audit_agent_entrypoints(project: LoadedProject) -> dict:
    """Observe only this root; never repair files or traverse child projects."""
    skill = project.definition.agent_skill
    skill_state = "current"
    skill_identity = None
    try:
        raw = _read(project, skill)
        text = raw.decode("utf-8")
        lines = text.splitlines()
        end = lines[1:].index("---") + 1 if "---" in lines[1:] else 0
        frontmatter = (
            _parse_yaml("\n".join(lines[1:end]))
            if lines and lines[0] == "---" and end
            else None
        )
        if (
            not isinstance(frontmatter, dict)
            or frontmatter.get("name") != "literate-ai"
        ):
            skill_state = "invalid"
        skill_identity = "sha256:" + hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        skill_state = "missing"
    except (
        OSError,
        UnicodeError,
        ValueError,
        yaml.YAMLError,
        UnsafeFilesystemPathError,
        RecursionError,
    ):
        skill_state = "unsafe-or-invalid"
    entries = []
    for relative in CANONICAL_AGENT_SHIMS:
        entry = {"path": relative}
        if not project_owns_agent_shim(project.definition, relative):
            entry["state"] = "independent-child"
            entries.append(entry)
            continue
        try:
            raw = _read(project, relative)
            text = raw.decode("utf-8")
            entry["identity"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            entry["state"] = (
                "current"
                if relative == skill or _delegates(text, relative, skill)
                else "stale"
            )
        except FileNotFoundError:
            entry["state"] = "missing"
        except (
            OSError,
            UnicodeError,
            ValueError,
            yaml.YAMLError,
            UnsafeFilesystemPathError,
            RecursionError,
        ):
            entry["state"] = "unsafe-or-invalid"
        if entry["state"] not in {"current", "independent-child"}:
            target = posixpath.relpath(skill, posixpath.dirname(relative) or ".")
            entry["required_action"] = (
                "Review an authored change preserving custom instructions; "
                + (
                    "the interface.default_prompt must invoke $literate-ai"
                    + (
                        f" and name the declared onboarding file {skill}."
                        if skill != "SKILL.md"
                        else "."
                    )
                    if relative == "agents/openai.yaml"
                    else f"delegate to the declared onboarding skill: {target}."
                )
                + " Rerun this audit after repair. Do not rewrite pinned parents "
                "or retained evidence."
            )
        entries.append(entry)
    report = {
        "schema": "urn:literate-ai:schema:v2:agent-entrypoint-audit",
        "project_identity": project.definition.identity.uri,
        "agent_skill": {
            "path": skill,
            "state": skill_state,
            "identity": skill_identity,
        },
        "entries": entries,
        "ready": skill_state == "current"
        and all(
            entry["state"] in {"current", "independent-child"} for entry in entries
        ),
        "writes": False,
        "repair_policy": (
            "Resolve findings through explicit reviewed authored changes before "
            "onboarding is complete. The audit never modifies project, parent "
            "or retained-source files."
        ),
        "lifecycle_admitted": False,
    }
    return {**report, "identity": canonical_identity(report).uri}
