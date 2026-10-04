"""BUILD_DIR/OBJ_DIR ownership, invalidation, and guarded cleanup tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import literate_ai.cache_directories as cache_directory_module
from literate_ai.adapters.cache import CachedBuildAdapter
from literate_ai.cache_directories import (
    CacheDirectoryError,
    clean_cache_directories,
    ensure_cache_directory,
    resolve_cache_directories,
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


class BuildResultCacheTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
