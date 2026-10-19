"""Private verifier fixtures and bounded, no-follow observation of file effects."""

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_project_inputs import _remove_tree, _stamp, _stream
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_project_filesystem import (
    RetainedFilesystemCase,
    RetainedFilesystemState,
)


@dataclass(frozen=True, slots=True)
class RetainedFilesystemObservation:
    case_identity: ContentIdentity
    entries: tuple[RetainedFilesystemState, ...]

    def __post_init__(self):
        if (
            not isinstance(self.case_identity, ContentIdentity)
            or type(self.entries) is not tuple
            or any(not isinstance(v, RetainedFilesystemState) for v in self.entries)
        ):
            raise TypeError("retained.filesystem.typed-observation-required")
        names = tuple(v.path for v in self.entries)
        if names != tuple(sorted(set(names))):
            raise ValueError("retained.filesystem.canonical-observation-required")
        if any(v.kind != "absent" and v.mode is None for v in self.entries):
            raise ValueError("retained.filesystem.complete-observed-mode-required")

    def to_dict(self):
        return {
            "schema": "literate-ai/retained-filesystem-observation@1",
            "case_identity": self.case_identity.uri,
            "entries": [v.to_dict() for v in self.entries],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


class RetainedFilesystemWorkspace:
    def __init__(self, case, parent, outputs):
        if not isinstance(case, RetainedFilesystemCase):
            raise TypeError("retained.filesystem.typed-case-required")
        parent, outputs = Path(parent).absolute(), Path(outputs).absolute()
        require_safe_directory(parent)
        if parent == outputs or outputs in parent.parents:
            raise ValueError("retained.filesystem.case-cannot-mutate-exports")
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix="verifier-case-", dir=parent))
        self.owned = directory_node(self.root)
        self._closed = False
        try:
            for fixture in case.inputs:
                path = self.root / fixture.path
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(fixture.content)
                path.chmod(0o644)
            self._initial = self.observe()
        except BaseException:
            self.close()
            raise

    def require_initial(self):
        if self.observe() != self._initial:
            raise ValueError("retained.filesystem.fixtures-changed")

    def close(self):
        if self._closed:
            return
        try:
            current = directory_node(self.root)
        except FileNotFoundError:
            self._closed = True
            return
        except ValueError:
            self._closed = True
            raise ValueError(
                "retained.filesystem.case-replaced-during-cleanup"
            ) from None
        if current[:2] != self.owned[:2]:
            self._closed = True
            raise ValueError("retained.filesystem.case-replaced-during-cleanup")
        self.root.chmod(0o700)
        _remove_tree(self.root)
        self._closed = True

    def observe(self):
        if directory_node(self.root) != self.owned:
            raise ValueError("retained.filesystem.case-replaced")
        root_device = self.root.lstat().st_dev
        pending = [("", _stamp(self.root.lstat()))]
        observed = {}
        aliases = set()
        total = 0
        while pending:
            name, expected_stamp = pending.pop()
            directory = self.root / name
            require_safe_directory(directory)
            if _stamp(directory.lstat()) != expected_stamp:
                raise ValueError("retained.filesystem.directory-changed")
            with os.scandir(directory) as children:
                for child in children:
                    path = f"{name}/{child.name}" if name else child.name
                    canonical_relative_posix_paths(
                        (path,), label="observed verifier path"
                    )
                    if (
                        len(observed) >= self.case.limits.max_entries
                        or path.casefold() in aliases
                    ):
                        raise ValueError("retained.filesystem.entry-budget-or-alias")
                    aliases.add(path.casefold())
                    current = self.root / path
                    metadata = current.lstat()
                    if metadata.st_dev != root_device:
                        raise ValueError("retained.filesystem.mount-escape")
                    mode = stat.S_IMODE(metadata.st_mode)
                    if stat.S_ISDIR(metadata.st_mode):
                        observed[path] = RetainedFilesystemState(
                            path, "directory", mode=mode
                        )
                        pending.append((path, _stamp(metadata)))
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        size, _normalized_mode, digest = _stream(
                            self.root,
                            path,
                            min(
                                self.case.limits.max_file_bytes,
                                self.case.limits.max_total_bytes - total,
                            ),
                        )
                        if _stamp(current.lstat()) != _stamp(metadata):
                            raise ValueError("retained.filesystem.file-changed")
                        total += size
                        observed[path] = RetainedFilesystemState(
                            path, "file", size, digest, mode
                        )
                    else:
                        raise ValueError("retained.filesystem.link-or-special-node")
            if _stamp(directory.lstat()) != expected_stamp:
                raise ValueError("retained.filesystem.directory-changed")
        for expected in self.case.expected:
            if expected.path not in observed:
                observed[expected.path] = RetainedFilesystemState(
                    expected.path, "absent"
                )
        return RetainedFilesystemObservation(
            self.case.identity, tuple(observed[name] for name in sorted(observed))
        )

    def matches(self, observation):
        return retained_filesystem_matches(self.case, observation)


def retained_filesystem_matches(case, observation):
    if (
        not isinstance(observation, RetainedFilesystemObservation)
        or observation.case_identity != case.identity
    ):
        return False
    actual = {v.path: v for v in observation.entries}
    expected_names = {v.path for v in case.expected}
    allowed = set()
    for expected in case.expected:
        allowed.add(expected.path)
        parent = expected.path.rpartition("/")[0]
        while parent:
            allowed.add(parent)
            parent = parent.rpartition("/")[0]
        value = actual.get(expected.path)
        if (
            value is None
            or (value.kind, value.size, value.sha256)
            != (expected.kind, expected.size, expected.sha256)
            or (expected.mode is not None and value.mode != expected.mode)
        ):
            return False
    return set(actual) <= allowed and all(
        value.kind == "directory"
        for name, value in actual.items()
        if name not in expected_names
    )
