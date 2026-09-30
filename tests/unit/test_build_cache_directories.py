"""BUILD_DIR/OBJ_DIR ownership, invalidation, and guarded cleanup tests."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import literate_ai.adapters.cache.build_result as build_result_cache
import literate_ai.cache_directories as cache_directory_module
from literate_ai.adapters.cache import BuildResultCacheError, CachedBuildAdapter
from literate_ai.cache_directories import (
    CacheDirectoryError,
    clean_cache_directories,
    ensure_cache_directory,
    resolve_cache_directories,
    select_cache_directories,
)
from literate_ai.contracts import canonical_identity
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile


class _Builder:
    builder_id = "builder:test-cache@1"

    def __init__(self, object_root: Path) -> None:
        self.object_root = object_root
        self.calls = 0
        self.authorization_checks: list[str] = []

    def require_cache_hit_authorized(self, request, authorization) -> None:
        del request
        authorization_id = authorization.get("authorization_id")
        if not isinstance(authorization_id, str) or not authorization_id:
            raise ValueError("current cache-hit authorization is required")
        self.authorization_checks.append(authorization_id)

    def build(self, request, authorization):
        self.calls += 1
        source = request["source_bundle_digest"]
        artifact = self.object_root / "artifacts" / source.removeprefix("sha256:")
        artifact.mkdir(parents=True, exist_ok=True)
        executable = artifact / "sample"
        executable.write_bytes(f"binary:{source}".encode())
        executable.chmod(0o700)
        digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
        return {
            "artifact_digest": digest,
            "artifact_path": str(artifact),
            "source_bundle_digest": source,
            "authorization_id": authorization["authorization_id"],
            "toolchain_identity": request["toolchain_digest"],
            "compiled_files": ["sample"],
        }


class _RecordingVerifier:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def require_build_valid(self, authorization, request, *, now) -> None:
        authorization.require_valid(request, now=now)
        self.checked.append(authorization.authorization_id)


class _TypedAuthorizationBuilder(_Builder):
    require_cache_hit_authorized = None

    def __init__(self, object_root: Path, *, now: datetime) -> None:
        super().__init__(object_root)
        self.authorization_verifier = _RecordingVerifier()
        self.clock = lambda: now


class _BarrierBuilder(_Builder):
    def __init__(self, object_root: Path, barrier: threading.Barrier) -> None:
        super().__init__(object_root)
        self.barrier = barrier

    def build(self, request, authorization):
        result = super().build(request, authorization)
        self.barrier.wait(timeout=10)
        return result


class _DivergentBarrierBuilder(_Builder):
    def __init__(
        self, object_root: Path, barrier: threading.Barrier, label: str
    ) -> None:
        super().__init__(object_root)
        self.barrier = barrier
        self.label = label

    def build(self, request, authorization):
        self.calls += 1
        source = request["source_bundle_digest"]
        artifact = self.object_root / "artifact"
        artifact.mkdir(parents=True, exist_ok=True)
        executable = artifact / "sample"
        executable.write_bytes(f"binary:{self.label}".encode())
        executable.chmod(0o700)
        digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
        self.barrier.wait(timeout=10)
        return {
            "artifact_digest": digest,
            "artifact_path": str(artifact),
            "source_bundle_digest": source,
            "authorization_id": authorization["authorization_id"],
            "toolchain_identity": request["toolchain_digest"],
            "compiled_files": ["sample"],
        }


class _RejectingDivergentBarrierBuilder(_DivergentBarrierBuilder):
    def require_cache_hit_authorized(self, request, authorization) -> None:
        del request, authorization
        raise RuntimeError("current authorization is revoked")


def _ensure_cache_process(
    cache_root: str,
    project_root: str,
    gate: Any,
    results: Any,
) -> None:
    gate.wait()
    try:
        ensured = ensure_cache_directory(
            Path(cache_root),
            kind="object",
            project_root=Path(project_root),
        )
    except Exception as exc:  # pragma: no cover - returned to the parent process
        results.put((False, f"{type(exc).__name__}: {exc}"))
    else:
        results.put((True, str(ensured)))


def _create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


def _request(source: str) -> dict[str, object]:
    return {
        "effective_revision_digest": canonical_identity({"revision": 1}).uri,
        "source_bundle_digest": source,
        "builder_id": _Builder.builder_id,
        "toolchain_digest": canonical_identity({"toolchain": 1}).uri,
        "sandbox_profile": "test",
        "requested_privileges": ["compiler"],
        "allowed_outputs": ["native-executable"],
    }


def _authorization(
    request: dict[str, object],
    authorization_id: str,
    *,
    now: datetime,
    expired: bool = False,
) -> dict[str, object]:
    typed_request = BuildRequest.from_dict(request)
    issued_at = now - timedelta(hours=2) if expired else now - timedelta(minutes=1)
    expires_at = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    return BuildAuthorization(
        authorization_id=authorization_id,
        classification_digest=canonical_identity({"classification": 1}).uri,
        request_digest=canonical_identity(typed_request.to_dict()).uri,
        effective_revision_digest=typed_request.effective_revision_digest,
        actor="cache-test",
        reason="verify current cache-hit authority",
        profile=SecurityProfile.CONSTRAINED,
        privileges=("compiler",),
        issued_at=issued_at,
        expires_at=expires_at,
    ).to_dict()


def _write_project_manifest(
    project: Path,
    *,
    component_roots: tuple[str, ...] = ("catalog/components",),
) -> None:
    template = json.loads(
        (Path(__file__).resolve().parents[2] / "literate.project.json").read_bytes()
    )
    template.update(
        {
            "project_id": "cache-directory-test",
            "component_roots": list(component_roots),
            "flavor_roots": ["catalog/flavors"],
            "skill_roots": ["catalog/skills"],
            "mcp_roots": [],
            "workflow_roots": ["catalog/workflows"],
            "routing_roots": ["catalog/routing"],
            "documentation_roots": ["catalog/docs"],
        }
    )
    (project / "literate.project.json").write_text(
        json.dumps(template),
        encoding="utf-8",
    )


class CacheDirectoryTests(unittest.TestCase):
    def test_defaults_and_relative_overrides_are_project_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            defaults = resolve_cache_directories(project, environment={})
            overridden = resolve_cache_directories(
                project,
                environment={"BUILD_DIR": "derived", "OBJ_DIR": "objects"},
            )

        self.assertEqual(defaults.build_dir, project / "generated")
        self.assertEqual(defaults.obj_dir, project / "_build")
        self.assertEqual(overridden.build_dir, project / "derived")
        self.assertEqual(overridden.obj_dir, project / "objects")

    def test_explicit_roots_win_over_a_disagreeing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            environment = {
                "BUILD_DIR": "from-environment",
                "OBJ_DIR": "obj-environment",
            }
            selected = select_cache_directories(
                project,
                build_dir="from-flag",
                obj_dir="obj-flag",
                environment=environment,
            )

        self.assertEqual(selected.build_dir, project / "from-flag")
        self.assertEqual(selected.obj_dir, project / "obj-flag")

    def test_environment_is_the_fallback_when_a_root_is_not_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            environment = {
                "BUILD_DIR": "from-environment",
                "OBJ_DIR": "obj-environment",
            }
            only_build = select_cache_directories(
                project, build_dir="from-flag", environment=environment
            )
            neither = select_cache_directories(project, environment=environment)

        self.assertEqual(only_build.build_dir, project / "from-flag")
        self.assertEqual(only_build.obj_dir, project / "obj-environment")
        self.assertEqual(neither.build_dir, project / "from-environment")
        self.assertEqual(neither.obj_dir, project / "obj-environment")

    def test_portable_defaults_apply_when_neither_flag_nor_environment_is_set(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            selected = select_cache_directories(project, environment={})

        self.assertEqual(selected.build_dir, project / "generated")
        self.assertEqual(selected.obj_dir, project / "_build")

    def test_explicit_roots_are_still_subject_to_the_authority_overlap_rules(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with self.assertRaisesRegex(CacheDirectoryError, "protected authority"):
                select_cache_directories(
                    project,
                    build_dir="components",
                    obj_dir="objects",
                    environment={},
                )

    def test_explicit_roots_must_remain_distinct_and_unnested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with self.assertRaisesRegex(CacheDirectoryError, "different directories"):
                select_cache_directories(
                    project, build_dir="shared", obj_dir="shared", environment={}
                )
            with self.assertRaisesRegex(CacheDirectoryError, "nested beneath"):
                select_cache_directories(
                    project,
                    build_dir="objects/derived",
                    obj_dir="objects",
                    environment={},
                )

    def test_selected_custody_identity_distinguishes_different_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            warm = select_cache_directories(
                project, build_dir="warm", obj_dir="warm-objects", environment={}
            )
            cold = select_cache_directories(
                project, build_dir="cold", obj_dir="cold-objects", environment={}
            )
            same = select_cache_directories(
                project, build_dir="warm", obj_dir="warm-objects", environment={}
            )

        self.assertNotEqual(warm.identity.uri, cold.identity.uri)
        self.assertEqual(warm.identity.uri, same.identity.uri)

    def test_cache_roots_cannot_overlap_baseline_authority_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            cases = (
                "components",
                "components/cache",
                "specs/generated",
                "src/objects",
                "docs/cache",
                "workflows/output",
            )
            for configured in cases:
                with self.subTest(configured=configured):
                    with self.assertRaisesRegex(
                        CacheDirectoryError, "protected authority"
                    ):
                        resolve_cache_directories(
                            project,
                            environment={
                                "BUILD_DIR": configured,
                                "OBJ_DIR": "objects",
                            },
                        )

    def test_cache_roots_cannot_overlap_manifest_declared_authority_roots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            _write_project_manifest(project)
            cases = (
                "catalog/components",
                "catalog/components/cache",
                "catalog/flavors/objects",
                "catalog",
            )
            for configured in cases:
                with self.subTest(configured=configured):
                    with self.assertRaisesRegex(
                        CacheDirectoryError, "protected authority"
                    ):
                        resolve_cache_directories(
                            project,
                            environment={
                                "BUILD_DIR": configured,
                                "OBJ_DIR": "objects",
                            },
                        )

            directories = resolve_cache_directories(
                project,
                environment={"BUILD_DIR": "derived", "OBJ_DIR": "objects"},
            )
            self.assertEqual(directories.build_dir, project / "derived")
            self.assertEqual(directories.obj_dir, project / "objects")

    def test_malformed_manifest_does_not_leak_partial_authority_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            (project / "literate.project.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "version": 7,
                        "component_roots": ["catalog/components"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CacheDirectoryError, "manifest is invalid"):
                resolve_cache_directories(
                    project,
                    environment={
                        "BUILD_DIR": "catalog/components",
                        "OBJ_DIR": "objects",
                    },
                )

    def test_omitted_optional_mcp_roots_are_not_required_on_the_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            _write_project_manifest(project)
            directories = resolve_cache_directories(
                project,
                environment={"BUILD_DIR": "derived", "OBJ_DIR": "objects"},
            )
            self.assertEqual(directories.build_dir, project / "derived")
            _write_project_manifest(project)
            manifest = json.loads(
                (project / "literate.project.json").read_text(encoding="utf-8")
            )
            manifest["mcp_roots"] = ["catalog/mcps"]
            (project / "literate.project.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(CacheDirectoryError, "protected authority"):
                resolve_cache_directories(
                    project,
                    environment={
                        "BUILD_DIR": "catalog/mcps",
                        "OBJ_DIR": "objects",
                    },
                )

    def test_clean_removes_only_marked_object_root_and_really_clean_removes_both(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            directories = resolve_cache_directories(project, environment={})
            ensure_cache_directory(
                directories.build_dir,
                kind="generated-source",
                project_root=project,
            )
            ensure_cache_directory(
                directories.obj_dir,
                kind="object",
                project_root=project,
            )
            (directories.build_dir / "source.txt").write_text("source")
            (directories.obj_dir / "sample").write_text("binary")

            removed = clean_cache_directories(project, environment={})

            self.assertEqual(removed, (directories.obj_dir,))
            self.assertTrue(directories.build_dir.is_dir())
            self.assertFalse(directories.obj_dir.exists())
            removed = clean_cache_directories(
                project,
                environment={},
                remove_generated_sources=True,
            )
            self.assertEqual(removed, (directories.build_dir,))
            self.assertFalse(directories.build_dir.exists())

    def test_clean_refuses_an_unmarked_preexisting_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "objects"
            object_root.mkdir(parents=True)
            (object_root / "do-not-delete").write_text("operator data")
            with self.assertRaises(CacheDirectoryError):
                clean_cache_directories(
                    project,
                    environment={"BUILD_DIR": "generated", "OBJ_DIR": "objects"},
                )
            self.assertTrue((object_root / "do-not-delete").is_file())

    def test_clean_preserves_declared_session_tool_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            ensure_cache_directory(
                object_root,
                kind="object",
                project_root=project,
            )
            python_environments = object_root / "python-envs"
            interpreter = python_environments / "session-b" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"session-b")
            transient = object_root / "artifacts" / "sample"
            transient.parent.mkdir()
            transient.write_bytes(b"artifact")

            removed = clean_cache_directories(
                project,
                environment={},
                preserve_object_subdirectories=(Path("python-envs"),),
            )

            self.assertEqual(removed, (object_root / "artifacts",))
            self.assertEqual(interpreter.read_bytes(), b"session-b")
            self.assertTrue((object_root / ".litai-cache-root.json").is_file())
            self.assertFalse(transient.exists())

    def test_clean_rejects_nested_preservation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with self.assertRaisesRegex(
                CacheDirectoryError, "must be direct subdirectories"
            ):
                clean_cache_directories(
                    project,
                    environment={},
                    preserve_object_subdirectories=(Path("python-envs/session"),),
                )

    def test_clean_removes_unmarked_default_build_products(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            build_root = project / "_build"
            nested = build_root / "setuptools" / "lib"
            nested.mkdir(parents=True)
            (nested / "stale.py").write_text("stale\n", encoding="utf-8")

            removed = clean_cache_directories(project, environment={})

            self.assertEqual(removed, (build_root,))
            self.assertFalse(build_root.exists())

    def test_default_build_products_can_be_adopted_as_object_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            build_root = project / "_build"
            nested = build_root / "setuptools"
            nested.mkdir(parents=True)
            (nested / "artifact").write_bytes(b"derived")

            adopted = ensure_cache_directory(
                build_root,
                kind="object",
                project_root=project,
            )

            self.assertEqual(adopted, build_root)
            self.assertTrue((build_root / ".litai-cache-root.json").is_file())
            self.assertEqual((nested / "artifact").read_bytes(), b"derived")

    def test_clean_unlinks_npm_style_links_without_following_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            outside = project / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("operator data", encoding="utf-8")
            build_root = project / "_build"
            binary_directory = build_root / "tools" / "node_modules" / ".bin"
            binary_directory.mkdir(parents=True)
            link = binary_directory / "tool"
            try:
                link.symlink_to(sentinel)
            except (OSError, NotImplementedError):
                self.skipTest("host cannot create symbolic links")

            removed = clean_cache_directories(project, environment={})

            self.assertEqual(removed, (build_root,))
            self.assertFalse(build_root.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "operator data")

    def test_ensure_refuses_to_adopt_nonempty_unmarked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            cache_root = project / "operator-data"
            cache_root.mkdir()
            sentinel = cache_root / "do-not-adopt"
            sentinel.write_text("operator data")

            with self.assertRaisesRegex(CacheDirectoryError, "refusing to adopt"):
                ensure_cache_directory(
                    cache_root,
                    kind="generated-source",
                    project_root=project,
                )

            self.assertEqual(sentinel.read_text(), "operator data")

    def test_clean_requires_the_exact_configured_cache_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            wrong_kind = project / "wrong-kind"
            ensure_cache_directory(
                wrong_kind,
                kind="generated-source",
                project_root=project,
            )
            sentinel = wrong_kind / "do-not-delete"
            sentinel.write_text("operator data")

            with self.assertRaisesRegex(
                CacheDirectoryError, "does not authorize removal"
            ):
                clean_cache_directories(
                    project,
                    environment={
                        "BUILD_DIR": "generated",
                        "OBJ_DIR": "wrong-kind",
                    },
                )

            self.assertEqual(sentinel.read_text(), "operator data")

    def test_clean_rechecks_manifest_authority_overlap_immediately_before_delete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            _write_project_manifest(project)
            environment = {"BUILD_DIR": "derived", "OBJ_DIR": "objects"}
            directories = resolve_cache_directories(project, environment=environment)
            ensure_cache_directory(
                directories.build_dir,
                kind="generated-source",
                project_root=project,
            )
            ensure_cache_directory(
                directories.obj_dir,
                kind="object",
                project_root=project,
            )
            source = directories.build_dir / "source.txt"
            artifact = directories.obj_dir / "artifact"
            source.write_text("source", encoding="utf-8")
            artifact.write_text("artifact", encoding="utf-8")
            real_require_managed = cache_directory_module._require_managed_for_removal
            changed = False

            def declare_cache_as_authority(*args, **kwargs):
                nonlocal changed
                result = real_require_managed(*args, **kwargs)
                if not changed:
                    changed = True
                    _write_project_manifest(
                        project, component_roots=("derived", "objects")
                    )
                return result

            with mock.patch.object(
                cache_directory_module,
                "_require_managed_for_removal",
                side_effect=declare_cache_as_authority,
            ):
                with self.assertRaisesRegex(CacheDirectoryError, "protected authority"):
                    clean_cache_directories(
                        project,
                        environment=environment,
                        remove_generated_sources=True,
                    )

            self.assertTrue(source.is_file())
            self.assertTrue(artifact.is_file())

    def test_concurrent_threads_initialize_one_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            cache_root = project / "_build"
            workers = 32
            barrier = threading.Barrier(workers)

            def initialize() -> Path:
                barrier.wait(timeout=10)
                return ensure_cache_directory(
                    cache_root,
                    kind="object",
                    project_root=project,
                )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                initialized = tuple(
                    executor.map(lambda _: initialize(), range(workers))
                )

            self.assertEqual(set(initialized), {cache_root})
            self.assertTrue((cache_root / ".litai-cache-root.json").is_file())

    def test_concurrent_processes_initialize_one_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            cache_root = project / "_build"
            context = multiprocessing.get_context("spawn")
            gate = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_ensure_cache_process,
                    args=(str(cache_root), str(project), gate, results),
                )
                for _ in range(6)
            ]
            for process in processes:
                process.start()
            gate.set()
            observed = []
            try:
                observed = [results.get(timeout=60) for _ in processes]
            except queue.Empty:
                self.fail("cache initialization process did not report a result")
            finally:
                for process in processes:
                    process.join(timeout=20)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertEqual(observed, [(True, str(cache_root))] * len(processes))
            self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_clean_waits_through_constructor_subtree_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            cache_root = project / "generated"
            subtree_started = threading.Event()
            continue_initialization = threading.Event()
            real_ensure = cache_directory_module.ensure_safe_directory

            def pause_subtree(path, **kwargs):
                result = real_ensure(path, **kwargs)
                if Path(path).parts[-2:] == ("sources", "sha256"):
                    subtree_started.set()
                    if not continue_initialization.wait(timeout=10):
                        raise TimeoutError("constructor/clean race timed out")
                return result

            with mock.patch.object(
                cache_directory_module,
                "ensure_safe_directory",
                side_effect=pause_subtree,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    initialize = executor.submit(
                        ensure_cache_directory,
                        cache_root,
                        kind="generated-source",
                        project_root=project,
                        required_subdirectories=(Path("sources") / "sha256",),
                    )
                    self.assertTrue(subtree_started.wait(timeout=10))
                    clean = executor.submit(
                        clean_cache_directories,
                        project,
                        environment={
                            "BUILD_DIR": "generated",
                            "OBJ_DIR": "objects",
                        },
                        remove_generated_sources=True,
                    )
                    self.assertFalse(clean.done())
                    continue_initialization.set()
                    self.assertEqual(initialize.result(timeout=20), cache_root)
                    removed = clean.result(timeout=20)

            self.assertIn(cache_root, removed)
            self.assertFalse(cache_root.exists())

    def test_symlink_cannot_redirect_lock_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            outside = project / "outside-locks"
            outside.mkdir()
            lock_namespace = project / ".litai-cache-locks"
            try:
                lock_namespace.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create directory symlinks: {error}")
            with self.assertRaisesRegex(CacheDirectoryError, "lock"):
                ensure_cache_directory(
                    project / "generated",
                    kind="generated-source",
                    project_root=project,
                )
            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertFalse((project / "generated").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_cannot_be_adopted_as_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            target = project / "operator-data"
            target.mkdir()
            junction = project / "generated"
            _create_windows_junction(junction, target)
            try:
                with self.assertRaisesRegex(CacheDirectoryError, "reparse point"):
                    ensure_cache_directory(
                        junction,
                        kind="generated-source",
                        project_root=project,
                    )
            finally:
                junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_cannot_redirect_lock_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            outside = project / "outside-locks"
            outside.mkdir()
            lock_namespace = project / ".litai-cache-locks"
            _create_windows_junction(lock_namespace, outside)
            try:
                with self.assertRaisesRegex(CacheDirectoryError, "lock"):
                    ensure_cache_directory(
                        project / "generated",
                        kind="generated-source",
                        project_root=project,
                    )
                self.assertEqual(tuple(outside.iterdir()), ())
                self.assertFalse((project / "generated").exists())
            finally:
                lock_namespace.rmdir()


class BuildResultCacheTests(unittest.TestCase):
    def test_exact_source_and_toolchain_hit_skips_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            authorization = {"authorization_id": "authorization:test"}
            source = canonical_identity({"source": 1}).uri

            first = cached.build(_request(source), authorization)
            second = cached.build(_request(source), authorization)

            self.assertEqual(first, second)
            artifact = Path(first["artifact_path"])
            self.assertEqual(
                cached.target_root.relative_to(object_root),
                Path("t") / cached.target_namespace_identity.digest,
            )
            self.assertEqual(cached.records, cached.target_root / "r")
            self.assertTrue(artifact.is_relative_to(cached.target_root / "a"))
            self.assertNotEqual(artifact, object_root / "artifacts" / source[7:])
            self.assertEqual(delegate.calls, 1)
            self.assertEqual(delegate.authorization_checks, ["authorization:test"])
            self.assertEqual(cached.report()["hits"], 1)
            self.assertEqual(cached.report()["misses"], 1)

    def test_builder_implementation_identity_change_forces_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            first_implementation = canonical_identity({"implementation": 1}).uri
            second_implementation = canonical_identity({"implementation": 2}).uri
            first_cache = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
                builder_implementation_identity=first_implementation,
            )
            authorization = {"authorization_id": "authorization:test"}
            source = canonical_identity({"source": 1}).uri

            first_cache.build(_request(source), authorization)
            first_cache.build(_request(source), authorization)
            changed_cache = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
                builder_implementation_identity=second_implementation,
            )
            changed_cache.build(_request(source), authorization)

            self.assertEqual(delegate.calls, 2)
            self.assertEqual(first_cache.report()["hits"], 1)
            self.assertEqual(changed_cache.report()["misses"], 1)
            self.assertNotEqual(first_cache.target_root, changed_cache.target_root)
            self.assertEqual(
                changed_cache.report()["builder_implementation_identity"],
                second_implementation,
            )

    def test_fresh_authorization_reuses_bytes_without_reusing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            source = canonical_identity({"source": 1}).uri

            first = cached.build(
                _request(source), {"authorization_id": "authorization:first"}
            )
            second = cached.build(
                _request(source), {"authorization_id": "authorization:fresh"}
            )

            self.assertEqual(delegate.calls, 1)
            self.assertEqual(delegate.authorization_checks, ["authorization:fresh"])
            self.assertEqual(first["authorization_id"], "authorization:first")
            self.assertEqual(second["authorization_id"], "authorization:fresh")
            self.assertEqual(
                second["cache_origin_authorization_id"], "authorization:first"
            )

    def test_revision_change_with_identical_source_reuses_build_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            source = canonical_identity({"source": "unchanged"}).uri
            first_request = _request(source)
            second_request = {
                **first_request,
                "effective_revision_digest": canonical_identity({"revision": 2}).uri,
            }

            first = cached.build(
                first_request, {"authorization_id": "authorization:first"}
            )
            second = cached.build(
                second_request, {"authorization_id": "authorization:second"}
            )

            self.assertEqual(delegate.calls, 1)
            self.assertEqual(first["artifact_digest"], second["artifact_digest"])
            self.assertEqual(second["authorization_id"], "authorization:second")
            self.assertEqual(
                second["cache_origin_authorization_id"], "authorization:first"
            )
            self.assertEqual(delegate.authorization_checks, ["authorization:second"])

    def test_cache_hit_rejects_an_expired_current_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            now = datetime(2026, 8, 7, tzinfo=UTC)
            delegate = _TypedAuthorizationBuilder(object_root, now=now)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            source = canonical_identity({"source": 1}).uri
            request = _request(source)
            cached.build(
                request,
                _authorization(request, "authorization:first", now=now),
            )

            with self.assertRaisesRegex(RuntimeError, "authorization_expired"):
                cached.build(
                    request,
                    _authorization(
                        request,
                        "authorization:expired",
                        now=now,
                        expired=True,
                    ),
                )

            reused = cached.build(
                request,
                _authorization(request, "authorization:fresh", now=now),
            )
            self.assertEqual(delegate.calls, 1)
            self.assertEqual(
                delegate.authorization_verifier.checked, ["authorization:fresh"]
            )
            self.assertEqual(reused["authorization_id"], "authorization:fresh")

    def test_target_platforms_have_disjoint_key_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            linux = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"variant": "release"},
                target_platform={
                    "operating_system": "linux",
                    "architecture": "x86_64",
                    "abi": "gnu",
                    "target_triple": "x86_64-unknown-linux-gnu",
                },
            )
            windows = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"variant": "release"},
                target_platform={
                    "operating_system": "windows",
                    "architecture": "x86_64",
                    "abi": "msvc",
                    "target_triple": "x86_64-pc-windows-msvc",
                },
            )
            cross_linux = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"variant": "release"},
                target_platform={
                    "operating_system": "linux",
                    "architecture": "x86_64",
                    "abi": "gnu",
                    "target_triple": "aarch64-unknown-linux-gnu",
                },
            )
            source = canonical_identity({"source": 1}).uri
            authorization = {"authorization_id": "authorization:test"}

            linux.build(_request(source), authorization)
            windows.build(_request(source), authorization)
            cross_linux.build(_request(source), authorization)

            self.assertNotEqual(linux.target_root, windows.target_root)
            self.assertNotEqual(linux.target_root, cross_linux.target_root)
            self.assertEqual(len(tuple(linux.records.glob("*.json"))), 1)
            self.assertEqual(len(tuple(windows.records.glob("*.json"))), 1)
            self.assertEqual(len(tuple(cross_linux.records.glob("*.json"))), 1)
            self.assertEqual(delegate.calls, 3)

    def test_first_build_copies_external_artifact_under_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            external_artifacts = project / "external-artifacts"
            delegate = _Builder(external_artifacts)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            authorization = {"authorization_id": "authorization:test"}
            source = canonical_identity({"source": 1}).uri

            first = cached.build(_request(source), authorization)
            second = cached.build(_request(source), authorization)

            artifact = Path(first["artifact_path"])
            self.assertTrue(artifact.is_relative_to(object_root))
            self.assertTrue(external_artifacts.is_dir())
            self.assertEqual(first, second)
            self.assertEqual(delegate.calls, 1)

    def test_source_change_rebuilds_and_artifact_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            delegate = _Builder(object_root)
            cached = CachedBuildAdapter(
                delegate,
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            authorization = {"authorization_id": "authorization:test"}
            first_source = canonical_identity({"source": 1}).uri
            second_source = canonical_identity({"source": 2}).uri
            first = cached.build(_request(first_source), authorization)
            cached.build(_request(second_source), authorization)
            self.assertEqual(delegate.calls, 2)

            (Path(first["artifact_path"]) / "sample").write_bytes(b"tampered")
            with self.assertRaises(BuildResultCacheError):
                cached.build(_request(first_source), authorization)

    def test_concurrent_builds_publish_one_record_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            barrier = threading.Barrier(2)
            delegates = (
                _BarrierBuilder(project / "external-a", barrier),
                _BarrierBuilder(project / "external-b", barrier),
            )
            adapters = tuple(
                CachedBuildAdapter(
                    delegate,
                    object_root=object_root,
                    project_root=project,
                    namespace_material={"platform": "fixture"},
                )
                for delegate in delegates
            )
            source = canonical_identity({"source": "concurrent"}).uri
            request = _request(source)
            authorization = {"authorization_id": "authorization:test"}

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(adapter.build, request, authorization)
                    for adapter in adapters
                ]
                results = tuple(future.result(timeout=20) for future in futures)

            self.assertEqual(results[0]["artifact_path"], results[1]["artifact_path"])
            self.assertEqual(len(tuple(adapters[0].records.glob("*.json"))), 1)
            artifact_root = adapters[0].target_root / "a"
            self.assertEqual(len(tuple(artifact_root.iterdir())), 1)

    def test_concurrent_divergent_builds_return_the_first_persisted_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            barrier = threading.Barrier(2)
            delegates = tuple(
                _DivergentBarrierBuilder(project / f"external-{label}", barrier, label)
                for label in ("a", "b")
            )
            adapters = tuple(
                CachedBuildAdapter(
                    delegate,
                    object_root=object_root,
                    project_root=project,
                    namespace_material={"platform": "fixture"},
                )
                for delegate in delegates
            )
            request = _request(canonical_identity({"source": "divergent"}).uri)
            authorization = {"authorization_id": "authorization:test"}

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(adapter.build, request, authorization)
                    for adapter in adapters
                )
                results = tuple(future.result(timeout=20) for future in futures)

            self.assertEqual(results[0], results[1])
            self.assertEqual(len(tuple(adapters[0].records.glob("*.json"))), 1)

    def test_convergence_loser_revalidates_current_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            barrier = threading.Barrier(2)
            adapters = tuple(
                CachedBuildAdapter(
                    _RejectingDivergentBarrierBuilder(
                        project / f"external-{label}", barrier, label
                    ),
                    object_root=object_root,
                    project_root=project,
                    namespace_material={"platform": "fixture"},
                )
                for label in ("a", "b")
            )
            request = _request(canonical_identity({"source": "revoked-race"}).uri)
            authorization = {"authorization_id": "authorization:revoked"}

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(adapter.build, request, authorization)
                    for adapter in adapters
                )
                outcomes: list[object] = []
                for future in futures:
                    try:
                        outcomes.append(future.result(timeout=20))
                    except RuntimeError as exc:
                        outcomes.append(exc)

            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertEqual(
                sum(
                    "revoked" in str(item)
                    for item in outcomes
                    if isinstance(item, Exception)
                ),
                1,
            )

    def test_record_publication_parent_symlink_cannot_escape_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            outside = project / "outside-records"
            outside.mkdir()
            adapter = CachedBuildAdapter(
                _Builder(project / "external"),
                object_root=project / "_build",
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            shutil.rmtree(adapter.records)
            try:
                adapter.records.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create directory symlinks: {error}")

            with self.assertRaisesRegex(BuildResultCacheError, "unsafe"):
                adapter.build(
                    _request(canonical_identity({"source": "redirect"}).uri),
                    {"authorization_id": "authorization:test"},
                )
            self.assertEqual(tuple(outside.iterdir()), ())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_record_publication_parent_junction_cannot_escape_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            outside = project / "outside-records"
            outside.mkdir()
            adapter = CachedBuildAdapter(
                _Builder(project / "external"),
                object_root=project / "_build",
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            shutil.rmtree(adapter.records)
            _create_windows_junction(adapter.records, outside)
            try:
                with self.assertRaisesRegex(BuildResultCacheError, "unsafe"):
                    adapter.build(
                        _request(canonical_identity({"source": "redirect"}).uri),
                        {"authorization_id": "authorization:test"},
                    )
                self.assertEqual(tuple(outside.iterdir()), ())
            finally:
                adapter.records.rmdir()

    def test_build_record_cannot_reference_another_target_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            platforms = (
                {
                    "operating_system": "linux",
                    "architecture": "x86_64",
                    "abi": "gnu",
                    "target_triple": "x86_64-unknown-linux-gnu",
                },
                {
                    "operating_system": "windows",
                    "architecture": "x86_64",
                    "abi": "msvc",
                    "target_triple": "x86_64-pc-windows-msvc",
                },
            )
            adapters = tuple(
                CachedBuildAdapter(
                    _Builder(project / f"external-{index}"),
                    object_root=object_root,
                    project_root=project,
                    namespace_material={"variant": "release"},
                    target_platform=target,
                )
                for index, target in enumerate(platforms)
            )
            request = _request(canonical_identity({"source": "cross-target"}).uri)
            authorization = {"authorization_id": "authorization:test"}
            first = adapters[0].build(request, authorization)
            second = adapters[1].build(request, authorization)
            record_path = next(adapters[0].records.glob("*.json"))
            record = json.loads(record_path.read_bytes())
            record["result"]["artifact_path"] = second["artifact_path"]
            record_path.write_text(json.dumps(record), encoding="utf-8")

            self.assertNotEqual(first["artifact_path"], second["artifact_path"])
            with self.assertRaisesRegex(BuildResultCacheError, "target namespace"):
                adapters[0].build(request, authorization)

    def test_clean_waits_for_build_result_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            adapter = CachedBuildAdapter(
                _Builder(project / "external"),
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            request = _request(canonical_identity({"source": "clean-race"}).uri)
            authorization = {"authorization_id": "authorization:test"}
            publishing = threading.Event()
            continue_publication = threading.Event()
            real_replace = os.replace

            def pause_record_publication(source, destination):
                if Path(destination).parent == adapter.records:
                    publishing.set()
                    if not continue_publication.wait(timeout=10):
                        raise TimeoutError("clean race test timed out")
                return real_replace(source, destination)

            with mock.patch(
                "literate_ai.adapters.cache.build_result.os.replace",
                side_effect=pause_record_publication,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    build = executor.submit(adapter.build, request, authorization)
                    self.assertTrue(publishing.wait(timeout=10))
                    clean = executor.submit(
                        clean_cache_directories,
                        project,
                        environment={"BUILD_DIR": "generated", "OBJ_DIR": "_build"},
                    )
                    self.assertFalse(clean.done())
                    continue_publication.set()
                    build.result(timeout=20)
                    removed = clean.result(timeout=20)

            self.assertEqual(removed, (object_root,))
            self.assertFalse(object_root.exists())

    def test_record_publication_rejects_a_recreated_cache_without_its_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "_build"
            adapter = CachedBuildAdapter(
                _Builder(project / "external"),
                object_root=object_root,
                project_root=project,
                namespace_material={"platform": "fixture"},
            )
            request = _request(canonical_identity({"source": "reincarnation"}).uri)
            authorization = {"authorization_id": "authorization:test"}
            ready_to_publish = threading.Event()
            continue_publication = threading.Event()
            real_publish = adapter._publish_record

            def pause_before_record_publication(*args, **kwargs):
                ready_to_publish.set()
                if not continue_publication.wait(timeout=10):
                    raise TimeoutError("cache reincarnation test timed out")
                return real_publish(*args, **kwargs)

            with mock.patch.object(
                adapter,
                "_publish_record",
                side_effect=pause_before_record_publication,
            ):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    build = executor.submit(adapter.build, request, authorization)
                    self.assertTrue(ready_to_publish.wait(timeout=10))
                    removed = clean_cache_directories(
                        project,
                        environment={"BUILD_DIR": "generated", "OBJ_DIR": "_build"},
                    )
                    recreated = CachedBuildAdapter(
                        _Builder(project / "replacement-external"),
                        object_root=object_root,
                        project_root=project,
                        namespace_material={"platform": "fixture"},
                    )
                    continue_publication.set()
                    with self.assertRaisesRegex(
                        BuildResultCacheError, "artifact is unavailable"
                    ):
                        build.result(timeout=20)

            self.assertEqual(removed, (object_root,))
            self.assertEqual(tuple(recreated.records.glob("*.json")), ())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_nested_artifact_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            artifact = project / "artifact"
            artifact.mkdir()
            (artifact / "sample.exe").write_bytes(b"binary")
            outside = project / "outside"
            outside.mkdir()
            (outside / "escaped.dll").write_bytes(b"outside")
            junction = artifact / "redirect"
            _create_windows_junction(junction, outside)
            try:
                with self.assertRaisesRegex(BuildResultCacheError, "reparse point"):
                    build_result_cache._artifact_tree_identity(artifact)
            finally:
                junction.rmdir()


if __name__ == "__main__":
    unittest.main()
