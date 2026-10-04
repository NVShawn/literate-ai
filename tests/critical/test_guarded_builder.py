"""The Python compiler cannot start without exact authorization."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    GuardedCppBuilder,
    GuardedPythonBuilder,
    canonical_tree_digest,
    discover_cpp_toolchain,
    discover_python_toolchain,
    executable_file_digest,
)
from literate_ai.adapters.builders import cpp as cpp_builder_module
from literate_ai.adapters.builders import python as python_builder_module
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)


class GuardedPythonBuilderTests(unittest.TestCase):
    @staticmethod
    def python_compile_calls(process_call):
        return [
            call
            for call in process_call.call_args_list
            if python_builder_module._PYTHON_COMPILE_SCRIPT in call.args[0]
        ]

    def request_and_authorization(
        self,
        source: Path,
        *,
        toolchain_digest: str | None = None,
        sandbox_profile: str = UNSANDBOXED_HOST_BUILD_PROFILE,
        requested_privileges: tuple[str, ...] = UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    ):
        source_digest = canonical_tree_digest(source)
        policy = SecurityPolicy(policy_digest=DIGEST_C)
        classification = policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=[
                OriginAttestation(
                    source_digest=source_digest,
                    signer="test",
                    trust_root="test",
                    signature_identity="test",
                    verified=True,
                )
            ],
            findings=[],
        )
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=GuardedPythonBuilder.builder_id,
            toolchain_digest=(toolchain_digest or discover_python_toolchain().identity),
            sandbox_profile=sandbox_profile,
            requested_privileges=requested_privileges,
            allowed_outputs=("python-bytecode",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_valid_authorization_builds_checked_hash_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text(
                "import json\n"
                "print(json.dumps({'value': 42}, sort_keys=True, "
                "separators=(',', ':')))\n",
                encoding="utf-8",
            )
            toolchain = discover_python_toolchain()
            request, authorization = self.request_and_authorization(
                source, toolchain_digest=toolchain.identity
            )
            artifact = GuardedPythonBuilder(
                AuthorizationRevocationSet(), toolchain=toolchain
            ).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            self.assertEqual(artifact.compiled_files, ("hello.pyc",))
            bytecode = artifact.artifact_path / "hello.pyc"
            self.assertTrue(bytecode.is_file())
            self.assertEqual(artifact.toolchain_identity, toolchain.identity)
            completed = subprocess.run(
                [*toolchain.command, "-I", "-S", str(bytecode)],
                cwd=root,
                env=python_builder_module.controlled_python_environment(
                    dict(os.environ)
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout.splitlines(), [b'{"value":42}'])

    def test_revoked_authorization_prevents_compiler_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            revocations = AuthorizationRevocationSet().revoke(
                authorization.authorization_id,
                actor="security",
                reason="fixture compromised",
            )

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(AuthorizationError, "revoked"):
                    GuardedPythonBuilder(revocations).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(self.python_compile_calls(process_call), [])


class GuardedCppBuilderTests(unittest.TestCase):
    @staticmethod
    def toolchain():
        try:
            return discover_cpp_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @staticmethod
    def request_and_authorization(source: Path, toolchain):
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
            builder_id=GuardedCppBuilder.builder_id,
            toolchain_digest=toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("native-executable",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_valid_authorization_builds_native_executable(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int answer();\nint main() { return answer() == 42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (source / "answer.cpp").write_text(
                "int answer() { return 42; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            artifact = GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            self.assertTrue(
                (artifact.artifact_path / artifact.executable_file).is_file()
            )
            self.assertEqual(artifact.compiler_identity, toolchain.identity)
            self.assertEqual(
                toolchain.executable_digest,
                executable_file_digest(Path(toolchain.command[0])),
            )
            completed = subprocess.run(
                [str(artifact.artifact_path / artifact.executable_file)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_source_drift_during_native_compilation_is_rejected(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "main.cpp"
            target.write_text("int main() { return 0; }\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source, toolchain)
            real_run = cpp_builder_module._run_bounded_process

            def compile_then_mutate(*args, **kwargs):
                result = real_run(*args, **kwargs)
                target.write_text("int main() { return 1; }\n", encoding="utf-8")
                return result

            with patch.object(
                cpp_builder_module,
                "_run_bounded_process",
                side_effect=compile_then_mutate,
            ):
                with self.assertRaisesRegex(
                    BuildError, "changed during compilation"
                ) as error:
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )

    def test_staged_native_artifact_rejects_undeclared_output_before_publish(
        self,
    ) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            real_match = cpp_builder_module._cached_native_artifact_matches

            def inject_undeclared_output(staging, *args, **kwargs):
                (staging / "undeclared.txt").write_text("rogue\n", encoding="utf-8")
                return real_match(staging, *args, **kwargs)

            with patch.object(
                cpp_builder_module,
                "_cached_native_artifact_matches",
                side_effect=inject_undeclared_output,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_invalid")
            self.assertEqual(list((root / "artifacts").iterdir()), [])


class BoundedCompilerProcessTests(unittest.TestCase):
    def test_timeout_kills_compiler_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "descendant-survived"
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "time.sleep(1)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n",
                encoding="utf-8",
            )
            parent = (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(BuildError) as error:
                cpp_builder_module._run_bounded_process(
                    [sys.executable, "-c", parent, str(child), str(sentinel)],
                    cwd=root,
                    environment=dict(os.environ),
                    timeout_seconds=0.2,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                    error_prefix="builder.test_compiler",
                )
            self.assertEqual(error.exception.code, "builder.test_compiler_timeout")
            time.sleep(1.1)
            self.assertFalse(
                sentinel.exists(), "compiler descendant escaped termination"
            )


if __name__ == "__main__":
    unittest.main()
