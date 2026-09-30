"""Bounded, identity-chained review state for genuine large Component-lock diffs."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.cache_directories import (
    CacheDirectoryError,
    ensure_cache_directory,
    resolve_cache_directories,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

from .component_locks import ComponentLockDifference, ComponentLockStoreError

REVIEW_PAGE_SIZE = 512
_MAXIMUM_PAGES = 64
_MAXIMUM_DIFFERENCES = REVIEW_PAGE_SIZE * _MAXIMUM_PAGES
_MAXIMUM_PAGE_BYTES = 8 * 1024 * 1024
_MAXIMUM_STATE_BYTES = 1024 * 1024
_TRANSACTION_SCHEMA = "literate-ai/component-lock-review-transaction@1"
_TRANSACTION_IDENTITY_SCHEMA = "literate-ai/component-lock-review-identity@1"
_PAGE_SCHEMA = "literate-ai/component-lock-review-page@1"
_RESULT_SCHEMA = "literate-ai/component-lock-review-command@1"
_IDENTITY = re.compile(r"sha256:([0-9a-f]{64})")


class ComponentLockReviewError(ComponentLockStoreError):
    """A large-review transaction is unsafe, malformed, stale, or incomplete."""


class ComponentLockReviewStore:
    """Persist derived review acknowledgements beneath the project's object root."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        try:
            directories = resolve_cache_directories(self.project_root)
            object_root = ensure_cache_directory(
                directories.obj_dir,
                kind="object",
                project_root=self.project_root,
                required_subdirectories=(Path("component-lock-reviews"),),
            )
            self.root = object_root / "component-lock-reviews"
            require_safe_directory(self.root)
            if os.name != "nt":
                self.root.chmod(0o700)
        except (CacheDirectoryError, UnsafeFilesystemPathError, OSError) as exc:
            raise ComponentLockReviewError(
                "component_lock.review_storage_unsafe",
                "Component lock review storage is unavailable or unsafe",
            ) from exc
        self.write_lock_path = self.root / ".component-lock-review.write.lock"

    def start(
        self,
        *,
        binding: Mapping[str, object],
        differences: Sequence[ComponentLockDifference],
    ) -> dict[str, object]:
        immutable, pages = _review_material(binding, differences)
        transaction_id = str(immutable["transaction_identity"])
        path = self._path(transaction_id)
        state = {**immutable, "acknowledgements": []}
        try:
            with exclusive_cache_lock(self.write_lock_path):
                if path.exists() or path_is_link_or_reparse(path):
                    existing = self._read(transaction_id)
                    self._require_immutable(existing, immutable)
                    return _report(existing, pages, operation="start")
                self._write(path, state)
                return _report(state, pages, operation="start")
        except ComponentLockReviewError:
            raise
        except CacheLockError as exc:
            raise ComponentLockReviewError(
                "component_lock.review_lock_failed",
                "Component lock review transaction could not be serialized",
            ) from exc

    def acknowledge(
        self,
        transaction_id: str,
        page_identity: str,
        *,
        binding: Mapping[str, object],
        differences: Sequence[ComponentLockDifference],
    ) -> dict[str, object]:
        immutable, pages = _review_material(binding, differences)
        if transaction_id != immutable["transaction_identity"]:
            raise ComponentLockReviewError(
                "component_lock.review_transaction_mismatch",
                "Component lock review transaction does not match current authority",
            )
        path = self._path(transaction_id)
        try:
            with exclusive_cache_lock(self.write_lock_path):
                state = self._read(transaction_id)
                self._require_immutable(state, immutable)
                acknowledgements = _acknowledgements(state)
                if page_identity in acknowledgements:
                    raise ComponentLockReviewError(
                        "component_lock.review_acknowledgement_replayed",
                        "Component lock review page acknowledgement was already "
                        "recorded",
                    )
                page_identities = _page_identities(state)
                index = len(acknowledgements)
                if index >= len(page_identities):
                    raise ComponentLockReviewError(
                        "component_lock.review_already_acknowledged",
                        "Every Component lock review page is already acknowledged",
                    )
                if page_identity != page_identities[index]:
                    raise ComponentLockReviewError(
                        "component_lock.review_acknowledgement_mismatch",
                        "Component lock review acknowledgement is skipped, reordered, "
                        "or belongs to another transaction",
                    )
                updated = {
                    **immutable,
                    "acknowledgements": [*acknowledgements, page_identity],
                }
                self._write(path, updated)
                return _report(updated, pages, operation="acknowledge")
        except ComponentLockReviewError:
            raise
        except CacheLockError as exc:
            raise ComponentLockReviewError(
                "component_lock.review_lock_failed",
                "Component lock review transaction could not be serialized",
            ) from exc

    def status(
        self,
        transaction_id: str,
        *,
        binding: Mapping[str, object],
        differences: Sequence[ComponentLockDifference],
    ) -> dict[str, object]:
        immutable, pages = _review_material(binding, differences)
        if transaction_id != immutable["transaction_identity"]:
            raise ComponentLockReviewError(
                "component_lock.review_transaction_mismatch",
                "Component lock review transaction does not match current authority",
            )
        state = self._read(transaction_id)
        self._require_immutable(state, immutable)
        return _report(state, pages, operation="status")

    def require_ready(
        self,
        transaction_id: str,
        *,
        binding: Mapping[str, object],
        differences: Sequence[ComponentLockDifference],
    ) -> dict[str, object]:
        report = self.status(
            transaction_id,
            binding=binding,
            differences=differences,
        )
        if not report["ready"]:
            raise ComponentLockReviewError(
                "component_lock.review_incomplete",
                "Every Component lock review page must be acknowledged before apply",
            )
        return report

    def complete(self, transaction_id: str) -> None:
        path = self._path(transaction_id)
        try:
            with exclusive_cache_lock(self.write_lock_path):
                self._read(transaction_id)
                path.unlink()
                _fsync_directory(self.root)
        except ComponentLockReviewError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentLockReviewError(
                "component_lock.review_cleanup_failed",
                "Completed Component lock review state could not be removed",
            ) from exc

    def cleanup(self, transaction_id: str, *, component: str) -> dict[str, object]:
        path = self._path(transaction_id)
        try:
            with exclusive_cache_lock(self.write_lock_path):
                state = self._read(transaction_id)
                binding = state.get("binding")
                if (
                    not isinstance(binding, dict)
                    or binding.get("component") != component
                ):
                    raise ComponentLockReviewError(
                        "component_lock.review_transaction_mismatch",
                        "Component lock review transaction belongs to another "
                        "Component",
                    )
                path.unlink()
                _fsync_directory(self.root)
        except ComponentLockReviewError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentLockReviewError(
                "component_lock.review_cleanup_failed",
                "Component lock review state could not be removed",
            ) from exc
        return {
            "schema": _RESULT_SCHEMA,
            "operation": "cleanup",
            "transaction_identity": transaction_id,
            "removed": True,
        }

    def _path(self, transaction_id: str) -> Path:
        matched = _IDENTITY.fullmatch(transaction_id)
        if matched is None:
            raise ComponentLockReviewError(
                "component_lock.review_identity_invalid",
                "Component lock review identity must be an exact sha256 identity",
            )
        return self.root / f"{matched.group(1)}.json"

    def _read(self, transaction_id: str) -> dict[str, object]:
        path = self._path(transaction_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise ComponentLockReviewError(
                "component_lock.review_missing",
                "Component lock review transaction does not exist",
            ) from None
        except OSError as exc:
            raise ComponentLockReviewError(
                "component_lock.review_read_failed",
                "Component lock review transaction could not be inspected",
            ) from exc
        if (
            stat_is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAXIMUM_STATE_BYTES
        ):
            raise ComponentLockReviewError(
                "component_lock.review_state_unsafe",
                "Component lock review transaction is unsafe or exceeds its bound",
            )
        try:
            with path.open("rb") as stream:
                content = stream.read(_MAXIMUM_STATE_BYTES + 1)
            after = path.lstat()
            value = json.loads(content)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ComponentLockReviewError(
                "component_lock.review_state_invalid",
                "Component lock review transaction is malformed",
            ) from exc
        if (
            stat_is_link_or_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or _node_signature(metadata) != _node_signature(after)
            or len(content) > _MAXIMUM_STATE_BYTES
            or not isinstance(value, dict)
            or value.get("schema") != _TRANSACTION_SCHEMA
            or content != canonical_json_bytes(value) + b"\n"
        ):
            raise ComponentLockReviewError(
                "component_lock.review_state_invalid",
                "Component lock review transaction is malformed or changed",
            )
        return value

    def _write(self, path: Path, state: Mapping[str, object]) -> None:
        content = canonical_json_bytes(state) + b"\n"
        if len(content) > _MAXIMUM_STATE_BYTES:
            raise ComponentLockReviewError(
                "component_lock.review_state_limit",
                "Component lock review transaction exceeds its persisted byte bound",
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".component-lock-review.", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path_is_link_or_reparse(path):
                raise ComponentLockReviewError(
                    "component_lock.review_state_unsafe",
                    "Component lock review destination cannot be a link or "
                    "reparse point",
                )
            os.replace(temporary, path)
            _fsync_directory(self.root)
        except OSError as exc:
            raise ComponentLockReviewError(
                "component_lock.review_write_failed",
                "Component lock review transaction could not be committed atomically",
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _require_immutable(
        state: Mapping[str, object], expected: Mapping[str, object]
    ) -> None:
        observed = {
            key: value for key, value in state.items() if key != "acknowledgements"
        }
        if observed != dict(expected):
            raise ComponentLockReviewError(
                "component_lock.review_state_tampered",
                "Component lock review transaction does not match its bound authority",
            )
        _acknowledgements(state)


def _review_material(
    binding: Mapping[str, object],
    differences: Sequence[ComponentLockDifference],
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    if len(differences) <= REVIEW_PAGE_SIZE:
        raise ComponentLockReviewError(
            "component_lock.review_not_large",
            "Explicit large review requires more than 512 semantic differences",
        )
    if len(differences) > _MAXIMUM_DIFFERENCES:
        raise ComponentLockReviewError(
            "component_lock.review_difference_limit",
            "Component lock review exceeds the bounded total-difference limit",
        )
    binding_value = dict(binding)
    try:
        canonical_json_bytes(binding_value)
    except (TypeError, ValueError) as exc:
        raise ComponentLockReviewError(
            "component_lock.review_binding_invalid",
            "Component lock review binding is not canonical JSON material",
        ) from exc
    transaction_identity = canonical_identity(
        {
            "schema": _TRANSACTION_IDENTITY_SCHEMA,
            "binding": binding_value,
            "difference_count": len(differences),
        }
    ).uri
    page_count = (len(differences) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE
    pages: list[dict[str, object]] = []
    previous: str | None = None
    for index in range(page_count):
        entries = [
            item.to_dict()
            for item in differences[
                index * REVIEW_PAGE_SIZE : (index + 1) * REVIEW_PAGE_SIZE
            ]
        ]
        page_material: dict[str, object] = {
            "schema": _PAGE_SCHEMA,
            "transaction_identity": transaction_identity,
            "page_number": index + 1,
            "page_count": page_count,
            "previous_page_identity": previous,
            "differences": entries,
        }
        page_identity = canonical_identity(page_material).uri
        page = {**page_material, "page_identity": page_identity}
        if len(canonical_json_bytes(page)) > _MAXIMUM_PAGE_BYTES:
            raise ComponentLockReviewError(
                "component_lock.review_page_limit",
                "One complete Component lock review page exceeds its byte bound",
            )
        pages.append(page)
        previous = page_identity
    immutable = {
        "schema": _TRANSACTION_SCHEMA,
        "transaction_identity": transaction_identity,
        "binding": binding_value,
        "difference_count": len(differences),
        "page_identities": [str(page["page_identity"]) for page in pages],
    }
    return immutable, tuple(pages)


def _acknowledgements(state: Mapping[str, object]) -> list[str]:
    raw = state.get("acknowledgements")
    page_identities = _page_identities(state)
    if (
        not isinstance(raw, list)
        or len(raw) > len(page_identities)
        or any(not isinstance(item, str) for item in raw)
        or raw != page_identities[: len(raw)]
    ):
        raise ComponentLockReviewError(
            "component_lock.review_state_tampered",
            "Component lock review acknowledgements are malformed or reordered",
        )
    return list(raw)


def _page_identities(state: Mapping[str, object]) -> list[str]:
    raw = state.get("page_identities")
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > _MAXIMUM_PAGES
        or any(
            _IDENTITY.fullmatch(item) is None for item in raw if isinstance(item, str)
        )
        or any(not isinstance(item, str) for item in raw)
        or len(set(raw)) != len(raw)
    ):
        raise ComponentLockReviewError(
            "component_lock.review_state_tampered",
            "Component lock review page identities are malformed",
        )
    return list(raw)


def _report(
    state: Mapping[str, object],
    pages: Sequence[Mapping[str, object]],
    *,
    operation: str,
) -> dict[str, object]:
    acknowledgements = _acknowledgements(state)
    next_page = (
        None
        if len(acknowledgements) == len(pages)
        else dict(pages[len(acknowledgements)])
    )
    return {
        "schema": _RESULT_SCHEMA,
        "operation": operation,
        "transaction_identity": state["transaction_identity"],
        "difference_count": state["difference_count"],
        "page_count": len(pages),
        "acknowledged_page_count": len(acknowledgements),
        "ready": next_page is None,
        "next_page": next_page,
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


__all__ = [
    "ComponentLockReviewError",
    "ComponentLockReviewStore",
    "REVIEW_PAGE_SIZE",
]
