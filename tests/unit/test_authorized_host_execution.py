"""Exact authorization is enforced inside the portable host artifact runner."""

from __future__ import annotations

import os
import py_compile
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
)
from literate_ai.adapters.lifecycle.execution import _run_bounded_process
from literate_ai.contracts import canonical_identity
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
                with self.assertRaisesRegex(Exception, "expired"):
                    authorized_runner(
                        host_execution_acknowledged=True,
                        clock=lambda: NOW + timedelta(minutes=6),
                    ).run(execution, request, authorization)
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


if __name__ == "__main__":
    unittest.main()
