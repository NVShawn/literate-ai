"""Non-executing Git adapter for repository-parent lineage resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.builders._process import (
    BoundedProcessResult,
    run_bounded_process,
)
from literate_ai.adapters.builders.python import BuildError
from literate_ai.application.repository_lineage import (
    RepositoryCatalogFile,
    ResolvedRepositoryCatalog,
    ResolvedRepositorySnapshot,
)
from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    RepositoryFetchDeadlinePolicy,
    RepositoryLineage,
    RepositoryParentReference,
    RepositoryParentSelection,
)
from literate_ai.diagnostics import report_progress
from literate_ai.projects import parse_project_configuration

REPOSITORY_PARENT_FILE = ".literate/repository-parent.json"
REPOSITORY_LINEAGE_FILE = ".literate/repository-lineage.json"
_PROJECT_FILE = "literate.project.json"
_MAX_AUTHORITY_BYTES = 16 * 1024 * 1024
_MAX_GIT_DIAGNOSTIC_BYTES = 8 * 1024 * 1024
_MAX_CATALOG_FILES = 16_384
_MAX_CATALOG_BYTES = 256 * 1024 * 1024
_NON_FETCH_GIT_TIMEOUT_SECONDS = 120
_SCP_REPOSITORY = re.compile(r"^(?P<user>[^/@:]+)@(?P<host>[^/:]+):(?P<path>.+)$")
RepositoryGitProcessRunner = Callable[..., BoundedProcessResult]


class GitRepositoryLineageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class RepositoryLineageStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def repository_parent_reference(
    source: str, *, base: Path | None = None
) -> RepositoryParentReference:
    """Normalize a CLI repository source and optional ``#revision`` selector."""

    if not isinstance(source, str) or not source.strip():
        raise GitRepositoryLineageError(
            "repository_lineage.source_invalid",
            "repository parent source must be a URL or local path",
        )
    raw = source.strip()
    locator, separator, revision = raw.rpartition("#")
    if not separator:
        locator, revision = raw, "HEAD"
    elif not locator or not revision:
        raise GitRepositoryLineageError(
            "repository_lineage.source_invalid",
            "repository parent #revision selector must be nonempty",
        )
    scp = _SCP_REPOSITORY.fullmatch(locator)
    if scp is not None:
        locator = f"ssh://{scp.group('user')}@{scp.group('host')}/{scp.group('path')}"
    elif not urlsplit(locator).scheme or (
        os.name == "nt" and len(locator) >= 2 and locator[1] == ":"
    ):
        configured = Path(locator).expanduser()
        if not configured.is_absolute():
            configured = (Path.cwd() if base is None else Path(base)) / configured
        locator = configured.resolve().as_uri()
    try:
        return RepositoryParentReference(locator, revision)
    except ValueError as exc:
        raise GitRepositoryLineageError(
            "repository_lineage.source_invalid",
            "repository parent URL or revision selector is invalid",
        ) from exc


def _document_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    ).encode("utf-8")


def _metadata_path(root: Path, relative: str) -> Path:
    path = root.resolve()
    for part in Path(relative).parts:
        path /= part
        if path_is_link_or_reparse(path):
            raise RepositoryLineageStoreError(
                "repository_lineage.path_unsafe",
                f"repository-lineage path crosses a link or reparse point: {relative}",
            )
    return path


