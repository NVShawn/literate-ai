"""Explicit operator-local sibling bindings for retained harness workspaces."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.contracts import ContentIdentity, canonical_identity

HARNESS_WORKSPACE_LINKS_SCHEMA = "literate-ai/harness-workspace-links@1"
HARNESS_WORKSPACE_RUNTIME_SCHEMA = "literate-ai/harness-workspace-runtime@1"
MAX_HARNESS_WORKSPACE_LINKS = 32
_DESTINATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESERVED_DESTINATIONS = frozenset({"legacy", "implementation"})
_WINDOWS_CMD_UNSAFE = frozenset({'"', "%", "!", "\r", "\n"})


class HarnessWorkspaceError(ValueError):
    """A retained-harness workspace binding is invalid or cannot be realized."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HarnessWorkspaceLink:
    """One validated external directory projected beside a disposable source root."""

    destination: str
    source: Path


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_harness_workspace_links(
    values: Sequence[str],
    *,
    base: Path,
    project_root: Path,
) -> tuple[HarnessWorkspaceLink, ...]:
    """Resolve bounded ``DESTINATION=SOURCE`` declarations before project mutation."""

    if isinstance(values, (str, bytes)) or len(values) > MAX_HARNESS_WORKSPACE_LINKS:
        raise HarnessWorkspaceError(
            "harness.workspace_links_invalid",
            f"at most {MAX_HARNESS_WORKSPACE_LINKS} workspace links may be declared",
        )
    try:
        project = require_safe_directory(
            Path(os.path.abspath(os.fspath(project_root)))
        ).resolve(strict=True)
        resolution_base = require_safe_directory(
            Path(os.path.abspath(os.fspath(base)))
        ).resolve(strict=True)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise HarnessWorkspaceError(
            "harness.workspace_link_root_unsafe",
            "workspace-link project and resolution roots must be safe directories",
        ) from exc
    links: list[HarnessWorkspaceLink] = []
    destinations: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise HarnessWorkspaceError(
                "harness.workspace_link_invalid",
                "each workspace link must use DESTINATION=SOURCE",
            )
        destination, source_text = raw.split("=", 1)
        if (
            _DESTINATION.fullmatch(destination) is None
            or destination in _RESERVED_DESTINATIONS
        ):
            raise HarnessWorkspaceError(
                "harness.workspace_link_destination_invalid",
                "workspace-link destination must be one safe sibling directory name",
            )
        if destination in destinations:
            raise HarnessWorkspaceError(
                "harness.workspace_link_destination_duplicate",
                f"workspace-link destination is duplicated: {destination}",
            )
        if not source_text:
            raise HarnessWorkspaceError(
                "harness.workspace_link_source_invalid",
                f"workspace-link source is empty: {destination}",
            )
        configured = Path(source_text).expanduser()
        if not configured.is_absolute():
            configured = resolution_base / configured
        configured = Path(os.path.abspath(os.fspath(configured)))
        try:
            source = require_safe_directory(configured).resolve(strict=True)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise HarnessWorkspaceError(
                "harness.workspace_link_source_unsafe",
                f"workspace-link source is not a safe directory: {destination}",
            ) from exc
        if _contains(project, source) or _contains(source, project):
            raise HarnessWorkspaceError(
                "harness.workspace_link_source_overlap",
                "workspace-link source and project roots must be disjoint: "
                f"{destination}",
            )
        destinations.add(destination)
        links.append(HarnessWorkspaceLink(destination, source))
    resolved = tuple(sorted(links, key=lambda item: item.destination))
    validate_harness_workspace_links(resolved, project_root=project)
    return resolved


def validate_harness_workspace_links(
    links: Sequence[HarnessWorkspaceLink], *, project_root: Path
) -> None:
    """Recheck typed bindings and disjoint source custody before each transaction."""

    harness_workspace_link_evidence(links)
    try:
        project = require_safe_directory(
            Path(os.path.abspath(os.fspath(project_root)))
        ).resolve(strict=True)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise HarnessWorkspaceError(
            "harness.workspace_link_root_unsafe",
            "workspace-link project root must be a safe directory",
        ) from exc
    for link in links:
        try:
            source = require_safe_directory(link.source).resolve(strict=True)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise HarnessWorkspaceError(
                "harness.workspace_link_source_unsafe",
                f"workspace-link source is not a safe directory: {link.destination}",
            ) from exc
        if (
            source != link.source
            or _contains(project, source)
            or _contains(source, project)
        ):
            raise HarnessWorkspaceError(
                "harness.workspace_link_source_overlap",
                "workspace-link sources must remain resolved and disjoint from the "
                f"project: {link.destination}",
            )


def harness_workspace_link_evidence(
    links: Sequence[HarnessWorkspaceLink],
) -> dict[str, object]:
    """Return portable topology evidence without disclosing host source paths."""

    if len(links) > MAX_HARNESS_WORKSPACE_LINKS or any(
        not isinstance(item, HarnessWorkspaceLink)
        or not isinstance(item.source, Path)
        or _DESTINATION.fullmatch(item.destination) is None
        or item.destination in _RESERVED_DESTINATIONS
        for item in links
    ):
        raise HarnessWorkspaceError(
            "harness.workspace_links_invalid",
            "workspace links must contain validated binding values",
        )
    destinations = [item.destination for item in links]
    if destinations != sorted(set(destinations)):
        raise HarnessWorkspaceError(
            "harness.workspace_links_invalid",
            "workspace links must be validated, sorted, and unique",
        )
    return {
        "schema": HARNESS_WORKSPACE_LINKS_SCHEMA,
        "destinations": destinations,
    }


