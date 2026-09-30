"""Renew reviewed publication under live multi-root ownership, before apply.

This pre-apply context must leave the reviewed source, index and pins unchanged.
Independent remote observations are not an atomic remote snapshot or acceptance.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import PurePosixPath

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy

from ._write_reservations import (
    WriteReservationSet,
    combine_body_and_release_errors,
    is_reservation_cleanup_error,
)
from .repository_orchestration import OrchestrationInventoryError
from .repository_publication import (
    PublishedRepositoryTree,
    capture_published_repository_tree,
)
from .repository_refresh import (
    require_refresh_child_unchanged,
    require_refresh_prospective_inventory,
    require_refresh_root_git_unchanged,
    require_repository_refresh_inputs_unchanged,
)
from .repository_refresh_publication import (
    PreparedRefreshPublication,
    _protected_roots,
    require_refresh_publication_proofs_unchanged,
    require_refresh_publication_unchanged,
)
from .repository_refresh_reservations import (
    refresh_custody_identity,
    reserve_refresh_inputs,
)

_CREATION_KEY = object()


def _within_root_pin_only_target(
    path: PurePosixPath, modes: tuple[tuple[str, str], ...]
) -> bool:
    if not isinstance(path, PurePosixPath):
        raise TypeError("refresh child ancestry requires a canonical path")
    return any(
        mode == "root-pin-only" and path.is_relative_to(PurePosixPath(target_path))
        for target_path, mode in modes
    )


def refresh_publication_custody_identity(prepared: PreparedRefreshPublication) -> str:
    """Bind local approval to exact input custody and all reviewed remote proofs."""
    if not isinstance(prepared, PreparedRefreshPublication):
        raise TypeError("refresh ownership requires prepared publication observations")
    return canonical_identity(
        {
            "refresh_custody_identity": refresh_custody_identity(prepared.refresh),
            "publication_identity": prepared.identity,
            "endpoints": [
                {"path": path, "url": url} for path, url in prepared.endpoints
            ],
            "deadline_policy_identity": prepared.deadline_policy.identity.uri,
        }
    ).uri


class OwnedRefreshPublication:
    """Live renewed observations; a saved proof cannot reconstruct ownership."""

    __slots__ = ("_prepared", "_reservations", "_application", "_deferred_stage")

    def __init__(
        self,
        key: object,
        prepared: PreparedRefreshPublication,
        reservations: WriteReservationSet,
    ) -> None:
        if key is not _CREATION_KEY:
            raise TypeError("refresh ownership must be acquired, not reconstructed")
        self._prepared = prepared
        self._reservations = reservations
        self._application = None
        self._deferred_stage = None

    def require_inputs_unchanged(self) -> None:
        if self._application is not None:
            if self._application.state == "rolled_back":
                self._application.require_terminal()
                return
            raise OrchestrationInventoryError(
                "refresh_application_active",
                "pre-apply input validation is unavailable after application starts",
            )
        require_repository_refresh_inputs_unchanged(
            self._prepared.refresh, reservations=self._reservations
        )

    def bind_application(self, application) -> None:
        from .repository_refresh_application import _LiveRefreshApplication

        if (
            not isinstance(application, _LiveRefreshApplication)
            or application.owner is not self
            or self._application is not None
            or application.state != "applying"
        ):
            raise OrchestrationInventoryError(
                "refresh_application_invalid",
                "live application does not match publication ownership",
            )
        self.require_inputs_unchanged()
        self._application = application

    def defer_terminal_stage(self, stage) -> None:
        from .repository_refresh_staging import StagedRefresh

        if (
            not isinstance(stage, StagedRefresh)
            or stage._application is not self._application
            or stage._application_state not in {"committed", "rolled_back"}
            or self._deferred_stage is not None
        ):
            raise OrchestrationInventoryError(
                "refresh_application_invalid",
                "deferred terminal staging does not match publication ownership",
            )
        self._deferred_stage = stage

    def renew_publication(self) -> None:
        """Renew all remote proofs, guarded by still-live local ownership."""
        self.require_inputs_unchanged()
        require_refresh_publication_unchanged(
            self._prepared, reservations=self._reservations
        )

    def require_application_inputs(self, application) -> None:
        """Guard unchanged root binding and exact-head children during application."""
        from .repository_refresh_application import _LiveRefreshApplication

        if (
            not isinstance(application, _LiveRefreshApplication)
            or application.owner is not self
            or self._application not in (None, application)
        ):
            raise OrchestrationInventoryError(
                "refresh_application_invalid",
                "application input custody does not match publication ownership",
            )
        require_refresh_root_git_unchanged(
            self._prepared.refresh.root_git,
            reservations=self._reservations,
        )
        root = self._prepared.refresh.repository.root
        for observed in self._prepared.refresh.children:
            path = PurePosixPath(observed.root.relative_to(root).as_posix())
            if _within_root_pin_only_target(path, self._prepared.target_modes):
                require_refresh_child_unchanged(
                    observed, reservations=self._reservations
                )

    def renew_application_publication(self, application) -> None:
        self.require_application_inputs(application)
        require_refresh_prospective_inventory(
            self._prepared.refresh, reservations=self._reservations
        )
        require_refresh_publication_proofs_unchanged(
            self._prepared, reservations=self._reservations
        )
        self.require_application_inputs(application)

    def capture_prospective_trees(
        self,
        *,
        tree_policy: RepositoryTreeCapturePolicy | None = None,
    ) -> tuple[tuple[str, PublishedRepositoryTree], ...]:
        """Capture a complete root-bound selection; this still writes no live files."""
        policy = RepositoryTreeCapturePolicy() if tree_policy is None else tree_policy
        if not isinstance(policy, RepositoryTreeCapturePolicy):
            raise TypeError("refresh tree capture requires a typed bounds policy")
        self.require_inputs_unchanged()
        result = []
        entries = metadata = blobs = 0
        for (path, endpoint), proof in zip(
            self._prepared.endpoints, self._prepared.observations, strict=True
        ):
            if dict(self._prepared.target_modes)[path] == "root-pin-only":
                continue
            remaining = max(1, policy.maximum_total_blob_bytes - blobs)
            limits = replace(
                policy,
                maximum_entries=max(1, policy.maximum_entries - entries),
                maximum_metadata_bytes=max(1, policy.maximum_metadata_bytes - metadata),
                maximum_blob_bytes=min(policy.maximum_blob_bytes, remaining),
                maximum_total_blob_bytes=remaining,
            )
            self.require_inputs_unchanged()
            try:
                captured = capture_published_repository_tree(
                    endpoint,
                    proof.commit,
                    deadline_policy=self._prepared.deadline_policy,
                    tree_policy=limits,
                    protected_roots=_protected_roots(self._prepared.refresh),
                )
            finally:
                self.require_inputs_unchanged()
            if (
                not isinstance(captured, PublishedRepositoryTree)
                or captured.publication != proof
            ):
                raise OrchestrationInventoryError(
                    "refresh_publication_changed",
                    "captured tree does not bind reviewed publication",
                )
            entries += len(captured.tree.entries)
            metadata += captured.tree.metadata_size_bound
            blobs += sum(len(entry.content or b"") for entry in captured.tree.entries)
            if (
                entries > policy.maximum_entries
                or metadata > policy.maximum_metadata_bytes
                or blobs > policy.maximum_total_blob_bytes
                or any(
                    len(entry.content or b"") > policy.maximum_blob_bytes
                    for entry in captured.tree.entries
                )
            ):
                raise OrchestrationInventoryError(
                    "refresh_tree_capture_limit",
                    "selected trees exceed aggregate capture bounds",
                )
            result.append((path, captured))
        self.require_inputs_unchanged()
        return tuple(result)

    def prepare_filesystem_changes(
        self, *, tree_policy: RepositoryTreeCapturePolicy | None = None
    ):
        """Capture current physical bytes and preflight collisions without applying."""
        from .repository_refresh_files import prepare_refresh_files

        return prepare_refresh_files(self, policy=tree_policy)


@contextmanager
def reserve_published_refresh(
    prepared: PreparedRefreshPublication,
    *,
    expected_custody_identity: str,
    acknowledge: bool,
) -> Iterator[OwnedRefreshPublication]:
    if acknowledge is not True:
        raise OrchestrationInventoryError(
            "refresh_acknowledgement_required",
            "publication ownership requires explicit acknowledgement",
        )
    if refresh_publication_custody_identity(prepared) != expected_custody_identity:
        raise OrchestrationInventoryError(
            "refresh_plan_stale", "reviewed publication custody does not match inputs"
        )
    owned = None
    body_error = None
    body_traceback = None
    release_error = None
    release_cleanup_expected = False
    try:
        with reserve_refresh_inputs(
            prepared.refresh,
            expected_custody_identity=refresh_custody_identity(prepared.refresh),
            acknowledge=True,
        ) as reservations:
            owned = OwnedRefreshPublication(_CREATION_KEY, prepared, reservations)
            owned.renew_publication()
            try:
                yield owned
            except BaseException as error:
                body_error = error
                body_traceback = error.__traceback__
            try:
                if owned._application is None:
                    owned.require_inputs_unchanged()
                else:
                    owned._application.require_terminal()
            except BaseException as error:
                if body_error is None:
                    body_error = error
                    body_traceback = error.__traceback__
    except BaseException as error:
        if owned is None:
            raise
        release_error = error
        release_cleanup_expected = is_reservation_cleanup_error(error)

    stage = owned._deferred_stage
    if stage is not None:
        if (
            body_error is not None
            and owned._application is not None
            and owned._application.state == "committed"
        ):
            stage.retain_deferred_cleanup()
        if release_error is not None:
            stage.retain_deferred_cleanup()
            stage.retain_deferred_cleanup("refresh-reservation-artifacts")
        elif not stage._deferred_cleanup_retained:
            stage.finalize_deferred_cleanup()

    if body_error is not None and release_error is not None:
        raise combine_body_and_release_errors(
            "outer-reservation",
            body_error,
            body_traceback,
            release_error,
        )
    if release_error is not None and not release_cleanup_expected:
        raise release_error
    if body_error is not None:
        raise body_error.with_traceback(body_traceback)
    if release_error is not None and (
        owned._application is None or owned._application.state != "committed"
    ):
        raise release_error
