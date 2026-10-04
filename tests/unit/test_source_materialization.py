from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import FileSystemSourceCache
from literate_ai.adapters.source_materialization import (
    SourceMaterializationError,
    capture_accepted_source_cache_archive,
    capture_source_archive,
    discover_accepted_source_provider_binding,
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
    _manifest_identity,
    extract_accepted_source_cache_archive,
    extract_source_archive,
    extract_source_archive_with_manifest,
    source_tree_identity,
    verify_materialized_source,
)
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_cache import _accepted_entry, _cache_key

_EXECUTABLE_CONTENT = b"#!/usr/bin/env python3\n"


def executable_archive(path: Path) -> str:
    entry = {
        "content_identity": "sha256:" + hashlib.sha256(_EXECUTABLE_CONTENT).hexdigest(),
        "executable": True,
        "kind": "file",
        "path": "run",
        "size": len(_EXECUTABLE_CONTENT),
    }
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo("literate-ai")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        executable = tarfile.TarInfo("literate-ai/run")
        executable.mode = 0o755
        executable.size = len(_EXECUTABLE_CONTENT)
        archive.addfile(executable, io.BytesIO(_EXECUTABLE_CONTENT))
    return _manifest_identity([entry])


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

    @unittest.skipUnless(os.name == "posix", "requires POSIX executable mode bits")
    def test_archive_manifest_retains_executable_intent_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            executable = project / "run"
            executable.write_bytes(b"#!/usr/bin/env python3\n")
            executable.chmod(0o755)
            captured = capture_source_archive(project, request(), directory=staging)

            extracted = root / "extracted"
            identity_uri, entries = extract_source_archive_with_manifest(
                captured.path,
                extracted,
                captured.materialization.source_tree_identity.uri,
            )
            materialized = extracted / "literate-ai"

            self.assertEqual(
                verify_materialized_source(
                    materialized, entries, executable_mode_supported=True
                ),
                identity_uri,
            )
            self.assertTrue((materialized / "run").stat().st_mode & 0o111)

    def test_windows_like_mode_round_trip_uses_accepted_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            source_identity = executable_archive(archive)

            extracted = root / "extracted"
            identity_uri, entries = extract_source_archive_with_manifest(
                archive,
                extracted,
                source_identity,
            )
            materialized = extracted / "literate-ai"
            (materialized / "run").chmod(0o644)

            self.assertEqual(
                verify_materialized_source(
                    materialized, entries, executable_mode_supported=False
                ),
                identity_uri,
            )
            with self.assertRaises(SourceGuardError):
                verify_materialized_source(
                    materialized, entries, executable_mode_supported=True
                )
            (materialized / "run").write_bytes(b"changed\n")
            with self.assertRaises(SourceGuardError):
                verify_materialized_source(
                    materialized, entries, executable_mode_supported=False
                )

    def test_gitignored_and_derived_state_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            (project / "secret.txt").write_text("do not stage\n", encoding="utf-8")
            (project / "_build").mkdir()
            (project / "_build/cache").write_text("derived\n", encoding="utf-8")
            import subprocess

            subprocess.run(
                ("git", "init", str(project)), check=True, capture_output=True
            )
            captured = capture_source_archive(project, request(), directory=staging)
            extracted = root / "extracted"
            extract_source_archive(
                captured.path,
                extracted,
                captured.materialization.source_tree_identity.uri,
            )
            tree = extracted / "literate-ai"
            self.assertTrue((tree / "component.md").is_file())
            self.assertFalse((tree / "secret.txt").exists())
            self.assertFalse((tree / "_build").exists())

    def test_existing_staging_archive_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            (staging / "source.tar.gz").write_bytes(b"occupied")
            with self.assertRaises(SourceMaterializationError) as raised:
                capture_source_archive(project, request(), directory=staging)
            self.assertEqual(raised.exception.code, "execution.source_staging_invalid")

    def test_accepted_source_only_request_stages_the_cache_and_restores_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            entry_dir = cache_root / "entries" / "sha256" / "ab"
            entry_dir.mkdir(parents=True)
            (entry_dir / "cd.json").write_text("{}", encoding="utf-8")
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            accepted_request = dataclasses.replace(request(), accepted_source_only=True)
            with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                captured = capture_source_archive(
                    project, accepted_request, directory=staging
                )

            self.assertIsNotNone(captured.accepted_source_cache_path)
            self.assertTrue(captured.accepted_source_cache_path.is_file())
            reference = captured.materialization.accepted_source_cache_reference
            self.assertIsNotNone(reference)
            self.assertEqual(reference.kind, "accepted-source-cache-archive")
            self.assertEqual(
                reference.identity.digest,
                hashlib.sha256(
                    captured.accepted_source_cache_path.read_bytes()
                ).hexdigest(),
            )

            restored = root / "restored-cache"
            extract_accepted_source_cache_archive(
                captured.accepted_source_cache_path, restored
            )
            self.assertEqual(
                (restored / "entries" / "sha256" / "ab" / "cd.json").read_text(
                    encoding="utf-8"
                ),
                "{}",
            )
            self.assertEqual(
                (restored / "format.json").read_text(encoding="utf-8"), "{}"
            )
            source = root / "restored-source"
            extract_source_archive(
                captured.path,
                source,
                captured.materialization.source_tree_identity.uri,
            )
            self.assertTrue((source / "literate-ai" / "component.md").is_file())
            self.assertFalse((source / "literate-ai" / "generated").exists())

    def test_source_capture_excludes_only_explicit_cache_custody(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            authored_generated = project / "generated" / "authored.txt"
            authored_generated.parent.mkdir()
            authored_generated.write_text("authored\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "BUILD_DIR": str(root / "external build"),
                    "OBJ_DIR": str(root / "external objects"),
                },
            ):
                captured = capture_source_archive(project, request(), directory=staging)

            extracted = root / "extracted"
            extract_source_archive(
                captured.path,
                extracted,
                captured.materialization.source_tree_identity.uri,
            )
            self.assertEqual(
                (extracted / "literate-ai" / "generated" / "authored.txt").read_text(
                    encoding="utf-8"
                ),
                "authored\n",
            )

    def test_request_without_accepted_source_only_stages_no_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            cache_root = project / "generated" / "accepted-source-cache"
            cache_root.mkdir(parents=True)
            (cache_root / "format.json").write_text("{}", encoding="utf-8")

            captured = capture_source_archive(project, request(), directory=staging)

            self.assertIsNone(captured.accepted_source_cache_path)
            self.assertIsNone(captured.materialization.accepted_source_cache_reference)

    def test_missing_cache_root_stages_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            reference = capture_accepted_source_cache_archive(
                project, directory=staging
            )
            self.assertIsNone(reference)

    def test_fresh_workspace_discovers_provider_from_copied_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            caller_cas = FileSystemCAS(root / "caller-cas")
            entry = _accepted_entry(caller_cas)
            admitted = workspace_a / "build" / "accepted-source-cache"
            FileSystemSourceCache("admitted", admitted).publish(
                entry, caller_cas=caller_cas
            )
            transported = workspace_b / "short-build" / "accepted-source-cache"
            shutil.copytree(admitted, transported)

            with patch.dict(
                os.environ, {"BUILD_DIR": str(workspace_b / "short-build")}
            ):
                binding = discover_accepted_source_provider_binding(workspace_b)

            self.assertEqual(
                binding,
                (
                    entry.derivation.cache_key.model_binding.provider_id,
                    entry.derivation.cache_key.coding_cli_tool_binding_identity,
                ),
            )

    def test_fresh_workspace_discovers_standard_admission_provider(self) -> None:
        from tests.support.fixtures_test_standard_source_cache_roundtrip import (
            _source_admission_entry,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            caller_cas = FileSystemCAS(root / "caller-cas")
            entry = _source_admission_entry(caller_cas)
            cache = FileSystemSourceCache(
                "admitted", workspace_a / "generated" / "accepted-source-cache"
            )
            cache.publish(entry, caller_cas=caller_cas)
            shutil.copytree(
                cache.root,
                workspace_b / "generated" / "accepted-source-cache",
            )

            # Proves discovery needs no BUILD_DIR/OBJ_DIR-style hint -- clear
            # every such override, but keep HOME/USERPROFILE/PATH so
            # unrelated stdlib home-directory resolution on Windows still
            # works; this is not what the test is exercising.
            preserved = {
                key: os.environ[key]
                for key in ("HOME", "USERPROFILE", "PATH")
                if key in os.environ
            }
            with patch.dict(os.environ, preserved, clear=True):
                binding = discover_accepted_source_provider_binding(workspace_b)

            self.assertEqual(
                binding,
                (
                    entry.cache_key.model_binding.provider_id,
                    entry.cache_key.coding_cli_tool_binding_identity,
                ),
            )

    def test_fresh_workspace_discovers_one_provider_across_56_memberships(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            caller_cas = FileSystemCAS(root / "caller-cas")
            admitted = workspace_a / "build" / "accepted-source-cache"
            cache = FileSystemSourceCache("admitted", admitted)
            entries = tuple(
                _accepted_entry(caller_cas, suffix=f"macos-{index:02d}")
                for index in range(56)
            )
            for entry in entries:
                cache.publish(entry, caller_cas=caller_cas)
            transported = workspace_b / "short-build" / "accepted-source-cache"
            shutil.copytree(admitted, transported)

            with patch.dict(
                os.environ, {"BUILD_DIR": str(workspace_b / "short-build")}
            ):
                binding = discover_accepted_source_provider_binding(workspace_b)

            self.assertEqual(len(cache.verified_published_entries()), 56)
            self.assertEqual(
                binding,
                (
                    entries[0].derivation.cache_key.model_binding.provider_id,
                    entries[0].derivation.cache_key.coding_cli_tool_binding_identity,
                ),
            )

    def test_provider_discovery_rejects_conflicting_published_memberships(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            caller_cas = FileSystemCAS(root / "caller-cas")
            cache = FileSystemSourceCache(
                "admitted", project / "generated" / "accepted-source-cache"
            )
            first = _accepted_entry(caller_cas, suffix="provider-a")
            conflicting_key = dataclasses.replace(
                _cache_key(),
                model_binding=dataclasses.replace(
                    _cache_key().model_binding, provider_id="other-provider"
                ),
            )
            second = _accepted_entry(
                caller_cas, suffix="provider-b", key=conflicting_key
            )
            cache.publish(first, caller_cas=caller_cas)
            cache.publish(second, caller_cas=caller_cas)

            with (
                patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}),
                self.assertRaises(SourceMaterializationError) as caught,
            ):
                discover_accepted_source_provider_binding(project)

            self.assertEqual(
                caught.exception.code,
                "execution.accepted_source_provider_ambiguous",
            )

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
