"""Read raw bounded objects through the publication store's constrained runner."""

from __future__ import annotations

from literate_ai.contracts.repository_refresh import RepositoryRefreshTarget
from literate_ai.contracts.repository_tree import (
    RepositoryTreeCapturePolicy,
    RepositoryTreeEntry,
    RepositoryTreeSnapshot,
)

from .repository_orchestration import OrchestrationInventoryError


def capture_repository_tree(
    run, commit: str, policy: RepositoryTreeCapturePolicy
) -> RepositoryTreeSnapshot:
    try:
        commit_content = run(
            "cat-file",
            "commit",
            commit,
            stdout_limit_bytes=policy.maximum_metadata_bytes,
        ).stdout
        raw = run(
            "ls-tree",
            "-r",
            "-t",
            "-l",
            "-z",
            "--full-tree",
            commit,
            stdout_limit_bytes=policy.maximum_metadata_bytes,
        ).stdout
        if raw and not raw.endswith(b"\0"):
            raise ValueError("incomplete tree listing")
        if (
            len(raw) > policy.maximum_metadata_bytes
            or len(commit_content) > policy.maximum_metadata_bytes
        ):
            raise ValueError("tree metadata exceeds its bound")
        lines = raw.split(b"\0")[:-1]
        if len(lines) > policy.maximum_entries:
            raise ValueError("tree entry count exceeds its bound")
        entries = []
        blobs = {}
        total = 0
        for line in lines:
            metadata, path = line.split(b"\t", 1)
            mode, kind, oid, size = metadata.decode("ascii").split()
            RepositoryRefreshTarget("tree-object", oid)
            if len(oid) != len(commit):
                raise ValueError("tree object formats disagree")
            expected_kind = {
                "040000": "tree",
                "160000": "commit",
                "100644": "blob",
                "100755": "blob",
                "120000": "blob",
            }.get(mode)
            if kind != expected_kind:
                raise ValueError("tree entry type disagrees with its mode")
            content = None
            if kind == "blob":
                if not size.isascii() or not size.isdecimal():
                    raise ValueError("blob size is invalid")
                length = int(size)
                total += length
                if (
                    length > policy.maximum_blob_bytes
                    or total > policy.maximum_total_blob_bytes
                ):
                    raise ValueError("tree blob bytes exceed their bounds")
                if oid not in blobs:
                    blobs[oid] = run(
                        "cat-file",
                        "blob",
                        oid,
                        stdout_limit_bytes=policy.maximum_blob_bytes,
                    ).stdout
                content = blobs[oid]
                if len(content) != length:
                    raise ValueError("blob bytes disagree with listed size")
            elif size != "-":
                raise ValueError("non-blob entries must not declare byte content")
            entries.append(
                RepositoryTreeEntry(path.decode("utf-8"), mode, oid, content)
            )
        return RepositoryTreeSnapshot(
            commit, commit_content, tuple(sorted(entries, key=lambda entry: entry.path))
        )
    except OrchestrationInventoryError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise OrchestrationInventoryError(
            "publication_tree_invalid",
            "published tree is unsafe, incomplete or exceeds capture bounds",
        ) from None
