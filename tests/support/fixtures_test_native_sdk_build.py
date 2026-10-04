"""Shared test fixtures extracted from test_native_sdk_build."""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.native_sdk_build import (
    NativeSdkBuildLayout,
    NativeSdkRepositoryBuilder,
)
from literate_ai.adapters.native_sdk_custody import (
    capture_native_sdk,
    materialize_native_sdk,
)
from literate_ai.adapters.native_sdk_runtime import observe_native_sdk_runtime
from literate_ai.adapters.source.git import GitSourceAdapter
from literate_ai.application.repository_sources import (
    RepositoryBuildApproval,
    RepositoryCheckout,
    RepositorySourceIndexBinding,
    RepositorySourceResolver,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.contracts.repositories import (
    RepositoryBuildCommand,
    RepositoryBuildPlan,
    RepositorySourceLock,
)
from literate_ai.security import (
    AuthorizationError,
    BuildAuthorization,
    DirectBuildAuthorizationVerifier,
    SecurityProfile,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.support.fixtures_test_repository_sources import dependency


class NativeSdkBuildTests(unittest.TestCase):
    def setUp(self):
        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("native SDK fixture requires CMake and a host C compiler")
        temporary = tempfile.TemporaryDirectory(prefix="sdk-build-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source_directory = tempfile.TemporaryDirectory(
            prefix="vendor-", dir=self.root
        )
        self.addCleanup(self.source_directory.cleanup)
        self.source = Path(self.source_directory.name)
        fixture = Path(__file__).resolve().parents[1] / "fixtures/native_sdk"
        shutil.copytree(fixture, self.source, dirs_exist_ok=True)
        (self.source / ".gitignore").write_text("_build/\n")
        for args in (
            ("init", "--quiet"),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "Vendor fixture",
            ),
        ):
            subprocess.run(
                ("git", *args),
                cwd=self.source,
                check=True,
                capture_output=True,
                timeout=30,
            )
        self.capture = GitSourceAdapter().capture(self.source)
        self.checkout = RepositoryCheckout(
            self.source,
            self.capture.git.head_commit,
            canonical_identity("fixture resolver"),
        )
        self.lock = RepositorySourceLock(
            dependency(),
            self.checkout.resolved_commit,
            self.capture.snapshot.identity,
            self.capture.snapshot.tree_identity,
            self.checkout.resolver_identity,
        )
        self.store = FileSystemCAS(self.root / "cas")
        suffix = (
            ".dll"
            if sys.platform == "win32"
            else ".dylib"
            if sys.platform == "darwin"
            else ".so"
        )
        license_blob = self.store.put_file(self.source / "LICENSE")
        self.layout = NativeSdkBuildLayout(
            "_build/sdk",
            "python",
            LibraryImportSurface(
                "python",
                "vendor_math",
                (
                    LibraryCapabilityImport(
                        "vendor.math",
                        canonical_identity("interface"),
                        "vendor_math",
                        ("scale",),
                    ),
                ),
            ),
            (
                f"python/vendor_math/lib/vendor_factor{suffix}",
                f"python/vendor_math/lib/vendor_math{suffix}",
            ),
            "LICENSE",
            "LICENSE",
            ContentIdentity.parse_uri(license_blob.identity),
            {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform),
            {"aarch64": "arm64", "amd64": "x86_64"}.get(
                platform.machine().lower(), platform.machine().lower()
            ),
        )
        self.tools = {"cmake": LocalComponentToolBinding(str(Path(cmake).resolve()))}
        self.grants = {}
        self.builder = NativeSdkRepositoryBuilder(
            layout=self.layout,
            tools=self.tools,
            store=self.store,
            grant_lookup=lambda identity: self.grants[identity.uri],
            environment=dict(os.environ),
            authorization_verifier=DirectBuildAuthorizationVerifier(),
        )
        self.plan = RepositoryBuildPlan(
            self.lock.identity,
            canonical_identity("effective"),
            canonical_identity("flavors"),
            (self.layout.identity,),
            canonical_identity("fixture plan authority"),
            tuple(tool.toolchain_identity for tool in self.tools.values()),
            (
                RepositoryBuildCommand(
                    "configure", ("cmake", "-S", ".", "-B", "_build")
                ),
                RepositoryBuildCommand(
                    "build",
                    (
                        "cmake",
                        "--build",
                        "_build",
                        "--config",
                        "Release",
                        "--parallel",
                        "2",
                    ),
                ),
            ),
            (self.layout.sdk_root,),
        )

    def remove_source(self):
        # Git objects are read-only on Windows; the directory owner handles them.
        self.source_directory.cleanup()
        self.assertFalse(self.source.exists())

    def approve(self, plan=None):
        plan = plan or self.plan
        request = self.builder.request(self.lock, plan)
        now = datetime.now(UTC)
        classification = canonical_identity("explicit fixture classification")
        grant = BuildAuthorization(
            "fixture-build",
            classification.uri,
            canonical_identity(request.to_dict()).uri,
            request.effective_revision_digest,
            "fixture",
            "Authorized native test",
            SecurityProfile.YOLO,
            request.requested_privileges,
            now - timedelta(seconds=1),
            now + timedelta(minutes=10),
            warning="Explicit isolated test grant for unsandboxed native fixture",
        )
        identity = canonical_identity(grant.to_dict())
        self.grants[identity.uri] = grant
        return RepositoryBuildApproval(
            self.lock.identity, plan.identity, classification, identity
        )

    def verify(self, approval=None, plan=None):
        return self.builder.verify(
            self.checkout,
            self.capture,
            self.lock,
            plan or self.plan,
            approval or self.approve(plan),
        )

    def manifest(self, identity):
        raw = (
            self.store.blob_root / identity.digest[:2] / identity.digest
        ).read_bytes()
        document = json.loads(raw)
        self.assertEqual(canonical_identity(document), identity)
        return document

    def test_real_repository_pipeline_retains_native_sdk_for_relocation(self):
        fixture = self

        class Intake:
            @contextmanager
            def acquire(self, _dependency):
                yield fixture.checkout

            def capture(self, checkout):
                return GitSourceAdapter().capture(checkout.source_root)

            def index(self, _checkout, capture):
                return RepositorySourceIndexBinding(
                    capture.snapshot.identity,
                    capture.snapshot.tree_identity,
                    canonical_identity("fixture indexer"),
                    canonical_identity("fixture index"),
                )

            def plan(self, _lock, index, _effective, _flavors, _tools):
                return dataclasses.replace(
                    fixture.plan, evidence=(fixture.layout.identity, index)
                )

            def authorize(self, _lock, _index, plan):
                return fixture.approve(plan)

            def admit(self, _checkout, _capture, admission):
                return ContentIdentity.parse_uri(
                    fixture.store.put_manifest(admission.to_dict()).identity
                )

        intake = Intake()
        resolution = RepositorySourceResolver(
            acquirer=intake,
            capturer=intake,
            indexer=intake,
            planner=intake,
            authorizer=intake,
            builder=self.builder,
            cache=intake,
        ).resolve(
            dependency(),
            effective_revision=self.plan.effective_revision,
            flavor_set=self.plan.flavor_set,
            toolchains=self.plan.toolchains,
        )
        output = resolution.admission.build_outputs[0]
        snapshot = NativeSdkSnapshot.from_dict(self.manifest(output.identity))
        self.assertEqual(snapshot.source_lock_identity, resolution.lock.identity)
        self.assertEqual(snapshot.recipe_identity, resolution.build_plan.identity)
        self.assertEqual(snapshot.license_identity, self.layout.license_identity)
        result = self.manifest(resolution.admission.build_result)
        self.assertEqual(len(result["commands"]), 2)
        self.assertEqual([item["returncode"] for item in result["commands"]], [0, 0])
        from literate_ai.contracts.blobs import BlobRef

        runtime = self.store.get_manifest(
            BlobRef.from_dict(result["runtime_observation"])
        )
        self.assertEqual(runtime["snapshot_identity"], snapshot.identity.to_dict())
        self.assertEqual(runtime["operating_system"], self.layout.operating_system)
        self.assertEqual(runtime["architecture"], self.layout.architecture)
        self.assertEqual(len(runtime["native_libraries"]), 2)
        self.remove_source()
        relocated = materialize_native_sdk(
            snapshot,
            expected_identity=output.identity,
            store=self.store,
            parent=self.root,
        )
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0,sys.argv[1]); "
                "import vendor_math; print(vendor_math.scale(1.5,3))",
                str(relocated / "python"),
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4.5")
        # A newly captured incomplete package is still inadmissible: the primary
        # image's actual loader import must resolve, even if the omitted library
        # is no longer listed in the candidate snapshot's declared outputs.
        (relocated / self.layout.native_libraries[0]).unlink()
        incomplete = capture_native_sdk(
            relocated,
            store=self.store,
            source_lock_identity=snapshot.source_lock_identity,
            recipe_identity=snapshot.recipe_identity,
            target_identity=snapshot.target_identity,
            license_identity=snapshot.license_identity,
            import_surface=snapshot.import_surface,
            import_root=snapshot.import_root,
            native_libraries=(self.layout.native_libraries[1],),
        )
        with self.assertRaises(DependencyObservationError):
            observe_native_sdk_runtime(
                incomplete,
                expected_identity=incomplete.identity,
                target_identity=incomplete.target_identity,
                operating_system=self.layout.operating_system,
                architecture=self.layout.architecture,
                root=relocated,
                store=self.store,
            )

    def test_default_verifier_refuses_before_any_build_process(self):
        self.builder = NativeSdkRepositoryBuilder(
            layout=self.layout,
            tools=self.tools,
            store=self.store,
            grant_lookup=lambda identity: self.grants[identity.uri],
            environment=dict(os.environ),
        )
        with mock.patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process"
        ) as run:
            with self.assertRaisesRegex(
                AuthorizationError, "live_revocation_verifier_required"
            ):
                self.verify()
            run.assert_not_called()

    def test_changed_plan_or_source_refuses_before_execution(self):
        approval = self.approve()
        changed = dataclasses.replace(self.plan, commands=(self.plan.commands[0],))
        with mock.patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process"
        ) as run:
            with self.assertRaisesRegex(ValueError, "exact live grant"):
                self.verify(approval, changed)
            (self.source / "vendor_math.c").write_text("changed source")
            with self.assertRaisesRegex(ValueError, "exact capture"):
                self.verify(approval)
            run.assert_not_called()

    def test_wrong_license_and_unbound_layout_refuse_before_execution(self):
        with mock.patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process"
        ) as run:
            self.builder.layout = dataclasses.replace(
                self.layout, license_identity=canonical_identity("wrong license")
            )
            with self.assertRaisesRegex(ValueError, "exact source lock and layout"):
                self.approve()
            plan = dataclasses.replace(
                self.plan, evidence=(self.builder.layout.identity,)
            )
            with self.assertRaisesRegex(ValueError, "source license"):
                self.verify(plan=plan)
            run.assert_not_called()

    def test_preexisting_output_is_preserved_and_never_executed(self):
        output = self.source / self.layout.sdk_root
        output.mkdir(parents=True)
        (output / "keep").write_text("prior result")
        with mock.patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process"
        ) as run:
            with self.assertRaisesRegex(ValueError, "must be absent"):
                self.verify()
            run.assert_not_called()
        self.assertEqual((output / "keep").read_text(), "prior result")

    def test_revocation_after_configuration_prevents_compilation(self):
        from literate_ai.adapters.native_sdk_build import run_bounded_process

        revoked = False

        class Verifier:
            def require_build_valid(self, authorization, request, *, now):
                if revoked:
                    raise AuthorizationError("security.authorization_revoked")
                authorization.require_valid(request, now=now)

        def configure_then_revoke(*args, **kwargs):
            nonlocal revoked
            result = run_bounded_process(*args, **kwargs)
            revoked = True
            return result

        self.builder.authorization_verifier = Verifier()
        with mock.patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process",
            side_effect=configure_then_revoke,
        ) as run:
            with self.assertRaisesRegex(AuthorizationError, "authorization_revoked"):
                self.verify()
            self.assertEqual(run.call_count, 1)
        self.assertFalse(
            (
                self.source / self.layout.sdk_root / self.layout.native_libraries[0]
            ).exists()
        )

    def test_missing_exported_license_cannot_produce_build_verification(self):
        self.builder.layout = dataclasses.replace(self.layout, output_license="missing")
        plan = dataclasses.replace(self.plan, evidence=(self.builder.layout.identity,))
        with self.assertRaisesRegex(ValueError, "retain the exact source license"):
            self.verify(plan=plan)
