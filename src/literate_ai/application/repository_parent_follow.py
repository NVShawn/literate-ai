"""Pure helpers for moving, pinned, and release-line parent selectors."""

from __future__ import annotations

import re

_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_RELEASE_LINE = re.compile(r"^release/(\d+)\.(\d+)\.x$")
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_MOVING_TIP_REFS = frozenset({"HEAD", "main", "master", "trunk"})


def is_exact_git_object_id(value: str) -> bool:
    return bool(_GIT_OBJECT_ID.fullmatch(value))


def parent_selector_kind(requested_revision: str) -> str:
    if is_exact_git_object_id(requested_revision):
        return "exact-commit"
    return "moving-ref"


def parse_release_line(reference: str) -> tuple[int, int] | None:
    matched = _RELEASE_LINE.fullmatch(reference)
    if matched is None:
        return None
    return int(matched.group(1)), int(matched.group(2))


def parse_release_tag(reference: str) -> tuple[int, int, int] | None:
    matched = _RELEASE_TAG.fullmatch(reference)
    if matched is None:
        return None
    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def highest_release_tag(
    tags: tuple[str, ...],
    *,
    at_most: str | None = None,
) -> str | None:
    """Highest published ``vX.Y.Z`` tag, never a moving tip or pre-release.

    ``at_most`` is a CLI/distribution version (``0.8.0`` or ``v0.8.0``). Newer
    tags are ignored so an older installed CLI cannot parent to a newer line.
    """

    cap: tuple[int, int, int] | None = None
    if at_most is not None:
        raw = at_most if at_most.startswith("v") else f"v{at_most}"
        cap = parse_release_tag(raw)
        if cap is None:
            return None
    candidates = [
        (version, tag)
        for tag in tags
        if (version := parse_release_tag(tag)) is not None
        and (cap is None or version <= cap)
    ]
    if not candidates:
        return None
    return max(candidates)[1]


def release_lines(heads: tuple[str, ...]) -> tuple[str, ...]:
    parsed = [
        (parse_release_line(head), head) for head in heads if parse_release_line(head)
    ]
    return tuple(head for _version, head in sorted(parsed))


def next_release_line(current: str, heads: tuple[str, ...]) -> str | None:
    """Choose an explicit newer ``release/X.Y.x`` line, never ``HEAD``/``main``.

    A SHA pin has no line identity, so the latest available release line is the
    conservative jump. A current ``release/X.Y.x`` advances to the lowest strictly
    newer line. ``HEAD``/``main`` is already a moving tip.
    """

    available = release_lines(heads)
    if not available:
        return None
    if current in _MOVING_TIP_REFS:
        return None
    current_line = parse_release_line(current)
    if current_line is None:
        return available[-1]
    newer = [
        head
        for head in available
        if parse_release_line(head) is not None
        and parse_release_line(head) > current_line
    ]
    return newer[0] if newer else None


__all__ = [
    "highest_release_tag",
    "is_exact_git_object_id",
    "next_release_line",
    "parent_selector_kind",
    "parse_release_line",
    "parse_release_tag",
    "release_lines",
]
