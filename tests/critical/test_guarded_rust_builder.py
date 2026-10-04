"""The Rust compiler cannot start without exact host-build authorization."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    canonical_tree_digest,
)
from literate_ai.adapters.builders.rust import (
    GuardedRustBuilder,
    RustToolchain,
    discover_rust_toolchain,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

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


if __name__ == "__main__":
    unittest.main()
