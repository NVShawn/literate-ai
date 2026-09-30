"""Exclusive bounded staging for a live refresh; no live source/index/ref apply.

Staged link payloads remain regular data files. Crashed or changed staging is
retained for explicit recovery, never reconstructed into a new live owner.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts.identity import canonical_json_bytes

from ._write_reservations import (
    WriteReservationSet,
    WriteReservationTarget,
    acquire_write_reservations,
    combine_body_and_release_errors,
    is_reservation_cleanup_error,
)
from .repository_file_custody import PhysicalNode, _node
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import _local_identity_wire
from .repository_refresh_files import PreparedRefreshFiles
from .repository_refresh_index import refresh_root_index_bytes, tree_index_bytes
from .repository_refresh_reflogs import prepare_staged_metadata

STAGING_DIRECTORY = "litai-refresh-stage"
_MAX_FILE = 16 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024
_MAX_FILES = 32768
_CREATION_KEY = object()
_WINDOWS_DELETE_RETRY_SECONDS = 1.0
_WINDOWS_DELETE_RETRY_INTERVAL_SECONDS = 0.01


def _fail(suffix, message):
    raise OrchestrationInventoryError("refresh_stage_" + suffix, message) from None


def _key(path):
    require_safe_directory(path)
    node = path.lstat()
    return node.st_dev, node.st_ino, node.st_mode


def _is_windows() -> bool:
    return os.name == "nt"


def _remove_owned_empty_directory(path, *, parent_node, node) -> None:
    """Retire one proven empty directory across Windows delete-pending races."""

    deadline = time.monotonic() + _WINDOWS_DELETE_RETRY_SECONDS
    while True:
        if _key(path.parent) != parent_node or _key(path) != node:
            _fail("changed", "staging directory ownership changed")
        try:
            path.rmdir()
            return
        except OSError as error:
            if (
                not _is_windows()
                or error.errno not in {errno.EACCES, errno.EPERM, errno.ENOTEMPTY}
                or time.monotonic() >= deadline
            ):
                raise
            # Closing the last marker descriptor can leave its successful unlink
            # delete-pending briefly. Revalidate custody before every bounded retry.
            time.sleep(_WINDOWS_DELETE_RETRY_INTERVAL_SECONDS)


@dataclass(frozen=True, slots=True)
class RefreshStageEntry:
    role: str
    repository: str
    path: str
    content: bytes
    mode: str | None = None

    def to_dict(self):
        return {
            "role": self.role,
            "repository": self.repository,
            "path": self.path,
            "mode": self.mode,
            "size": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }


def _entries(files):
    refresh = files._owner._prepared.refresh
    entries = []

    def append(role, repository, path, content, mode=None):
        if content is not None:
            entries.append(RefreshStageEntry(role, repository, path, content, mode))

    append("before-manifest", ".", "literate.project.json", refresh.manifest.content)
    append(
        "before-lock",
        ".",
        ".literate/repository.lock.json",
        refresh.repository_lock.content,
    )
    append("before-index", ".", "index", refresh.root_git.index.content)
    append("before-head", ".", "HEAD", refresh.root_git.head.content)
    append(
        "prospective-index",
        ".",
        "index",
        refresh_root_index_bytes(
            refresh.root_git.index.content,
            refresh.authority.request,
            object_width=len(refresh.root_git.commit),
        ),
    )
    observations = {observed.root: observed for observed in refresh.children}
    for repository, plan in files.plans:
        observed = observations[plan.root]
        append("before-index", repository, "index", observed.index.content)
        append("before-head", repository, "HEAD", observed.head.content)
        append(
            "prospective-index",
            repository,
            "index",
            (
                observed.index.content
                if plan.previous.commit == plan.prospective.commit
                else tree_index_bytes(plan.prospective)
            ),
        )
        for node in plan.nodes:
            append("before-file", repository, node.path, node.content, node.kind)
        for change in plan.changes:
            if change.prospective is not None:
                entry = change.prospective
                append(
                    "prospective-file",
                    repository,
                    entry.path,
                    entry.content,
                    entry.mode,
                )
    if (
        len(entries) + 1 > _MAX_FILES
        or any(len(entry.content) > _MAX_FILE for entry in entries)
        or sum(len(entry.content) for entry in entries) > _MAX_TOTAL
    ):
        _fail("limit", "staging records exceed bounded file or aggregate limits")
    return tuple(entries)


class StagedRefresh:
    """A live staging handle; file paths are data, not authority to apply them."""

    def __init__(self, key, files, path, marker, objects=None):
        if key is not _CREATION_KEY:
            raise TypeError("refresh staging must be acquired, not reconstructed")
        self._files = files
        self._objects = objects
        self.path = path
        self._marker = marker
        self._records: dict[str, PhysicalNode] = {}
        self._active = True
        self._complete = False
        self._recovery_armed = False
        self._application = None
        self._application_state = None
        self._terminal_publication_failed = False
        self._deferred_node = None
        self._deferred_records = None
        self._deferred_cleanup_retained = False
        self._marker_released = False
        self.entries: tuple[RefreshStageEntry, ...] = ()
        self.metadata = ()

    def _write(self, name, content):
        self._marker.verify_all()
        require_safe_directory(self.path)
        path = self.path / name
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        original = os.fstat(descriptor)
        written = 0
        try:
            if not _identity_matches(path, path.lstat(), descriptor, original):
                _fail("changed", "staged file changed while opening")
            while written < len(content):
                count = os.write(
                    descriptor, memoryview(content)[written : written + 65536]
                )
                if count <= 0:
                    _fail("write_failed", "staged file write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            try:
                # Retain a cleanup record even for a short or failed write, but
                # never adopt bytes or an inode supplied by another writer.
                node = _node(self.path, name, set(), max(_MAX_FILE, len(content)))
                named = path.lstat()
                if (
                    node.kind == "file"
                    and node.content == content[:written]
                    and _identity_matches(path, named, descriptor, original)
                ):
                    self._records[name] = node
            finally:
                os.close(descriptor)
        if name not in self._records:
            _fail("changed", "staged file changed during writing")

    def require_current(self):
        if not self._active or not self._complete:
            _fail("inactive", "staging handle is not live and complete")
        if self._application is None:
            self._files.require_current()
        elif self._application.state == "applying":
            self._application.require_coherent()
        else:
            self._application.require_terminal()
        self._marker.verify_all()
        self._verify_records()
        if self._application is None:
            self._files.require_current()
        elif self._application.state == "applying":
            self._application.require_coherent()
        else:
            self._application.require_terminal()

    def arm_recovery(self):
        """Record retention before the first logical mutation.

        This does not authorize a write or relax input guards. Until a complete
        apply/rollback API proves a terminal state, an armed stage is retained
        even when its caller exits normally. A saved marker cannot reconstitute
        the live handle or bypass explicit recovery.
        """
        self.require_current()
        if self._recovery_armed:
            _fail("application_started", "recovery is already armed")
        content = (
            canonical_json_bytes(
                {
                    "schema": "literate-ai/refresh-application@1",
                    "state": "applying",
                    "physical_custody": self._files.identity,
                }
            )
            + b"\n"
        )
        if (
            len(self._records) + 1 > _MAX_FILES
            or len(content) > _MAX_FILE
            or sum(len(node.content) for node in self._records.values()) + len(content)
            > _MAX_TOTAL
        ):
            _fail("limit", "application journal exceeds staging bounds")
        # Set retention before attempting the journal write: a short write or
        # failed fsync must never let context cleanup delete before-state.
        self._recovery_armed = True
        self._application_state = "applying"
        self._write("application.json", content)
        self.require_current()

    def bind_application(self, application):
        from .repository_refresh_application import _LiveRefreshApplication

        if (
            not isinstance(application, _LiveRefreshApplication)
            or application.stage is not self
            or self._application is not None
            or self._application_state != "applying"
        ):
            _fail("application_invalid", "live application does not match this stage")
        self.require_current()
        self._application = application
        self.require_current()

    def read_entry(self, role, repository, entry_path):
        self._marker.verify_all()
        self._verify_records()
        matches = [
            (index, entry)
            for index, entry in enumerate(self.entries)
            if (entry.role, entry.repository, entry.path)
            == (role, repository, entry_path)
        ]
        if len(matches) != 1:
            _fail("entry_invalid", "staged application entry is missing or ambiguous")
        index, entry = matches[0]
        name = f"{index:05d}"
        expected = self._records[name]
        current = _node(self.path, name, set(), max(_MAX_FILE, len(entry.content)))
        if current != expected or current.content != entry.content:
            _fail("changed", "staged application bytes changed")
        return current.content

    def _replace_journal(self, content, state):
        self._marker.verify_all()
        self._verify_records()
        name = "application.next-" + secrets.token_hex(8)
        self._write(name, content)
        self._marker.verify_all()
        self._verify_records()
        replaced = False
        try:
            os.replace(self.path / name, self.path / "application.json")
            replaced = True
            self._application_state = state
            node = _node(self.path, "application.json", set(), _MAX_FILE)
            del self._records[name]
            self._records["application.json"] = node
            if os.name != "nt":
                descriptor = os.open(
                    self.path,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._marker.verify_all()
            self._verify_records()
        except BaseException:
            if replaced:
                self._terminal_publication_failed = True
            raise

    def record_terminal(self, state):
        if state not in {"committed", "rolled_back"}:
            _fail("state_invalid", "application terminal state is unsupported")
        if (
            self._application is None
            or self._application_state != "applying"
            or self._application.state != state
        ):
            _fail("state_invalid", "application terminal state is not live and proven")
        self._application.require_terminal()
        content = (
            canonical_json_bytes(
                {
                    "schema": "literate-ai/refresh-application@1",
                    "state": state,
                    "physical_custody": self._files.identity,
                    "authority_identity": self._application.authority.identity,
                }
            )
            + b"\n"
        )
        self._replace_journal(content, state)

    def retain_terminal_failure(self):
        if self._application_state not in {"committed", "rolled_back"}:
            _fail("state_invalid", "terminal recovery requires a published journal")
        self._terminal_publication_failed = True

    def defer_terminal_cleanup(self, directory_node):
        if (
            self._application is None
            or self._application_state not in {"committed", "rolled_back"}
            or self._deferred_records is not None
            or _key(self.path) != directory_node
        ):
            _fail("state_invalid", "terminal staging is not eligible for deferral")
        self._application.require_terminal()
        self._marker.verify_all()
        self._verify_records()
        self._deferred_node = directory_node
        self._deferred_records = dict(self._records)
        self._active = False
        self._files._owner.defer_terminal_stage(self)
        if self._terminal_publication_failed:
            self.retain_deferred_cleanup()

    def retain_deferred_cleanup(self, label="terminal-refresh-staging"):
        self._deferred_cleanup_retained = True
        if (
            self._application is not None
            and self._application.state == "committed"
            and self._application._result is not None
        ):
            self._application.retain_cleanup(label)

    def finalize_deferred_cleanup(self) -> bool:
        if (
            self._deferred_records is None
            or self._deferred_node is None
            or not self._marker_released
            or self._deferred_cleanup_retained
        ):
            self.retain_deferred_cleanup()
            return False
        evidence = self.path.parent / (
            ".litai-refresh-terminal-" + secrets.token_hex(16) + ".json"
        )
        descriptor = -1
        evidence_expected = None
        try:
            require_safe_directory(self.path.parent)
            if _key(self.path) != self._deferred_node:
                _fail("changed", "deferred staging directory custody changed")
            names = {entry.name for entry in os.scandir(self.path)}
            if names != set(self._deferred_records):
                _fail("changed", "deferred staging membership changed")
            for name, expected in self._deferred_records.items():
                if (
                    _node(
                        self.path,
                        name,
                        set(),
                        max(_MAX_FILE, len(expected.content)),
                    )
                    != expected
                ):
                    _fail("changed", "deferred staging record custody changed")
            journal = self._deferred_records.get("application.json")
            if journal is None:
                _fail("state_invalid", "terminal staging journal is missing")
            descriptor = os.open(
                evidence,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            written = 0
            while written < len(journal.content):
                count = os.write(
                    descriptor,
                    memoryview(journal.content)[written : written + 65536],
                )
                if count <= 0:
                    _fail("write_failed", "terminal evidence write made no progress")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            evidence_expected = _node(evidence.parent, evidence.name, set(), _MAX_FILE)
            if evidence_expected.content != journal.content:
                _fail("changed", "terminal cleanup evidence differs from journal")
            for name, expected in reversed(tuple(self._deferred_records.items())):
                current = _node(
                    self.path,
                    name,
                    set(),
                    max(_MAX_FILE, len(expected.content)),
                )
                if current != expected:
                    _fail("changed", "deferred staging changed during cleanup")
                (self.path / name).unlink()
            self.path.rmdir()
            if (
                _node(evidence.parent, evidence.name, set(), _MAX_FILE)
                != evidence_expected
            ):
                _fail("changed", "terminal cleanup evidence changed")
            evidence.unlink()
            return True
        except (
            OSError,
            UnsafeFilesystemPathError,
            OrchestrationInventoryError,
        ):
            self.retain_deferred_cleanup(
                "terminal-refresh-evidence"
                if os.path.lexists(evidence)
                else "terminal-refresh-staging"
            )
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def installed_objects(self):
        """Add immutable object packs while holding cooperative Git keep markers."""
        from .repository_object_install import install_refresh_objects

        return install_refresh_objects(self)

    def _verify_records(self):
        require_safe_directory(self.path)
        names = set()
        with os.scandir(self.path) as entries:
            for entry in entries:
                if len(names) >= _MAX_FILES + 1:
                    _fail("changed", "staging directory contains foreign entries")
                names.add(entry.name)
        if names != {"owner", *self._records}:
            _fail("changed", "staging directory contains missing or foreign entries")
        for name, expected in self._records.items():
            if (
                _node(self.path, name, set(), max(_MAX_FILE, len(expected.content)))
                != expected
            ):
                _fail("changed", "staged file custody changed")

    def _cleanup(self):
        if self._terminal_publication_failed:
            _fail(
                "recovery_required",
                "terminal application evidence retained after ambiguous failure",
            )
        if self._application is not None and self._application_state in {
            "committed",
            "rolled_back",
        }:
            self._application.require_terminal()
        self._active = False
        if self._recovery_armed and self._application_state not in {
            "committed",
            "rolled_back",
        }:
            _fail(
                "recovery_required",
                "application staging retained until explicit commit or rollback proof",
            )
        try:
            self._marker.verify_all()
            # All-or-retain on preexisting drift; never recursively delete.
            self._verify_records()
            cleanup = [
                item
                for item in reversed(tuple(self._records.items()))
                if item[0] != "application.json"
            ]
            if "application.json" in self._records:
                cleanup.append(("application.json", self._records["application.json"]))
            for name, expected in cleanup:
                self._marker.verify_all()
                if (
                    _node(self.path, name, set(), max(_MAX_FILE, len(expected.content)))
                    != expected
                ):
                    _fail("changed", "staging cleanup encountered changed custody")
                (self.path / name).unlink()
        except (OSError, UnsafeFilesystemPathError, OrchestrationInventoryError):
            _fail(
                "cleanup_incomplete",
                "changed staging was retained for explicit recovery",
            )


@contextmanager
def _exclusive_directory(root_git):
    path = root_git.git_directory / STAGING_DIRECTORY
    if _key(path.parent) != root_git.git_node:
        _fail("changed", "staging anchor changed")
    WriteReservationSet._no_alias(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _fail("busy", "existing staging requires its owner's explicit recovery")
    node = _key(path)
    deferred = {"value": False}
    body_error = None
    body_traceback = None
    try:
        try:
            yield path, node, deferred
        except BaseException as error:
            body_error = error
            body_traceback = error.__traceback__
            raise
    finally:
        if not deferred["value"]:
            try:
                _remove_owned_empty_directory(
                    path, parent_node=root_git.git_node, node=node
                )
            except (
                OSError,
                UnsafeFilesystemPathError,
                OrchestrationInventoryError,
            ) as cleanup_error:
                if body_error is not None:
                    if body_error.__cause__ is not None:
                        raise body_error.with_traceback(body_traceback) from (
                            body_error.__cause__
                        )
                    raise body_error.with_traceback(body_traceback) from cleanup_error
                _fail(
                    "cleanup_incomplete",
                    "staging directory retained for explicit recovery",
                )


@contextmanager
def stage_refresh_files(
    files: PreparedRefreshFiles, *, objects=None, metadata=False
) -> Iterator[StagedRefresh]:
    if not isinstance(files, PreparedRefreshFiles):
        raise TypeError("staging requires live prepared physical custody")
    if type(metadata) is not bool:
        raise TypeError("metadata staging selection must be boolean")
    files.require_current()
    entries = _entries(files)
    transitions = prepare_staged_metadata(files) if metadata else ()
    entries += tuple(
        RefreshStageEntry(role, transition.repository, transition.name, content)
        for transition in transitions
        for role, content in (
            ("metadata-before-" + transition.kind, transition.before.content),
            ("metadata-prospective-" + transition.kind, transition.prospective),
        )
        if content is not None
    )
    if len(entries) + 1 > _MAX_FILES or any(
        len(entry.content) > _MAX_FILE for entry in entries
    ):
        _fail("limit", "metadata staging exceeds the file count or byte bound")
    if objects is not None:
        from .repository_refresh_objects import PreparedRefreshObjects

        if (
            not isinstance(objects, PreparedRefreshObjects)
            or objects.files is not files
        ):
            raise TypeError("object staging requires matching live physical custody")
        objects.require_current()
        entries += tuple(
            RefreshStageEntry(role, repository, captured.objects.pack_id, content)
            for repository, captured in objects.packs
            for role, content in (
                ("objects-pack", captured.objects.pack),
                ("objects-index", captured.objects.index),
            )
        )
        if len(entries) + 1 > _MAX_FILES:
            _fail("limit", "object staging exceeds the file count bound")
    manifest = (
        canonical_json_bytes(
            _local_identity_wire(
                {
                    "schema": "literate-ai/refresh-stage@1",
                    "physical_custody": files.identity,
                    "object_custody": None if objects is None else objects.identity,
                    "metadata": [transition.to_dict() for transition in transitions],
                    "metadata_directories": [
                        item.to_dict()
                        for item in files._owner._prepared.refresh.metadata_directories
                    ]
                    if metadata
                    else [],
                    "worktrees": [
                        {
                            "repository": repository,
                            "previous_commit": plan.previous.commit,
                            "prospective_commit": plan.prospective.commit,
                            "root_node": plan.root_node,
                            "nodes": [
                                {
                                    "path": node.path,
                                    "kind": node.kind,
                                    "signature": node.signature,
                                }
                                for node in plan.nodes
                            ],
                            "directories": [
                                {
                                    "path": directory.path,
                                    "signature": directory.signature,
                                    "members": [
                                        (name.hex(), signature)
                                        for name, signature in directory.members
                                    ],
                                }
                                for directory in plan.directories
                            ],
                        }
                        for repository, plan in files.plans
                    ],
                    "entries": [
                        {"file": f"{index:05d}", **entry.to_dict()}
                        for index, entry in enumerate(entries)
                    ],
                }
            )
        )
        + b"\n"
    )
    if (
        len(manifest) > _MAX_FILE
        or len(manifest) + sum(len(entry.content) for entry in entries) > _MAX_TOTAL
    ):
        _fail("limit", "staging manifest exceeds aggregate bounds")
    root_git = files._owner._prepared.refresh.root_git
    try:
        files.require_current()
        body_error = None
        body_traceback = None
        marker_error = None
        marker_cleanup_expected = False
        stage = None
        with _exclusive_directory(root_git) as (path, node, deferred):
            target = WriteReservationTarget(path / "owner", path, node)
            try:
                with acquire_write_reservations((target,)) as marker:
                    stage = StagedRefresh(_CREATION_KEY, files, path, marker, objects)
                    try:
                        stage.entries = entries
                        stage.metadata = transitions
                        for index, entry in enumerate(entries):
                            stage._write(f"{index:05d}", entry.content)
                        stage._write("manifest.json", manifest)
                        stage._complete = True
                        stage.require_current()
                        try:
                            yield stage
                            stage.require_current()
                        except BaseException as error:
                            body_error = error
                            body_traceback = error.__traceback__
                    finally:
                        if stage._application_state in {"committed", "rolled_back"}:
                            # A terminal journal is already recovery authority. Keep
                            # its directory even if validating the deferred handoff
                            # itself fails, so cleanup cannot mask that real error.
                            deferred["value"] = True
                            stage.defer_terminal_cleanup(node)
                        else:
                            try:
                                stage._cleanup()
                            except BaseException as cleanup_error:
                                if body_error is not None:
                                    raise body_error.with_traceback(
                                        body_traceback
                                    ) from cleanup_error
                                raise
                stage._marker_released = True
            except BaseException as error:
                if stage is None or stage._deferred_records is None:
                    raise
                marker_error = error
                stage.retain_deferred_cleanup()
                stage.retain_deferred_cleanup("stage-marker-artifacts")
                marker_cleanup_expected = is_reservation_cleanup_error(error)
        if body_error is not None and marker_error is not None:
            if (
                stage._application is not None
                and stage._application.state == "committed"
            ):
                stage.retain_deferred_cleanup()
            raise combine_body_and_release_errors(
                "stage-marker",
                body_error,
                body_traceback,
                marker_error,
            )
        if body_error is not None:
            if (
                stage._application is not None
                and stage._application.state == "committed"
            ):
                stage.retain_deferred_cleanup()
            raise body_error.with_traceback(body_traceback)
        if marker_error is not None and (
            not marker_cleanup_expected
            or stage._application is None
            or stage._application.state != "committed"
        ):
            raise marker_error
        if stage._application is None:
            files.require_current()
        else:
            stage._application.require_terminal()
    except OrchestrationInventoryError:
        raise
    except (OSError, UnsafeFilesystemPathError):
        _fail("write_failed", "refresh staging could not complete safely")
