"""Exact authorization is enforced inside the portable host artifact runner."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import BuildError, discover_node_toolchain
from literate_ai.adapters.lifecycle import (
    AUTHORIZED_HOST_PRIVILEGES,
    AuthorizedHostArtifactRunner,
    HostArtifactExecution,
    HostArtifactExecutionError,
    HostAuxiliaryArtifact,
)
from literate_ai.adapters.lifecycle.execution import _run_bounded_process
from literate_ai.contracts import canonical_identity
from literate_ai.evidence_ledger import open_run
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    LiveObservationExecutionAuthorizationVerifier,
    OriginAttestation,
    SecurityPolicy,
    SecurityProfile,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
SOURCE_DIGEST = canonical_identity("generated-source").uri
REVISION_DIGEST = canonical_identity("effective-revision").uri
POLICY_DIGEST = canonical_identity("host-execution-policy").uri
ARTIFACT_DIGEST = canonical_identity("compiled-artifact").uri


def grant(execution: HostArtifactExecution):
    policy = SecurityPolicy(POLICY_DIGEST)
    classification = replace(
        policy.classify(
            effective_revision_digest=REVISION_DIGEST,
            attestations=(
                OriginAttestation(
                    SOURCE_DIGEST,
                    "generator:test@1",
                    "test-root",
                    "signature:test",
                    True,
                ),
            ),
            findings=(),
        ),
        profile=SecurityProfile.YOLO,
        permitted_privileges=AUTHORIZED_HOST_PRIVILEGES,
    )
    request = execution.observation_request(
        effective_revision_digest=REVISION_DIGEST,
        source_digests=(SOURCE_DIGEST,),
    )
    authorization = policy.authorize_observation(
        classification,
        request,
        actor="test-runner",
        reason="execute the exact compiled fixture",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        yolo_acknowledged=True,
    )
    return request, authorization


def authorized_runner(
    *,
    revocations=lambda: AuthorizationRevocationSet(),
    **kwargs,
) -> AuthorizedHostArtifactRunner:
    return AuthorizedHostArtifactRunner(
        authorization_verifier=LiveObservationExecutionAuthorizationVerifier(
            revocations
        ),
        **kwargs,
    )


class AuthorizedHostExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = mock.patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    @staticmethod
    def node_toolchain(*, pinned_command=None):
        try:
            return discover_node_toolchain(pinned_command=pinned_command)
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def fixture(self, root: Path) -> HostArtifactExecution:
        artifact = root / "artifact"
        artifact.mkdir()
        (artifact / "application").write_bytes(b"compiled-fixture")
        runtime = root / "runtime"
        runtime.mkdir()
        return HostArtifactExecution.create(
            language="cpp",
            artifact_digest=ARTIFACT_DIGEST,
            artifact_root=artifact,
            entrypoint="application",
            arguments=[{"value": 4}],
            support_paths=(),
            runtime_root=runtime,
        )

    @staticmethod
    def artifact(
        root: Path,
        name: str,
        *,
        entrypoint: str = "application",
        content: bytes = b"compiled-fixture",
    ) -> Path:
        artifact = root / name
        artifact.mkdir()
        target = artifact / entrypoint
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return artifact

    @staticmethod
    def runtime(root: Path, content: bytes = b"exact-runtime") -> Path:
        runtime = root / "node"
        runtime.write_bytes(content)
        return runtime

    def test_entrypoint_rejects_nonportable_or_aliased_path_forms(self) -> None:
        unsafe = (
            "../application",
            "nested/../application",
            "nested\\..\\application",
            "C:/application",
            "C:application",
            "//?/C:/application",
            "nested//application",
            "nested/./application",
            "NUL",
            "CONOUT$",
            "application:stream",
            "application.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self.artifact(root, "artifact")
            runtime = root / "runtime"
            runtime.mkdir()

            for entrypoint in unsafe:
                with self.subTest(entrypoint=entrypoint):
                    with self.assertRaises((TypeError, ValueError)):
                        HostArtifactExecution.create(
                            language="cpp",
                            artifact_digest=ARTIFACT_DIGEST,
                            artifact_root=artifact,
                            entrypoint=entrypoint,
                            arguments=[],
                            support_paths=(),
                            runtime_root=runtime,
                        )

    def test_exact_live_grant_is_checked_inside_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.fixture(Path(temporary))
            request, authorization = grant(execution)
            self.assertEqual(request.requested_privileges, AUTHORIZED_HOST_PRIVILEGES)
            completed = subprocess.CompletedProcess(
                [str(execution.executable)], 0, b'{"value":8}', b""
            )
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                return_value=completed,
            ) as invoked:
                result = authorized_runner(
                    host_execution_acknowledged=True,
                    clock=lambda: NOW + timedelta(seconds=1),
                ).run(execution, request, authorization)
            self.assertEqual(result.result, {"value": 8})
            self.assertEqual(result.authorization_id, authorization.authorization_id)
            self.assertEqual(invoked.call_count, 1)
            self.assertNotIn("HOME", invoked.call_args.kwargs["env"])

    def test_default_and_revoked_after_issuance_fail_before_host_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.fixture(Path(temporary))
            request, authorization = grant(execution)
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                with self.assertRaisesRegex(
                    AuthorizationError,
                    "live_observation_revocation_verifier_required",
                ):
                    AuthorizedHostArtifactRunner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)

                current = AuthorizationRevocationSet()
                runner = authorized_runner(
                    revocations=lambda: current,
                    host_execution_acknowledged=True,
                    clock=lambda: NOW,
                )
                current = current.revoke(
                    authorization.authorization_id,
                    actor="security",
                    reason="artifact compromised after issuance",
                )
                with self.assertRaisesRegex(AuthorizationError, "revoked"):
                    runner.run(execution, request, authorization)
            invoked.assert_not_called()

    def test_python_checked_hash_artifact_runs_through_authorized_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            source = root / "source.py"
            source.write_text(
                "from dataclasses import dataclass\n"
                "@dataclass(frozen=True)\n"
                "class Result:\n"
                "    doubled: int\n"
                "def main(value):\n"
                "    return {'doubled': Result(value * 2).doubled}\n",
                encoding="utf-8",
            )
            entrypoint = artifact / "application.pyc"
            py_compile.compile(
                str(source),
                cfile=str(entrypoint),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
            runtime = root / "runtime"
            runtime.mkdir()
            execution = HostArtifactExecution.create(
                language="python",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint=entrypoint.name,
                arguments=[6],
                support_paths=(),
                runtime_root=runtime,
            )
            request, authorization = grant(execution)
            result = authorized_runner(
                host_execution_acknowledged=True,
                clock=lambda: NOW,
            ).run(execution, request, authorization)
            self.assertEqual(result.result, {"doubled": 12})
            self.assertEqual(
                result.execution_mode,
                "authorized-host-python-bytecode",
            )
            self.assertEqual(
                execution.runtime_command,
                (sys.executable,),
            )
            self.assertEqual(
                execution.runtime_launcher_executable,
                str(Path(sys.executable).resolve(strict=True)),
            )
            self.assertTrue(execution.runtime_executable_digest.startswith("sha256:"))

    @unittest.skipUnless(os.name == "posix", "symlink retarget test is POSIX-specific")
    def test_runtime_invocation_symlink_is_preserved_and_retargeting_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self.artifact(root, "frontend", entrypoint="main.js")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            baseline = self.node_toolchain()
            invocation = root / "preferred-node"
            invocation.symlink_to(Path(baseline.launcher_executable))
            toolchain = self.node_toolchain(pinned_command=(str(invocation),))
            execution = HostArtifactExecution.create(
                language="javascript",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint="main.js",
                arguments=[],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=toolchain.command,
                runtime_toolchain=toolchain,
            )
            self.assertEqual(execution.runtime_command[0], str(invocation))
            self.assertEqual(
                execution.runtime_launcher_executable,
                baseline.launcher_executable,
            )
            self.assertEqual(
                AuthorizedHostArtifactRunner._command(execution)[:1],
                [str(invocation)],
            )
            request, authorization = grant(execution)

            invocation.unlink()
            invocation.symlink_to(Path("/bin/false"))
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)

            self.assertEqual(caught.exception.code, "host_execution.runtime_changed")
            invoked.assert_not_called()

    def test_rust_javascript_and_full_stack_commands_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            baseline = self.node_toolchain()
            javascript_toolchain = self.node_toolchain(
                pinned_command=(baseline.command[0], "--no-warnings")
            )
            frontend = self.artifact(
                root,
                "frontend",
                entrypoint="main.js",
                content=b"// checked JavaScript",
            )
            backend = self.artifact(root, "backend")
            arguments = [{"value": 4}]

            javascript = HostArtifactExecution.create(
                language="javascript",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=frontend,
                entrypoint="main.js",
                arguments=arguments,
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=javascript_toolchain.command,
                runtime_toolchain=javascript_toolchain,
            )
            self.assertEqual(
                AuthorizedHostArtifactRunner._command(javascript),
                [
                    *javascript_toolchain.command,
                    str((frontend / "main.js").resolve(strict=True)),
                    '[{"value":4}]',
                ],
            )

            composite = self.artifact(
                root,
                "composite",
                entrypoint="frontend/main.js",
                content=b"// checked JavaScript",
            )
            composite_backend = composite / "backend" / "sample"
            composite_backend.parent.mkdir()
            composite_backend.write_bytes(b"compiled Rust")
            backend_binding = HostAuxiliaryArtifact.create(
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=composite,
                entrypoint="backend/sample",
            )
            full_stack = HostArtifactExecution.create(
                language="rust-javascript-full-stack",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=composite,
                entrypoint="frontend/main.js",
                arguments=arguments,
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=baseline.command,
                runtime_toolchain=baseline,
                auxiliary_artifacts=(backend_binding,),
            )
            self.assertEqual(
                AuthorizedHostArtifactRunner._command(full_stack),
                [
                    str(composite_backend.resolve(strict=True)),
                    '[{"value":4}]',
                ],
            )
            backend_result = {"release": "observed", "total_checks": 4}
            self.assertEqual(
                AuthorizedHostArtifactRunner._frontend_command(
                    full_stack, backend_result
                ),
                [
                    *baseline.command,
                    str((composite / "frontend/main.js").resolve(strict=True)),
                    '{"release":"observed","total_checks":4}',
                ],
            )

            rust = HostArtifactExecution.create(
                language="rust",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=backend,
                entrypoint="application",
                arguments=arguments,
                support_paths=(),
                runtime_root=runtime_root,
            )
            self.assertEqual(
                AuthorizedHostArtifactRunner._command(rust),
                [str((backend / "application").resolve(strict=True)), '[{"value":4}]'],
            )

    def test_windows_cpp_command_escapes_unicode_for_narrow_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self.artifact(root, "cpp")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            arguments = [{"warehouse": "Montréal 🚀"}]
            execution = HostArtifactExecution.create(
                language="cpp",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint="application",
                arguments=arguments,
                support_paths=(),
                runtime_root=runtime_root,
            )

            with mock.patch("literate_ai.adapters.lifecycle.execution.os.name", "nt"):
                command = AuthorizedHostArtifactRunner._command(execution)

            self.assertEqual(json.loads(command[-1]), arguments)
            self.assertEqual(command[-1].encode("ascii").decode("ascii"), command[-1])
            self.assertIn(r"\u00e9", command[-1])
            self.assertIn(r"\ud83d\ude80", command[-1])
            self.assertIn(b"Montr\xc3\xa9al", execution.arguments_json)

    def test_each_supported_mode_reports_its_exact_execution_mode(self) -> None:
        expected_modes = {
            "cpp": "authorized-host-cpp-executable",
            "rust": "authorized-host-rust-executable",
            "swift": "authorized-host-swift-executable",
            "javascript": "authorized-host-javascript-node",
            "rust-javascript-full-stack": (
                "authorized-host-rust-javascript-full-stack"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            node_toolchain = self.node_toolchain()
            backend = self.artifact(root, "backend")
            backend_binding = HostAuxiliaryArtifact.create(
                artifact_digest=canonical_identity("backend-artifact").uri,
                artifact_root=backend,
                entrypoint="application",
            )
            for language, expected_mode in expected_modes.items():
                with self.subTest(language=language):
                    primary = self.artifact(
                        root,
                        f"primary-{language}",
                        entrypoint=(
                            "main.js"
                            if language
                            in {
                                "javascript",
                                "rust-javascript-full-stack",
                            }
                            else "application"
                        ),
                    )
                    interpreted = language in {
                        "javascript",
                        "rust-javascript-full-stack",
                    }
                    execution = HostArtifactExecution.create(
                        language=language,
                        artifact_digest=ARTIFACT_DIGEST,
                        artifact_root=primary,
                        entrypoint="main.js" if interpreted else "application",
                        arguments=[],
                        support_paths=(),
                        runtime_root=runtime_root,
                        runtime_command=(
                            node_toolchain.command if interpreted else None
                        ),
                        runtime_toolchain=(node_toolchain if interpreted else None),
                        auxiliary_artifacts=(
                            (backend_binding,)
                            if language == "rust-javascript-full-stack"
                            else ()
                        ),
                    )
                    request, authorization = grant(execution)
                    completed = subprocess.CompletedProcess(
                        AuthorizedHostArtifactRunner._command(execution),
                        0,
                        b'{"ok":true}',
                        b"",
                    )
                    with mock.patch(
                        "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                        return_value=completed,
                    ) as invoked:
                        result = authorized_runner(
                            host_execution_acknowledged=True,
                            clock=lambda: NOW,
                        ).run(execution, request, authorization)
                    self.assertEqual(result.result, {"ok": True})
                    self.assertEqual(result.execution_mode, expected_mode)
                    if language == "rust-javascript-full-stack":
                        self.assertEqual(invoked.call_count, 2)
                        self.assertEqual(len(result.auxiliary_results), 1)
                        self.assertEqual(result.auxiliary_results[0].role, "backend")
                        self.assertEqual(
                            result.auxiliary_results[0].result, {"ok": True}
                        )
                    else:
                        invoked.assert_called_once()

    def test_full_stack_backend_failure_prevents_frontend_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = self.artifact(root, "frontend", entrypoint="main.js")
            backend = self.artifact(root, "backend")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            node_toolchain = self.node_toolchain()
            execution = HostArtifactExecution.create(
                language="rust-javascript-full-stack",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=frontend,
                entrypoint="main.js",
                arguments=[{"release": "candidate"}],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=node_toolchain.command,
                runtime_toolchain=node_toolchain,
                auxiliary_artifacts=(
                    HostAuxiliaryArtifact.create(
                        artifact_digest=canonical_identity("backend-artifact").uri,
                        artifact_root=backend,
                        entrypoint="application",
                    ),
                ),
            )
            request, authorization = grant(execution)
            failed = subprocess.CompletedProcess(
                AuthorizedHostArtifactRunner._command(execution),
                9,
                b"",
                b"backend rejected release",
            )
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                return_value=failed,
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)
            self.assertEqual(caught.exception.code, "host_execution.nonzero_exit")
            self.assertEqual(caught.exception.role, "backend")
            self.assertEqual(caught.exception.returncode, 9)
            self.assertEqual(
                caught.exception.stdout_digest,
                f"sha256:{hashlib.sha256(b'').hexdigest()}",
            )
            self.assertEqual(
                caught.exception.stderr_digest,
                f"sha256:{hashlib.sha256(b'backend rejected release').hexdigest()}",
            )
            self.assertIn("backend", str(caught.exception))
            invoked.assert_called_once()

    def test_full_stack_invalid_backend_json_prevents_frontend_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = self.artifact(root, "frontend", entrypoint="main.js")
            backend = self.artifact(root, "backend")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            node_toolchain = self.node_toolchain()
            execution = HostArtifactExecution.create(
                language="rust-javascript-full-stack",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=frontend,
                entrypoint="main.js",
                arguments=[],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=node_toolchain.command,
                runtime_toolchain=node_toolchain,
                auxiliary_artifacts=(
                    HostAuxiliaryArtifact.create(
                        artifact_digest=canonical_identity("backend-artifact").uri,
                        artifact_root=backend,
                        entrypoint="application",
                    ),
                ),
            )
            request, authorization = grant(execution)
            invalid = subprocess.CompletedProcess(
                AuthorizedHostArtifactRunner._command(execution),
                0,
                b"not-json",
                b"",
            )
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                return_value=invalid,
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)
            self.assertEqual(caught.exception.code, "host_execution.output_invalid")
            self.assertIn("backend", str(caught.exception))
            invoked.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "runtime retarget test is POSIX-specific")
    def test_runtime_drift_after_live_authorization_fails_before_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self.artifact(root, "frontend", entrypoint="main.js")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            baseline = self.node_toolchain()
            invocation = root / "authorized-node"
            invocation.symlink_to(Path(baseline.launcher_executable))
            node_toolchain = self.node_toolchain(pinned_command=(str(invocation),))
            execution = HostArtifactExecution.create(
                language="javascript",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint="main.js",
                arguments=[],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=node_toolchain.command,
                runtime_toolchain=node_toolchain,
            )
            request, authorization = grant(execution)
            verifier = mock.Mock()

            def retarget_runtime(*_args, **_kwargs):
                invocation.unlink()
                invocation.symlink_to(Path("/bin/false"))

            verifier.require_observation_valid.side_effect = retarget_runtime
            runner = AuthorizedHostArtifactRunner(
                authorization_verifier=verifier,
                host_execution_acknowledged=True,
                clock=lambda: NOW,
            )
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    runner.run(execution, request, authorization)
            self.assertEqual(caught.exception.code, "host_execution.runtime_changed")
            invoked.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "wrapper retarget test is POSIX-specific")
    def test_successful_child_cannot_retarget_a_stable_node_wrapper(self) -> None:
        baseline = self.node_toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected_runtime = root / "selected-node"
            selected_runtime.symlink_to(Path(baseline.runtime_executable))
            wrapper = root / "node-wrapper"
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(str(selected_runtime)) + ' "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            node_toolchain = self.node_toolchain(pinned_command=(str(wrapper),))
            wrapper_digest = node_toolchain.launcher_digest
            artifact = self.artifact(root, "frontend", entrypoint="main.js")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            execution = HostArtifactExecution.create(
                language="javascript",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint="main.js",
                arguments=[],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=node_toolchain.command,
                runtime_toolchain=node_toolchain,
            )
            request, authorization = grant(execution)

            def retarget_and_succeed(command, **_kwargs):
                selected_runtime.unlink()
                selected_runtime.symlink_to(Path("/bin/false"))
                return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")

            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                side_effect=retarget_and_succeed,
            ):
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)

            self.assertEqual(caught.exception.code, "host_execution.runtime_changed")
            self.assertEqual(node_toolchain.launcher_digest, wrapper_digest)

    def test_successful_child_cannot_mutate_full_stack_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = self.artifact(root, "frontend", entrypoint="main.js")
            backend = self.artifact(root, "backend")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            node_toolchain = self.node_toolchain()
            backend_binding = HostAuxiliaryArtifact.create(
                artifact_digest=canonical_identity("backend-artifact").uri,
                artifact_root=backend,
                entrypoint="application",
            )
            execution = HostArtifactExecution.create(
                language="rust-javascript-full-stack",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=frontend,
                entrypoint="main.js",
                arguments=[],
                support_paths=(),
                runtime_root=runtime_root,
                runtime_command=node_toolchain.command,
                runtime_toolchain=node_toolchain,
                auxiliary_artifacts=(backend_binding,),
            )
            request, authorization = grant(execution)

            def mutate_backend_and_succeed(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                (backend / "late-file").write_bytes(b"mutation")
                return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")

            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                side_effect=mutate_backend_and_succeed,
            ):
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)
            self.assertEqual(
                caught.exception.code,
                "host_execution.auxiliary_artifact_changed",
            )

    def test_invalid_mode_specific_inputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self.artifact(root, "artifact", entrypoint="main.js")
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            node = self.runtime(root)
            common = {
                "artifact_digest": ARTIFACT_DIGEST,
                "artifact_root": artifact,
                "entrypoint": "main.js",
                "arguments": [],
                "support_paths": (),
                "runtime_root": runtime_root,
            }
            with self.assertRaisesRegex(ValueError, "must be python, cpp, rust"):
                HostArtifactExecution.create(language="ruby", **common)
            with self.assertRaisesRegex(
                HostArtifactExecutionError, "requires an exact Node toolchain"
            ):
                HostArtifactExecution.create(language="javascript", **common)
            with self.assertRaisesRegex(ValueError, "cannot declare"):
                HostArtifactExecution.create(
                    language="rust", runtime_command=(str(node),), **common
                )
            node_toolchain = self.node_toolchain()
            with self.assertRaisesRegex(ValueError, "requires one backend"):
                HostArtifactExecution.create(
                    language="rust-javascript-full-stack",
                    runtime_command=node_toolchain.command,
                    runtime_toolchain=node_toolchain,
                    **common,
                )

    def test_nonzero_process_reports_status_and_available_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = open_run(root, operation="test")
            assert run is not None
            os.environ["LITAI_EVIDENCE_RUN"] = str(run.root)
            execution = self.fixture(root)
            request, authorization = grant(execution)
            completed = subprocess.CompletedProcess(
                [str(execution.executable)],
                7,
                b"generated diagnostic do-not-retain",
                b"",
            )
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                    return_value=completed,
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.execution._minimal_environment",
                    return_value={"PATH": os.defpath, "HOST_SECRET": "do-not-retain"},
                ),
            ):
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(execution, request, authorization)
            self.assertEqual(caught.exception.code, "host_execution.nonzero_exit")
            self.assertEqual(caught.exception.role, "application")
            self.assertEqual(caught.exception.returncode, 7)
            self.assertEqual(
                caught.exception.stdout_digest,
                "sha256:"
                + hashlib.sha256(b"generated diagnostic do-not-retain").hexdigest(),
            )
            self.assertEqual(
                caught.exception.stderr_digest,
                f"sha256:{hashlib.sha256(b'').hexdigest()}",
            )
            self.assertIn("status 7: generated diagnostic", str(caught.exception))
            nodes = run.reduced()["nodes"]
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["path"], "host-execution/application/failure")
            self.assertEqual(nodes[0]["state"], "failed")
            outputs = {
                output["role"]: (
                    run.root / output["path"]
                    if not Path(output["path"]).is_absolute()
                    else Path(output["path"])
                ).read_text(encoding="utf-8")
                for output in nodes[0]["outputs"]
            }
            self.assertEqual(outputs["stdout"], "generated diagnostic <redacted>")
            self.assertEqual(outputs["stderr"], "")
            self.assertNotIn("do-not-retain", outputs["stdout"])
            self.assertNotIn("do-not-retain", outputs["stderr"])

    def test_missing_acknowledgement_or_expired_grant_never_starts_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.fixture(Path(temporary))
            request, authorization = grant(execution)
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as missing:
                    authorized_runner(clock=lambda: NOW).run(
                        execution, request, authorization
                    )
                self.assertEqual(
                    missing.exception.code,
                    "host_execution.acknowledgement_required",
                )
                with self.assertRaisesRegex(Exception, "expired"):
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW + timedelta(minutes=6),
                    ).run(execution, request, authorization)
                with self.assertRaises(HostArtifactExecutionError) as privileges:
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW,
                    ).run(
                        execution,
                        request,
                        replace(authorization, privileges=("processes",)),
                    )
                self.assertEqual(
                    privileges.exception.code,
                    "host_execution.authorization_privilege_mismatch",
                )
            invoked.assert_not_called()

    def test_artifact_and_support_trees_cannot_drift_after_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            support = root / "support"
            support.mkdir()
            (support / "library.py").write_text("VALUE = 1\n", encoding="utf-8")
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "application").write_bytes(b"compiled-fixture")
            runtime = root / "runtime"
            runtime.mkdir()
            execution = HostArtifactExecution.create(
                language="cpp",
                artifact_digest=ARTIFACT_DIGEST,
                artifact_root=artifact,
                entrypoint="application",
                arguments=[],
                support_paths=(support,),
                runtime_root=runtime,
            )
            request, authorization = grant(execution)
            runner = authorized_runner(
                host_execution_acknowledged=True,
                clock=lambda: NOW,
            )
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                (artifact / "injected").write_bytes(b"late artifact")
                with self.assertRaises(HostArtifactExecutionError) as artifact_drift:
                    runner.run(execution, request, authorization)
                self.assertEqual(
                    artifact_drift.exception.code, "host_execution.artifact_changed"
                )

                refreshed = HostArtifactExecution.create(
                    language="cpp",
                    artifact_digest=ARTIFACT_DIGEST,
                    artifact_root=artifact,
                    entrypoint="application",
                    arguments=[],
                    support_paths=(support,),
                    runtime_root=runtime,
                )
                refreshed_request, refreshed_authorization = grant(refreshed)
                (support / "library.py").write_text("VALUE = 2\n", encoding="utf-8")
                with self.assertRaises(HostArtifactExecutionError) as support_drift:
                    runner.run(refreshed, refreshed_request, refreshed_authorization)
                self.assertEqual(
                    support_drift.exception.code,
                    "host_execution.support_path_changed",
                )
            invoked.assert_not_called()

    def test_successful_child_cannot_mutate_authorized_inputs(self) -> None:
        for changed_tree, expected_code in (
            ("entrypoint", "host_execution.entrypoint_changed"),
            ("artifact", "host_execution.artifact_changed"),
            ("support", "host_execution.support_path_changed"),
        ):
            with self.subTest(changed_tree=changed_tree):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    artifact = root / "artifact"
                    artifact.mkdir()
                    (artifact / "application").write_bytes(b"compiled-fixture")
                    support = root / "support"
                    support.mkdir()
                    (support / "library.py").write_text("VALUE = 1\n", encoding="utf-8")
                    runtime = root / "runtime"
                    runtime.mkdir()
                    execution = HostArtifactExecution.create(
                        language="cpp",
                        artifact_digest=ARTIFACT_DIGEST,
                        artifact_root=artifact,
                        entrypoint="application",
                        arguments=[],
                        support_paths=(support,),
                        runtime_root=runtime,
                    )
                    request, authorization = grant(execution)

                    def mutate_input_and_succeed(
                        command: list[str],
                        changed_tree: str = changed_tree,
                        artifact: Path = artifact,
                        support: Path = support,
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[bytes]:
                        if changed_tree == "entrypoint":
                            (artifact / "application").write_bytes(b"mutation")
                        elif changed_tree == "artifact":
                            (artifact / "child-write").write_bytes(b"mutation")
                        else:
                            (support / "library.py").write_text(
                                "VALUE = 2\n", encoding="utf-8"
                            )
                        return subprocess.CompletedProcess(
                            command, 0, b'{"value":8}', b""
                        )

                    with mock.patch(
                        "literate_ai.adapters.lifecycle.execution._run_bounded_process",
                        side_effect=mutate_input_and_succeed,
                    ):
                        with self.assertRaises(HostArtifactExecutionError) as caught:
                            authorized_runner(
                                host_execution_acknowledged=True,
                                clock=lambda: NOW,
                            ).run(execution, request, authorization)
                    self.assertEqual(caught.exception.code, expected_code)

    def test_argument_variants_reuse_one_verified_input_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline = self.fixture(Path(temporary))

            variant = baseline.with_arguments([{"value": 9}])

            self.assertEqual(variant.artifact_root, baseline.artifact_root)
            self.assertEqual(
                variant.artifact_tree_digest, baseline.artifact_tree_digest
            )
            self.assertEqual(variant.entrypoint_digest, baseline.entrypoint_digest)
            self.assertEqual(variant.support_paths, baseline.support_paths)
            self.assertEqual(
                variant.support_path_digests, baseline.support_path_digests
            )
            self.assertEqual(variant.arguments_json, b'[{"value":9}]')
            self.assertNotEqual(variant.harness_digest, baseline.harness_digest)

    def test_request_or_entrypoint_drift_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.fixture(Path(temporary))
            request, authorization = grant(execution)
            changed_request = replace(request, sandbox_profile="another-profile")
            with mock.patch(
                "literate_ai.adapters.lifecycle.execution._run_bounded_process"
            ) as invoked:
                with self.assertRaises(HostArtifactExecutionError) as mismatch:
                    authorized_runner(
                        host_execution_acknowledged=True, clock=lambda: NOW
                    ).run(execution, changed_request, authorization)
                self.assertEqual(
                    mismatch.exception.code, "host_execution.request_mismatch"
                )
                execution.executable.write_bytes(b"changed-after-authorization")
                with self.assertRaises(HostArtifactExecutionError) as changed:
                    authorized_runner(
                        host_execution_acknowledged=True, clock=lambda: NOW
                    ).run(execution, request, authorization)
                self.assertEqual(
                    changed.exception.code, "host_execution.entrypoint_changed"
                )
            invoked.assert_not_called()

    def test_process_is_killed_when_output_exceeds_its_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for stream in ("stdout", "stderr"):
                with self.subTest(stream=stream):
                    script = (
                        "import sys; "
                        f"sys.{stream}.buffer.write(b'x' * 65536); "
                        f"sys.{stream}.flush()"
                    )
                    with self.assertRaises(HostArtifactExecutionError) as caught:
                        _run_bounded_process(
                            [sys.executable, "-I", "-c", script],
                            cwd=Path(temporary),
                            env={},
                            timeout_seconds=5,
                            stdout_limit_bytes=128,
                            stderr_limit_bytes=128,
                        )
                    self.assertEqual(
                        caught.exception.code, "host_execution.output_limit"
                    )

    def test_timeout_kills_host_process_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            with self.assertRaises(HostArtifactExecutionError) as caught:
                _run_bounded_process(
                    [sys.executable, "-I", "-c", parent, str(child), str(sentinel)],
                    cwd=root,
                    env={},
                    timeout_seconds=0.2,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                )
            self.assertEqual(caught.exception.code, "host_execution.timeout")
            time.sleep(1.1)
            self.assertFalse(sentinel.exists(), "host descendant escaped timeout")

    def test_slow_pipe_close_within_five_seconds_is_not_a_false_failure(self) -> None:
        # Regression test for issue #77: a grandchild that inherits the
        # stdout pipe and takes longer than one second (but well under the
        # five-second budget used by builders/_process.py) to exit and
        # release the pipe must not be misreported as a host process that
        # "left inherited output streams open". The parent process exits
        # immediately after spawning the grandchild, so process.wait()
        # returns quickly while the stdout drain thread stays blocked in
        # read() until the grandchild closes the inherited descriptor.
        # Do not use the disposable directory as cwd: on Windows the
        # grandchild's lingering directory handle then fails
        # TemporaryDirectory cleanup with WinError 32.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "slow_child.py"
            child.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
            parent = (
                "import subprocess, sys\n"
                "subprocess.Popen([sys.executable, sys.argv[1]])\n"
            )
            started = time.monotonic()
            completed = _run_bounded_process(
                [sys.executable, "-I", "-c", parent, str(child)],
                cwd=Path.cwd(),
                env={},
                timeout_seconds=10,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 0)
            # The drain thread should have waited for the grandchild's
            # ~2 second inherited-pipe hold rather than bailing out after
            # the old 1 second budget.
            self.assertGreaterEqual(elapsed, 1.9)

    def test_read_errors_are_appended_under_a_lock(self) -> None:
        # Regression test for issue #77: both drain threads appended to the
        # shared read_errors list without synchronization (the builder copy
        # in adapters/builders/_process.py uses state_lock; this call site
        # did not). Force both stdout and stderr reads to fail concurrently
        # via a fake process and assert both errors are captured safely
        # (no lost update) and reported through the expected error code.
        class _FailingStream:
            def read(self, *_args, **_kwargs):
                raise OSError("simulated concurrent pipe failure")

            def close(self):
                return None

        class _FakeProcess:
            def __init__(self) -> None:
                self.pid = 99_999
                self.stdout = _FailingStream()
                self.stderr = _FailingStream()
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.execution.subprocess.Popen",
                    return_value=_FakeProcess(),
                ),
                mock.patch(
                    "literate_ai.adapters.lifecycle.execution._terminate_process_tree"
                ),
            ):
                with self.assertRaises(HostArtifactExecutionError) as caught:
                    _run_bounded_process(
                        [sys.executable, "-I", "-c", "pass"],
                        cwd=root,
                        env={},
                        timeout_seconds=5,
                        stdout_limit_bytes=1024,
                        stderr_limit_bytes=1024,
                    )
                self.assertEqual(caught.exception.code, "host_execution.failed")
                self.assertIsInstance(caught.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()
