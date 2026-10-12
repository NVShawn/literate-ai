"""Read operator-provisioned FINALIZE grants without caching or issuing permission.

Private startup owns the path. Its directory must be protected from generated
code and remote requests by the deployment; these checks do not provide OS
isolation. Atomic file replacement permits revocation; directory replacement
requires a fresh private startup. This module never writes grant state.
"""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import stat_is_link_or_reparse
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_authority import FinalizeExecutionGuard
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.contracts import ContentIdentity
from literate_ai.security import BuildAuthorization

MAX_FINALIZE_GRANT_BYTES = 65536


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate grant field")
        result[key] = value
    return result


def _node(metadata):
    if stat_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("grant is not a regular file")
    if not 0 < metadata.st_size <= MAX_FINALIZE_GRANT_BYTES:
        raise ValueError("grant size exceeds bound")
    node = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if os.name != "nt":
        return (*node, metadata.st_ctime_ns)
    # Windows CPython 3.12 pathname stat reports creation time in st_ctime,
    # while fstat reports metadata change time, so the grant's lstat and fstat
    # never agree there. Compare creation time instead; 3.11 has no
    # st_birthtime_ns, but both of its APIs report creation time in st_ctime.
    return (*node, getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns))


class PrivateFinalizeGrantProvider:
    """A startup-selected grant file; every invocation reopens current bytes."""

    def __init__(self, path):
        if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
            raise ValueError("private grant requires an absolute path")
        self.path = path
        self.parent = directory_node(path.parent)

    def __call__(self):
        try:
            if directory_node(self.path.parent) != self.parent:
                raise ValueError("grant directory changed")
            before = _node(self.path.lstat())
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(self.path, flags)
            try:
                if _node(os.fstat(descriptor)) != before:
                    raise ValueError("grant changed before read")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FINALIZE_GRANT_BYTES + 1)
                if (
                    len(raw) != before[3]
                    or _node(os.fstat(descriptor)) != before
                    or _node(self.path.lstat()) != before
                    or directory_node(self.path.parent) != self.parent
                ):
                    raise ValueError("grant changed during read")
            finally:
                os.close(descriptor)
            return BuildAuthorization.from_dict(
                json.loads(raw, object_pairs_hook=_unique)
            )
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError) as exc:
            raise ActionWireError(
                "action_finalize.grant_unavailable",
                "private FINALIZE grant unavailable",
            ) from exc


class FileFinalizeExecutionAuthority:
    """Shared startup composition for supervision and exact child stage checks."""

    def __init__(
        self,
        *,
        grant_path,
        runtime_identity,
        observe_runtime,
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(runtime_identity, ContentIdentity) or not all(
            callable(item) for item in (observe_runtime, clock)
        ):
            raise TypeError("FINALIZE requires a measured runtime and current clock")
        self.provider = PrivateFinalizeGrantProvider(grant_path)
        self.runtime_identity = runtime_identity
        self.observe_runtime = observe_runtime
        self.clock = clock

    def _guard(self, value):
        return FinalizeExecutionGuard(
            value,
            self.runtime_identity,
            grant_provider=self.provider,
            observe_runtime=self.observe_runtime,
            clock=self.clock,
        )

    def __call__(self, value):
        self._guard(value)(value)

    def require_stage(self, stage, prepared):
        from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs

        if not isinstance(prepared, FinalizeRuntimeInputs):
            raise ActionWireError(
                "action_finalize.authority_mismatch", "unexpected FINALIZE input"
            )
        self._guard(prepared.intent).require_stage(stage, prepared)
