"""Bounded native FreeBSD hints and global library-map custody.

The loader's configuration is evidence, not an execution mechanism. Constrained
libmap rules have process-wide order effects and are explicitly unqualified here.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.freebsd_loader_paths import FreeBsdLoaderPaths
from literate_ai.adapters.user_paths import FreeBsdLoaderSystemPaths
from literate_ai.contracts import canonical_identity

from .types import DependencyObservationError

_MAX_FILES = 256
_MAX_ENTRIES = 100_000
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024


def _fail(kind, message):
    raise DependencyObservationError("dependencies.freebsd-" + kind, message)


def _path(value):
    try:
        FreeBsdLoaderPaths((str(value),))
    except (TypeError, ValueError) as exc:
        _fail("path-unsafe", f"Loader path is not qualified: {type(exc).__name__}")
    return Path(value)


def parse_freebsd_hints(text: str, expected_path: Path) -> tuple[str, ...]:
    """Parse `ldconfig -r` directory policy, not Linux's soname cache format."""
    if not isinstance(text, str) or len(text) > 16 * 1024 * 1024:
        _fail("hints-invalid", "FreeBSD hints report exceeds its bound")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if (
        len(lines) < 2
        or len(lines) > _MAX_ENTRIES
        or lines[0] != str(expected_path) + ":"
        or not lines[1].startswith("search directories: ")
    ):
        _fail("hints-invalid", "FreeBSD hints report has an unknown header")
    directories = lines[1].removeprefix("search directories: ").split(":")
    for directory in directories:
        _path(directory)
    if len(directories) > 128:
        _fail("hints-invalid", "FreeBSD hints contain too many search paths")
    for line in lines[2:]:
        match = re.fullmatch(r"[0-9]+:-l[^\s]+\s+=>\s+(/\S+)", line)
        if match is None:
            _fail("hints-invalid", "FreeBSD hints report has an unknown entry")
        _path(match[1])
    return tuple(dict.fromkeys(directories))


def _stamp(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _file_fact(path):
    require_safe_directory(path.parent, allow_missing=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
        _fail(
            "config-unsafe", "Loader configuration is redirected, special or oversized"
        )
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        content = stream.read(_MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (
        _stamp(before) != _stamp(opened)
        or _stamp(before) != _stamp(after)
        or _stamp(before) != _stamp(path.lstat())
        or len(content) != before.st_size
    ):
        _fail("config-changed", "Loader configuration changed during capture")
    return (_stamp(before), hashlib.sha256(content).hexdigest(), content)


def _directory_fact(path):
    require_safe_directory(path, allow_missing=True)
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    records = []
    with os.scandir(path) as entries:
        for entry in entries:
            if len(records) >= _MAX_ENTRIES:
                _fail("config-limit", "Library-map directory exceeds its entry bound")
            records.append((entry.name, _stamp(entry.stat(follow_symlinks=False))))
    if _stamp(path.stat()) != _stamp(before):
        _fail("config-changed", "Library-map directory changed during capture")
    # Ambiguous mappings refuse; sorting avoids a receipt changing just because
    # directory enumeration order changes when the rule result is identical.
    return (_stamp(before), tuple(sorted(records)))


class FreeBsdLoaderState:
    def __init__(self, paths: FreeBsdLoaderSystemPaths):
        self.paths = paths
        self.files = {}
        self.directories = {}
        self.maps = {}
        self.hints = ()
        self._bytes = 0
        self._entries = 0
        self._visited = set()
        hints = self._file(_path(paths.hints))
        if hints is None:
            _fail("hints-missing", "Native FreeBSD hints are unavailable")
        self._parse_file(_path(paths.libmap))

    def _file(self, path):
        if path in self.files:
            return self.files[path]
        if len(self.files) + len(self.directories) >= _MAX_FILES:
            _fail("config-limit", "Loader configuration exceeds its file bound")
        fact = _file_fact(path)
        if fact is not None:
            self._bytes += len(fact[2])
            if self._bytes > _MAX_TOTAL_BYTES:
                _fail("config-limit", "Loader configuration exceeds its byte bound")
        self.files[path] = fact
        return fact

    def _parse_file(self, path):
        fact = self._file(path)
        if fact is None:
            return
        inode = fact[0][:2]
        if inode in self._visited:
            return
        self._visited.add(inode)
        try:
            text = fact[2].decode("utf-8")
        except UnicodeError:
            _fail("libmap-invalid", "Library-map configuration is not UTF-8")
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("["):
                _fail(
                    "libmap-constrained", "Constrained library maps are not qualified"
                )
            tokens = line.split()
            if len(tokens) != 2 or any(len(token) > 4096 for token in tokens):
                _fail("libmap-invalid", "Library-map configuration has an unknown rule")
            source, target = tokens
            if source == "include":
                self._parse_file(_path(target))
            elif source == "includedir":
                self._parse_directory(_path(target))
            else:
                for value in tokens:
                    if any(ord(c) < 32 or c in "$[]\\" for c in value):
                        _fail("libmap-invalid", "Library map uses an unmodeled token")
                    if "/" in value:
                        _path(value)
                previous = self.maps.get(source)
                if previous is not None and previous != target:
                    _fail(
                        "libmap-ambiguous", "Conflicting library maps are not qualified"
                    )
                self.maps[source] = target

    def _parse_directory(self, path):
        if path in self.directories:
            return
        if len(self.files) + len(self.directories) >= _MAX_FILES:
            _fail("config-limit", "Loader configuration exceeds its file bound")
        fact = _directory_fact(path)
        self._entries += len(fact[1]) if fact else 0
        if self._entries > _MAX_ENTRIES:
            _fail("config-limit", "Library-map directories exceed their entry bound")
        self.directories[path] = fact
        if fact is None:
            return
        for name, stamp in fact[1]:
            # rtld's includedir admits regular .conf entries, excluding links.
            if name.endswith(".conf") and stat.S_ISREG(stamp[2]):
                self._parse_file(path / name)

    def bind_hints(self, text):
        self.hints = parse_freebsd_hints(text, _path(self.paths.hints))
        self.require_unchanged()

    def require_unchanged(self):
        for path, fact in self.files.items():
            if _file_fact(path) != fact:
                _fail("config-changed", "FreeBSD loader configuration changed")
        for path, fact in self.directories.items():
            if _directory_fact(path) != fact:
                _fail("config-changed", "FreeBSD library-map directory changed")

    def projection(self):
        return {
            "schema": "literate-ai/freebsd-loader-state@1",
            "hints": list(self.hints),
            "defaults": [str(path) for path in self.paths.defaults],
            "interpreter": str(self.paths.interpreter),
            "maps": sorted(self.maps.items()),
            "files": [
                {"path": str(path), "sha256": fact[1] if fact else None}
                for path, fact in sorted(self.files.items())
            ],
            "directories": [
                {
                    "path": str(path),
                    "entries": [r[0] for r in fact[1]] if fact else None,
                }
                for path, fact in sorted(self.directories.items())
            ],
        }

    @property
    def identity(self):
        return canonical_identity(self.projection())
