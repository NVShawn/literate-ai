"""Observe published commit reachability without borrowing any checkout's objects.

This internal transport boundary accepts an already-resolved endpoint. It writes
only disposable proof storage, not a child/root checkout, and runs no fetched
code. Observations must be renewed before apply; they are not write authority or
child acceptance. Transport time/diagnostic bounds are not a disk quota.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_lineage import (
    MIN_REPOSITORY_FETCH_CONNECT_SECONDS,
    MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
    MIN_REPOSITORY_FETCH_TOTAL_SECONDS,
    RepositoryFetchDeadlinePolicy,
)
from literate_ai.contracts.repository_orchestration import gitlink_url
from literate_ai.contracts.repository_refresh import RepositoryRefreshTarget
from literate_ai.contracts.repository_tree import (
    RepositoryTreeCapturePolicy,
    RepositoryTreeSnapshot,
)

from ._repository_pack_capture import (
    RepositoryObjectPack,
    RepositoryPackPolicy,
    capture_repository_pack,
)
from ._repository_tree_capture import capture_repository_tree
from .builders._process import BoundedProcessResult, run_bounded_process
from .builders.python import BuildError
from .repository_orchestration import OrchestrationInventoryError

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NAMESPACE = "refs/litai-publication/"
_MAXIMUM_REFS = 4096
_STREAM_BYTES = 2 * 1024 * 1024
_MAXIMUM_ATTEMPTS = 3
_TRANSIENT_FAILURES = frozenset(
    {
        "orchestration.publication_git_failed",
        "orchestration.publication_transport_failed",
        "orchestration.publication_timeout",
        "orchestration.publication_no_progress_timeout",
    }
)


def _fail(suffix: str, message: str) -> NoReturn:
    raise OrchestrationInventoryError("publication_" + suffix, message) from None


class _PublicationCleanupFailure(OrchestrationInventoryError):
    pass


class _ExactTipShallowFetchFailure(OrchestrationInventoryError):
    pass


def _cleanup_failure(error: BaseException) -> BaseException:
    if isinstance(error, (OSError, UnicodeError)):
        return _PublicationCleanupFailure(
            "publication_transport_failed",
            "publication proof storage or transport is unavailable",
        )
    return error


@contextmanager
def _temporary_proof_storage(protected_roots: tuple[Path, ...]):
    temporary = tempfile.TemporaryDirectory(
        prefix="litai-pub-", dir=_proof_storage_parent(protected_roots)
    )
    primary = None
    try:
        yield Path(temporary.name)
    except BaseException as error:
        primary = (error, error.__traceback__)
    try:
        temporary.cleanup()
    except BaseException as error:
        cleanup = _cleanup_failure(error)
        if primary is not None:
            raise primary[0].with_traceback(primary[1]) from cleanup
        raise cleanup from None
    if primary is not None:
        raise primary[0].with_traceback(primary[1])


def _transport_failure(suffix: str, message: str, primary: BaseException) -> NoReturn:
    error = OrchestrationInventoryError("publication_" + suffix, message)
    if isinstance(primary.__cause__, _PublicationCleanupFailure):
        raise error from primary.__cause__
    raise error from None


def _endpoint(value: str) -> str:
    try:
        gitlink_url(value)
        if re.match(r"^[A-Za-z]:", value) and not Path(value).is_absolute():
            raise ValueError("drive-relative endpoint is host-dependent")
        # Relative .gitmodules URLs need root-owned resolution, not this process's
        # working directory or a child's potentially overridden origin setting.
        if not (
            Path(value).is_absolute()
            or "://" in value
            or re.fullmatch(r"(?:[^/@:]+@)?[^/:]+:.+", value)
        ):
            raise ValueError("unresolved endpoint")
    except (TypeError, ValueError):
        _fail("endpoint_invalid", "publication requires a safe resolved endpoint")
    return value


def _proof_storage_parent(protected_roots: tuple[Path, ...]) -> Path:
    parent = Path(tempfile.gettempdir()).resolve()
    if any(parent.is_relative_to(root.resolve()) for root in protected_roots):
        _fail("storage_overlap", "proof storage must be outside protected repositories")
    return parent


def _references(raw: bytes, *, fetched: bool = False) -> tuple[tuple[str, str], ...]:
    if len(raw) > _STREAM_BYTES or (raw and not raw.endswith(b"\n")):
        _fail("refs_invalid", "advertisement is oversized or incomplete")
    values: dict[str, str] = {}
    for line in raw.splitlines():
        try:
            oid, name = line.decode("utf-8").split("\t")
        except (UnicodeError, ValueError):
            _fail("refs_invalid", "advertisement contains a malformed reference")
        if fetched:
            if not name.startswith(_NAMESPACE):
                _fail("refs_invalid", "proof storage contains an unexpected reference")
            name = "refs/" + name[len(_NAMESPACE) :]
        if (
            not _OID.fullmatch(oid)
            or not oid.strip("0")
            or not name.startswith("refs/")
            or name in values
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(char in " ~^:?*[\\" for char in name)
            or ".." in name
            or "@{" in name
            or name.endswith(".")
            or any(
                not part or part.startswith(".") or part.endswith(".lock")
                for part in name.split("/")
            )
        ):
            _fail("refs_invalid", "advertisement contains an invalid reference")
        values[name] = oid
        if len(values) > _MAXIMUM_REFS:
            _fail("refs_invalid", "advertisement exceeds the reference count bound")
    return tuple(sorted(values.items()))


def _exact_advertised_branch_tip(
    references: tuple[tuple[str, str], ...], commit: str
) -> tuple[str, str, str] | None:
    """Select the first canonical branch whose directly advertised OID is exact."""
    for name, oid in sorted(references):
        if oid == commit and name.startswith("refs/heads/"):
            return name, oid, oid
    return None


@dataclass(frozen=True, slots=True)
class RepositoryPublicationObservation:
    """Internal, ephemeral evidence, with no persisted-observation admission API."""

    repository_identity: str
    commit: str
    advertisement_identity: str
    reference: str
    reference_object: str
    reference_commit: str
    deadline_policy_identity: str

    def __post_init__(self) -> None:
        for identity in (
            self.repository_identity,
            self.advertisement_identity,
            self.deadline_policy_identity,
        ):
            if not isinstance(identity, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", identity
            ):
                _fail(
                    "observation_invalid",
                    "publication requires exact evidence identities",
                )
        for commit in (self.commit, self.reference_object, self.reference_commit):
            RepositoryRefreshTarget("publication-target", commit)
            if len(commit) != len(self.commit):
                _fail("observation_invalid", "publication object formats must agree")
        if not isinstance(self.reference, str):
            _fail("observation_invalid", "publication witness requires a reference")
        _references(
            (self.reference_object + "\t" + self.reference + "\n").encode("utf-8")
        )

    @property
    def identity(self) -> str:
        return canonical_identity(asdict(self)).uri


@dataclass(frozen=True, slots=True)
class PublishedRepositoryTree:
    publication: RepositoryPublicationObservation
    tree: RepositoryTreeSnapshot

    def __post_init__(self) -> None:
        if not isinstance(
            self.publication, RepositoryPublicationObservation
        ) or not isinstance(self.tree, RepositoryTreeSnapshot):
            raise TypeError("published tree requires typed proof and complete tree")
        if self.publication.commit != self.tree.commit:
            raise ValueError("published tree must match its proven commit")


@dataclass(frozen=True, slots=True)
class PublishedRepositoryPack:
    publication: RepositoryPublicationObservation
    tree: RepositoryTreeSnapshot
    objects: RepositoryObjectPack

    def __post_init__(self):
        PublishedRepositoryTree(self.publication, self.tree)
        if (
            not isinstance(self.objects, RepositoryObjectPack)
            or self.objects.commit != self.tree.commit
        ):
            raise ValueError("published pack must match its proven tree")


def _observe_repository_publication(
    repository_url: str,
    commit: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy,
    reviewed_deadline_policy: RepositoryFetchDeadlinePolicy,
    expires: float,
    protected_roots: tuple[Path, ...] = (),
    process_runner: Callable[..., BoundedProcessResult] = run_bounded_process,
    clock: Callable[[], float] = time.monotonic,
    tree_policy: RepositoryTreeCapturePolicy | None = None,
    pack_policy: RepositoryPackPolicy | None = None,
    exact_tip_allowed: bool = True,
) -> (
    RepositoryPublicationObservation | PublishedRepositoryTree | PublishedRepositoryPack
):
    """Prove an exact commit is reachable from a stable advertised remote ref.

    Never fetch the requested object ID directly: servers may serve dangling objects.
    An exact advertised branch tip may use a depth-one fetch of that fully qualified
    ref. Every other target uses fresh full-history storage, and only advertised refs
    fetched into our private namespace can witness ancestry.
    """
    endpoint = repository_url
    policy = deadline_policy
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="file:git:http:https:ssh",
        GIT_SSH_COMMAND=(
            f"ssh -F {shlex.quote(os.devnull)} -o BatchMode=yes "
            "-o StrictHostKeyChecking=yes -o ConnectionAttempts=1 "
            f"-o ConnectTimeout={policy.connect_seconds}"
        ),
        LC_ALL="C",
    )
    try:
        with _temporary_proof_storage(protected_roots) as repository:
            hooks = repository / "empty-hooks"
            hooks.mkdir()

            def run(
                *arguments: str,
                allowed: tuple[int, ...] = (0,),
                stdout_limit_bytes: int = _STREAM_BYTES,
            ) -> BoundedProcessResult:
                # Floating-point addition/subtraction can round a fresh deadline
                # above its reviewed budget, even with a nondecreasing clock.
                remaining = min(policy.total_seconds, expires - clock())
                if remaining <= 0:
                    _fail("timeout", "publication exceeded its total deadline")
                result = process_runner(
                    (
                        "git",
                        "-c",
                        f"core.hooksPath={hooks}",
                        "-c",
                        "gc.auto=0",
                        "-c",
                        "maintenance.auto=false",
                        "-c",
                        "fetch.writeCommitGraph=false",
                        "-c",
                        "http.followRedirects=false",
                        "-c",
                        "protocol.file.allow=always",
                        *arguments,
                    ),
                    cwd=repository,
                    environment=environment,
                    timeout_seconds=remaining,
                    inactivity_timeout_seconds=min(
                        policy.no_progress_seconds, remaining
                    ),
                    stdout_limit_bytes=stdout_limit_bytes,
                    stderr_limit_bytes=_STREAM_BYTES,
                    error_prefix="orchestration.publication",
                )
                if result.returncode not in allowed:
                    # Never copy Git/transport diagnostics: helpers may echo secrets.
                    _fail("git_failed", "Git could not establish publication evidence")
                if clock() >= expires:
                    _fail("timeout", "publication exceeded its total deadline")
                return result

            run(
                "init",
                "--bare",
                "--template=",
                "--object-format=" + ("sha1" if len(commit) == 40 else "sha256"),
                ".",
            )

            def advertised():
                return _references(run("ls-remote", "--refs", endpoint).stdout)

            before = advertised()
            if not before:
                _fail("unpublished", "repository advertises no publication witness")
            exact_witness = _exact_advertised_branch_tip(before, commit)
            use_shallow_witness = exact_tip_allowed and exact_witness is not None
            exact_refspec = None

            def require_exact_fetched_witness(name: str, oid: str) -> None:
                destination = _NAMESPACE + name.removeprefix("refs/")
                fetched = _references(
                    run(
                        "for-each-ref",
                        "--format=%(objectname)%09%(refname)",
                        destination,
                    ).stdout,
                    fetched=True,
                )
                if fetched != ((name, oid),):
                    _fail(
                        "refs_changed",
                        "fetched branch tip differs from its advertisement",
                    )
                resolved = run(
                    "rev-parse",
                    "--verify",
                    destination + "^{commit}",
                    allowed=(0, 1, 128),
                )
                if resolved.returncode or resolved.stdout != (oid + "\n").encode(
                    "ascii"
                ):
                    _fail(
                        "refs_changed",
                        "fetched branch witness does not resolve to "
                        "its advertised commit",
                    )

            if use_shallow_witness:
                assert exact_witness is not None
                name, oid, _resolved = exact_witness
                destination = _NAMESPACE + name.removeprefix("refs/")
                exact_refspec = "+" + name + ":" + destination
                try:
                    run(
                        "fetch",
                        "--depth=1",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--no-write-fetch-head",
                        "--no-auto-maintenance",
                        "--progress",
                        endpoint,
                        exact_refspec,
                    )
                except OrchestrationInventoryError as error:
                    if error.code != "orchestration.publication_git_failed":
                        raise
                    raise _ExactTipShallowFetchFailure(
                        error.code,
                        "Git could not establish publication evidence",
                    ) from None
                require_exact_fetched_witness(name, oid)
                if before != advertised():
                    _fail("refs_changed", "advertised references changed during proof")
            else:
                run(
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--no-write-fetch-head",
                    "--no-auto-maintenance",
                    "--progress",
                    endpoint,
                    "+refs/*:" + _NAMESPACE + "*",
                )
                fetched = _references(
                    run(
                        "for-each-ref",
                        "--format=%(objectname)%09%(refname)",
                        _NAMESPACE,
                    ).stdout,
                    fetched=True,
                )
                if before != fetched or before != advertised():
                    _fail("refs_changed", "advertised references changed during proof")
                if exact_witness is not None:
                    require_exact_fetched_witness(exact_witness[0], exact_witness[1])
            target = run("cat-file", "-t", commit, allowed=(0, 1, 128))
            if target.returncode or target.stdout != b"commit\n":
                _fail("unpublished", "requested object is not a published commit")
            target_tree = run("cat-file", "-t", commit + "^{tree}", allowed=(0, 1, 128))
            if target_tree.returncode or target_tree.stdout != b"tree\n":
                _fail("unpublished", "published commit does not resolve to a tree")
            witness = exact_witness
            if witness is None:
                for name, oid in before:
                    reference = _NAMESPACE + name.removeprefix("refs/")
                    kind = run("cat-file", "-t", reference + "^{}").stdout
                    if kind in (b"blob\n", b"tree\n"):
                        continue
                    if kind != b"commit\n":
                        _fail(
                            "refs_invalid",
                            "advertised object cannot witness a commit",
                        )
                    tip = run("rev-parse", "--verify", reference + "^{commit}").stdout
                    if not _OID.fullmatch(tip.decode("ascii").removesuffix("\n")):
                        _fail(
                            "refs_invalid",
                            "advertised commit could not be resolved",
                        )
                    if not run(
                        "merge-base",
                        "--is-ancestor",
                        commit,
                        oid + "^{commit}",
                        allowed=(0, 1),
                    ).returncode:
                        witness = (name, oid, tip.decode("ascii").strip())
                        break
            if witness is None:
                _fail("unpublished", "commit is not reachable from advertised history")
            # Ancestry traversal also consumes time in which the remote may move.
            if before != advertised():
                _fail("refs_changed", "advertised references changed during proof")
            observation = RepositoryPublicationObservation(
                canonical_identity({"repository_url": endpoint}).uri,
                commit,
                canonical_identity({"references": before}).uri,
                *witness,
                reviewed_deadline_policy.identity.uri,
            )
            if tree_policy is None:
                return observation
            tree = capture_repository_tree(run, commit, tree_policy)
            objects = None
            if pack_policy is not None:
                if exact_refspec is not None:
                    shallow = run("rev-parse", "--is-shallow-repository").stdout
                    if shallow == b"true\n":
                        run(
                            "fetch",
                            "--unshallow",
                            "--no-tags",
                            "--no-recurse-submodules",
                            "--no-write-fetch-head",
                            "--no-auto-maintenance",
                            "--progress",
                            endpoint,
                            exact_refspec,
                        )
                    elif shallow != b"false\n":
                        _fail(
                            "refs_invalid",
                            "proof storage has an invalid shallow state",
                        )
                    require_exact_fetched_witness(witness[0], commit)
                    target = run("cat-file", "-t", commit, allowed=(0, 1, 128))
                    if target.returncode or target.stdout != b"commit\n":
                        _fail(
                            "refs_changed",
                            "exact branch target changed during pack capture",
                        )
                objects = capture_repository_pack(run, repository, commit, pack_policy)
            if before != advertised():
                _fail(
                    "refs_changed", "advertised references changed during tree capture"
                )
            return (
                PublishedRepositoryTree(observation, tree)
                if objects is None
                else PublishedRepositoryPack(observation, tree, objects)
            )
    except BuildError as error:
        suffix = (
            "no_progress_timeout"
            if error.code.endswith("_no_progress_timeout")
            else "timeout"
            if error.code.endswith("_timeout")
            else "transport_failed"
        )
        _transport_failure(
            suffix,
            "publication transport failed within its configured bounds",
            error,
        )
    except (OSError, UnicodeError) as error:
        _transport_failure(
            "transport_failed",
            "publication proof storage or transport is unavailable",
            error,
        )


def _retry_repository_publication(
    repository_url: str,
    commit: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    protected_roots: tuple[Path, ...] = (),
    process_runner: Callable[..., BoundedProcessResult] = run_bounded_process,
    clock: Callable[[], float] = time.monotonic,
    tree_policy: RepositoryTreeCapturePolicy | None = None,
    pack_policy: RepositoryPackPolicy | None = None,
) -> (
    RepositoryPublicationObservation | PublishedRepositoryTree | PublishedRepositoryPack
):
    endpoint = _endpoint(repository_url)
    RepositoryRefreshTarget("publication-target", commit)
    policy = (
        RepositoryFetchDeadlinePolicy() if deadline_policy is None else deadline_policy
    )
    if not isinstance(policy, RepositoryFetchDeadlinePolicy):
        raise TypeError("publication requires a typed fetch deadline policy")
    if tree_policy is not None and not isinstance(
        tree_policy, RepositoryTreeCapturePolicy
    ):
        raise TypeError("publication tree capture requires a typed bounds policy")
    if pack_policy is not None and not isinstance(pack_policy, RepositoryPackPolicy):
        raise TypeError("publication pack capture requires a typed bounds policy")
    expires = clock() + policy.total_seconds
    last_error = None
    exact_tip_allowed = True
    for _attempt in range(_MAXIMUM_ATTEMPTS):
        remaining = expires - clock()
        if remaining <= 0:
            if last_error is not None:
                raise last_error
            _fail("timeout", "publication exceeded its total deadline")
        # Contract policies use bounded whole seconds. The absolute deadline below
        # remains authoritative when less than a contract minimum remains.
        remaining_seconds = max(1, math.ceil(remaining))
        attempt_total = max(
            MIN_REPOSITORY_FETCH_TOTAL_SECONDS,
            min(policy.total_seconds, remaining_seconds),
        )
        attempt_no_progress = max(
            MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
            min(policy.no_progress_seconds, attempt_total, remaining_seconds),
        )
        attempt_policy = RepositoryFetchDeadlinePolicy(
            total_seconds=attempt_total,
            no_progress_seconds=attempt_no_progress,
            connect_seconds=max(
                MIN_REPOSITORY_FETCH_CONNECT_SECONDS,
                min(policy.connect_seconds, attempt_no_progress, remaining_seconds),
            ),
        )
        try:
            return _observe_repository_publication(
                endpoint,
                commit,
                deadline_policy=attempt_policy,
                reviewed_deadline_policy=policy,
                expires=expires,
                protected_roots=protected_roots,
                process_runner=process_runner,
                clock=clock,
                tree_policy=tree_policy,
                pack_policy=pack_policy,
                exact_tip_allowed=exact_tip_allowed,
            )
        except _ExactTipShallowFetchFailure as error:
            # Some dumb transports advertise refs but cannot negotiate a shallow
            # fetch. Consume this attempt, discard its storage and retry only the
            # pre-existing full ancestry proof under the original absolute deadline.
            exact_tip_allowed = False
            last_error = error
        except OrchestrationInventoryError as error:
            if error.code not in _TRANSIENT_FAILURES:
                raise
            last_error = error
    assert last_error is not None
    raise last_error


def verify_repository_publication(
    repository_url: str,
    commit: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    protected_roots: tuple[Path, ...] = (),
    process_runner: Callable[..., BoundedProcessResult] = run_bounded_process,
    clock: Callable[[], float] = time.monotonic,
) -> RepositoryPublicationObservation:
    """Prove advertised ancestry in fresh storage, without capturing source bytes."""
    result = _retry_repository_publication(
        repository_url,
        commit,
        deadline_policy=deadline_policy,
        protected_roots=protected_roots,
        process_runner=process_runner,
        clock=clock,
    )
    assert isinstance(result, RepositoryPublicationObservation)
    return result


def capture_published_repository_tree(
    repository_url: str,
    commit: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    tree_policy: RepositoryTreeCapturePolicy | None = None,
    protected_roots: tuple[Path, ...] = (),
    process_runner: Callable[..., BoundedProcessResult] = run_bounded_process,
    clock: Callable[[], float] = time.monotonic,
) -> PublishedRepositoryTree:
    """Capture exact published raw objects without checkout or child execution."""
    result = _retry_repository_publication(
        repository_url,
        commit,
        deadline_policy=deadline_policy,
        protected_roots=protected_roots,
        process_runner=process_runner,
        clock=clock,
        tree_policy=RepositoryTreeCapturePolicy()
        if tree_policy is None
        else tree_policy,
    )
    assert isinstance(result, PublishedRepositoryTree)
    return result


def capture_published_repository_pack(
    repository_url: str,
    commit: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    tree_policy: RepositoryTreeCapturePolicy | None = None,
    pack_policy: RepositoryPackPolicy | None = None,
    protected_roots: tuple[Path, ...] = (),
    process_runner: Callable[..., BoundedProcessResult] = run_bounded_process,
    clock: Callable[[], float] = time.monotonic,
) -> PublishedRepositoryPack:
    result = _retry_repository_publication(
        repository_url,
        commit,
        deadline_policy=deadline_policy,
        tree_policy=RepositoryTreeCapturePolicy()
        if tree_policy is None
        else tree_policy,
        pack_policy=RepositoryPackPolicy() if pack_policy is None else pack_policy,
        protected_roots=protected_roots,
        process_runner=process_runner,
        clock=clock,
    )
    assert isinstance(result, PublishedRepositoryPack)
    return result
