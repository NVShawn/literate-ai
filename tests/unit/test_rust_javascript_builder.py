"""Exact composite Rust/JavaScript build and lifecycle-adapter tests."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    GuardedJavaScriptBuilder,
    GuardedRustBuilder,
    GuardedRustJavaScriptBuilder,
    RustJavaScriptToolchain,
    canonical_tree_digest,
    controlled_node_environment,
    discover_rust_javascript_toolchain,
)
from literate_ai.adapters.builders import (
    rust_javascript as composite_builder_module,
)
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.lifecycle import (
    JavaScriptBuildAdapter,
    RustBuildAdapter,
    RustJavaScriptBuildAdapter,
)
from literate_ai.adapters.lifecycle import local as local_adapter_module
from literate_ai.ports import BuildInputConsumption
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 3, tzinfo=UTC)
FULL_STACK_OUTPUTS = ("javascript-checked-bundle", "native-executable")

RUST_SOURCE = """\
use std::env;

fn main() {
    let payload = env::args().nth(1).expect("one JSON argument");
    println!("{{\\\"echo\\\":{}}}", payload);
}
"""

JAVASCRIPT_SOURCE = """\
'use strict';
if (process.argv.length !== 3) {
  process.stderr.write('expected one canonical backend JSON argument');
  process.exit(2);
}
const output = { frontend: 'checked', backend: JSON.parse(process.argv[2]) };
process.stdout.write(JSON.stringify(output));
"""


def _materialize(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root.joinpath(*Path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


def _source_digest(files: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _materialize(root, files)
        return canonical_tree_digest(root)


def _request_and_authorization(
    *,
    source_digest: str,
    builder_id: str,
    toolchain_identity: str,
    allowed_outputs: tuple[str, ...],
) -> tuple[BuildRequest, object]:
    policy = SecurityPolicy(policy_digest=DIGEST_C)
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
    request = BuildRequest(
        effective_revision_digest=DIGEST_B,
        source_bundle_digest=source_digest,
        builder_id=builder_id,
        toolchain_digest=toolchain_identity,
        sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
        requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
        allowed_outputs=allowed_outputs,
    )
    authorization = policy.authorize_build(
        classification,
        request,
        actor="test",
        reason="composite host build test",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        yolo_acknowledged=True,
    )
    return request, authorization


class GuardedRustJavaScriptBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.toolchain = discover_rust_javascript_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @staticmethod
    def files() -> dict[str, str]:
        return {
            "source/backend/main.rs": RUST_SOURCE,
            "source/frontend/helper.js": "'use strict';\nmodule.exports = 1;\n",
            "source/frontend/main.js": JAVASCRIPT_SOURCE,
        }

    def request_and_authorization(
        self,
        source: Path,
        *,
        toolchain: RustJavaScriptToolchain | None = None,
    ):
        return _request_and_authorization(
            source_digest=canonical_tree_digest(source),
            builder_id=GuardedRustJavaScriptBuilder.builder_id,
            toolchain_identity=(toolchain or self.toolchain).identity,
            allowed_outputs=FULL_STACK_OUTPUTS,
        )

    def test_lifecycle_adapter_builds_one_strict_runnable_artifact(self) -> None:
        files = {
            **self.files(),
            "source/.literate/sbom.cdx.json": "{}\n",
            "source/tests/manifest.json": "{}\n",
        }
        request, authorization = _request_and_authorization(
            source_digest=_source_digest(files),
            builder_id=GuardedRustJavaScriptBuilder.builder_id,
            toolchain_identity=self.toolchain.identity,
            allowed_outputs=FULL_STACK_OUTPUTS,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = RustJavaScriptBuildAdapter(
                artifact_store=root / "artifacts",
                toolchain=self.toolchain,
                clock=lambda: NOW + timedelta(minutes=1),
                authorization_verifier=AuthorizationRevocationSet(),
            ).build(
                {
                    **request.to_dict(),
                    "artifact": {
                        "files": files,
                        "generated_test_suite_path": "source/tests/manifest.json",
                        "source_sbom_path": "source/.literate/sbom.cdx.json",
                    },
                },
                authorization.to_dict(),
            )

            artifact = Path(str(result["artifact_path"]))
            expected_files = {
                "backend/sample.exe" if os.name == "nt" else "backend/sample",
                "build-manifest.json",
                "frontend/helper.js",
                "frontend/main.js",
            }
            self.assertEqual(
                {
                    path.relative_to(artifact).as_posix()
                    for path in artifact.rglob("*")
                    if path.is_file()
                },
                expected_files,
            )
            self.assertEqual(
                result["source_bundle_digest"], request.source_bundle_digest
            )
            self.assertEqual(result["authorization_id"], authorization.authorization_id)
            self.assertEqual(result["compiler_identity"], self.toolchain.rust.identity)
            self.assertEqual(result["runtime_identity"], self.toolchain.node.identity)
            self.assertEqual(result["toolchain_identity"], self.toolchain.identity)
            self.assertEqual(
                result["lifecycle_consumed_files"],
                [
                    "source/tests/manifest.json",
                    "source/.literate/sbom.cdx.json",
                ],
            )
            self.assertEqual(
                result["runtime_command"], list(self.toolchain.node.command)
            )
            self.assertEqual(result["frontend_entrypoint_file"], "frontend/main.js")
            self.assertEqual(
                result["consumed_backend_files"], ["source/backend/main.rs"]
            )
            self.assertEqual(
                result["checked_frontend_files"],
                ["source/frontend/helper.js", "source/frontend/main.js"],
            )
            self.assertEqual(result["build_system_consumed_files"], [])
            self.assertIsNone(result["build_input_consumption_identity"])
            backend_completed = subprocess.run(
                [
                    str(artifact / str(result["backend_executable_file"])),
                    '{"value":7}',
                ],
                cwd=artifact,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(
                backend_completed.returncode,
                0,
                backend_completed.stderr.decode(),
            )
            completed = subprocess.run(
                [
                    *self.toolchain.node.command,
                    str(artifact / str(result["frontend_entrypoint_file"])),
                    backend_completed.stdout.decode().strip(),
                ],
                cwd=artifact,
                env=controlled_node_environment(dict(os.environ)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(
                json.loads(completed.stdout),
                {"frontend": "checked", "backend": {"echo": {"value": 7}}},
            )

    def test_lifecycle_adapter_accepts_typed_outer_build_input_consumption(
        self,
    ) -> None:
        files = {
            **self.files(),
            "source/BUILD.bazel": "# consumed by outer Bazel build\n",
            "source/MODULE.bazel": 'module(name = "fixture", version = "1.0.0")\n',
        }
        source_digest = _source_digest(files)
        request, authorization = _request_and_authorization(
            source_digest=source_digest,
            builder_id=GuardedRustJavaScriptBuilder.builder_id,
            toolchain_identity=self.toolchain.identity,
            allowed_outputs=FULL_STACK_OUTPUTS,
        )
        consumption = BuildInputConsumption(
            consumer_id="bazel/bzlmod@1",
            source_bundle_digest=source_digest,
            files=("source/BUILD.bazel", "source/MODULE.bazel"),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = RustJavaScriptBuildAdapter(
                artifact_store=Path(directory) / "artifacts",
                toolchain=self.toolchain,
                clock=lambda: NOW + timedelta(minutes=1),
                authorization_verifier=AuthorizationRevocationSet(),
            ).build_with_input_consumption(
                {**request.to_dict(), "artifact": {"files": files}},
                authorization.to_dict(),
                consumption,
            )

        self.assertEqual(
            result["build_system_consumed_files"],
            ["source/BUILD.bazel", "source/MODULE.bazel"],
        )
        self.assertEqual(
            result["build_input_consumption_identity"], consumption.identity
        )

    def test_request_must_bind_the_exact_composite_toolchain(self) -> None:
        other_node = replace(
            self.toolchain.node,
            version=self.toolchain.node.version + "-other",
        )
        selected_other = RustJavaScriptToolchain(self.toolchain.rust, other_node)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated"
            _materialize(source, self.files())
            request, authorization = self.request_and_authorization(
                source, toolchain=selected_other
            )

            with patch.object(
                composite_builder_module, "_run_bounded_process"
            ) as process_call:
                with self.assertRaises(BuildError) as error:
                    GuardedRustJavaScriptBuilder(
                        self.toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.toolchain_mismatch")
            process_call.assert_not_called()

    def test_referenced_backend_module_is_recorded_as_consumed(self) -> None:
        files = self.files()
        files["source/backend/main.rs"] = (
            'mod response;\nfn main() { println!("{}", response::payload()); }\n'
        )
        files["source/backend/response.rs"] = (
            'pub fn payload() -> &\'static str { "module-compiled" }\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated"
            _materialize(source, files)
            request, authorization = self.request_and_authorization(source)

            artifact = GuardedRustJavaScriptBuilder(
                self.toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            expected_backend = (
                "source/backend/main.rs",
                "source/backend/response.rs",
            )
            expected_frontend = (
                "source/frontend/helper.js",
                "source/frontend/main.js",
            )
            self.assertEqual(artifact.consumed_backend_files, expected_backend)
            self.assertEqual(artifact.checked_frontend_files, expected_frontend)
            manifest = json.loads(
                (artifact.artifact_path / "build-manifest.json").read_text()
            )
            self.assertEqual(manifest["consumed_backend_files"], list(expected_backend))
            self.assertEqual(
                manifest["checked_frontend_files"], list(expected_frontend)
            )

    def test_unconsumed_generated_backend_file_is_rejected(self) -> None:
        files = self.files()
        files["source/backend/dead.rs"] = "pub fn never_compiled() {}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated"
            _materialize(source, files)
            request, authorization = self.request_and_authorization(source)

            with self.assertRaises(BuildError) as error:
                GuardedRustJavaScriptBuilder(
                    self.toolchain, AuthorizationRevocationSet()
                ).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(error.exception.code, "builder.generated_file_unconsumed")
            self.assertIn("source/backend/dead.rs", str(error.exception))
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_whole_source_tree_drift_during_backend_compile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated"
            _materialize(source, self.files())
            request, authorization = self.request_and_authorization(source)
            target = source / "source" / "frontend" / "helper.js"
            real_run = composite_builder_module._run_bounded_process

            def compile_then_mutate(*args, **kwargs):
                completed = real_run(*args, **kwargs)
                target.write_text(
                    "'use strict';\nmodule.exports = 2;\n", encoding="utf-8"
                )
                return completed

            with patch.object(
                composite_builder_module,
                "_run_bounded_process",
                side_effect=compile_then_mutate,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedRustJavaScriptBuilder(
                        self.toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_tampered_cached_artifact_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated"
            _materialize(source, self.files())
            request, authorization = self.request_and_authorization(source)
            builder = GuardedRustJavaScriptBuilder(
                self.toolchain, AuthorizationRevocationSet()
            )
            artifact = builder.build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            backend = artifact.artifact_path.joinpath(
                *Path(artifact.backend_executable_file).parts
            )
            original_backend = backend.read_bytes()
            backend.write_bytes(b"tampered")
            real_run = composite_builder_module._run_bounded_process

            def compile_with_stable_backend(command, **kwargs):
                completed = real_run(command, **kwargs)
                if "--emit=link,dep-info" in command:
                    output_root = Path(command[command.index("--out-dir") + 1])
                    (
                        output_root / Path(artifact.backend_executable_file).name
                    ).write_bytes(original_backend)
                return completed

            # Make the second backend candidate byte-identical even when the host
            # linker embeds nondeterministic PE/COFF metadata. The existing entry's
            # platform-specific executable is then the exact tamper target.
            with patch.object(
                composite_builder_module,
                "_run_bounded_process",
                side_effect=compile_with_stable_backend,
            ):
                with self.assertRaises(BuildError) as error:
                    builder.build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_collision")


class LanguageLifecycleAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.toolchain = discover_rust_javascript_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def test_rust_adapter_materializes_whole_tree_and_maps_artifact(self) -> None:
        files = {
            "source/main.rs": "fn main() {}\n",
            "source/support.rs": "// support\n",
        }
        request, authorization = _request_and_authorization(
            source_digest=_source_digest(files),
            builder_id=GuardedRustBuilder.builder_id,
            toolchain_identity=self.toolchain.rust.identity,
            allowed_outputs=("native-executable",),
        )
        built = SimpleNamespace(
            artifact_digest="sha256:" + "d" * 64,
            artifact_path=Path("/sealed/rust"),
            source_bundle_digest=request.source_bundle_digest,
            authorization_id=authorization.authorization_id,
            executable_file="sample",
            compiler_identity=self.toolchain.rust.identity,
            consumed_source_files=("source/main.rs", "source/support.rs"),
            lifecycle_consumed_files=(),
            build_system_consumed_files=(),
            build_input_consumption_identity=None,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(local_adapter_module, "GuardedRustBuilder") as builder_type,
        ):

            def inspect_tree(*args, source_root: Path, **kwargs):
                self.assertEqual(
                    (source_root / "source" / "support.rs").read_text(), "// support\n"
                )
                return built

            builder_type.return_value.build.side_effect = inspect_tree
            result = RustBuildAdapter(
                artifact_store=Path(directory) / "artifacts",
                toolchain=self.toolchain.rust,
                authorization_verifier=AuthorizationRevocationSet(),
            ).build(
                {**request.to_dict(), "artifact": {"files": files}},
                authorization.to_dict(),
            )

        self.assertEqual(result["executable_file"], "sample")
        self.assertEqual(result["compiled_files"], ["sample"])
        self.assertEqual(result["compiler_identity"], self.toolchain.rust.identity)
        self.assertEqual(
            result["consumed_source_files"],
            ["source/main.rs", "source/support.rs"],
        )
        self.assertEqual(result["lifecycle_consumed_files"], [])
        self.assertEqual(result["build_system_consumed_files"], [])
        self.assertIsNone(result["build_input_consumption_identity"])

    def test_javascript_adapter_materializes_whole_tree_and_maps_artifact(self) -> None:
        files = {
            "source/main.js": "'use strict';\n",
            "source/helper.js": "module.exports = 1;\n",
        }
        request, authorization = _request_and_authorization(
            source_digest=_source_digest(files),
            builder_id=GuardedJavaScriptBuilder.builder_id,
            toolchain_identity=self.toolchain.node.identity,
            allowed_outputs=("javascript-checked-bundle",),
        )
        built = SimpleNamespace(
            artifact_digest="sha256:" + "d" * 64,
            artifact_path=Path("/sealed/javascript"),
            source_bundle_digest=request.source_bundle_digest,
            authorization_id=authorization.authorization_id,
            entrypoint_file="source/main.js",
            checked_files=("source/helper.js", "source/main.js"),
            runtime_command=self.toolchain.node.command,
            toolchain_identity=self.toolchain.node.identity,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                local_adapter_module, "GuardedJavaScriptBuilder"
            ) as builder_type,
        ):

            def inspect_tree(*args, source_root: Path, **kwargs):
                self.assertEqual(
                    (source_root / "source" / "helper.js").read_text(),
                    "module.exports = 1;\n",
                )
                return built

            builder_type.return_value.build.side_effect = inspect_tree
            result = JavaScriptBuildAdapter(
                artifact_store=Path(directory) / "artifacts",
                toolchain=self.toolchain.node,
                authorization_verifier=AuthorizationRevocationSet(),
            ).build(
                {**request.to_dict(), "artifact": {"files": files}},
                authorization.to_dict(),
            )

        self.assertEqual(result["entrypoint_file"], "source/main.js")
        self.assertEqual(
            result["compiled_files"], ["source/helper.js", "source/main.js"]
        )
        self.assertEqual(result["runtime_command"], list(self.toolchain.node.command))


if __name__ == "__main__":
    unittest.main()
