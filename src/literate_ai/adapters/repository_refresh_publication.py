"""Bind remote publication observations to unchanged root-owned refresh intent.

This internal preparation runs transport and writes disposable proof storage only.
It is not a public refresh plan, child acceptance or filesystem transaction authority.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_orchestration import gitlink_url

from ._write_reservations import WriteReservationSet
from .repository_orchestration import OrchestrationInventoryError, _git
from .repository_publication import (
    RepositoryPublicationObservation,
    _endpoint,
    _proof_storage_parent,
    verify_repository_publication,
)
from .repository_refresh import (
    PreparedRepositoryRefresh,
    require_repository_refresh_inputs_unchanged,
)


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError("refresh_" + suffix, message) from None


def _absolute_endpoint(root: Path, value: str) -> str:
    try:
        gitlink_url(value)
    except (TypeError, ValueError):
        _fail("endpoint_invalid", "root remote endpoint is unsafe or ambiguous")
    if (
        Path(value).is_absolute()
        or "://" in value
        or re.fullmatch(r"(?:[^/@:]+@)?[^/:]+:.+", value)
    ):
        return _endpoint(value)
    return _endpoint(os.path.abspath(root / value))


def _default_remote(prepared: PreparedRepositoryRefresh) -> str:
    root = prepared.repository.root
    raw = _git(root, "config", "--null", "--list", "--includes")
    if (
        "sha256:" + hashlib.sha256(raw).hexdigest()
        != prepared.root_git.configuration_identity
    ):
        _fail("inputs_changed", "root remote configuration changed during resolution")
    configuration: dict[str, list[str]] = {}
    try:
        for entry in raw.split(b"\0")[:-1]:
            key, _, value = entry.decode("utf-8").partition("\n")
            configuration.setdefault(key, []).append(value)
    except UnicodeError:
        _fail("endpoint_invalid", "root remote configuration is not readable text")

    def single(key: str) -> str | None:
        values = configuration.get(key, [])
        if len(values) > 1 or (values and not values[0]):
            _fail("endpoint_invalid", "root default remote selection is ambiguous")
        return values[0] if values else None

    branch = (
        _git(root, "rev-parse", "--symbolic-full-name", "HEAD")
        .decode("utf-8")
        .removesuffix("\n")
    )
    remote = (
        single("branch." + branch.removeprefix("refs/heads/") + ".remote")
        if branch.startswith("refs/heads/")
        else None
    ) or "origin"
    if len(remote) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in remote):
        _fail("endpoint_invalid", "root default remote name is invalid")
    if remote == ".":
        return str(root)
    value = single("remote." + remote + ".url")
    return str(root) if value is None else _absolute_endpoint(root, value)


def _protected_roots(prepared: PreparedRepositoryRefresh) -> tuple[Path, ...]:
    return tuple(
        path
        for observation in (prepared.root_git, *prepared.children)
        for path in (
            observation.root,
            observation.git_directory,
            observation.common_directory,
        )
    )


def _resolve_refresh_endpoints(
    prepared: PreparedRepositoryRefresh,
    reservations: WriteReservationSet | None = None,
) -> tuple[tuple[str, str], ...]:
    """Use root declarations, never child remotes or local submodule URL overrides."""
    require_repository_refresh_inputs_unchanged(prepared, reservations=reservations)
    pins = {pin.path: pin for pin in prepared.authority.previous.repositories}
    targets = prepared.authority.request.targets
    if any(len(target.commit) != len(pins[target.path].commit) for target in targets):
        _fail(
            "object_format_mismatch", "refresh cannot change a Gitlink's object format"
        )
    default = None
    endpoints: list[tuple[str, str]] = []
    for target in targets:
        url = pins[target.path].url
        if url.startswith(("./", "../")):
            default = _default_remote(prepared) if default is None else default
            # Let Git own relative URL semantics, including scp/ssh/file forms.
            # This synthetic index contains no checkout, child source or commit.
            with tempfile.TemporaryDirectory(
                prefix="litai-url-",
                dir=_proof_storage_parent(_protected_roots(prepared)),
            ) as temporary:
                scratch = Path(temporary)
                _git(
                    scratch,
                    "init",
                    "--template=",
                    "--object-format="
                    + ("sha1" if len(target.commit) == 40 else "sha256"),
                    ".",
                )
                _git(scratch, "config", "remote.origin.url", default)
                _git(
                    scratch,
                    "config",
                    "--file",
                    ".gitmodules",
                    "submodule.target.path",
                    "child",
                )
                _git(
                    scratch,
                    "config",
                    "--file",
                    ".gitmodules",
                    "submodule.target.url",
                    url,
                )
                _git(
                    scratch,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "160000",
                    target.commit,
                    "child",
                )
                _git(scratch, "submodule", "init", "--", "child")
                raw = _git(scratch, "config", "--null", "--get", "submodule.target.url")
                if not raw.endswith(b"\0") or raw.count(b"\0") != 1:
                    _fail("endpoint_invalid", "Git returned an ambiguous resolved URL")
                url = raw[:-1].decode("utf-8")
        endpoints.append(
            (target.path, _absolute_endpoint(prepared.repository.root, url))
        )
    require_repository_refresh_inputs_unchanged(prepared, reservations=reservations)
    return tuple(endpoints)


def resolve_refresh_endpoints(
    prepared: PreparedRepositoryRefresh,
    *,
    reservations: WriteReservationSet | None = None,
) -> tuple[tuple[str, str], ...]:
    try:
        return _resolve_refresh_endpoints(prepared, reservations)
    except (OSError, UnicodeError):
        _fail(
            "endpoint_invalid",
            "root endpoint resolution or proof storage is unavailable",
        )


@dataclass(frozen=True, slots=True)
class PreparedRefreshPublication:
    """Local prepared inputs plus ordered ephemeral proofs, never apply authority."""

    refresh: PreparedRepositoryRefresh
    endpoints: tuple[tuple[str, str], ...]
    observations: tuple[RepositoryPublicationObservation, ...]
    deadline_policy: RepositoryFetchDeadlinePolicy
    target_modes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        previous = {
            item.path: item.commit
            for item in self.refresh.authority.previous.repositories
        }
        children = {
            item.root.relative_to(self.refresh.repository.root).as_posix(): item
            for item in self.refresh.children
        }
        expected = tuple(
            (
                target.path,
                (
                    "root-pin-only"
                    if previous[target.path] != target.commit
                    and children[target.path].commit == proof.commit == target.commit
                    else "source-transition"
                ),
            )
            for target, (path, _endpoint), proof in zip(
                self.refresh.authority.request.targets,
                self.endpoints,
                self.observations,
                strict=True,
            )
            if path == target.path
        )
        if len(expected) != len(self.observations) or self.target_modes != expected:
            raise OrchestrationInventoryError(
                "refresh_publication_mismatch",
                "publication target modes do not match exact local and remote custody",
            )

    @property
    def identity(self) -> str:
        return canonical_identity(
            {
                "refresh_authority_identity": self.refresh.authority.identity,
                "root_authority_identity": (
                    self.refresh.repository.lock.authority_identity
                ),
                "targets": [
                    {
                        "path": path,
                        "mode": mode,
                        "publication_identity": proof.identity,
                    }
                    for (path, _), proof, (_, mode) in zip(
                        self.endpoints,
                        self.observations,
                        self.target_modes,
                        strict=True,
                    )
                ],
            }
        ).uri


def prepare_refresh_publication(
    prepared: PreparedRepositoryRefresh,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    reservations: WriteReservationSet | None = None,
) -> PreparedRefreshPublication:
    policy = (
        RepositoryFetchDeadlinePolicy() if deadline_policy is None else deadline_policy
    )
    if not isinstance(policy, RepositoryFetchDeadlinePolicy):
        raise TypeError("refresh publication requires a typed deadline policy")
    endpoints = resolve_refresh_endpoints(prepared, reservations=reservations)
    observations: list[RepositoryPublicationObservation] = []
    for (path, endpoint), target in zip(
        endpoints, prepared.authority.request.targets, strict=True
    ):
        require_repository_refresh_inputs_unchanged(prepared, reservations=reservations)
        try:
            proof = verify_repository_publication(
                endpoint,
                target.commit,
                deadline_policy=policy,
                protected_roots=_protected_roots(prepared),
            )
        finally:
            require_repository_refresh_inputs_unchanged(
                prepared, reservations=reservations
            )
        if (
            not isinstance(proof, RepositoryPublicationObservation)
            or path != target.path
            or proof.commit != target.commit
            or proof.repository_identity
            != canonical_identity({"repository_url": endpoint}).uri
            or proof.deadline_policy_identity != policy.identity.uri
        ):
            _fail(
                "publication_mismatch", "publication proof does not match root intent"
            )
        observations.append(proof)
    if resolve_refresh_endpoints(prepared, reservations=reservations) != endpoints:
        _fail("inputs_changed", "root endpoint selection changed during publication")
    previous = {
        item.path: item.commit for item in prepared.authority.previous.repositories
    }
    children = {
        item.root.relative_to(prepared.repository.root).as_posix(): item
        for item in prepared.children
    }
    modes = tuple(
        (
            target.path,
            (
                "root-pin-only"
                if previous[target.path] != target.commit
                and children[target.path].commit == target.commit
                else "source-transition"
            ),
        )
        for target in prepared.authority.request.targets
    )
    return PreparedRefreshPublication(
        prepared, endpoints, tuple(observations), policy, modes
    )


def require_refresh_publication_unchanged(
    prepared: PreparedRefreshPublication,
    *,
    reservations: WriteReservationSet | None = None,
) -> None:
    if not isinstance(prepared, PreparedRefreshPublication):
        raise TypeError("publication revalidation requires prepared observations")
    fresh = prepare_refresh_publication(
        prepared.refresh,
        deadline_policy=prepared.deadline_policy,
        reservations=reservations,
    )
    if fresh != prepared:
        _fail("publication_changed", "publication evidence changed after preparation")


def require_refresh_publication_proofs_unchanged(
    prepared: PreparedRefreshPublication,
    *,
    reservations: WriteReservationSet,
) -> None:
    """Renew exact-head proofs after expected local source transitions."""
    if not isinstance(prepared, PreparedRefreshPublication) or not isinstance(
        reservations, WriteReservationSet
    ):
        raise TypeError("publication renewal requires typed live custody")
    reservations.verify_all()
    modes = dict(prepared.target_modes)
    for (path, endpoint), proof in zip(
        prepared.endpoints, prepared.observations, strict=True
    ):
        if modes[path] != "root-pin-only":
            continue
        fresh = verify_repository_publication(
            endpoint,
            proof.commit,
            deadline_policy=prepared.deadline_policy,
            protected_roots=_protected_roots(prepared.refresh),
        )
        if fresh != proof:
            _fail(
                "publication_changed",
                "publication proof changed before terminal root commit",
            )
        reservations.verify_all()
