"""Hold original run-directory handles; grant no execution or publication rights."""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai._filesystem import require_safe_directory


@contextmanager
def hold_qualification_workspace_namespace(path):
    """Retain actual ancestor identities until the run owner releases custody.

    POSIX reuses the bounded retained-input directory owner, without its optional
    handle-free fallback. Windows holds native directory handles with deletion
    sharing disabled and compares current names through the same native API.
    Neither branch serializes handles or restores an owner from saved metadata.
    """

    path = Path(path).absolute()
    require_safe_directory(path)
    active = True
    with ExitStack() as stack:
        if os.name == "posix":
            from literate_ai.adapters.retained_project_inputs import _DirectoryAccess

            access = _DirectoryAccess(path, len(path.parts) + 2)
            stack.callback(access.close)
            check = access.require_current
        elif os.name == "nt":
            from literate_ai._cache_lock import _Kernel32Windows

            kernel = _Kernel32Windows()
            ancestors = (*reversed(path.parents), path)
            held = []
            for ancestor in ancestors:
                handle = kernel.open_reparse_aware(
                    str(ancestor), share_delete=False, share_write=True
                )
                stack.callback(kernel.close, handle)
                held.append((ancestor, handle, kernel.file_identity(handle)))

            def check():
                require_safe_directory(path)
                for ancestor, handle, expected in held:
                    probe = kernel.open_reparse_aware(str(ancestor))
                    try:
                        if (
                            kernel.file_identity(handle) != expected
                            or kernel.file_identity(probe) != expected
                        ):
                            raise ValueError("qualification.authored.workspace_changed")
                    finally:
                        kernel.close(probe)
        else:
            raise ValueError("qualification.authored.workspace_platform_unsupported")

        def require_current():
            if not active:
                raise ValueError("qualification.authored.workspace_owner_expired")
            check()

        try:
            require_current()
            yield require_current
            require_current()
        finally:
            active = False
