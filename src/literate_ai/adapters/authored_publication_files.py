"""Transaction-local, bounded immutable writes under original directory owners.

Reuse qualification directory custody and the CAS's atomic create-if-absent link
pattern. POSIX operations use checked original parent descriptors; Windows holds
native ancestors against deletion while using their current names. Saved paths
and metadata never restore this owner or authorize cleanup.
"""

import os
import stat
import uuid
from contextlib import ExitStack, suppress

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.qualification_workspace_namespace import (
    hold_qualification_workspace_namespace,
)
from literate_ai.adapters.retained_project_inputs import _same_open_file
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.storage.cas import _native_cas_path


class AuthoredPublicationFiles:
    def __init__(self, current, *, writable):
        if (
            not isinstance(current, CurrentAuthoredQualificationPlan)
            or type(writable) is not bool
        ):
            raise TypeError(
                "original current authored plan and explicit writer mode required"
            )
        self.current = current
        self.root = current.oracles.authority.project.root
        self.writable = writable
        self.stack = ExitStack()
        self.access = None
        self.held = {}
        self.active = False
        self._original = (self, current, self.root, writable)

    def __enter__(self):
        self.current.require_unchanged()
        require_safe_directory(self.root)
        try:
            if os.name == "posix":
                from literate_ai.adapters.retained_project_inputs import (
                    _DirectoryAccess,
                )

                self.access = _DirectoryAccess(self.root, 512)
                self.stack.callback(self.access.close)
            else:
                self.held[()] = self.stack.enter_context(
                    hold_qualification_workspace_namespace(self.root)
                )
            self.active = True
            self.require_current()
            return self
        except BaseException:
            self.active = False
            self.stack.close()
            raise

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.require_current()
        finally:
            self.active = False
            self.stack.close()

    def require_current(self):
        if not self.active:
            raise ValueError("qualification.authored.publication_owner_expired")
        if self._original != (self, self.current, self.root, self.writable):
            raise ValueError(
                "qualification.authored.original_publication_owner_required"
            )
        if self._original[0] is not self or self._original[1] is not self.current:
            raise ValueError(
                "qualification.authored.original_publication_owner_required"
            )
        self.current.require_unchanged()
        if self.access is not None:
            self.access.require_current()
        else:
            for check in self.held.values():
                check()

    @staticmethod
    def parts(relative):
        parts = canonical_relative_posix_path(relative, label="publication path").parts
        if len(parts) > 64:
            raise ValueError("qualification.authored.publication_path_limit")
        return parts

    def _directory(self, parts, *, create=False, exclusive=False):
        self.require_current()
        for index, name in enumerate(parts):
            parent = parts[:index]
            child = parts[: index + 1]
            if self.access is not None:
                descriptor = self.access.directory(parent)[0]
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        if exclusive and index == len(parts) - 1:
                            raise
                self.access.directory(child)
            else:
                path = self.root.joinpath(*child)
                if child not in self.held:
                    if create:
                        try:
                            os.mkdir(_native_cas_path(path), 0o700)
                        except FileExistsError:
                            if exclusive and index == len(parts) - 1:
                                raise
                    require_safe_directory(path)
                    self.held[child] = self.stack.enter_context(
                        hold_qualification_workspace_namespace(path)
                    )
                elif create and exclusive and index == len(parts) - 1:
                    raise FileExistsError(path)
        self.require_current()
        return self.access.directory(parts)[0] if self.access is not None else None

    def mkdir(self, relative, *, exclusive=False):
        if self.writable is not True:
            raise ValueError("qualification.authored.publication_read_only")
        self._directory(self.parts(relative), create=True, exclusive=exclusive)

    def _location(self, parts, descriptor):
        if descriptor is not None:
            return parts[-1], {"dir_fd": descriptor}
        return _native_cas_path(self.root.joinpath(*parts)), {}

    def read(self, relative, *, max_bytes):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("qualification.authored.publication_byte_limit")
        parts = self.parts(relative)
        parent = self._directory(parts[:-1])
        name, kwargs = self._location(parts, parent)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        # Pin Windows leaves as well as parents: CRT O_NOFOLLOW is unavailable.
        with ExitStack() as leaves:
            if os.name == "nt":
                from literate_ai._cache_lock import _Kernel32Windows

                kernel = _Kernel32Windows()
                handle = kernel.open_reparse_aware(
                    str(self.root.joinpath(*parts)),
                    share_delete=False,
                    share_write=False,
                )
                leaves.callback(kernel.close, handle)
                if stat_is_link_or_reparse(os.lstat(name)):
                    raise ValueError("qualification.authored.publication_file_invalid")
            descriptor = os.open(name, flags, **kwargs)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                    raise ValueError("qualification.authored.publication_file_invalid")
                chunks, total = [], 0
                while block := os.read(
                    descriptor, min(1024 * 1024, max_bytes - total + 1)
                ):
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError(
                            "qualification.authored.publication_byte_limit"
                        )
                    chunks.append(block)
                after = os.fstat(descriptor)
                current = os.stat(name, follow_symlinks=False, **kwargs)
                fields = (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
                if any(
                    getattr(before, n) != getattr(after, n) for n in fields
                ) or not _same_open_file(current, after):
                    raise ValueError("qualification.authored.publication_file_changed")
                self.require_current()
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def write(self, relative, payload, *, max_bytes, reuse=False, before_link=None):
        if self.writable is not True:
            raise ValueError("qualification.authored.publication_read_only")
        if (
            not isinstance(payload, bytes)
            or type(max_bytes) is not int
            or not 0 < len(payload) <= max_bytes
        ):
            raise ValueError("qualification.authored.publication_byte_limit")
        parts = self.parts(relative)
        parent = self._directory(parts[:-1], create=True)
        temporary_parts = (*parts[:-1], ".t-" + uuid.uuid4().hex)
        temporary, kwargs = self._location(temporary_parts, parent)
        destination, _ = self._location(parts, parent)
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            **kwargs,
        )
        owned = os.fstat(descriptor)
        try:
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("publication write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.require_current()
            if before_link is not None:
                before_link()
            link_args = (
                {} if parent is None else {"src_dir_fd": parent, "dst_dir_fd": parent}
            )
            try:
                os.link(temporary, destination, follow_symlinks=False, **link_args)
            except FileExistsError:
                if not reuse or self.read(relative, max_bytes=max_bytes) != payload:
                    raise
            if parent is not None:
                os.fsync(parent)
            if self.read(relative, max_bytes=max_bytes) != payload:
                raise ValueError("qualification.authored.publication_file_changed")
        finally:
            # Only the exact temporary inode created by this live operation may
            # be collected. Interrupted/foreign state stays for diagnosis.
            with suppress(OSError, ValueError, RuntimeError):
                self.require_current()
                observed = os.stat(temporary, follow_symlinks=False, **kwargs)
                if (observed.st_dev, observed.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(temporary, **kwargs)
