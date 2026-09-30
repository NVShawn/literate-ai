"""Exact, explicitly reviewed input custody; never grants lifecycle acceptance."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory
from literate_ai.adapters.intelligence import generated_source_tree_identity
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths


class RetainedSourceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def _read(root: Path) -> tuple[tuple[str, bytes], ...]:
    if root.absolute() != root.resolve(strict=True) or path_is_link_or_reparse(root):
        raise RetainedSourceError(
            "retained_source.redirected", "Source root must not use links"
        )
    if not root.is_dir():
        raise RetainedSourceError(
            "retained_source.not_directory", "Source must be a directory"
        )
    require_safe_directory(root)
    files: list[tuple[str, bytes]] = []
    total = 0
    entries = 0

    def refuse_unreadable(error: OSError) -> None:
        raise RetainedSourceError(
            "retained_source.unreadable",
            "Source directory could not be completely read",
        ) from error

    for directory, dirs, names in os.walk(
        root, followlinks=False, onerror=refuse_unreadable
    ):
        require_safe_directory(Path(directory))
        for name in sorted(dirs + names):
            entries += 1
            if entries > 4096 or len(files) >= 1024:
                raise RetainedSourceError(
                    "retained_source.too_large", "Source exceeds entry limit"
                )
            path = Path(directory) / name
            relative = "source/" + path.relative_to(root).as_posix()
            if (
                len(relative.encode("utf-8")) > 512
                or len(path.relative_to(root).parts) + 1 > 32
            ):
                raise RetainedSourceError(
                    "retained_source.path_limit",
                    "Source path exceeds generation limits",
                )
            if ".codegraph" in path.relative_to(root).parts:
                raise RetainedSourceError(
                    "retained_source.reserved", "Source contains reserved index state"
                )
            metadata = path.lstat()
            if path_is_link_or_reparse(path):
                raise RetainedSourceError(
                    "retained_source.redirected", "Source contains a link"
                )
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RetainedSourceError(
                    "retained_source.special_file", "Source contains a special file"
                )
            if (
                metadata.st_size > 8 * 1024 * 1024
                or total + metadata.st_size > 16 * 1024 * 1024
            ):
                raise RetainedSourceError(
                    "retained_source.too_large", "Source exceeds byte limit"
                )
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise RetainedSourceError(
                        "retained_source.special_file", "Source entry changed type"
                    )
                content = stream.read(8 * 1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
            if (
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                or len(content) != metadata.st_size
                or path_is_link_or_reparse(path)
                or (opened.st_size, opened.st_mtime_ns)
                != (after.st_size, after.st_mtime_ns)
            ):
                raise RetainedSourceError(
                    "retained_source.changed", "Source changed while reading"
                )
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RetainedSourceError(
                    "retained_source.binary", "Only UTF-8 retained source is supported"
                ) from exc
            total += len(content)
            files.append((relative, content))
    if not files:
        raise RetainedSourceError("retained_source.empty", "Source tree is empty")
    try:
        canonical_relative_posix_paths(
            (name for name, _content in files), label="retained source"
        )
    except ValueError as exc:
        raise RetainedSourceError(
            "retained_source.invalid_paths", "Source paths are not portable and unique"
        ) from exc
    return tuple(sorted(files))


@dataclass(frozen=True, slots=True)
class RetainedSourceInput:
    root: Path
    files: tuple[tuple[str, bytes], ...]
    component_lock_identity: ContentIdentity
    project_authority_identity: ContentIdentity
    target: str

    @classmethod
    def capture(
        cls,
        root: Path,
        *,
        component_lock_identity: ContentIdentity,
        project_authority_identity: ContentIdentity,
        target: str,
    ) -> RetainedSourceInput:
        if not isinstance(component_lock_identity, ContentIdentity) or not isinstance(
            project_authority_identity, ContentIdentity
        ):
            raise TypeError(
                "Retained source requires typed lock and project authority identities"
            )
        if not isinstance(target, str) or not target:
            raise ValueError("Retained source requires an exact target")
        root = root.absolute()
        value = cls(
            root,
            _read(root),
            component_lock_identity,
            project_authority_identity,
            target,
        )
        value.require_unchanged()
        return value

    @property
    def tree_identity(self) -> ContentIdentity:
        return ContentIdentity.parse_uri(
            generated_source_tree_identity(dict(self.files))
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/retained-source-input@1",
            "origin": "operator-retained-source",
            "tree_identity": self.tree_identity.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "project_authority_identity": self.project_authority_identity.to_dict(),
            "target": self.target,
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def require_authorization(self, authorization: str | None) -> None:
        if authorization != self.identity.uri:
            raise RetainedSourceError(
                "retained_source.authorization_mismatch",
                "Authorize the exact current retained-source plan identity",
            )
        self.require_unchanged()

    def require_unchanged(self) -> None:
        if _read(self.root) != self.files:
            raise RetainedSourceError(
                "retained_source.changed", "Retained source changed after review"
            )