def _read_contract(root: Path, relative: str, contract_type):
    path = _metadata_path(root, relative)
    if not path.is_file():
        raise RepositoryLineageStoreError(
            "repository_lineage.evidence_missing",
            f"repository-lineage evidence is missing: {relative}",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RepositoryLineageStoreError(
            "repository_lineage.evidence_unreadable",
            f"repository-lineage evidence is unreadable: {relative}",
        ) from exc
    if len(content) > _MAX_AUTHORITY_BYTES:
        raise RepositoryLineageStoreError(
            "repository_lineage.evidence_oversized",
            f"repository-lineage evidence exceeds the byte limit: {relative}",
        )
    try:
        return contract_type.from_dict(json.loads(content))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepositoryLineageStoreError(
            "repository_lineage.evidence_invalid",
            f"repository-lineage evidence is invalid: {relative}",
        ) from exc


class FilesystemRepositoryLineageStore:
    """Read or atomically replace the authored parent and its resolved lineage lock."""

    def __init__(self, project_root: Path) -> None:
        self.root = Path(project_root).resolve()

    def load(self) -> tuple[RepositoryParentSelection, RepositoryLineage]:
        selection = _read_contract(
            self.root, REPOSITORY_PARENT_FILE, RepositoryParentSelection
        )
        lineage = _read_contract(self.root, REPOSITORY_LINEAGE_FILE, RepositoryLineage)
        if lineage.selection != selection:
            raise RepositoryLineageStoreError(
                "repository_lineage.selection_mismatch",
                "repository parent authority does not match its resolved lineage",
            )
        return selection, lineage

    def load_optional(
        self,
    ) -> tuple[RepositoryParentSelection, RepositoryLineage] | None:
        """Return no lineage only when both authority documents are absent."""

        selection = self._optional(REPOSITORY_PARENT_FILE)
        lineage = self._optional(REPOSITORY_LINEAGE_FILE)
        if selection is None and lineage is None:
            return None
        if selection is None or lineage is None:
            raise RepositoryLineageStoreError(
                "repository_lineage.evidence_incomplete",
                "repository parent authority and resolved lineage must both be present",
            )
        if lineage.selection != selection:
            raise RepositoryLineageStoreError(
                "repository_lineage.selection_mismatch",
                "repository parent authority does not match its resolved lineage",
            )
        return selection, lineage

    def replace(
        self,
        selection: RepositoryParentSelection,
        lineage: RepositoryLineage,
        *,
        expected_selection_identity: ContentIdentity | None = None,
        expected_lineage_identity: ContentIdentity | None = None,
        expected_absent: bool = False,
    ) -> None:
        if not isinstance(selection, RepositoryParentSelection) or not isinstance(
            lineage, RepositoryLineage
        ):
            raise TypeError("repository-lineage replacement requires typed contracts")
        if lineage.selection != selection:
            raise RepositoryLineageStoreError(
                "repository_lineage.selection_mismatch",
                "replacement lineage does not bind the selected repository parents",
            )
        if not isinstance(expected_absent, bool):
            raise TypeError("expected-absence guard must be boolean")
        if expected_absent and (
            expected_selection_identity is not None
            or expected_lineage_identity is not None
        ):
            raise TypeError(
                "expected absence cannot be combined with expected identities"
            )
        metadata = _metadata_path(self.root, ".literate")
        if metadata.exists() and not metadata.is_dir():
            raise RepositoryLineageStoreError(
                "repository_lineage.path_unsafe",
                "repository-lineage metadata root must be a directory",
            )
        metadata.mkdir(parents=True, exist_ok=True)
        lock = metadata / "repository-lineage.lock"
        try:
            with exclusive_cache_lock(lock):
                current_selection = self._optional(REPOSITORY_PARENT_FILE)
                current_lineage = self._optional(REPOSITORY_LINEAGE_FILE)
                if expected_absent and (
                    current_selection is not None or current_lineage is not None
                ):
                    raise RepositoryLineageStoreError(
                        "repository_lineage.concurrent_change",
                        "repository-lineage evidence appeared before replacement",
                    )
                if expected_selection_identity is not None and (
                    current_selection is None
                    or current_selection.identity != expected_selection_identity
                ):
                    raise RepositoryLineageStoreError(
                        "repository_lineage.concurrent_change",
                        "repository parent authority changed before replacement",
                    )
                if expected_lineage_identity is not None and (
                    current_lineage is None
                    or current_lineage.identity != expected_lineage_identity
                ):
                    raise RepositoryLineageStoreError(
                        "repository_lineage.concurrent_change",
                        "repository lineage changed before replacement",
                    )
                parent_path = _metadata_path(self.root, REPOSITORY_PARENT_FILE)
                lineage_path = _metadata_path(self.root, REPOSITORY_LINEAGE_FILE)
                previous_parent = (
                    parent_path.read_bytes() if parent_path.is_file() else None
                )
                previous_lineage = (
                    lineage_path.read_bytes() if lineage_path.is_file() else None
                )
                self._atomic_write(parent_path, _document_bytes(selection.to_dict()))
                try:
                    self._atomic_write(lineage_path, _document_bytes(lineage.to_dict()))
                except Exception:
                    self._restore(parent_path, previous_parent)
                    self._restore(lineage_path, previous_lineage)
                    raise
        except RepositoryLineageStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise RepositoryLineageStoreError(
                "repository_lineage.write_failed",
                "repository-lineage evidence could not be replaced atomically",
            ) from exc

    def clear(
        self,
        *,
        expected_selection_identity: ContentIdentity,
        expected_lineage_identity: ContentIdentity,
    ) -> None:
        """Remove both documents as one guarded rollback unit."""

        if not isinstance(
            expected_selection_identity, ContentIdentity
        ) or not isinstance(expected_lineage_identity, ContentIdentity):
            raise TypeError("repository-lineage clearing requires typed identities")
        metadata = _metadata_path(self.root, ".literate")
        if not metadata.is_dir():
            raise RepositoryLineageStoreError(
                "repository_lineage.concurrent_change",
                "repository-lineage evidence disappeared before rollback",
            )
        lock = metadata / "repository-lineage.lock"
        try:
            with exclusive_cache_lock(lock):
                current_selection = self._optional(REPOSITORY_PARENT_FILE)
                current_lineage = self._optional(REPOSITORY_LINEAGE_FILE)
                if (
                    current_selection is None
                    or current_selection.identity != expected_selection_identity
                    or current_lineage is None
                    or current_lineage.identity != expected_lineage_identity
                ):
                    raise RepositoryLineageStoreError(
                        "repository_lineage.concurrent_change",
                        "repository-lineage evidence changed before rollback",
                    )
                parent_path = _metadata_path(self.root, REPOSITORY_PARENT_FILE)
                lineage_path = _metadata_path(self.root, REPOSITORY_LINEAGE_FILE)
                previous_parent = parent_path.read_bytes()
                previous_lineage = lineage_path.read_bytes()
                try:
                    parent_path.unlink()
                    lineage_path.unlink()
                except Exception:
                    self._restore(parent_path, previous_parent)
                    self._restore(lineage_path, previous_lineage)
                    raise
        except RepositoryLineageStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise RepositoryLineageStoreError(
                "repository_lineage.write_failed",
                "repository-lineage evidence could not be cleared atomically",
            ) from exc

    def _optional(
        self, relative: str
    ) -> RepositoryParentSelection | RepositoryLineage | None:
        path = _metadata_path(self.root, relative)
        if not path.exists():
            return None
        contract_type = (
            RepositoryParentSelection
            if relative == REPOSITORY_PARENT_FILE
            else RepositoryLineage
        )
        return _read_contract(self.root, relative, contract_type)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _restore(cls, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            cls._atomic_write(path, content)


class GitRepositorySnapshotProvider:
    """Resolve exact Git commits and read authority without a working-tree checkout."""

    def __init__(
        self,
        cache_root: Path,
        *,
        git_binary: str = "git",
        deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
        deadline_provenance: str = "framework-default",
        deadline_field_provenance: Mapping[str, str] | None = None,
        process_runner: RepositoryGitProcessRunner = run_bounded_process,
    ) -> None:
        if not git_binary:
            raise ValueError("Git binary must not be empty")
        if deadline_provenance not in {"framework-default", "cli"}:
            raise ValueError("Git deadline provenance must be framework-default or cli")
        self.cache_root = Path(cache_root)
        self.git_binary = git_binary
        self.deadline_policy = deadline_policy or RepositoryFetchDeadlinePolicy()
        self.deadline_provenance = deadline_provenance
        fields = ("total_seconds", "no_progress_seconds", "connect_seconds")
        self.deadline_field_provenance = {name: deadline_provenance for name in fields}
        if deadline_field_provenance is not None:
            supplied = dict(deadline_field_provenance)
            if set(supplied) != set(fields) or any(
                value not in {"framework-default", "project-policy", "cli"}
                for value in supplied.values()
            ):
                raise ValueError("Git deadline field provenance is invalid")
            self.deadline_field_provenance = supplied
        self._process_runner = process_runner

    def list_remote_heads(self, repository_url: str) -> tuple[str, ...]:
        """List remote branch names without checking out a working tree."""

        with self._locked_repository(repository_url) as repository:
            pass
        report_progress(f"Listing remote heads of {repository_url}")
        result = self._required(
            repository,
            "ls-remote",
            "--heads",
            "--quiet",
            repository_url,
            code="repository_lineage.heads_unavailable",
        )
        heads: list[str] = []
        for line in result.stdout.decode("utf-8", errors="strict").splitlines():
            _object_id, separator, ref = line.partition("\t")
            prefix = "refs/heads/"
            if separator and ref.startswith(prefix) and ref != prefix:
                heads.append(ref[len(prefix) :])
        return tuple(heads)

    def list_remote_tags(self, repository_url: str) -> tuple[str, ...]:
        """List remote tag names without checking out a working tree."""

        with self._locked_repository(repository_url) as repository:
            pass
        report_progress(f"Listing remote tags of {repository_url}")
        result = self._required(
            repository,
            "ls-remote",
            "--tags",
            "--quiet",
            repository_url,
            code="repository_lineage.tags_unavailable",
        )
        tags: list[str] = []
        prefix = "refs/tags/"
        for line in result.stdout.decode("utf-8", errors="strict").splitlines():
            _object_id, separator, ref = line.partition("\t")
            if not separator or not ref.startswith(prefix) or ref.endswith("^{}"):
                continue
            name = ref[len(prefix) :]
            if name:
                tags.append(name)
        return tuple(tags)

    @property
    def deadline_evidence(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/repository-fetch-deadline-selection@1",
            "policy": self.deadline_policy.to_dict(),
            "policy_identity": self.deadline_policy.identity.uri,
            "provenance": self.deadline_provenance,
            "field_provenance": dict(self.deadline_field_provenance),
        }

    def resolve(
        self, reference: RepositoryParentReference
    ) -> ResolvedRepositorySnapshot:
        with self._locked_repository(reference.repository_url) as repository:
            self._fetch(repository, reference)
            revision = self._required_text(
                repository,
                "rev-parse",
                "--verify",
                "FETCH_HEAD^{commit}",
                code="repository_lineage.revision_unavailable",
            ).strip()
        manifest = self._blob(repository, revision, _PROJECT_FILE)
        parent = self._blob(repository, revision, REPOSITORY_PARENT_FILE)
        try:
            definition = parse_project_configuration(manifest)
        except ValueError as exc:
            raise GitRepositoryLineageError(
                "repository_lineage.authority_invalid",
                f"repository project authority is invalid: {exc}",
            ) from exc
        try:
            parent_selection = RepositoryParentSelection.from_dict(json.loads(parent))
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise GitRepositoryLineageError(
                "repository_lineage.authority_invalid",
                f"repository parent authority is invalid: {exc}",
            ) from exc
        return ResolvedRepositorySnapshot(
            reference=reference,
            resolved_revision=revision,
            project_id=definition.project_id,
            project_identity=ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(manifest).hexdigest()
            ),
            parent_selection=parent_selection,
        )

    def update_blobs(
        self, reference: RepositoryParentReference, paths: tuple[str, ...]
    ) -> dict[str, bytes]:
        """Read historical update bases without checkout, filters, or hooks."""
        import re

        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", reference.requested_revision):
            raise ValueError("update base recovery requires an exact commit")
        for path in paths:
            parsed = PurePosixPath(path)
            if (
                parsed.is_absolute()
                or parsed.as_posix() != path
                or ".." in parsed.parts
                or "\\" in path
            ):
                raise ValueError("unsafe update base path")
        with self._locked_repository(reference.repository_url) as repository:
            present = self._run(
                repository, "cat-file", "-e", reference.requested_revision + "^{commit}"
            )
            if present.returncode:
                self._fetch(repository, reference)
            result = {}
            for path in paths:
                try:
                    result[path] = self._blob(
                        repository, reference.requested_revision, path
                    )
                except GitRepositoryLineageError as exc:
                    if exc.code != "repository_lineage.authority_missing":
                        raise
            return result

    def catalog(self, node) -> ResolvedRepositoryCatalog:
        """Read declared catalog roots at one locked revision without checkout."""

        from literate_ai.contracts import RepositoryLineageNode

        if not isinstance(node, RepositoryLineageNode):
            raise TypeError("repository catalog resolution requires a lineage node")
        reference = RepositoryParentReference(
            node.repository_url, node.resolved_revision
        )
        with self._locked_repository(node.repository_url) as repository:
            self._fetch(repository, reference)
            revision = self._required_text(
                repository,
                "rev-parse",
                "--verify",
                "FETCH_HEAD^{commit}",
                code="repository_lineage.revision_unavailable",
            ).strip()
        if revision != node.resolved_revision:
            raise GitRepositoryLineageError(
                "repository_lineage.revision_changed",
                "exact repository revision changed during catalog resolution",
            )
        manifest = self._blob(repository, revision, _PROJECT_FILE)
        if (
            ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(manifest).hexdigest())
            != node.project_identity
        ):
            raise GitRepositoryLineageError(
                "repository_lineage.project_changed",
                "repository project authority differs from the resolved lineage",
            )
        try:
            definition = parse_project_configuration(manifest)
        except ValueError as exc:
            raise GitRepositoryLineageError(
                "repository_lineage.authority_invalid",
                f"repository project authority is invalid: {exc}",
            ) from exc
        roots = tuple(
            sorted(
                set(
                    definition.component_roots
                    + definition.flavor_roots
                    + definition.skill_roots
                    + definition.mcp_roots
                    + definition.workflow_roots
                    + definition.routing_roots
                )
            )
        )
        if not roots:
            return ResolvedRepositoryCatalog(node, definition, ())
        result = self._run(
            repository,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
            "--",
            *roots,
        )
        if result.returncode != 0:
            raise GitRepositoryLineageError(
                "repository_lineage.catalog_unavailable",
                "repository catalog tree could not be read",
            )
        records = tuple(item for item in result.stdout.split(b"\0") if item)
        if len(records) > _MAX_CATALOG_FILES:
            raise GitRepositoryLineageError(
                "repository_lineage.catalog_oversized",
                "repository catalog exceeds the tracked-file limit",
            )
        files: list[RepositoryCatalogFile] = []
        total = 0
        for record in records:
            metadata, separator, raw_path = record.partition(b"\t")
            fields = metadata.split(b" ")
            if not separator or len(fields) != 3:
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_invalid",
                    "repository catalog tree contains an invalid Git record",
                )
            mode, object_type, object_id = fields
            if object_type != b"blob" or mode not in {b"100644", b"100755"}:
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_unsafe",
                    "repository catalogs may contain only regular tracked files",
                )
            try:
                path = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_invalid",
                    "repository catalog paths must be UTF-8",
                ) from exc
            parsed = PurePosixPath(path)
            if (
                parsed.is_absolute()
                or parsed.as_posix() != path
                or ".." in parsed.parts
                or "\\" in path
            ):
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_unsafe",
                    "repository catalog contains an unsafe path",
                )
            blob = self._run(repository, "cat-file", "blob", object_id.decode("ascii"))
            if blob.returncode != 0:
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_unavailable",
                    "repository catalog blob could not be read",
                )
            total += len(blob.stdout)
            if total > _MAX_CATALOG_BYTES:
                raise GitRepositoryLineageError(
                    "repository_lineage.catalog_oversized",
                    "repository catalog exceeds the total byte limit",
                )
            files.append(
                RepositoryCatalogFile(path, blob.stdout, executable=mode == b"100755")
            )
        return ResolvedRepositoryCatalog(
            node, definition, tuple(sorted(files, key=lambda x: x.path))
        )

    @contextmanager
    def _locked_repository(self, repository_url: str) -> Iterator[Path]:
        """Hold one process-safe lock for this repository's derived bare-cache entry.

        The lock is keyed by the same content-derived hash used for the cache
        entry's directory name, so unrelated repository identities never share
        a lock and stay concurrent. The lock guards bare-cache creation and
        the fetch that follows: two processes racing an uncoordinated fetch
        into the same bare cache previously collided on Git's own lock files
        (`repository_lineage.fetch_failed`, issue #324). The underlying
        `exclusive_cache_lock` is an OS-level advisory lock (``flock``/
        ``msvcrt.locking``) tied to an open file descriptor, so a killed or
        crashed owner releases it automatically when its descriptor closes --
        there is no PID file to go stale and no unrecoverable lock.
        """

        key = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()
        root = self.cache_root.resolve()
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise GitRepositoryLineageError(
                "repository_lineage.cache_unsafe",
                "repository-lineage cache root must be a regular directory",
            )
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{key}.lock"
        try:
            with exclusive_cache_lock(lock_path):
                yield self._ensure_repository(root, key)
        except CacheLockError as exc:
            raise GitRepositoryLineageError(
                "repository_lineage.cache_lock_unavailable",
                "repository-lineage cache entry lock could not be acquired for "
                f"entry {key} (another process may be fetching this repository, "
                "or it left a stale lock)",
            ) from exc

    def _ensure_repository(self, root: Path, key: str) -> Path:
        repository = root / f"{key}.git"
        if repository.is_symlink():
            raise GitRepositoryLineageError(
                "repository_lineage.cache_unsafe",
                "repository-lineage cache entry cannot be a symbolic link",
            )
        if not repository.exists():
            repository.mkdir()
            try:
                self._required(
                    repository,
                    "init",
                    "--bare",
                    ".",
                    code="repository_lineage.git_unavailable",
                )
            except Exception:
                shutil.rmtree(repository, ignore_errors=True)
                raise
        elif not repository.is_dir():
            raise GitRepositoryLineageError(
                "repository_lineage.cache_unsafe",
                "repository-lineage cache entry must be a directory",
            )
        return repository

    def _fetch(self, repository: Path, reference: RepositoryParentReference) -> None:
        report_progress(
            f"Fetching {reference.repository_url}#{reference.requested_revision}"
        )
        self._required(
            repository,
            "fetch",
            "--force",
            "--depth=1",
            "--no-recurse-submodules",
            # This also disables modern Git's automatic maintenance. A detached
            # child can rewrite shallow state after our cache lock is released.
            "--no-auto-gc",
            "--progress",
            reference.repository_url,
            reference.requested_revision,
            code="repository_lineage.fetch_failed",
        )

    def _blob(self, repository: Path, revision: str, path: str) -> bytes:
        result = self._run(repository, "show", f"{revision}:{path}")
        if result.returncode != 0:
            raise GitRepositoryLineageError(
                "repository_lineage.authority_missing",
                f"repository revision does not contain required authority: {path}",
            )
        if len(result.stdout) > _MAX_AUTHORITY_BYTES:
            raise GitRepositoryLineageError(
                "repository_lineage.authority_oversized",
                f"repository authority exceeds the byte limit: {path}",
            )
        return result.stdout

    def _required_text(
        self,
        repository: Path,
        *arguments: str,
        code: str,
    ) -> str:
        return self._required(repository, *arguments, code=code).stdout.decode(
            "utf-8", errors="strict"
        )

    def _required(
        self,
        repository: Path,
        *arguments: str,
        code: str,
    ) -> BoundedProcessResult:
        result = self._run(repository, *arguments)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").splitlines()
            suffix = f": {detail[-1][-500:]}" if detail else ""
            raise GitRepositoryLineageError(
                code,
                "Git could not resolve repository lineage"
                f" (elapsed_seconds={result.elapsed_seconds:.3f}, "
                f"deadline_seconds={self.deadline_policy.total_seconds}, "
                f"policy_identity={self.deadline_policy.identity.uri}, "
                f"provenance={self.deadline_provenance}){suffix}",
            )
        return result

    def _run(self, repository: Path, *arguments: str) -> BoundedProcessResult:
        executable = shutil.which(self.git_binary)
        if executable is None:
            raise GitRepositoryLineageError(
                "repository_lineage.git_unavailable",
                "Git is required to resolve repository parents; install Git and retry",
            )
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_SSH_COMMAND": (
                    "ssh -o BatchMode=yes -o ConnectionAttempts=1 "
                    f"-o ConnectTimeout={self.deadline_policy.connect_seconds}"
                ),
                "LC_ALL": "C",
            }
        )
        is_fetch = bool(arguments) and arguments[0] == "fetch"
        try:
            return self._process_runner(
                (
                    executable,
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-C",
                    str(repository),
                    *arguments,
                ),
                cwd=repository,
                environment=environment,
                timeout_seconds=(
                    self.deadline_policy.total_seconds
                    if is_fetch
                    else _NON_FETCH_GIT_TIMEOUT_SECONDS
                ),
                inactivity_timeout_seconds=(
                    self.deadline_policy.no_progress_seconds if is_fetch else None
                ),
                stdout_limit_bytes=_MAX_AUTHORITY_BYTES,
                stderr_limit_bytes=_MAX_GIT_DIAGNOSTIC_BYTES,
                error_prefix="repository_lineage.git",
            )
        except BuildError as exc:
            if exc.code.endswith("_no_progress_timeout"):
                code = "repository_lineage.fetch_no_progress_timeout"
            elif exc.code.endswith("_timeout"):
                code = "repository_lineage.fetch_timeout"
            elif exc.code.endswith("_output_stream"):
                code = "repository_lineage.git_output_stream"
            else:
                code = "repository_lineage.git_failed"
            raise GitRepositoryLineageError(
                code,
                "Git could not be executed safely while resolving repository lineage: "
                f"{exc}; policy_identity={self.deadline_policy.identity.uri}; "
                f"provenance={self.deadline_provenance}",
            ) from exc


__all__ = [
    "FilesystemRepositoryLineageStore",
    "GitRepositoryLineageError",
    "GitRepositorySnapshotProvider",
    "RepositoryGitProcessRunner",
    "REPOSITORY_LINEAGE_FILE",
    "REPOSITORY_PARENT_FILE",
    "RepositoryLineageStoreError",
    "repository_parent_reference",
]
