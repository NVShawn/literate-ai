"""Portable, bounded custody of explicitly reviewed retained generation input."""

import json
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.source_index import generated_source_tree_identity


@dataclass(frozen=True)
class RetainedGenerationInput:
    """Transport evidence, not permission for model egress or lifecycle acceptance."""

    metadata: bytes
    authorization: ContentIdentity
    files: tuple[CachedSourceFile, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/retained-generation-input@1",
                "metadata": json.loads(self.metadata),
                "authorization": self.authorization.uri,
                "files": [item.to_dict() for item in self.files],
            }
        )

    @classmethod
    def capture(cls, retained, authorization, cas, deadline):
        if not isinstance(retained, RetainedSourceInput):
            raise TypeError("retained input must be typed")
        deadline.remaining()
        retained.require_authorization(authorization)
        files = []
        for name, content in retained.files:
            deadline.remaining()
            files.append(CachedSourceFile(name, cas.put_bytes(content)))
        retained.require_authorization(authorization)
        value = cls(
            canonical_json_bytes(retained.to_dict()), retained.identity, tuple(files)
        )
        content = value.to_bytes()
        return cls.admit(content, record_identity(content), deadline)

    @classmethod
    def admit(cls, content, identity, deadline):
        deadline.remaining()
        try:
            if (
                not isinstance(content, bytes)
                or len(content) > 2 * 1024 * 1024
                or record_identity(content) != identity
            ):
                raise ValueError("invalid envelope")
            value = json.loads(content)
            if (
                not isinstance(value, dict)
                or canonical_json_bytes(value) != content
                or set(value) != {"schema", "metadata", "authorization", "files"}
                or value["schema"] != "literate-ai/retained-generation-input@1"
            ):
                raise ValueError("invalid fields")
            metadata = value["metadata"]
            if (
                not isinstance(metadata, dict)
                or set(metadata)
                != {
                    "schema",
                    "origin",
                    "tree_identity",
                    "component_lock_identity",
                    "project_authority_identity",
                    "target",
                }
                or metadata["schema"] != "literate-ai/retained-source-input@1"
                or metadata["origin"] != "operator-retained-source"
                or not isinstance(metadata["target"], str)
                or not metadata["target"]
            ):
                raise ValueError("invalid metadata")
            for key in (
                "tree_identity",
                "component_lock_identity",
                "project_authority_identity",
            ):
                ContentIdentity.from_dict(metadata[key])
            authorization = ContentIdentity.parse_uri(value["authorization"])
            if (
                authorization != canonical_identity(metadata)
                or not isinstance(value["files"], list)
                or not 1 <= len(value["files"]) <= 1024
            ):
                raise ValueError("invalid authorization or files")
            files = tuple(CachedSourceFile.from_dict(item) for item in value["files"])
            paths = tuple(item.path for item in files)
            canonical_relative_posix_paths(paths, label="retained generation")
            if (
                paths != tuple(sorted(set(paths)))
                or any(
                    not name.startswith("source/")
                    or ".codegraph" in name.split("/")
                    or len(name.encode("utf-8")) > 512
                    or len(name.split("/")) > 32
                    for name in paths
                )
                or any(item.blob.size > 8 * 1024 * 1024 for item in files)
                or sum(item.blob.size for item in files) > 16 * 1024 * 1024
            ):
                raise ValueError("invalid source bounds")
            manifest = [
                {
                    "path": item.path,
                    "size": item.blob.size,
                    "digest": item.blob.identity,
                }
                for item in files
            ]
            if canonical_identity(manifest) != ContentIdentity.from_dict(
                metadata["tree_identity"]
            ):
                raise ValueError("source manifest differs from reviewed tree")
            result = cls(canonical_json_bytes(metadata), authorization, files)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ActionWireError(
                "action_generate.retained_invalid", "retained source transfer refused"
            ) from exc
        deadline.remaining()
        return result

    def read_files(self, read_blob, deadline):
        """Verify bounded bytes before allocation or private runtime construction."""
        content = self.to_bytes()
        self.admit(content, record_identity(content), deadline)
        files = {}
        for item in self.files:
            deadline.remaining()
            payload = read_blob(item.blob)
            deadline.remaining()
            if (
                not isinstance(payload, bytes)
                or len(payload) != item.blob.size
                or record_identity(payload).uri != item.blob.identity
            ):
                raise ActionWireError(
                    "action_generate.retained_invalid", "retained file bytes differ"
                )
            try:
                payload.decode("utf-8")
            except UnicodeError as exc:
                raise ActionWireError(
                    "action_generate.retained_invalid", "retained input is not UTF-8"
                ) from exc
            files[item.path] = payload
        expected = ContentIdentity.from_dict(json.loads(self.metadata)["tree_identity"])
        if generated_source_tree_identity(files) != expected.uri:
            raise ActionWireError(
                "action_generate.retained_invalid", "retained tree differs"
            )
        return tuple(files.items())


def transfer_retained_input(value, *, cas, deadline, admission_guard, blob_source=None):
    """Admit the complete tree before retaining fetched bytes in worker custody."""
    from literate_ai.storage.cas import BlobNotFoundError

    if not isinstance(value, RetainedGenerationInput) or not callable(admission_guard):
        raise TypeError("retained transfer requires typed input and live admission")
    if blob_source is not None and not callable(blob_source):
        raise TypeError("retained transport must be callable")
    pending = {}

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    def read(reference):
        current()
        try:
            content = cas.get_bytes(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            content = blob_source(reference)
            pending[reference] = content
        current()
        return content

    current()
    files = value.read_files(read, deadline)
    for reference, content in pending.items():
        current()
        if cas.put_bytes(content, media_type=reference.media_type) != reference:
            raise ActionWireError(
                "action_generate.retained_invalid", "retained transport changed custody"
            )
        current()
    current()
    return files


@contextmanager
def materialize_retained_input(value, *, cas, root, deadline, admission_guard):
    """Own a private input tree for exactly the duration of worker generation."""
    from literate_ai._filesystem import require_safe_directory
    from literate_ai.adapters.action_build_result import _remove_owned_stage
    from literate_ai.adapters.exclusive_directory import directory_node

    files = transfer_retained_input(
        value,
        cas=cas,
        deadline=deadline,
        admission_guard=admission_guard,
    )
    root = Path(root)
    require_safe_directory(root)
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise ActionWireError(
            "action_generate.workspace_invalid", "retained root must be canonical"
        )
    parent = directory_node(root)
    stage = Path(tempfile.mkdtemp(prefix="retained-", dir=root))
    owned = directory_node(stage)

    def current():
        deadline.remaining()
        admission_guard()
        if directory_node(root) != parent or directory_node(stage) != owned:
            raise ActionWireError(
                "action_generate.workspace_changed", "retained workspace changed"
            )
        deadline.remaining()

    try:
        for name, content in files:
            current()
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            require_safe_directory(destination.parent)
            with destination.open("xb") as stream:
                stream.write(content)
            current()
        metadata = json.loads(value.metadata)
        retained = RetainedSourceInput.capture(
            stage / "source",
            component_lock_identity=ContentIdentity.from_dict(
                metadata["component_lock_identity"]
            ),
            project_authority_identity=ContentIdentity.from_dict(
                metadata["project_authority_identity"]
            ),
            target=metadata["target"],
        )
        retained.require_authorization(value.authorization.uri)
        current()
        yield retained
        current()
        retained.require_authorization(value.authorization.uri)
    finally:
        try:
            same_parent = directory_node(root) == parent
        except (OSError, ValueError):
            same_parent = False
        if same_parent:
            _remove_owned_stage(stage, owned)
