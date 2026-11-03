"""Initial scratch ownership inside a current retained Cargo wrapper.

Only fresh, absent roots are provisioned here. Existing outputs require separate
current package/provenance reopening; ignored files never supply that authority.
No native package, manifest edit, execution or admission is authorized here.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters._write_reservations import (
    WriteReservationTarget,
    acquire_write_reservations,
)
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.exclusive_directory import (
    directory_node,
    publish_directory_between_owned_parents_exclusive,
)
from literate_ai.adapters.retained_cargo_tree_v2 import capture_retained_cargo_tree_v2
from literate_ai.adapters.retained_cargo_wrapper_v2 import RetainedCargoWrapperContextV2

_CREATION_KEY = object()


def _ignored(context, paths):
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("retained.cargo.v2-scratch-git-required")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    root = context.reviewed.project.root

    def run(arguments, content=None):
        return run_bounded_process(
            (executable, "-c", "core.excludesFile=" + os.devnull, *arguments),
            cwd=root,
            environment=environment,
            timeout_seconds=10,
            stdout_limit_bytes=65536,
            stderr_limit_bytes=4096,
            input_bytes=content,
            error_prefix="retained.cargo.v2-scratch-git",
        )

    repository = run(("rev-parse", "--show-toplevel"))
    reported = Path(os.fsdecode(repository.stdout).rstrip("\r\n"))
    if (
        repository.returncode
        or reported != root
        or directory_node(reported) != directory_node(root)
    ):
        raise ValueError("retained.cargo.v2-scratch-project-git-required")
    names = tuple(path.relative_to(root).as_posix() + "/" for path in paths)
    content = b"".join(os.fsencode(name) + b"\0" for name in names)
    result = run(("check-ignore", "--no-index", "-z", "--stdin"), content)
    if result.returncode or result.stdout != content:
        raise ValueError("retained.cargo.v2-scratch-ignore-required")


def _require_absent(path, *, maximum_entries):
    require_safe_directory(path.parent)
    alias = unicodedata.normalize("NFC", path.name.casefold())
    for index, entry in enumerate(path.parent.iterdir()):
        if index >= maximum_entries:
            raise ValueError("retained.cargo.v2-scratch-parent-limit")
        if unicodedata.normalize("NFC", entry.name.casefold()) == alias:
            raise ValueError("retained.cargo.v2-scratch-existing-root")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError("retained.cargo.v2-scratch-existing-root")


@dataclass(slots=True)
class _ScratchLifetime:
    active: bool = True


@dataclass(init=False, frozen=True, eq=False)
class RetainedCargoScratchScopeV2:
    """Live reservations and current source custody, never a saved capability."""

    def __init__(self, *, key, context, reservations, nodes, source, lifetime):
        if key is not _CREATION_KEY:
            raise TypeError("retained.cargo.v2-live-scratch-required")
        for name, value in (
            ("context", context),
            ("_reservations", reservations),
            ("_nodes", nodes),
            ("source", source),
            ("_lifetime", lifetime),
        ):
            object.__setattr__(self, name, value)

    def __copy__(self):
        raise TypeError("retained.cargo.v2-scratch-not-copyable")

    def __deepcopy__(self, memo):
        raise TypeError("retained.cargo.v2-scratch-not-copyable")

    def __reduce_ex__(self, protocol):
        raise TypeError("retained.cargo.v2-scratch-not-serializable")

    def __getstate__(self):
        raise TypeError("retained.cargo.v2-scratch-not-serializable")

    @property
    def workspace_exclusions(self):
        self.require_unchanged()
        return tuple(root.path for root in self.context.reviewed.plan.scratch_roots)

    def require_unchanged(self):
        if not self._lifetime.active:
            raise ValueError("retained.cargo.v2-scratch-operation-ended")
        self.context.require_unchanged()
        self._reservations.verify_all()
        if any(directory_node(path) != node for path, node in self._nodes):
            raise ValueError("retained.cargo.v2-scratch-root-changed")
        _ignored(self.context, tuple(path for path, _ in self._nodes))
        self.source.require_unchanged()
        if any(directory_node(path) != node for path, node in self._nodes):
            raise ValueError("retained.cargo.v2-scratch-root-changed")
        self._reservations.verify_all()
        self.context.require_unchanged()


@contextmanager
def reserve_retained_cargo_scratch_v2(
    context: RetainedCargoWrapperContextV2,
    *,
    maximum_metadata_bytes: int = 1024 * 1024 * 1024,
):
    """Prove complete source and absence before creating any excluded root.

    All destination parents must already exist. No authored namespace is created
    on the way to an output. Identified private staging inodes are published
    exclusively; public paths are never adopted after an unguarded mkdir.
    Roots and private stages are retained when the scope ends, including empty
    roots. Reuse and recovery require separate current provenance, never a
    check-then-delete operation on a public name.
    Existing packages and outputs cannot be reopened through this initial path.
    """
    if type(context) is not RetainedCargoWrapperContextV2:
        raise TypeError("retained.cargo.v2-live-wrapper-required")
    context.require_unchanged()
    reviewed = context.reviewed
    declarations = reviewed.plan.scratch_roots
    if not declarations:
        raise ValueError("retained.cargo.v2-scratch-declaration-required")
    roots = tuple(context.workspace / item.path for item in declarations)
    budget = next(
        item.budget
        for item in reviewed.plan.input_custody.owners
        if item.scope == "workspace"
    )
    for path in roots:
        _require_absent(path, maximum_entries=budget.maximum_entries + len(roots))
    _ignored(context, roots)
    before = capture_retained_cargo_tree_v2(
        context.workspace,
        budget=budget,
        maximum_metadata_bytes=maximum_metadata_bytes,
    )
    context.require_unchanged()
    root = reviewed.project.root
    anchor = directory_node(root)
    targets = tuple(
        WriteReservationTarget(
            root
            / ".litai-locks"
            / (
                "cargo-scratch-"
                + hashlib.sha256(
                    path.relative_to(root).as_posix().casefold().encode()
                ).hexdigest()[:32]
                + ".lock"
            ),
            root,
            anchor,
        )
        for path in roots
    )
    stage_parent = (
        root / ".litai-locks" / ("cargo-scratch-stage-" + secrets.token_hex(12))
    )
    targets += (WriteReservationTarget(stage_parent / "owner.lock", root, anchor),)
    nodes = []
    lifetime = _ScratchLifetime()
    with acquire_write_reservations(targets) as reservations:
        try:
            before.require_unchanged()
            context.require_unchanged()
            if not reservations.owns_directory(stage_parent):
                raise ValueError("retained.cargo.v2-scratch-private-parent-required")
            private_parent_node = directory_node(stage_parent)
            for index, path in enumerate(roots):
                reservations.verify_all()
                _require_absent(
                    path, maximum_entries=budget.maximum_entries + len(roots)
                )
                stage = stage_parent / ("s" + str(index))
                stage.mkdir(mode=0o700)
                stage_node = directory_node(stage)
                if any(stage.iterdir()):
                    raise ValueError("retained.cargo.v2-scratch-stage-not-empty")
                publish_directory_between_owned_parents_exclusive(
                    stage,
                    path,
                    expected_source=stage_node,
                    expected_source_parent=private_parent_node,
                    expected_destination_parent=directory_node(path.parent),
                )
                if directory_node(path) != stage_node or any(path.iterdir()):
                    raise ValueError("retained.cargo.v2-scratch-publication-changed")
                nodes.append((path, stage_node))
            source = capture_retained_cargo_tree_v2(
                context.workspace,
                budget=budget,
                exclusions=tuple(item.path for item in declarations),
                maximum_metadata_bytes=maximum_metadata_bytes,
            )
            if any(
                getattr(before, name) != getattr(source, name)
                for name in ("directories", "files", "aliases", "_physical")
            ):
                raise ValueError("retained.cargo.v2-scratch-source-changed")
            scope = RetainedCargoScratchScopeV2(
                key=_CREATION_KEY,
                context=context,
                reservations=reservations,
                nodes=tuple(nodes),
                source=source,
                lifetime=lifetime,
            )
            scope.require_unchanged()
            yield scope
            scope.require_unchanged()
        finally:
            lifetime.active = False
            # Preserve stages, public roots and replacements. The reservation
            # owner may retire only its marker, never public scratch contents.
            reservations.transfer_committed_directories((stage_parent,))
