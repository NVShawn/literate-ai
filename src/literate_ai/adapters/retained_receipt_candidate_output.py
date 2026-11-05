"""Exclusive external candidate publication through original held filesystem owners.

Failed stages are retained. No pathname-based rollback or deletion can discard a
concurrent foreign file. This cooperative local custody grants no execution.
"""

import os
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.exclusive_directory import _native_rename
from literate_ai.contracts.paths import canonical_relative_posix_path


def _node(node):
    return node.st_dev, node.st_ino, node.st_mode, node.st_uid


class _WindowsOutput:
    """Reuse the canonical native identity wrapper; rename the actual file HANDLE."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        from literate_ai._cache_lock import _Kernel32Windows

        self.kernel = _Kernel32Windows()
        self.ctypes = ctypes
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self.rename = dll.SetFileInformationByHandle
        self.rename.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.rename.restype = wintypes.BOOL

        # FILE_RENAME_INFO's first union is DWORD-sized on current supported
        # Windows. Natural HANDLE alignment preserves both x86 and x64/ARM64 ABI.
        class RenameInfo(ctypes.Structure):
            _fields_ = [
                ("flags", wintypes.DWORD),
                ("root", wintypes.HANDLE),
                ("length", wintypes.DWORD),
                ("name", wintypes.WCHAR * 1),
            ]

        self.info = RenameInfo

    def create(self, path):
        import msvcrt

        # CREATE_NEW, GENERIC_READ|GENERIC_WRITE|DELETE, FILE_SHARE_READ only.
        # This original handle prevents other writers/deleters while it lives.
        handle = self.kernel._create_file(
            str(path),
            0xC0010000,
            1,
            None,
            1,
            0x00200080,
            None,
        )
        if not handle or handle == self.kernel._invalid_handle:
            raise OSError(
                self.ctypes.get_last_error(), "exclusive candidate stage refused"
            )
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            self.kernel.close(handle)
            raise
        return descriptor

    def file_identity(self, descriptor):
        import msvcrt

        return self.kernel.file_identity(msvcrt.get_osfhandle(descriptor))

    def move(self, descriptor, destination):
        import msvcrt

        encoded = str(destination).encode("utf-16-le")
        offset = self.info.name.offset
        buffer = self.ctypes.create_string_buffer(
            max(self.ctypes.sizeof(self.info), offset + len(encoded) + 2)
        )
        info = self.info.from_buffer(buffer)
        info.flags = 0  # ReplaceIfExists FALSE: an existing destination refuses.
        info.root = None
        info.length = len(encoded)
        self.ctypes.memmove(
            self.ctypes.addressof(buffer) + offset, encoded, len(encoded)
        )
        if not self.rename(msvcrt.get_osfhandle(descriptor), 3, buffer, len(buffer)):
            raise OSError(
                self.ctypes.get_last_error(), "exclusive candidate move refused"
            )


def write_external_retained_candidate(path, content, *, project_root, require_current):
    """Publish only into the same held external parent; retain interrupted stages."""
    from literate_ai.test_receipts import MAXIMUM_PROJECT_TEST_RECEIPT_BYTES

    if (
        not callable(require_current)
        or type(content) is not bytes
        or not 0 < len(content) <= MAXIMUM_PROJECT_TEST_RECEIPT_BYTES
    ):
        raise TypeError("original candidate bytes/currentness guard required")
    destination, project = Path(path).absolute(), Path(project_root).absolute()
    if len(str(destination)) > 32760 or len(str(project)) > 32760:
        raise ValueError("bounded candidate/project paths required")
    if ".." in destination.parts or ".." in project.parts:
        raise ValueError("canonical candidate/project paths required")
    canonical_relative_posix_path(destination.name, label="external candidate name")
    if len(os.fsencode(destination.name)) > 255:
        raise ValueError("bounded candidate filename required")
    parent = destination.parent
    require_safe_directory(parent)
    require_safe_directory(project)
    if destination.is_relative_to(project):
        raise ValueError("retained.promotion.external-candidate-required")
    if len(parent.parts) + len(project.parts) > 1024:
        raise ValueError("bounded candidate ancestor closure required")
    windows = _WindowsOutput() if os.name == "nt" else None
    with ExitStack() as stack:
        ancestors = []
        held = {}
        parent_ancestors = set()
        for directory in (parent, project):
            current = Path(directory.anchor)
            descriptor = None
            for name in (None, *directory.parts[1:]):
                current = current if name is None else current / name
                if current in held:
                    descriptor = held[current]
                    continue
                require_safe_directory(current)
                node = current.lstat()
                if stat_is_link_or_reparse(node) or not stat.S_ISDIR(node.st_mode):
                    raise ValueError("candidate ancestor is unsafe")
                if windows is None:
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    descriptor = os.open(
                        current if name is None else name,
                        flags,
                        **({} if name is None else {"dir_fd": descriptor}),
                    )
                    stack.callback(os.close, descriptor)
                    identity = _node(os.fstat(descriptor))
                    if identity != _node(node):
                        raise ValueError("candidate ancestor changed while opening")
                else:
                    descriptor = windows.kernel.open_reparse_aware(
                        str(current), share_delete=False, share_write=False
                    )
                    stack.callback(windows.kernel.close, descriptor)
                    identity = windows.kernel.file_identity(descriptor)
                held[current] = descriptor
                ancestors.append((current, descriptor, _node(node), identity))
            if directory == parent:
                parent_ancestors = {row[3] for row in ancestors}
        project_identity = (
            _node(os.fstat(held[project]))
            if windows is None
            else windows.kernel.file_identity(held[project])
        )
        if project_identity in parent_ancestors:
            raise ValueError("retained.promotion.external-candidate-required")
        parent_fd = held[parent]
        private_stage = None
        source_parent_fd = parent_fd
        if windows is None:
            parent_node = os.fstat(parent_fd)
            if (
                parent_node.st_uid != os.geteuid()
                or stat.S_IMODE(parent_node.st_mode) & 0o022
            ):
                raise ValueError(
                    "retained.promotion.operator-owned-non-writable-parent-required"
                )

        def require_stage():
            if private_stage is not None:
                name, expected = private_stage
                opened = os.fstat(source_parent_fd)
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    _node(opened) != expected
                    or _node(named) != expected
                    or stat.S_IMODE(opened.st_mode) not in {0o700, 0o2700}
                    or opened.st_uid != os.geteuid()
                ):
                    raise ValueError(
                        "original private candidate staging directory changed"
                    )

        def current():
            for directory, descriptor, node, identity in ancestors:
                require_safe_directory(directory)
                if _node(directory.lstat()) != node:
                    raise ValueError("original candidate/project ancestor changed")
                if windows is None:
                    if _node(os.fstat(descriptor)) != identity:
                        raise ValueError("held candidate ancestor changed")
                else:
                    probe = windows.kernel.open_reparse_aware(str(directory))
                    try:
                        if (
                            windows.kernel.file_identity(descriptor) != identity
                            or windows.kernel.file_identity(probe) != identity
                        ):
                            raise ValueError("native candidate ancestor changed")
                    finally:
                        windows.kernel.close(probe)
            require_stage()
            require_current()
            require_stage()
            # Rejoin after the possibly long full project/source closure read.
            for directory, _descriptor, node, _identity in ancestors:
                if _node(directory.lstat()) != node or stat_is_link_or_reparse(
                    directory.lstat()
                ):
                    raise ValueError("candidate ancestor changed during authority read")

        def named(name, *, staged=False):
            if windows is None:
                return os.stat(
                    name,
                    dir_fd=source_parent_fd if staged else parent_fd,
                    follow_symlinks=False,
                )
            return (parent / name).lstat()

        current()
        if os.path.lexists(destination):
            raise FileExistsError("new external candidate required")
        temporary = ".litai-retained-candidate-" + secrets.token_hex(16)
        if windows is None:
            stage_name = ".litai-retained-stage-" + secrets.token_hex(16)
            os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
            source_parent_fd = os.open(
                stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            stack.callback(os.close, source_parent_fd)
            created_stage = os.fstat(source_parent_fd)
            named_stage = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _node(created_stage) != _node(named_stage)
                or not stat.S_ISDIR(created_stage.st_mode)
                or created_stage.st_uid != os.geteuid()
            ):
                raise ValueError("fresh exclusively owned candidate stage required")
            with os.scandir(source_parent_fd) as entries:
                if next(entries, None) is not None:
                    raise ValueError("fresh empty candidate stage required")
            # setgid inheritance is safe with all group/other permission bits
            # absent; retain that exact actual mode rather than repairing it.
            if stat.S_IMODE(created_stage.st_mode) not in {0o700, 0o2700}:
                raise ValueError("private candidate stage permissions are unsafe")
            private_stage = (stage_name, _node(os.fstat(source_parent_fd)))
            current()
            descriptor = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=source_parent_fd,
            )
        else:
            descriptor = windows.create(parent / temporary)
        stack.callback(os.close, descriptor)
        original = os.fstat(descriptor)
        original_native = (
            windows.file_identity(descriptor) if windows is not None else None
        )
        if (
            not stat.S_ISREG(original.st_mode)
            or original.st_nlink != 1
            or original.st_size != 0
        ):
            raise ValueError("new regular candidate stage required")
        # All names below are relative to the held POSIX directory, or to Windows
        # ancestors whose original native handles deny deletion/rename.
        view = memoryview(content)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("candidate write made no progress")
            view = view[count:]
        os.fsync(descriptor)

        def require_file(name, *, staged=False):
            current()
            node, opened = named(name, staged=staged), os.fstat(descriptor)
            if (
                stat_is_link_or_reparse(node)
                or not stat.S_ISREG(node.st_mode)
                or _node(node) != _node(opened)
                or _node(opened) != _node(original)
                or node.st_nlink != 1
                or opened.st_nlink != 1
                or node.st_size != len(content)
                or opened.st_size != len(content)
            ):
                raise ValueError("actual created candidate file changed")
            if windows is not None:
                probe = windows.kernel.open_reparse_aware(str(parent / name))
                try:
                    if (
                        windows.file_identity(descriptor) != original_native
                        or windows.kernel.file_identity(probe) != original_native
                    ):
                        raise ValueError("actual native candidate file changed")
                finally:
                    windows.kernel.close(probe)
            before = os.fstat(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            offset = 0
            while offset <= len(content):
                block = os.read(descriptor, min(65536, len(content) - offset + 1))
                if not block:
                    break
                if block != content[offset : offset + len(block)]:
                    raise ValueError("actual created candidate bytes changed")
                offset += len(block)
            after = os.fstat(descriptor)
            if offset != len(content) or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("candidate changed during same-descriptor readback")
            if _node(named(name, staged=staged)) != _node(after):
                raise ValueError("candidate pathname no longer names original file")
            current()

        require_file(temporary, staged=True)
        if windows is None:
            _native_rename(
                source_parent_fd,
                os.fsencode(temporary),
                os.fsencode(destination.name),
                destination_parent=parent_fd,
            )
        else:
            windows.move(descriptor, destination)
        require_file(destination.name)
        if windows is None:
            os.fsync(parent_fd)
        current()
        # No unlink or pathname rollback, on success OR failure. The exclusive
        # native move consumes the original file name on success. The private
        # staging directory, interrupted file or published output is retained
        # for explicit operator review; names never justify automatic deletion.
