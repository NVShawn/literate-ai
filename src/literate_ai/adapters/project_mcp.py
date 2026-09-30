"""Load and hygiene-check project-owned MCP catalog items (ADR 0024)."""

from __future__ import annotations

from pathlib import Path

from literate_ai.contracts._validation import ContractValidationError, unique
from literate_ai.contracts.authoring_markdown import (
    AuthoringMarkdownError,
    parse_authoring_markdown,
)
from literate_ai.contracts.channel_events import reject_secret_shaped
from literate_ai.contracts.project_mcp import (
    PROJECT_MCP_SENTINEL,
    ProjectMcpMarkdown,
    generation_skill_requires_operator_mcp,
)


class ProjectMcpError(ValueError):
    """Project MCP catalog hygiene failed closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _iter_mcp_sentinels(catalog_root: Path) -> tuple[Path, ...]:
    if not catalog_root.is_dir() or catalog_root.is_symlink():
        return ()
    found: list[Path] = []
    for path in sorted(catalog_root.rglob(PROJECT_MCP_SENTINEL)):
        if path.is_symlink() or not path.is_file():
            raise ProjectMcpError(
                "project_mcp.sentinel_invalid",
                f"mcp.md must be a regular file: {path}",
            )
        found.append(path)
    return tuple(found)


def parse_project_mcp_markdown(content: bytes, *, source: str) -> ProjectMcpMarkdown:
    try:
        frontmatter, body = parse_authoring_markdown(content, source=source)
        return ProjectMcpMarkdown.from_frontmatter(frontmatter, body, source=source)
    except (AuthoringMarkdownError, ContractValidationError) as exc:
        raise ProjectMcpError("project_mcp.invalid", str(exc)) from exc


def load_project_mcps(*catalog_roots: Path) -> tuple[ProjectMcpMarkdown, ...]:
    items: list[ProjectMcpMarkdown] = []
    for catalog_root in catalog_roots:
        for sentinel in _iter_mcp_sentinels(catalog_root):
            item = parse_project_mcp_markdown(
                sentinel.read_bytes(),
                source=sentinel.as_posix(),
            )
            expected = sentinel.parent.name
            if item.mcp_id != expected:
                raise ProjectMcpError(
                    "project_mcp.id_mismatch",
                    f"mcp.md name {item.mcp_id!r} must match directory {expected!r}",
                )
            _reject_secret_siblings(sentinel.parent)
            items.append(item)
    try:
        unique(tuple(item.mcp_id for item in items), "project mcp catalog", "mcp ids")
    except ContractValidationError as exc:
        raise ProjectMcpError("project_mcp.duplicate_id", str(exc)) from exc
    return tuple(items)


def _reject_secret_siblings(directory: Path) -> None:
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProjectMcpError(
                "project_mcp.sibling_unreadable",
                f"project MCP sibling is not UTF-8 text: {path}",
            ) from exc
        try:
            reject_secret_shaped(text, path.as_posix())
        except ContractValidationError as exc:
            raise ProjectMcpError("project_mcp.secret", str(exc)) from exc


def assert_generation_skills_are_mcp_free(skill_roots: tuple[Path, ...]) -> None:
    for skill_root in skill_roots:
        generation = skill_root / "specification-to-source"
        if not generation.is_dir() or generation.is_symlink():
            continue
        for path in sorted(generation.rglob("SKILL.md")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ProjectMcpError(
                    "project_mcp.generation_skill_unreadable",
                    f"generation skill is not UTF-8 text: {path}",
                ) from exc
            if generation_skill_requires_operator_mcp(text):
                raise ProjectMcpError(
                    "project_mcp.generation_prompt",
                    "specification-to-source skills must not require an operator MCP: "
                    f"{path}",
                )


def assert_project_mcp_hygiene(
    *,
    mcp_roots: tuple[Path, ...],
    skill_roots: tuple[Path, ...],
) -> tuple[ProjectMcpMarkdown, ...]:
    items = load_project_mcps(*mcp_roots)
    assert_generation_skills_are_mcp_free(skill_roots)
    return items
