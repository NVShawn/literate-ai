"""Bazel conformance decorates, but never impersonates, a native builder."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import (
    BAZEL_DEPENDENCY_EVIDENCE_OUTPUT,
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BazelConformanceBuildAdapter,
    BazelToolchain,
    BuildError,
    CompositeNativeBuildAdapter,
    bazel_resolver_identity,
    executable_file_digest,
)
from literate_ai.adapters.builders import bazel as bazel_builder_module
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.application import create_composite_build_request, realize_manifest
from literate_ai.contracts import (
    ArtifactExport,
    ArtifactMaterializationPlan,
    BlobRef,
    BuildActionRequest,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentBuildManifest,
    ContentIdentity,
    SourceTreeEntry,
    SourceTreeEntryOrigin,
    canonical_identity,
    generated_source_tree_identity,
)
from literate_ai.ports import (
    BuildDependencyObservation,
    BuildInputConsumption,
    BzlmodModule,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildAuthorization,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = datetime(2026, 8, 6, tzinfo=UTC)

GRAPH_BYTES = json.dumps(
    {
        "key": "<root>",
        "name": "conformance_fixture",
        "version": "1.0.0",
        "apparentName": "conformance_fixture",
        "dependencies": [
            {
                "key": "rules_python@2.2.0",
                "name": "rules_python",
                "version": "2.2.0",
                "apparentName": "rules_python",
                "dependencies": [],
                "indirectDependencies": [],
                "cycles": [],
                "root": False,
            }
        ],
        "indirectDependencies": [],
        "cycles": [],
        "root": True,
    },
    separators=(",", ":"),
).encode()
REPOSITORY_BYTES = (
    json.dumps(
        {
            "canonicalName": "rules_python+",
            "repoRuleName": "http_archive",
            "repoRuleBzlLabel": "@@bazel_tools//tools/build_defs/repo:http.bzl",
            "attribute": [
                {
                    "name": "urls",
                    "type": "STRING_LIST",
                    "stringListValue": [
                        "https://registry.example/rules_python-2.2.0.tar.gz"
                    ],
                },
                {
                    "name": "integrity",
                    "type": "STRING",
                    "stringValue": "sha256-fixture",
                },
                {
                    "name": "remote_module_file_urls",
                    "type": "STRING_LIST",
                    "stringListValue": [
                        "https://bcr.bazel.build/modules/rules_python/2.2.0/"
                        "MODULE.bazel"
                    ],
                },
                {
                    "name": "remote_module_file_integrity",
                    "type": "STRING",
                    "stringValue": "sha256-module-fixture",
                },
            ],
        },
        separators=(",", ":"),
    ).encode()
    + b"\n"
)
LOCK_BYTES = b'{"lockFileVersion":28}\n'


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class _Delegate:
    builder_id = "builder:fixture-native@1"

    def __init__(self, artifact_path: Path) -> None:
        self.artifact_path = artifact_path
        self.calls: list[tuple[object, object]] = []
        artifact_path.mkdir(parents=True)
        (artifact_path / "run").write_bytes(b"fixture executable\n")
        manifest = b'{"delegate":true}'
        (artifact_path / "build-manifest.json").write_bytes(manifest)
        self.digest = _sha256(manifest)
        self.artifact_exports = None

    def build(self, request, authorization):
        self.calls.append((request, authorization))
        result = {
            "artifact_digest": self.digest,
            "artifact_path": str(self.artifact_path),
            "source_bundle_digest": request["source_bundle_digest"],
            "authorization_id": authorization["authorization_id"],
            "toolchain_identity": request["toolchain_digest"],
            "executable_file": "run",
            "compiled_files": ["run"],
        }
        if self.artifact_exports is not None:
            result["artifact_exports"] = self.artifact_exports
        return result


class _ConsumptionAwareDelegate(_Delegate):
    def __init__(self, artifact_path: Path) -> None:
        super().__init__(artifact_path)
        self.consumptions: list[BuildInputConsumption] = []

    def build_with_input_consumption(self, request, authorization, consumption):
        self.consumptions.append(consumption)
        return self.build(request, authorization)


class BazelToolchainDiscoveryTests(unittest.TestCase):
    def _cached_binary(self, root: Path, content: bytes) -> Path:
        digest = hashlib.sha256(content).hexdigest()
        executable = (
            root
            / "downloads"
            / "sha256"
            / digest
            / "bin"
            / ("bazel.exe" if sys.platform == "win32" else "bazel")
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(content)
        return executable

    def test_autodiscovery_uses_highest_direct_content_addressed_bazel_9(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = self._cached_binary(cache, b"direct-bazel-7")
            selected = self._cached_binary(cache, b"direct-bazel-9")

            def probe(command, *_args, **_kwargs):
                return (
                    "bazel 7.4.1"
                    if Path(command[0]) == old.resolve()
                    else "bazel 9.2.0"
                )

            with (
                patch.object(bazel_builder_module, "_probe_bazel", side_effect=probe),
                patch.object(
                    bazel_builder_module.shutil,
                    "which",
                    side_effect=AssertionError("PATH launcher must not execute"),
                ),
            ):
                toolchain = bazel_builder_module.discover_bazel_toolchain(
                    {"BAZELISK_HOME": str(cache), "PATH": "ignored"}
                )

            self.assertEqual(toolchain.command, (str(selected.resolve()),))
            self.assertEqual(toolchain.launcher_executable, str(selected.resolve()))
            self.assertEqual(toolchain.version, "bazel 9.2.0")
            self.assertEqual(
                toolchain.launcher_digest, executable_file_digest(selected)
            )

    def test_autodiscovery_rejects_cache_path_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            executable = (
                cache
                / "downloads"
                / "sha256"
                / ("a" * 64)
                / "bin"
                / ("bazel.exe" if sys.platform == "win32" else "bazel")
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"not-the-addressed-content")

            with self.assertRaises(BuildError) as raised:
                bazel_builder_module.discover_bazel_toolchain(
                    {"BAZELISK_HOME": str(cache), "PATH": "ignored"}
                )

            self.assertEqual(
                raised.exception.code, "builder.bazel_cache_digest_mismatch"
            )

    def test_explicit_bazel_must_be_the_direct_content_addressed_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bazelisk = root / ("bazel.exe" if sys.platform == "win32" else "bazel")
            bazelisk.write_bytes(b"native-bazelisk-launcher")
            with (
                patch.object(
                    bazel_builder_module.shutil, "which", return_value=str(bazelisk)
                ),
                patch.object(
                    bazel_builder_module,
                    "_probe_bazel",
                    side_effect=AssertionError("Bazelisk must never execute"),
                ),
                self.assertRaises(BuildError) as raised,
            ):
                bazel_builder_module.discover_bazel_toolchain(
                    {
                        "BAZEL": str(bazelisk),
                        "BAZELISK_HOME": str(root / "cache"),
                        "PATH": str(root),
                    }
                )
            self.assertEqual(
                raised.exception.code, "builder.bazel_direct_toolchain_required"
            )

    def test_explicit_bazel_accepts_exact_direct_cached_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            direct = self._cached_binary(cache, b"direct-bazel-9")
            with (
                patch.object(
                    bazel_builder_module.shutil, "which", return_value=str(direct)
                ),
                patch.object(
                    bazel_builder_module,
                    "_probe_bazel",
                    return_value="bazel 9.2.0",
                ),
            ):
                toolchain = bazel_builder_module.discover_bazel_toolchain(
                    {
                        "BAZEL": str(direct),
                        "BAZELISK_HOME": str(cache),
                        "PATH": "ignored",
                    }
                )
            self.assertEqual(toolchain.command, (str(direct.resolve()),))

    def test_discovered_toolchain_supports_provider_neutral_drift_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            self._cached_binary(cache, b"direct-bazel-9")
            with patch.object(
                bazel_builder_module,
                "_probe_bazel",
                return_value="bazel 9.2.0",
            ) as probe:
                toolchain = bazel_builder_module.discover_bazel_toolchain(
                    {"BAZELISK_HOME": str(cache), "PATH": "ignored"}
                )
                toolchain.require_unchanged()

            self.assertEqual(probe.call_count, 2)


class BazelTemporaryDirectoryTests(unittest.TestCase):
    def test_windows_cleanup_uses_extended_length_path_namespace(self) -> None:
        root = Path("C:/fixture/bazel-output")
        with patch.object(bazel_builder_module.sys, "platform", "win32"):
            removal_path = bazel_builder_module._bazel_removal_path(root)

        self.assertTrue(removal_path.startswith("\\\\?\\"), removal_path)
        self.assertTrue(removal_path.endswith(str(root)), removal_path)

    def test_windows_unc_cleanup_uses_extended_unc_namespace(self) -> None:
        class AbsoluteWindowsPath:
            def __str__(self) -> str:
                return r"\\server\share\bazel-output"

        with (
            patch.object(bazel_builder_module.sys, "platform", "win32"),
            patch.object(
                Path,
                "absolute",
                return_value=AbsoluteWindowsPath(),
            ),
        ):
            removal_path = bazel_builder_module._bazel_removal_path(Path("ignored"))

        self.assertEqual(removal_path, "\\\\?\\UNC\\server\\share\\bazel-output")

    def test_cleanup_clears_a_read_only_bazel_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            output.write_bytes(b"artifact")
            output.chmod(stat.S_IREAD)

            bazel_builder_module._remove_bazel_read_only(
                os.unlink,
                str(output),
                (PermissionError, PermissionError("read-only"), None),
            )

            self.assertFalse(output.exists())

    def test_cleanup_retries_a_read_only_bazel_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bazel-output"
            output = root / "tree-artifact" / "lib"
            output.mkdir(parents=True)
            (output / "libsample.a").write_bytes(b"archive")
            output.chmod(stat.S_IWRITE)

            bazel_builder_module._remove_bazel_directory(root)

            self.assertFalse(root.exists())

    def test_cleanup_retries_a_transient_windows_lock(self) -> None:
        real_rmtree = bazel_builder_module.shutil.rmtree
        attempts = 0

        def transient_lock(path: Path, **kwargs: object) -> None:
            nonlocal attempts
            del kwargs
            attempts += 1
            if attempts == 1:
                raise OSError("runfiles tree is still locked")
            real_rmtree(path)

        with (
            patch.object(
                bazel_builder_module.shutil, "rmtree", side_effect=transient_lock
            ),
            patch.object(bazel_builder_module.time, "sleep") as sleep,
        ):
            with bazel_builder_module._temporary_bazel_directory() as temporary:
                root = Path(temporary)
                self.assertTrue(
                    root.name.startswith(
                        bazel_builder_module._BAZEL_TEMPORARY_DIRECTORY_PREFIX
                    )
                )
                self.assertLessEqual(len(root.name), 16)
                (root / "artifact").write_text("built\n", encoding="utf-8")

        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(0.05)
        self.assertFalse(root.exists())

    def test_cleanup_does_not_suppress_a_build_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "original build failure"):
            with bazel_builder_module._temporary_bazel_directory():
                raise RuntimeError("original build failure")

    def test_persistent_cleanup_failure_is_explicit(self) -> None:
        retry_delays = bazel_builder_module._BAZEL_CLEANUP_RETRY_DELAYS_SECONDS
        with (
            patch.object(
                bazel_builder_module.shutil,
                "rmtree",
                side_effect=OSError("persistent lock"),
            ),
            patch.object(bazel_builder_module.time, "sleep") as sleep,
            self.assertRaises(BuildError) as raised,
        ):
            with bazel_builder_module._temporary_bazel_directory():
                pass

        self.assertEqual(raised.exception.code, "builder.bazel_cleanup_failed")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            list(retry_delays[1:]),
        )
        self.assertLessEqual(sum(retry_delays), 30.0)


class BazelArtifactManifestTests(unittest.TestCase):
    def test_file_records_use_portable_text_order_not_host_path_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "z-lower.txt").write_text("z\n", encoding="utf-8")
            (root / "Z-upper.txt").write_text("Z\n", encoding="utf-8")
            records = bazel_builder_module._file_records(root)

        paths = [record["path"] for record in records]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(paths, ["Z-upper.txt", "z-lower.txt"])


class _CompileRejectingDelegate(_Delegate):
    def build(self, request, authorization):
        self.calls.append((request, authorization))
        raise BuildError(
            "builder.cpp_generated_source_rejected",
            "healthy native compiler rejected generated source",
        )


class BazelConformanceBuildAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        executable = Path(sys.executable).resolve()
        self.toolchain = BazelToolchain(
            command=(str(executable),),
            launcher_executable=str(executable),
            launcher_digest=executable_file_digest(executable),
            version="bazel 9.2.0",
        )
        self.files = {
            "source/MODULE.bazel": (
                'module(name = "conformance_fixture", version = "1.0.0")\n'
                'bazel_dep(name = "rules_python", version = "2.2.0")\n'
            ),
            "source/BUILD.bazel": ('py_binary(name = "run", srcs = ["app.py"])\n'),
            "source/app.py": 'print("fixture")\n',
        }

    def request_and_authorization(self, *, include_evidence: bool = True):
        source_digest = generated_source_tree_identity(
            {path: content.encode() for path, content in self.files.items()}
        )
        policy = SecurityPolicy(policy_digest=DIGEST_A)
        classification = policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=(
                OriginAttestation(
                    source_digest=source_digest,
                    signer="test",
                    trust_root="test",
                    signature_identity="test",
                    verified=True,
                ),
            ),
            findings=(),
        )
        outputs = ["native-executable"]
        if include_evidence:
            outputs.append(BAZEL_DEPENDENCY_EVIDENCE_OUTPUT)
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=_Delegate.builder_id,
            toolchain_digest=DIGEST_A,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=tuple(outputs),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="Bazel conformance",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        request_document = {**request.to_dict(), "artifact": {"files": self.files}}
        return request_document, authorization.to_dict()

    @staticmethod
    def successful_bazel(command, *, cwd, **_kwargs):
        if "mod" in command and "--lockfile_mode=update" in command:
            (cwd / "MODULE.bazel.lock").write_bytes(LOCK_BYTES)
        if "graph" in command:
            return BoundedProcessResult(0, GRAPH_BYTES, b"")
        if "show_repo" in command:
            return BoundedProcessResult(0, REPOSITORY_BYTES, b"")
        if "query" in command:
            content = (
                b"//:BUILD.bazel\n"
                if "buildfiles(//...)" in command
                else b"//:app.py\n"
            )
            return BoundedProcessResult(0, content, b"")
        return BoundedProcessResult(0, b"", b"")

    def adapter(
        self,
        root: Path,
        delegate: _Delegate,
        *,
        build_options: tuple[str, ...] = (),
    ):
        return BazelConformanceBuildAdapter(
            delegate,
            self.toolchain,
            root / "composite-artifacts",
            AuthorizationRevocationSet(),
            clock=lambda: NOW + timedelta(minutes=1),
            build_options=build_options,
        )

    def composite_invocation(
        self,
        *,
        action_id: str = "compile-application",
        outputs: tuple[str, ...] = (
            BAZEL_DEPENDENCY_EVIDENCE_OUTPUT,
            "native-executable",
        ),
        privileges: tuple[BuildPrivilege, ...] = tuple(BuildPrivilege),
        compiler_identity: ContentIdentity | None = None,
        authorization_identity: ContentIdentity | None = None,
    ):
        request_document, authorization_document = self.request_and_authorization()
        request = BuildRequest.from_dict(
            {key: value for key, value in request_document.items() if key != "artifact"}
        )
        authorization = BuildAuthorization.from_dict(authorization_document)
        source_identity = ContentIdentity.parse_uri(request.source_bundle_digest)
        compiler = compiler_identity or ContentIdentity.parse_uri(
            request.toolchain_digest
        )
        authority = authorization_identity or canonical_identity(
            authorization.to_dict()
        )
        component = canonical_identity({"component": "fixture"})
        target = canonical_identity({"target": "host"})
        abi = canonical_identity({"abi": "fixture"})
        producer = canonical_identity({"producer": _Delegate.builder_id})
        action = BuildActionRequest(
            action_id=action_id,
            component_revision=component,
            role="application",
            abi_identity=abi,
            target_identity=target,
            media_type="application/octet-stream",
            producer_identity=producer,
            source_tree_identity=source_identity,
            toolchain_identity=compiler,
            authorization_identity=authority,
            dependency_artifacts=(),
            declared_output_ids=tuple(sorted(outputs)),
        )
        exports = tuple(
            ArtifactExport(
                export_id=output,
                component_revision=component,
                role=action.role,
                abi_identity=abi,
                target_identity=target,
                media_type=action.media_type,
                producer_identity=producer,
                source_tree_identity=source_identity,
                toolchain_identity=compiler,
                authorization_identity=authority,
                dependency_artifact_identities=(),
                blob=BlobRef("0" * 64, 0),
            )
            for output in sorted(outputs)
        )
        build_toolchain = ContentIdentity.parse_uri(self.toolchain.identity)
        manifest = ComponentBuildManifest(
            component_revision=component,
            source_tree_identity=source_identity,
            build_system_driver_identity=build_toolchain,
            actions=(action,),
            exports=exports,
        )
        entries = tuple(
            SourceTreeEntry(
                path=path,
                role="generated-source",
                origin=SourceTreeEntryOrigin.GENERATED_TEXT,
                blob=BlobRef(
                    hashlib.sha256(content.encode()).hexdigest(),
                    len(content.encode()),
                    media_type="text/plain",
                ),
            )
            for path, content in sorted(self.files.items())
        )
        materialization = ArtifactMaterializationPlan(
            source_tree_identity=source_identity,
            execution_root_identity=canonical_identity({"root": "fixture"}),
            entries=entries,
        )
        runtime = canonical_identity({"runtime": "fixture"})
        composite = create_composite_build_request(
            manifest,
            materialization,
            build_system_resolver_identity=bazel_resolver_identity(self.toolchain),
            language_compiler_identity=compiler,
            language_runtime_identity=runtime,
            ordered_actions=((BuildSubActionKind.COMPILE, action_id),),
            requested_privileges=privileges,
        )
        return (
            composite,
            manifest,
            materialization,
            request,
            authorization,
            {"files": self.files},
            runtime,
        )

    def native_adapter(self, delegate: _Delegate, runtime: ContentIdentity):
        return CompositeNativeBuildAdapter(
            delegate,
            build_system_resolver_identity=bazel_resolver_identity(self.toolchain),
            build_system_toolchain_identity=ContentIdentity.parse_uri(
                self.toolchain.identity
            ),
            language_runtime_identity=runtime,
            authorization_verifier=AuthorizationRevocationSet(),
            clock=lambda: NOW + timedelta(minutes=1),
        )

    def typed_adapter(self, root: Path, delegate: _Delegate, runtime: ContentIdentity):
        return self.adapter(root, self.native_adapter(delegate, runtime))

    def test_post_build_validation_requires_exact_declared_exports(self) -> None:
        _, manifest, *_ = self.composite_invocation()
        planned = replace(manifest, exports=())

        self.assertEqual(
            realize_manifest(planned, manifest.exports).exports,
            manifest.exports,
        )
        mismatched = (
            replace(
                manifest.exports[0],
                producer_identity=canonical_identity({"producer": "unexpected"}),
            ),
            *manifest.exports[1:],
        )
        with self.assertRaisesRegex(ValueError, "realized exports differ"):
            realize_manifest(planned, mismatched)

    def test_preserves_delegate_contract_and_emits_exact_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _Delegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=self.successful_bazel,
                ) as process_call,
            ):
                options = ("--repo_env=SDKROOT=/selected/sdk",)
                result = self.adapter(root, delegate, build_options=options).build(
                    request, authorization
                )
                repeated = self.adapter(root, delegate, build_options=options).build(
                    request, authorization
                )

            self.assertIs(delegate.calls[0][0], request)
            self.assertIs(delegate.calls[0][1], authorization)
            self.assertEqual(repeated["artifact_digest"], result["artifact_digest"])
            self.assertEqual(repeated["artifact_path"], result["artifact_path"])
            self.assertEqual(result["delegate_artifact_digest"], delegate.digest)
            self.assertEqual(result["executable_file"], "run")
            self.assertEqual(result["toolchain_identity"], DIGEST_A)
            artifact = Path(result["artifact_path"])
            self.assertEqual(
                (artifact / ".literate/bazel/MODULE.bazel.lock").read_bytes(),
                LOCK_BYTES,
            )
            self.assertEqual(
                (artifact / ".literate/bazel/module-graph.json").read_bytes(),
                GRAPH_BYTES,
            )
            self.assertEqual(
                (artifact / ".literate/bazel/repositories.ndjson").read_bytes(),
                REPOSITORY_BYTES,
            )
            self.assertEqual(
                (artifact / ".literate/delegate/build-manifest.json").read_bytes(),
                b'{"delegate":true}',
            )
            observation = BuildDependencyObservation.from_dict(
                result["dependency_observation"]
            )
            self.assertEqual(
                observation.resolver_toolchain_identity,
                self.toolchain.identity,
            )
            manifest_bytes = (artifact / "build-manifest.json").read_bytes()
            self.assertEqual(result["artifact_digest"], _sha256(manifest_bytes))
            manifest = json.loads(manifest_bytes)
            self.assertEqual(
                manifest["observation_identity"], observation.observation_identity
            )
            commands = [call.args[0] for call in process_call.call_args_list]
            self.assertTrue(all("--batch" in command for command in commands))
            self.assertTrue(all(options[0] in command for command in commands))
            self.assertFalse(
                any("--enable_runfiles" in command for command in commands)
            )
            self.assertFalse(any("test" in command for command in commands))
            self.assertTrue(
                any(
                    "--nobuild" in command and "//..." in command
                    for command in commands
                )
            )
            self.assertTrue(
                any(
                    "--nobuild" not in command
                    and "build" in command
                    and "//..." in command
                    for command in commands
                )
            )
            self.assertTrue(
                any(
                    'kind("source file", deps(//...))' in command
                    for command in commands
                )
            )
            self.assertNotIn("source/MODULE.bazel.lock", request["artifact"]["files"])

    def test_passes_typed_exact_build_input_consumption_to_aware_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _ConsumptionAwareDelegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=self.successful_bazel,
                ),
            ):
                result = self.adapter(root, delegate).build(request, authorization)

            self.assertEqual(len(delegate.consumptions), 1)
            consumption = delegate.consumptions[0]
            self.assertEqual(
                consumption.files,
                (
                    "source/BUILD.bazel",
                    "source/MODULE.bazel",
                    "source/app.py",
                ),
            )
            self.assertEqual(
                consumption.source_bundle_digest,
                request["source_bundle_digest"],
            )
            self.assertEqual(
                result["build_input_consumption_identity"], consumption.identity
            )
            artifact = Path(result["artifact_path"])
            self.assertEqual(
                (artifact / ".literate/bazel/buildfiles.txt").read_bytes(),
                b"//:BUILD.bazel\n",
            )
            self.assertEqual(
                (artifact / ".literate/bazel/source-inputs.txt").read_bytes(),
                b"//:app.py\n",
            )
            self.assertEqual(
                json.loads(
                    (
                        artifact / ".literate/bazel/build-input-consumption.json"
                    ).read_bytes()
                ),
                consumption.to_dict(),
            )

    def test_typed_composite_path_preserves_native_result_and_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _ConsumptionAwareDelegate(root / "delegate-artifact")
            invocation = self.composite_invocation()
            *arguments, runtime = invocation
            manifest = arguments[1]
            delegate.artifact_exports = [item.to_dict() for item in manifest.exports]
            planned = replace(manifest, exports=())
            arguments[0] = replace(
                arguments[0], component_build_manifest_identity=planned.identity
            )
            arguments[1] = planned

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=self.successful_bazel,
                ),
            ):
                result = self.typed_adapter(root, delegate, runtime).build_composite(
                    *arguments
                )

            self.assertEqual(len(delegate.calls), 1)
            self.assertEqual(len(delegate.consumptions), 1)
            self.assertEqual(result["delegate_artifact_digest"], delegate.digest)
            self.assertEqual(result["executable_file"], "run")
            self.assertEqual(result["toolchain_identity"], DIGEST_A)
            realized = ComponentBuildManifest.from_dict(
                result["component_build_manifest"]
            )
            self.assertEqual(realized.exports, manifest.exports)
            self.assertEqual(len(result["artifact_exports"]), 2)
            self.assertEqual(
                result["build_input_consumption_identity"],
                delegate.consumptions[0].identity,
            )
            observation = BuildDependencyObservation.from_dict(
                result["dependency_observation"]
            )
            self.assertEqual(
                observation.resolver_toolchain_identity, self.toolchain.identity
            )

    def test_native_composite_does_not_silently_omit_sdk_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delegate = _Delegate(Path(directory) / "delegate-artifact")
            composite, manifest, plan, request, authorization, artifact, runtime = (
                self.composite_invocation()
            )
            plan = replace(
                plan,
                native_sdk_input_identities=(canonical_identity("sdk-input"),),
            )
            composite = replace(composite, materialization_plan_identity=plan.identity)
            with self.assertRaisesRegex(BuildError, "native SDK input materialization"):
                self.native_adapter(delegate, runtime).build_composite(
                    composite, manifest, plan, request, authorization, artifact
                )
            self.assertEqual(delegate.calls, [])

    def test_native_composite_rejects_mismatched_realized_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delegate = _Delegate(Path(directory) / "delegate-artifact")
            composite, manifest, plan, request, authorization, artifact, runtime = (
                self.composite_invocation()
            )
            delegate.artifact_exports = [
                replace(
                    manifest.exports[0],
                    producer_identity=canonical_identity({"producer": "wrong"}),
                ).to_dict(),
                manifest.exports[1].to_dict(),
            ]
            planned = replace(manifest, exports=())
            with self.assertRaisesRegex(BuildError, "realized exports differ"):
                self.native_adapter(delegate, runtime).build_composite(
                    replace(
                        composite, component_build_manifest_identity=planned.identity
                    ),
                    planned,
                    plan,
                    request,
                    authorization,
                    artifact,
                )
            self.assertEqual(len(delegate.calls), 1)

    def test_typed_composite_path_rejects_authority_mismatches_before_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _ConsumptionAwareDelegate(root / "delegate-artifact")
            base = self.composite_invocation()
            composite, manifest, plan, request, authorization, artifact, runtime = base
            alternate = self.composite_invocation(action_id="alternate-action")
            mismatches = {
                "authorization": self.composite_invocation(
                    authorization_identity=canonical_identity(
                        {"authorization": "wrong"}
                    )
                )[:6],
                "compiler": self.composite_invocation(
                    compiler_identity=canonical_identity({"compiler": "wrong"})
                )[:6],
                "outputs": self.composite_invocation(outputs=("native-executable",))[
                    :6
                ],
                "privileges": self.composite_invocation(
                    privileges=tuple(
                        privilege
                        for privilege in BuildPrivilege
                        if privilege is not BuildPrivilege.NETWORK_ACCESS
                    )
                )[:6],
                "resolver": (
                    replace(
                        composite,
                        build_system_resolver_identity=canonical_identity(
                            {"resolver": "wrong"}
                        ),
                    ),
                    manifest,
                    plan,
                    request,
                    authorization,
                    artifact,
                ),
                "runtime": (
                    replace(
                        composite,
                        language_runtime_identity=canonical_identity(
                            {"runtime": "wrong"}
                        ),
                    ),
                    manifest,
                    plan,
                    request,
                    authorization,
                    artifact,
                ),
                "sub-action": (
                    alternate[0],
                    manifest,
                    plan,
                    request,
                    authorization,
                    artifact,
                ),
            }

            with patch.object(
                bazel_builder_module, "_run_bounded_process"
            ) as process_call:
                for label, arguments in mismatches.items():
                    with (
                        self.subTest(label=label),
                        self.assertRaises(BuildError) as error,
                    ):
                        self.typed_adapter(root, delegate, runtime).build_composite(
                            *arguments
                        )
                    self.assertEqual(
                        error.exception.code, "builder.composite_request_mismatch"
                    )

            process_call.assert_not_called()
            self.assertEqual(delegate.calls, [])

    def test_native_rejection_provides_stronger_candidate_failure_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _CompileRejectingDelegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            def fail_full_build(command, *, cwd, **kwargs):
                result = self.successful_bazel(command, cwd=cwd, **kwargs)
                if (
                    "--nobuild" not in command
                    and "build" in command
                    and "//..." in command
                ):
                    return BoundedProcessResult(1, b"", b"C++ action failed")
                return result

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=fail_full_build,
                ) as process_call,
                self.assertRaises(BuildError) as raised,
            ):
                self.adapter(root, delegate).build(request, authorization)

            commands = [call.args[0] for call in process_call.call_args_list]
            self.assertEqual(
                raised.exception.code, "builder.cpp_generated_source_rejected"
            )
            self.assertEqual(len(delegate.calls), 1)
            self.assertTrue(
                any(
                    "--nobuild" in command and "//..." in command
                    for command in commands
                )
            )
            self.assertTrue(any("query" in command for command in commands))
            self.assertTrue(
                any(
                    "--nobuild" not in command
                    and "build" in command
                    and "//..." in command
                    for command in commands
                )
            )

    def test_generic_bazel_build_failure_remains_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _Delegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            def fail_full_build(command, *, cwd, **kwargs):
                result = self.successful_bazel(command, cwd=cwd, **kwargs)
                if (
                    "--nobuild" not in command
                    and "build" in command
                    and "//..." in command
                ):
                    return BoundedProcessResult(1, b"", b"executor unavailable")
                return result

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=fail_full_build,
                ),
                self.assertRaises(BuildError) as raised,
            ):
                self.adapter(root, delegate).build(request, authorization)

            self.assertEqual(raised.exception.code, "builder.bazel_build_failed")
            self.assertEqual(len(delegate.calls), 1)

    def test_requires_explicit_evidence_output_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _Delegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization(
                include_evidence=False
            )

            with patch.object(
                bazel_builder_module, "_run_bounded_process"
            ) as process_call:
                with self.assertRaises(BuildError) as error:
                    self.adapter(root, delegate).build(request, authorization)

            self.assertEqual(
                error.exception.code, "builder.bazel_evidence_not_authorized"
            )
            process_call.assert_not_called()
            self.assertEqual(delegate.calls, [])

    def test_nonzero_graph_query_rejects_partial_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _Delegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            def partial_graph(command, *, cwd, **kwargs):
                result = self.successful_bazel(command, cwd=cwd, **kwargs)
                if "graph" in command and "--lockfile_mode=error" in command:
                    return BoundedProcessResult(7, GRAPH_BYTES, b"failed")
                return result

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=partial_graph,
                ),
            ):
                with self.assertRaises(BuildError) as error:
                    self.adapter(root, delegate).build(request, authorization)

            self.assertEqual(error.exception.code, "builder.bazel_graph_failed")
            self.assertEqual(delegate.calls, [])

    def test_rejects_lock_mutation_during_bazel_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = _Delegate(root / "delegate-artifact")
            request, authorization = self.request_and_authorization()

            def mutating_build(command, *, cwd, **kwargs):
                result = self.successful_bazel(command, cwd=cwd, **kwargs)
                if "build" in command and "//..." in command:
                    (cwd / "MODULE.bazel.lock").write_bytes(b'{"lockFileVersion":29}\n')
                return result

            with (
                patch.object(BazelToolchain, "require_unchanged"),
                patch.object(
                    bazel_builder_module,
                    "_run_bounded_process",
                    side_effect=mutating_build,
                ),
            ):
                with self.assertRaises(BuildError) as error:
                    self.adapter(root, delegate).build(request, authorization)

            self.assertEqual(error.exception.code, "builder.bazel_lock_changed")
            self.assertEqual(delegate.calls, [])

    def test_repository_evidence_requires_a_registry_record_per_module(self) -> None:
        with self.assertRaises(BuildError) as error:
            _, _, modules = bazel_builder_module._normalized_module_graph(GRAPH_BYTES)
            bazel_builder_module._normalized_repositories(b"", modules=modules)

        self.assertEqual(error.exception.code, "builder.bazel_repositories_incomplete")

    def test_only_named_platforms_module_uses_unversioned_well_known_repo(self) -> None:
        evidence = REPOSITORY_BYTES.replace(b"rules_python+", b"platforms")
        for version in ("1.0.0", "1.1.0"):
            with self.subTest(version=version):
                platforms = BzlmodModule(
                    f"platforms@{version}", "platforms", version, ()
                )
                versioned_evidence = evidence.replace(
                    b"rules_python/2.2.0", f"platforms/{version}".encode()
                )
                bazel_builder_module._normalized_repositories(
                    versioned_evidence,
                    modules=(platforms,),
                )

        other = BzlmodModule("other@1.0.0", "other", "1.0.0", ())
        other_evidence = evidence.replace(b"platforms", b"other").replace(
            b"rules_python/2.2.0", b"other/1.0.0"
        )
        with self.assertRaises(BuildError) as error:
            bazel_builder_module._normalized_repositories(
                other_evidence,
                modules=(other,),
            )

        self.assertEqual(
            error.exception.code,
            "builder.bazel_repositories_incomplete",
        )

    def test_delegate_artifact_is_verified_before_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "link").symlink_to(root)

            with self.assertRaises(BuildError):
                bazel_builder_module._copy_delegate_artifact(
                    artifact, root / "composite"
                )


if __name__ == "__main__":
    unittest.main()
