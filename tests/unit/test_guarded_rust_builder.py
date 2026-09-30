"""The Rust compiler cannot start without exact host-build authorization."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import rust as rust_builder_module
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    canonical_tree_digest,
    executable_file_digest,
)
from literate_ai.adapters.builders.rust import (
    GuardedRustBuilder,
    RustToolchain,
    discover_rust_toolchain,
)
from literate_ai.ports import BuildInputConsumption
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 3, tzinfo=UTC)


class GuardedRustBuilderTests(unittest.TestCase):
    @staticmethod
    def toolchain() -> RustToolchain:
        try:
            return discover_rust_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @staticmethod
    def request_and_authorization(
        source: Path,
        toolchain: RustToolchain,
        *,
        toolchain_digest: str | None = None,
        sandbox_profile: str = UNSANDBOXED_HOST_BUILD_PROFILE,
        requested_privileges: tuple[str, ...] = UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    ):
        source_digest = canonical_tree_digest(source)
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
            builder_id=GuardedRustBuilder.builder_id,
            toolchain_digest=toolchain_digest or toolchain.identity,
            sandbox_profile=sandbox_profile,
            requested_privileges=requested_privileges,
            allowed_outputs=("native-executable",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="Rust conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_discovery_binds_command_version_and_executable_bytes(self) -> None:
        toolchain = self.toolchain()

        self.assertTrue(Path(toolchain.command[0]).is_absolute())
        self.assertEqual(
            toolchain.launcher_executable,
            str(Path(toolchain.command[0]).resolve(strict=True)),
        )
        self.assertIn("rustc", toolchain.version)
        self.assertEqual(
            toolchain.launcher_digest,
            executable_file_digest(Path(toolchain.launcher_executable)),
        )
        self.assertEqual(
            toolchain.compiler_digest,
            executable_file_digest(Path(toolchain.compiler_executable)),
        )
        self.assertTrue(toolchain.identity.startswith("sha256:"))
        with_argument = RustToolchain(
            (*toolchain.command, "--cfg", "literate_ai_identity_probe"),
            toolchain.launcher_executable,
            toolchain.launcher_digest,
            toolchain.compiler_executable,
            toolchain.compiler_digest,
            toolchain.version,
        )
        self.assertNotEqual(with_argument.identity, toolchain.identity)

    def test_valid_authorization_builds_runnable_native_executable(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text(
                'fn main() { println!("{{\\"answer\\":42}}"); }\n',
                encoding="utf-8",
            )
            tests = source / "tests"
            tests.mkdir()
            (tests / "manifest.json").write_text("{}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )

            artifact = GuardedRustBuilder(
                toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=generated,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
                lifecycle_consumed_files=("source/tests/manifest.json",),
            )

            executable = artifact.artifact_path / artifact.executable_file
            completed = subprocess.run(
                [str(executable)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout, b'{"answer":42}\n')
            self.assertEqual(artifact.compiler_identity, toolchain.identity)
            self.assertEqual(
                artifact.lifecycle_consumed_files,
                ("source/tests/manifest.json",),
            )

    def test_referenced_modules_and_included_assets_are_compiler_inputs(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            data = source / "fixture data"
            data.mkdir(parents=True)
            (source / "main.rs").write_text(
                "mod support;\n"
                'const MESSAGE: &str = include_str!("fixture data/message #1.txt");\n'
                'fn main() { println!("{}:{}", support::answer(), '
                "MESSAGE.trim()); }\n",
                encoding="utf-8",
            )
            (source / "support.rs").write_text(
                "pub fn answer() -> u8 { 42 }\n", encoding="utf-8"
            )
            (data / "message #1.txt").write_text("compiled\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )

            artifact = GuardedRustBuilder(
                toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=generated,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            expected_inputs = (
                "source/fixture data/message #1.txt",
                "source/main.rs",
                "source/support.rs",
            )
            self.assertEqual(artifact.consumed_source_files, expected_inputs)
            manifest = json.loads(
                (artifact.artifact_path / "build-manifest.json").read_text()
            )
            self.assertEqual(manifest["consumed_source_files"], list(expected_inputs))
            completed = subprocess.run(
                [str(artifact.artifact_path / artifact.executable_file)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout, b"42:compiled\n")

    def test_unconsumed_generated_file_is_rejected(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            (source / "dead.rs").write_text(
                "pub fn never_compiled() {}\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )

            with self.assertRaises(BuildError) as error:
                GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=generated,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(error.exception.code, "builder.generated_file_unconsumed")
            self.assertIn("source/dead.rs", str(error.exception))
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_typed_build_system_consumption_covers_only_exact_bazel_inputs(
        self,
    ) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            (source / "MODULE.bazel").write_text(
                'module(name = "fixture", version = "1.0.0")\n', encoding="utf-8"
            )
            (source / "BUILD.bazel").write_text(
                'rust_binary(name = "run", srcs = ["main.rs"])\n',
                encoding="utf-8",
            )
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            consumption = BuildInputConsumption(
                consumer_id="bazel/bzlmod@1",
                source_bundle_digest=request.source_bundle_digest,
                files=(
                    "source/BUILD.bazel",
                    "source/MODULE.bazel",
                    "source/main.rs",
                ),
            )

            artifact = GuardedRustBuilder(
                toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=generated,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
                build_input_consumption=consumption,
            )

            self.assertEqual(
                artifact.build_system_consumed_files,
                (
                    "source/BUILD.bazel",
                    "source/MODULE.bazel",
                    "source/main.rs",
                ),
            )
            self.assertEqual(
                artifact.build_input_consumption_identity, consumption.identity
            )
            manifest = json.loads(
                (artifact.artifact_path / "build-manifest.json").read_text()
            )
            self.assertEqual(
                manifest["build_input_consumption_identity"], consumption.identity
            )

    def test_build_system_consumption_must_bind_the_same_source_tree(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            consumption = BuildInputConsumption(
                consumer_id="bazel/bzlmod@1",
                source_bundle_digest=DIGEST_A,
                files=("source/main.rs",),
            )

            with self.assertRaises(BuildError) as error:
                GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=generated,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                    build_input_consumption=consumption,
                )

            self.assertEqual(
                error.exception.code, "builder.build_input_consumption_mismatch"
            )

    def test_build_requires_exact_rust_toolchain(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain, toolchain_digest=DIGEST_A
            )

            with patch.object(
                rust_builder_module, "_run_bounded_process"
            ) as compiler_call:
                with self.assertRaisesRegex(BuildError, "another Rust toolchain"):
                    GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            compiler_call.assert_not_called()

    def test_builder_rejects_falsely_constrained_host_request(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated,
                toolchain,
                sandbox_profile="constrained",
                requested_privileges=("compiler",),
            )

            with patch.object(
                rust_builder_module, "_run_bounded_process"
            ) as compiler_call:
                with self.assertRaisesRegex(BuildError, "unsandboxed-host") as error:
                    GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.unsandboxed_host_profile_required"
            )
            compiler_call.assert_not_called()

    def test_compiler_bytes_are_checked_before_and_after_compilation(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            changed = BuildError(
                "builder.rust_toolchain_changed",
                "Rust compiler bytes changed after toolchain selection",
            )
            with patch.object(
                RustToolchain, "require_unchanged", side_effect=(None, changed)
            ) as unchanged_call:
                with self.assertRaisesRegex(BuildError, "bytes changed") as error:
                    GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(error.exception.code, "builder.rust_toolchain_changed")
            self.assertEqual(unchanged_call.call_count, 2)

    def test_selected_sysroot_compiler_bytes_are_part_of_the_live_check(self) -> None:
        toolchain = self.toolchain()
        real_digest = rust_builder_module.executable_file_digest

        def changed_compiler(path: Path) -> str:
            if path == Path(toolchain.compiler_executable):
                return DIGEST_A
            return real_digest(path)

        with patch.object(
            rust_builder_module,
            "executable_file_digest",
            side_effect=changed_compiler,
        ):
            with self.assertRaisesRegex(BuildError, "bytes changed") as error:
                toolchain.require_unchanged()
        self.assertEqual(error.exception.code, "builder.rust_toolchain_changed")

    def test_source_drift_during_compilation_is_rejected(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            target = source / "main.rs"
            target.write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            real_run = rust_builder_module._run_bounded_process

            def compile_then_mutate(*args, **kwargs):
                result = real_run(*args, **kwargs)
                command = args[0]
                if "source/main.rs" in command:
                    target.write_text("fn main() { panic!(); }\n", encoding="utf-8")
                return result

            with patch.object(
                rust_builder_module,
                "_run_bounded_process",
                side_effect=compile_then_mutate,
            ):
                with self.assertRaisesRegex(
                    BuildError, "changed during compilation"
                ) as error:
                    GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )

    def test_compiler_output_is_bounded(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        script = (
            "import os\n"
            "chunk = b'x' * 4096\n"
            "while True:\n"
            "    os.write(1, chunk)\n"
            "    os.write(2, chunk)\n"
        )
        toolchain = RustToolchain(
            (str(executable), "-c", script),
            str(executable),
            executable_file_digest(executable),
            str(executable),
            executable_file_digest(executable),
            "adversarial fixture rustc",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )

            with patch.object(RustToolchain, "require_unchanged", return_value=None):
                with self.assertRaises(BuildError) as error:
                    GuardedRustBuilder(
                        toolchain,
                        AuthorizationRevocationSet(),
                        stdout_limit_bytes=1024,
                        stderr_limit_bytes=1024,
                    ).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(error.exception.code, "builder.rust_compile_output_limit")

    def test_cached_artifact_is_verified_before_reuse(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            builder = GuardedRustBuilder(toolchain, AuthorizationRevocationSet())
            artifact = builder.build(
                request,
                authorization,
                source_root=generated,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            executable = artifact.artifact_path / artifact.executable_file
            original_executable = executable.read_bytes()
            executable.write_bytes(b"tampered")
            real_run = rust_builder_module._run_bounded_process

            def compile_with_stable_executable(command, **kwargs):
                completed = real_run(command, **kwargs)
                if "--emit=link,dep-info" in command:
                    output_root = Path(command[command.index("--out-dir") + 1])
                    (output_root / artifact.executable_file).write_bytes(
                        original_executable
                    )
                return completed

            # Linkers are not required to emit byte-identical binaries across two
            # invocations (notably PE/COFF on Windows). Stabilize the new candidate
            # so this test exercises verification of the existing cache entry.
            with patch.object(
                rust_builder_module,
                "_run_bounded_process",
                side_effect=compile_with_stable_executable,
            ):
                with self.assertRaisesRegex(BuildError, "Existing artifact differs"):
                    builder.build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

    def test_staged_artifact_rejects_undeclared_output_before_publish(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            source = generated / "source"
            source.mkdir(parents=True)
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                generated, toolchain
            )
            real_match = rust_builder_module._cached_rust_artifact_matches

            def inject_undeclared_output(staging, *args, **kwargs):
                (staging / "undeclared.txt").write_text("rogue\n", encoding="utf-8")
                return real_match(staging, *args, **kwargs)

            with patch.object(
                rust_builder_module,
                "_cached_rust_artifact_matches",
                side_effect=inject_undeclared_output,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedRustBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=generated,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_invalid")
            self.assertEqual(list((root / "artifacts").iterdir()), [])


class RustToolchainProcessTests(unittest.TestCase):
    def test_discovery_enforces_version_output_cap(self) -> None:
        executable = str(Path(sys.executable).resolve(strict=True))
        script = "__import__('os').write(1,b'x'*4096)"
        command = (
            subprocess.list2cmdline([executable, "-c", script])
            if os.name == "nt"
            else shlex.join([executable, "-c", script])
        )
        environment = dict(os.environ)
        environment["RUSTC"] = command

        with self.assertRaises(BuildError) as error:
            discover_rust_toolchain(
                environment,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            )
        self.assertEqual(error.exception.code, "builder.rust_version_output_limit")


if __name__ == "__main__":
    unittest.main()