def require_harness_workspace_link_evidence(
    evidence: object,
    links: Sequence[HarnessWorkspaceLink],
) -> None:
    """Require a run to supply exactly the portable topology that was qualified."""

    expected = harness_workspace_link_evidence(links)
    if evidence is None:
        evidence = harness_workspace_link_evidence(())
    if evidence != expected:
        raise HarnessWorkspaceError(
            "harness.workspace_links_mismatch",
            "workspace links must match every and only destination qualified at "
            "conversion",
        )


def harness_workspace_runtime_identity(
    links: Sequence[HarnessWorkspaceLink],
) -> ContentIdentity:
    """Bind one run to exact operator-local locators without exposing those locators."""

    harness_workspace_link_evidence(links)
    return canonical_identity(
        {
            "schema": HARNESS_WORKSPACE_RUNTIME_SCHEMA,
            "links": [
                {
                    "destination": item.destination,
                    "source_locator": os.fsencode(item.source).hex(),
                }
                for item in links
            ],
        }
    )


def _windows_junction(destination: Path, source: Path) -> None:
    command_processor = shutil.which("cmd.exe")
    if command_processor is None:
        raise HarnessWorkspaceError(
            "harness.workspace_link_unavailable",
            "Windows directory-link fallback requires cmd.exe on PATH",
        )
    destination_text, source_text = str(destination), str(source)
    if any(
        character in _WINDOWS_CMD_UNSAFE
        for value in (destination_text, source_text)
        for character in value
    ):
        raise HarnessWorkspaceError(
            "harness.workspace_link_path_unsupported",
            "Windows directory-link paths contain unsupported command characters",
        )
    completed = subprocess.run(
        (
            command_processor,
            "/d",
            "/v:off",
            "/s",
            "/c",
            f'mklink /J "{destination_text}" "{source_text}"',
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise HarnessWorkspaceError(
            "harness.workspace_link_unavailable",
            "Windows could not create the requested directory link",
        )


def _create_directory_link(destination: Path, source: Path) -> str:
    try:
        os.symlink(source, destination, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            raise HarnessWorkspaceError(
                "harness.workspace_link_unavailable",
                "host could not create the requested directory link",
            ) from exc
        _windows_junction(destination, source)
        return "junction"
    return "symlink"


def _verify_directory_link(destination: Path, source: Path) -> None:
    try:
        linked = path_is_link_or_reparse(destination)
        resolved = destination.resolve(strict=True)
    except OSError as exc:
        raise HarnessWorkspaceError(
            "harness.workspace_link_changed",
            "workspace link became unavailable during harness execution",
        ) from exc
    if not linked or resolved != source:
        raise HarnessWorkspaceError(
            "harness.workspace_link_changed",
            "workspace link changed during harness execution",
        )


@contextmanager
def materialize_harness_workspace_links(
    workspace_root: Path,
    links: Sequence[HarnessWorkspaceLink],
) -> Iterator[None]:
    """Project validated external siblings for one bounded harness execution."""

    harness_workspace_link_evidence(links)
    try:
        root = Path(workspace_root).resolve(strict=True)
        require_safe_directory(root)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise HarnessWorkspaceError(
            "harness.workspace_root_unsafe",
            "harness workspace root must be a safe existing directory",
        ) from exc
    created: list[tuple[Path, str, Path]] = []
    try:
        for link in links:
            try:
                require_safe_directory(link.source)
                (root / link.destination).lstat()
            except FileNotFoundError:
                pass
            except (OSError, UnsafeFilesystemPathError) as exc:
                raise HarnessWorkspaceError(
                    "harness.workspace_link_collision",
                    f"workspace-link destination is unavailable: {link.destination}",
                ) from exc
            else:
                raise HarnessWorkspaceError(
                    "harness.workspace_link_collision",
                    f"workspace-link destination already exists: {link.destination}",
                )
            destination = root / link.destination
            kind = _create_directory_link(destination, link.source)
            created.append((destination, kind, link.source))
            _verify_directory_link(destination, link.source)
        try:
            yield
        except BaseException:
            raise
        else:
            for destination, _kind, source in created:
                _verify_directory_link(destination, source)
    finally:
        for destination, kind, _source in reversed(created):
            try:
                if kind == "junction":
                    os.rmdir(destination)
                else:
                    os.unlink(destination)
            except OSError:
                pass


__all__ = [
    "HARNESS_WORKSPACE_LINKS_SCHEMA",
    "HarnessWorkspaceError",
    "HarnessWorkspaceLink",
    "harness_workspace_link_evidence",
    "harness_workspace_runtime_identity",
    "materialize_harness_workspace_links",
    "require_harness_workspace_link_evidence",
    "resolve_harness_workspace_links",
    "validate_harness_workspace_links",
]
