"""Strict Markdown authority documents with constrained YAML frontmatter.

Human-authored Literate AI catalogs use this small, deterministic format.  The
frontmatter carries typed selection metadata; the Markdown body carries the prose that
is presented to a coding agent.  Machine records continue to use their versioned JSON
wire contracts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .yaml_subset import YamlSubsetError, load_yaml_subset

_MAXIMUM_DOCUMENT_BYTES = 512 * 1024
_MAXIMUM_FRONTMATTER_LINES = 8192
AGENT_SKILL_EVALUATION_AUTHOR = (
    "Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>"
)


class AuthoringMarkdownError(ValueError):
    """A human-authority Markdown document is malformed or ambiguous."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def parse_authoring_markdown(
    content: bytes, *, source: str
) -> tuple[dict[str, Any], str]:
    """Parse exact UTF-8 bytes into strict frontmatter and non-empty Markdown prose."""

    if len(content) > _MAXIMUM_DOCUMENT_BYTES:
        raise AuthoringMarkdownError(
            "authoring_markdown.document_limit",
            f"authoring document exceeds {_MAXIMUM_DOCUMENT_BYTES} bytes: {source}",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthoringMarkdownError(
            "authoring_markdown.not_utf8",
            f"authoring document must be UTF-8: {source}",
        ) from exc
    if "\r" in text:
        raise AuthoringMarkdownError(
            "authoring_markdown.newline_invalid",
            f"authoring document must use LF newlines: {source}",
        )
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AuthoringMarkdownError(
            "authoring_markdown.frontmatter_missing",
            f"authoring document must begin with YAML frontmatter: {source}",
        )
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AuthoringMarkdownError(
            "authoring_markdown.frontmatter_unterminated",
            f"authoring frontmatter is not terminated: {source}",
        ) from exc
    if closing > _MAXIMUM_FRONTMATTER_LINES:
        raise AuthoringMarkdownError(
            "authoring_markdown.frontmatter_limit",
            f"authoring frontmatter is too long: {source}",
        )
    try:
        raw = load_yaml_subset("\n".join(lines[1:closing]))
    except YamlSubsetError as exc:
        raise AuthoringMarkdownError(
            "authoring_markdown.frontmatter_invalid",
            f"invalid frontmatter at line {exc.line}, column {exc.column}: {source}",
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AuthoringMarkdownError(
            "authoring_markdown.frontmatter_invalid",
            f"authoring frontmatter must be a string-keyed mapping: {source}",
        )
    body = "\n".join(lines[closing + 1 :]).strip()
    if not body:
        raise AuthoringMarkdownError(
            "authoring_markdown.body_missing",
            f"authoring Markdown body must not be empty: {source}",
        )
    return raw, body


def render_authoring_markdown(frontmatter: Mapping[str, object], body: str) -> bytes:
    """Render one canonical authority document from typed metadata and prose."""

    if not isinstance(body, str) or not body.strip() or "\r" in body:
        raise AuthoringMarkdownError(
            "authoring_markdown.body_invalid",
            "authoring Markdown body must be non-empty normalized text",
        )
    lines = ["---"]
    for key, value in frontmatter.items():
        if not isinstance(key, str) or not key:
            raise AuthoringMarkdownError(
                "authoring_markdown.frontmatter_invalid",
                "frontmatter keys must be non-empty strings",
            )
        _render_field(lines, key, value, indent=0)
    lines.extend(("---", body.strip(), ""))
    content = "\n".join(lines).encode("utf-8")
    # Rendering must never create a dialect the strict reader cannot consume.
    parse_authoring_markdown(content, source="rendered authority")
    return content


def project_typed_agent_skill(
    frontmatter: Mapping[str, object], body: str, *, source: str
) -> tuple[dict[str, object], str]:
    """Validate standard discovery metadata and return only typed skill authority."""

    value = dict(frontmatter)
    discovery = {"name", "description", "metadata"}
    present = discovery & value.keys()
    if not present:
        # Bounded compatibility for pre-migration canonical Markdown.
        return value, body
    if present != discovery:
        raise AuthoringMarkdownError(
            "authoring_markdown.skill_discovery_incomplete",
            f"agent skill discovery fields must be complete: {source}",
        )
    skill_id = value.get("skill_id")
    title = value.get("title")
    name = value.pop("name")
    description = value.pop("description")
    metadata = value.pop("metadata")
    if name != skill_id or not isinstance(description, str) or not description.strip():
        raise AuthoringMarkdownError(
            "authoring_markdown.skill_discovery_invalid",
            f"agent skill discovery must match its typed skill ID: {source}",
        )
    if metadata != {"author": AGENT_SKILL_EVALUATION_AUTHOR}:
        raise AuthoringMarkdownError(
            "authoring_markdown.skill_discovery_invalid",
            f"agent skill discovery author is not canonical: {source}",
        )
    heading = f"# {title}"
    lines = body.splitlines()
    if not isinstance(title, str) or not lines or lines[0] != heading:
        raise AuthoringMarkdownError(
            "authoring_markdown.skill_heading_invalid",
            f"agent skill body must begin with {heading!r}: {source}",
        )
    instructions = "\n".join(lines[1:]).strip()
    if not instructions:
        raise AuthoringMarkdownError(
            "authoring_markdown.body_missing",
            f"agent skill instructions must not be empty: {source}",
        )
    return value, instructions


def _render_field(lines: list[str], key: str, value: object, *, indent: int) -> None:
    prefix = " " * indent
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{prefix}{key}: {{}}")
            return
        lines.append(f"{prefix}{key}:")
        for nested_key, nested_value in value.items():
            if not isinstance(nested_key, str) or not nested_key:
                raise AuthoringMarkdownError(
                    "authoring_markdown.frontmatter_invalid",
                    "nested frontmatter keys must be non-empty strings",
                )
            _render_field(lines, nested_key, nested_value, indent=indent + 2)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = tuple(value)
        if not items:
            lines.append(f"{prefix}{key}: []")
            return
        lines.append(f"{prefix}{key}:")
        for item in items:
            _render_sequence_item(lines, item, indent=indent + 2)
        return
    lines.append(f"{prefix}{key}: {_scalar(value)}")


def _render_sequence_item(lines: list[str], value: object, *, indent: int) -> None:
    prefix = " " * indent
    if isinstance(value, Mapping):
        items = tuple(value.items())
        if not items:
            lines.append(f"{prefix}- {{}}")
            return
        first_key, first_value = items[0]
        if not isinstance(first_key, str) or not first_key:
            raise AuthoringMarkdownError(
                "authoring_markdown.frontmatter_invalid",
                "sequence mapping keys must be non-empty strings",
            )
        if isinstance(first_value, (Mapping, list, tuple)):
            lines.append(f"{prefix}- {first_key}:")
            _render_nested_value(lines, first_value, indent=indent + 4)
        else:
            lines.append(f"{prefix}- {first_key}: {_scalar(first_value)}")
        for key, item in items[1:]:
            if not isinstance(key, str) or not key:
                raise AuthoringMarkdownError(
                    "authoring_markdown.frontmatter_invalid",
                    "sequence mapping keys must be non-empty strings",
                )
            _render_field(lines, key, item, indent=indent + 2)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        lines.append(f"{prefix}- {_flow_value(value)}")
        return
    lines.append(f"{prefix}- {_scalar(value)}")


def _render_nested_value(lines: list[str], value: object, *, indent: int) -> None:
    if isinstance(value, Mapping):
        if not value:
            lines.append(" " * indent + "{}")
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuthoringMarkdownError(
                    "authoring_markdown.frontmatter_invalid",
                    "nested mapping keys must be strings",
                )
            _render_field(lines, key, item, indent=indent)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _render_sequence_item(lines, item, indent=indent)
        return
    lines.append(" " * indent + _scalar(value))


def _flow_value(value: Sequence[object]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(", ", ": "))


def _scalar(value: object) -> str:
    if value is None or isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False)
    raise AuthoringMarkdownError(
        "authoring_markdown.frontmatter_invalid",
        f"unsupported frontmatter value type: {type(value).__name__}",
    )


__all__ = [
    "AGENT_SKILL_EVALUATION_AUTHOR",
    "AuthoringMarkdownError",
    "parse_authoring_markdown",
    "project_typed_agent_skill",
    "render_authoring_markdown",
]
