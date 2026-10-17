"""Execute the production source-index phase from exact worker CAS custody."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.intelligence.standard import DisabledGenerationIndexer
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components import GeneratedSourceCandidate
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.source_index import generated_source_tree_identity
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobNotFoundError

SOURCE_GENERATION_RESULT_SCHEMA = "literate-ai/source-generation-action-result@1"
MAX_SOURCE_FILES = 100_000
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _invalid() -> None:
    raise ActionWireError(
        "action_source.invalid", "source-index action has invalid source custody"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load(content: bytes):
    if not isinstance(content, bytes) or len(content) > MAX_ACTION_RECORD_BYTES:
        _invalid()
    try:
        return json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_source.invalid", "source-index record is invalid"
        ) from exc


def _validate_files(files: tuple[CachedSourceFile, ...]) -> ContentIdentity:
    if (
        not files
        or len(files) > MAX_SOURCE_FILES
        or any(not isinstance(item, CachedSourceFile) for item in files)
        or sum(item.blob.size for item in files) > MAX_SOURCE_BYTES
    ):
        _invalid()
    paths = tuple(item.path for item in files)
    if paths != tuple(sorted(set(paths))):
        _invalid()
    # Reuse the source contract's complete portable-path and reserved-metadata
    # validation without loading source bytes into the control-plane message.
    generated_source_tree_identity(dict.fromkeys(paths, b""))
    return canonical_identity(
        [
            {"path": item.path, "size": item.blob.size, "digest": item.blob.identity}
            for item in files
        ]
    )


def source_generation_result(
    execution_plan_identity: ContentIdentity,
    candidate: GeneratedSourceCandidate,
    files: tuple[CachedSourceFile, ...],
) -> bytes:
    """Retain the generated predecessor and its transitive source blobs."""
    if not isinstance(execution_plan_identity, ContentIdentity) or not isinstance(
        candidate, GeneratedSourceCandidate
    ):
        _invalid()
    if _validate_files(files) != candidate.tree_identity:
        _invalid()
    content = canonical_json_bytes(
        {
            "schema": SOURCE_GENERATION_RESULT_SCHEMA,
            "execution_plan_identity": execution_plan_identity.uri,
            "candidate": candidate.to_dict(),
            "files": [item.to_dict() for item in files],
        }
    )
    if len(content) > MAX_ACTION_RECORD_BYTES:
        _invalid()
    return content


def hydrate_action_source(
    files: tuple[CachedSourceFile, ...],
    expected_tree: ContentIdentity,
    deadline: ActionDispatchDeadline,
    *,
    cas: FileSystemCAS,
    blob_source: Callable[[BlobRef], bytes] | None = None,
    require_current: Callable[[], None] = lambda: None,
) -> None:
    """Verify or fetch a bounded source manifest without allocating a workspace."""
    if not isinstance(files, tuple) or _validate_files(files) != expected_tree:
        _invalid()
    deadline.remaining()
    for item in files:
        require_current()
        deadline.remaining()
        try:
            cas.verify(item.blob)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            content = blob_source(item.blob)
            require_current()
            deadline.remaining()
            if (
                not isinstance(content, bytes)
                or len(content) != item.blob.size
                or record_identity(content).uri != item.blob.identity
            ):
                _invalid()
            cas.put_bytes(content, media_type=item.blob.media_type)
    require_current()
    deadline.remaining()


@contextmanager
def materialize_action_source(
    files: tuple[CachedSourceFile, ...],
    expected_tree: ContentIdentity,
    deadline: ActionDispatchDeadline,
    *,
    cas: FileSystemCAS,
    workspace_root: Path,
    blob_source: Callable[[BlobRef], bytes] | None = None,
    require_current: Callable[[], None] = lambda: None,
) -> Iterator[Path]:
    """Materialize exact bounded source bytes with disposable filesystem custody."""
    if not callable(require_current):
        raise TypeError("source materialization requires a callable authority guard")
    require_current()
    require_safe_directory(workspace_root)
    hydrate_action_source(
        files,
        expected_tree,
        deadline,
        cas=cas,
        blob_source=blob_source,
        require_current=require_current,
    )
    with tempfile.TemporaryDirectory(prefix="source-", dir=workspace_root) as scratch:
        root = Path(scratch)
        for item in files:
            require_current()
            deadline.remaining()
            target = root.joinpath(*item.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            cas.copy_to(item.blob, target)
        require_current()
        deadline.remaining()
        yield root
        require_current()
        deadline.remaining()


def execute_source_index_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
    cas: FileSystemCAS,
    workspace_root: Path,
    blob_source: Callable[[BlobRef], bytes] | None = None,
) -> bytes:
    """Verify, materialize, and index; no build or acceptance is authorized here."""
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_source.worker_mismatch", "receiver has another worker identity"
        )
    if request.action.kind is not LifecycleActionKind.INDEX:
        raise ActionWireError(
            "action_source.unsupported_phase", "receiver does not support this phase"
        )
    if request.deadline_identity != deadline.identity:
        _invalid()
    deadline.remaining()
    if len(request.predecessor_result_identities) != 1:
        _invalid()
    expected = {
        request.action.payload_identity,
        *request.predecessor_result_identities,
    }
    if set(records) != expected or any(
        not isinstance(content, bytes)
        or len(content) > MAX_ACTION_RECORD_BYTES
        or record_identity(content) != identity
        for identity, content in records.items()
    ):
        _invalid()
    try:
        payload = _load(records[request.action.payload_identity])
        previous = _load(records[request.predecessor_result_identities[0]])
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "schema",
                "execution_plan_identity",
                "generation_plan_identity",
                "component_revision",
                "kind",
            }
            or payload["schema"] != "literate-ai/lifecycle-action-payload@1"
            or payload["kind"] != LifecycleActionKind.INDEX.value
            or not isinstance(previous, dict)
            or set(previous)
            != {"schema", "execution_plan_identity", "candidate", "files"}
            or previous["schema"] != SOURCE_GENERATION_RESULT_SCHEMA
            or not isinstance(previous["files"], list)
            or len(previous["files"]) > MAX_SOURCE_FILES
        ):
            _invalid()
        ContentIdentity.parse_uri(payload["execution_plan_identity"])
        candidate = GeneratedSourceCandidate.from_dict(previous["candidate"])
        files = tuple(CachedSourceFile.from_dict(item) for item in previous["files"])
        if (
            previous["execution_plan_identity"] != payload["execution_plan_identity"]
            or candidate.component_generation_plan_identity.uri
            != payload["generation_plan_identity"]
            or candidate.component_revision.uri != payload["component_revision"]
            or candidate.component_revision != request.action.component_revision
            or _validate_files(files) != candidate.tree_identity
        ):
            _invalid()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ActionWireError(
            "action_source.invalid", "source-index candidate or manifest is invalid"
        ) from exc
    with materialize_action_source(
        files,
        candidate.tree_identity,
        deadline,
        cas=cas,
        workspace_root=workspace_root,
        blob_source=blob_source,
    ) as root:
        registry = LocalSourceTreeRegistry()
        registry.register(candidate, root)
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
        indexer = DisabledGenerationIndexer(registry)
        indexer.retain_evidence_with(recorder)
        identity = indexer.index(candidate.component_revision, candidate.tree_identity)
        deadline.remaining()
        return dict(recorder.entries)[identity]
