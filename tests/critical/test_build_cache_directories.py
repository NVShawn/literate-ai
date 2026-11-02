"""BUILD_DIR/OBJ_DIR ownership, invalidation, and guarded cleanup tests."""

from __future__ import annotations

import hashlib
import json
import os
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
from literate_ai.perf import (
    PerformanceRecorder,
    performance_log_directories,
    read_performance_spans,
)
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile
from tests.support.fixtures_test_project_cli import invoke


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
    def test_diagnostic_state_accepts_parent_alias_but_refuses_leaf_redirection(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            project = parent / "project"
            project.mkdir()
            _write_project_manifest(project)
            before = {p.name: p.read_bytes() for p in project.iterdir()}
            real = parent / "operator"
            real.mkdir()
            alias = parent / "parent-alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"host cannot create directory aliases: {exc}")
            objects = parent / "objects"
            with mock.patch.dict(
                "os.environ",
                {"OBJ_DIR": str(objects), "LITAI_STATE_DIR": str(alias / "state")},
            ):
                code, result = invoke("--json", "status", "--project", str(project))
                self.assertEqual(code, 0, result)
                self.assertEqual(len(read_performance_spans(objects)), 1)
                leaf = parent / "state-alias"
                leaf.symlink_to(real / "state", target_is_directory=True)
                with mock.patch.dict("os.environ", {"LITAI_STATE_DIR": str(leaf)}):
                    code, result = invoke("--json", "status", "--project", str(project))
                    self.assertEqual(code, 0, result)
                self.assertEqual(len(read_performance_spans(objects)), 1)
                self.assertFalse(objects.exists())
                self.assertEqual(
                    {p.name: p.read_bytes() for p in project.iterdir()}, before
                )

    def test_diagnostic_run_id_and_leaf_cannot_redirect_writes_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            project = parent / "project"
            project.mkdir()
            source = project / "keep"
            source.write_bytes(b"source bytes")
            with mock.patch.dict(
                "os.environ", {"LITAI_STATE_DIR": str(parent / "state")}
            ):
                for run_id in ("../keep", str(source), "..\\keep", "CON"):
                    with self.subTest(run_id=run_id):
                        recorder = PerformanceRecorder(
                            build_root=parent / "objects",
                            project_root=project,
                            run_id=run_id,
                        )
                        with recorder.span(
                            "test", target_kind="test", target_id="refusal"
                        ):
                            pass
                recorder = PerformanceRecorder(
                    build_root=parent / "objects",
                    project_root=project,
                    run_id="redirect",
                )
                recorder.log_path.parent.mkdir(parents=True)
                for kind in ("hardlink", "symlink"):
                    with self.subTest(kind=kind):
                        try:
                            if kind == "hardlink":
                                os.link(source, recorder.log_path)
                            else:
                                recorder.log_path.symlink_to(source)
                        except OSError:
                            if os.name == "nt":
                                continue
                            raise
                        try:
                            with recorder.span(
                                "test", target_kind="test", target_id=kind
                            ):
                                pass
                            self.assertEqual(source.read_bytes(), b"source bytes")
                        finally:
                            recorder.log_path.unlink()
                self.assertEqual(source.read_bytes(), b"source bytes")
                self.assertFalse((parent / "objects").exists())

    def test_unavailable_diagnostic_state_cannot_mutate_observed_project_or_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            project = parent / "project"
            project.mkdir()
            _write_project_manifest(project)
            before = {p.name: p.read_bytes() for p in project.iterdir()}
            objects = parent / "objects"
            blocked = parent / "not-a-directory"
            blocked.write_bytes(b"operator data")
            states = [project / "state", objects / "state", blocked]
            if os.name != "nt":
                loop = parent / "loop"
                loop.symlink_to(loop)
                states.append(loop)
            for state in states:
                with (
                    self.subTest(state=state.name),
                    mock.patch.dict(
                        "os.environ",
                        {"OBJ_DIR": str(objects), "LITAI_STATE_DIR": str(state)},
                    ),
                ):
                    code, result = invoke("--json", "status", "--project", str(project))
                    self.assertEqual(code, 0, result)
                    self.assertFalse(objects.exists())
                    self.assertEqual(
                        {p.name: p.read_bytes() for p in project.iterdir()}, before
                    )
                    self.assertEqual(blocked.read_bytes(), b"operator data")

    def test_cli_diagnostics_preserve_fresh_and_foreign_cache_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            project = parent / "project"
            project.mkdir()
            _write_project_manifest(project)
            before = {p.name: p.read_bytes() for p in project.iterdir()}
            objects = parent / "objects"
            selected = parent / "selected"
            with mock.patch.dict(
                "os.environ",
                {"OBJ_DIR": str(objects), "LITAI_STATE_DIR": str(parent / "state")},
            ):
                code, result = invoke("--json", "status", "--project", str(project))
                self.assertEqual(code, 0, result)
                self.assertFalse(objects.exists())
                self.assertEqual(
                    {p.name: p.read_bytes() for p in project.iterdir()}, before
                )
                self.assertEqual(len(read_performance_spans(objects)), 1)
                # A typed refusal must retain timing under the explicitly selected
                # root, even when OBJ_DIR names a different root.
                code, result = invoke(
                    "--json",
                    "package",
                    "retained-units",
                    "execute",
                    "--project",
                    str(project),
                    "--obj-dir",
                    str(selected),
                    "--retained-project",
                    "missing.json",
                    "--retained-project-profile",
                    "missing.json",
                    "--policy",
                    "missing.json",
                    "--tools",
                    "missing.json",
                    "--authorize-retained-project",
                    "sha256:" + "a" * 64,
                    "--authorize-unit-packages",
                    "sha256:" + "b" * 64,
                )
                self.assertEqual(code, 2, result)
                self.assertNotEqual(result["error"]["code"], "cli.usage")
                failed = read_performance_spans(selected)
                self.assertEqual(len(failed), 1)
                self.assertFalse(failed[0].ok)
                self.assertTrue(failed[0].error_code)
                self.assertFalse(selected.exists())
                self.assertEqual(len(read_performance_spans(objects)), 1)
                ensure_cache_directory(objects, kind="object", project_root=project)
                # Existing cache-local logs remain visible, and literal-directory
                # reports do not accidentally merge another namespace.
                legacy, current = performance_log_directories(selected)
                legacy.mkdir(parents=True)
                (legacy / "old.jsonl").write_text(
                    json.dumps(failed[0].to_dict()) + "\nmalformed\n{}\n",
                    encoding="utf-8",
                )
                self.assertEqual(len(read_performance_spans(selected)), 2)
                code, report = invoke("--json", "perf", "show", "--dir", str(current))
                self.assertEqual(code, 0, report)
                self.assertEqual(report["result"]["span_count"], 1)
                self.assertEqual(report["result"]["log_directories"], [str(current)])
                foreign = parent / "foreign"
                foreign.mkdir()
                (foreign / "keep").write_bytes(b"operator data")
                with mock.patch.dict("os.environ", {"OBJ_DIR": str(foreign)}):
                    code, result = invoke("--json", "status", "--project", str(project))
                self.assertEqual(code, 0, result)
                self.assertEqual(list(foreign.iterdir()), [foreign / "keep"])
                self.assertEqual(len(read_performance_spans(foreign)), 1)
                with self.assertRaisesRegex(CacheDirectoryError, "unmarked"):
                    ensure_cache_directory(foreign, kind="object", project_root=project)
                self.assertEqual((foreign / "keep").read_bytes(), b"operator data")
                retained_spans = read_performance_spans(objects)
                clean_cache_directories(project, environment={"OBJ_DIR": str(objects)})
                self.assertFalse(objects.exists())
                self.assertEqual(read_performance_spans(objects), retained_spans)

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
