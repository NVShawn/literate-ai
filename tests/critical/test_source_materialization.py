from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.source_materialization import (
    capture_source_archive,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionDispatchRequest,
    ExecutionRequirements,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
)
from literate_ai.remote_source_guard import (
    SourceGuardError,
    extract_accepted_source_cache_archive,
    extract_source_archive,
    source_tree_identity,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def request() -> ExecutionDispatchRequest:
    worker = ExecutionWorker(
        "ssh", ExecutionWorkerKind.SSH, endpoint="u@h", workspace="~/w"
    )
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.BUILD,
        "component://example/app",
        "components/app",
        "host",
        (),
        worker.identity,
        ExecutionRequirements(),
        (),
        (),
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        None,
        30,
    )


class SourceMaterializationTests(unittest.TestCase):
    def test_archive_binds_request_transport_and_verified_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            captured = capture_source_archive(project, request(), directory=staging)

            materialization = captured.materialization
            self.assertEqual(
                materialization.kind, ExecutionSourceMaterializationKind.ARCHIVE
            )
            self.assertEqual(materialization.request_identity, request().identity)
            self.assertEqual(
                materialization.archive_reference.identity.digest,
                hashlib.sha256(captured.path.read_bytes()).hexdigest(),
            )
            extracted = root / "extracted"
            extract_source_archive(
                captured.path, extracted, materialization.source_tree_identity.uri
            )
            self.assertEqual(
                source_tree_identity(extracted / "literate-ai"),
                materialization.source_tree_identity.uri,
            )

    def test_declared_cache_transport_preserves_read_policy_and_refuses_corruption(
        self,
    ):
        from literate_ai.adapters.cache import FileSystemSourceCache
        from literate_ai.adapters.source_materialization import (
            SourceMaterializationError,
        )
        from literate_ai.contracts import (
            ProjectDefinition,
            SourceCacheConfiguration,
            SourceCacheMode,
            SourceCacheRootKind,
            SourceCacheTarget,
        )
        from literate_ai.projects import ProjectConfigurationStore
        from literate_ai.storage import FileSystemCAS
        from tests.support.fixtures_test_source_cache import (
            _accepted_entry,
            _source_intelligence_policy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            target = SourceCacheTarget(
                "committed", SourceCacheRootKind.PROJECT_RELATIVE, "cache"
            )
            configuration = SourceCacheConfiguration(
                mode=SourceCacheMode.READ_WRITE,
                targets=(target,),
                write_target_id="committed",
                require_unique=True,
            )
            definition = ProjectDefinition(
                project_id="transport",
                version="1.0.0",
                profile="canonical",
                agent_skill="SKILL.md",
                component_roots=("components",),
                flavor_roots=("flavors",),
                skill_roots=("skills",),
                workflow_roots=("workflows",),
                routing_roots=(),
                documentation_roots=("docs",),
                source_intelligence=_source_intelligence_policy(),
                source_cache=configuration,
            )
            store = ProjectConfigurationStore(project)
            snapshot = store.create(definition)
            accepted = replace(request(), accepted_source_only=True)
            environment = {
                "BUILD_DIR": str(root / "build"),
                "OBJ_DIR": str(root / "objects"),
            }

            def capture(name):
                staging = root / name
                staging.mkdir()
                with patch.dict(os.environ, environment):
                    return capture_source_archive(project, accepted, directory=staging)

            capture("absent")
            self.assertFalse((project / "cache").exists())
            cas = FileSystemCAS(root / "caller-cas")
            entry = _accepted_entry(cas)
            cache = FileSystemSourceCache("committed", project / "cache")
            cache.publish(entry, caller_cas=cas)
            before = {
                p.relative_to(cache.root): p.read_bytes()
                for p in cache.root.rglob("*")
                if p.is_file()
            }
            captured = capture("valid")
            destination = root / "received"
            extract_source_archive(
                captured.path,
                destination,
                captured.materialization.source_tree_identity.uri,
            )
            restored = FileSystemSourceCache(
                "committed", destination / "literate-ai/cache", writable=False
            )
            self.assertEqual(restored.verified_published_entries(), (entry,))
            self.assertEqual(
                before,
                {
                    p.relative_to(cache.root): p.read_bytes()
                    for p in cache.root.rglob("*")
                    if p.is_file()
                },
            )

            snapshot = store.update(
                snapshot,
                replace(
                    definition,
                    source_cache=replace(
                        configuration, mode=SourceCacheMode.WRITE_ONLY
                    ),
                ),
            )
            captured = capture("write-only")
            with tarfile.open(captured.path) as archive:
                self.assertFalse(
                    any(p.name.startswith("literate-ai/cache/") for p in archive)
                )
            store.update(snapshot, definition)
            (cache.root / "format.json").chmod(0o600)
            (cache.root / "format.json").write_bytes(b"invalid")
            with self.assertRaises(SourceMaterializationError):
                capture("corrupt")

    def test_extraction_rejects_path_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("../escape.json")
                info.size = 2
                tar.addfile(info, io.BytesIO(b"{}"))
            with self.assertRaises(SourceGuardError):
                extract_accepted_source_cache_archive(archive, root / "restored")

            symlink_archive = root / "symlink.tar.gz"
            with tarfile.open(symlink_archive, "w:gz") as tar:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            with self.assertRaises(SourceGuardError):
                extract_accepted_source_cache_archive(
                    symlink_archive, root / "restored-symlink"
                )


if __name__ == "__main__":
    unittest.main()
