"""Inert, explicit target source projection selected by locked Python authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .python_lock import PythonWheelLock, _strict_object
from .types import DependencyObservationError

_SCHEMA = "literate-ai/python-source-projection@1"
_MAX_BYTES = 1024 * 1024


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-projection-invalid", message)


@dataclass(frozen=True)
class PythonSourceProjection:
    path: str
    sha256: str
    excluded_paths: tuple[str, ...]


def prepare_python_source_projection(
    files: Mapping[str, object], *, path: str, lock: PythonWheelLock
) -> PythonSourceProjection:
    """Bind exact excluded source bytes and the full selected marker/tag target."""
    if not isinstance(path, str):
        _fail("Projection requires a string manifest path")
    location = PurePosixPath(path)
    if (
        location.is_absolute()
        or location.as_posix() != path
        or ".." in location.parts
        or "\\" in path
        or ":" in path
        or location.name != "python-source-projection.json"
    ):
        _fail("Projection requires one canonical relative manifest path")
    content = files.get(path)
    if not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_BYTES:
        _fail("Projection manifest must be bounded UTF-8 source text")
    try:
        document = json.loads(content, object_pairs_hook=_strict_object)
    except (ValueError, RecursionError, DependencyObservationError) as exc:
        raise DependencyObservationError(
            "dependencies.python-projection-invalid", "Invalid projection JSON"
        ) from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "environment", "tags", "excluded"}
        or document["schema"] != _SCHEMA
        or document["environment"] != dict(lock.environment)
        or document["tags"] != list(lock.tags)
    ):
        _fail("Projection must bind the exact wheel-lock environment and tags")
    excluded = document["excluded"]
    if not isinstance(excluded, list) or not 1 <= len(excluded) <= 4096:
        _fail("Projection requires a bounded nonempty excluded-file inventory")
    paths = []
    for record in excluded:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "reason"}:
            _fail("Projection exclusions require a path, exact hash and review reason")
        filename, digest, reason = record["path"], record["sha256"], record["reason"]
        if not isinstance(filename, str) or len(filename) > 4096:
            _fail("Excluded Python paths must be bounded strings")
        selected = PurePosixPath(filename)
        if (
            selected.is_absolute()
            or selected.as_posix() != filename
            or ".." in selected.parts
            or "\\" in filename
            or ":" in filename
            or selected.suffix != ".py"
            or not selected.is_relative_to(location.parent)
        ):
            _fail("Excluded Python paths must be canonical and inside the package root")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 512
            or any(ord(char) < 32 for char in reason)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            _fail("Exclusions require an exact SHA-256 and bounded review reason")
        original = files.get(filename)
        if (
            not isinstance(original, str)
            or hashlib.sha256(original.encode("utf-8")).hexdigest() != digest
        ):
            _fail("Excluded source is absent or differs from its reviewed bytes")
        paths.append(filename)
    if paths != sorted(paths) or len({name.casefold() for name in paths}) != len(paths):
        _fail("Projection paths must be sorted and unique, including case aliases")
    return PythonSourceProjection(
        path, hashlib.sha256(content.encode("utf-8")).hexdigest(), tuple(paths)
    )
