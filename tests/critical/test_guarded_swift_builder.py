"""Native Swift source sets have one entrypoint, including support and tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders.cpp import discover_cpp_toolchain
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    canonical_tree_digest,
)
from literate_ai.adapters.builders.swift import GuardedSwiftBuilder
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class GuardedSwiftBuilderTests(unittest.TestCase):
    def setUp(self):
        environment = dict(os.environ)
        environment["CXX"] = "xcrun swiftc" if sys.platform == "darwin" else "swiftc"
        try:
            self.toolchain = discover_cpp_toolchain(environment)
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "generated"
        (self.source / "source/tools").mkdir(parents=True)
        (self.source / "source/tests").mkdir()
        (self.source / "source/tools/support.swift").write_text(
            "func answer() -> Int { 40 + 2 }\n"
        )
        (self.source / "source/tests/litai_test.swift").write_text(
            "func checkAnswer() -> Bool { answer() == 42 }\n"
        )
        (self.source / "source/main.swift").write_text(
            'if CommandLine.arguments.dropFirst().first == "--litai-test" {\n'
            '    precondition(checkAnswer()); print("passed")\n'
            "} else { print(answer()) }\n"
        )

    def build(self):
        source_digest = canonical_tree_digest(self.source)
        policy = SecurityPolicy(policy_digest="sha256:" + "c" * 64)
        classification = policy.classify(
            effective_revision_digest="sha256:" + "b" * 64,
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
            effective_revision_digest="sha256:" + "b" * 64,
            source_bundle_digest=source_digest,
            builder_id=GuardedSwiftBuilder.builder_id,
            toolchain_digest=self.toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("native-executable",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="Swift entrypoint conformance",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return GuardedSwiftBuilder(self.toolchain, AuthorizationRevocationSet()).build(
            request,
            authorization,
            source_root=self.source,
            artifact_store=self.root / "artifacts",
            now=NOW + timedelta(minutes=1),
        )

    def test_support_and_generated_tests_link_into_one_runnable_executable(self):
        artifact = self.build()
        executable = artifact.artifact_path / artifact.executable_file
        for arguments, expected in (([], b"42\n"), (["--litai-test"], b"passed\n")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(executable), *arguments],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(result.stdout, expected)

    def test_direct_swiftc_alias_preserves_compiler_mode_and_native_execution(self):
        compiler = (
            subprocess.check_output(
                ["xcrun", "--find", "swiftc"], text=True, timeout=10
            ).strip()
            if sys.platform == "darwin"
            else shutil.which("swiftc")
        )
        self.assertIsNotNone(compiler)
        environment = dict(os.environ, CXX=f'"{compiler}"')
        self.toolchain = discover_cpp_toolchain(environment)
        if Path(compiler).resolve().stem.casefold() != "swiftc":
            self.assertEqual(self.toolchain.command[1], "--driver-mode=swiftc")
        explicit = discover_cpp_toolchain(
            dict(environment, CXX=f'"{compiler}" --driver-mode=swiftc')
        )
        self.assertEqual(explicit.command.count("--driver-mode=swiftc"), 1)
        if Path(compiler).resolve().stem.casefold() != "swiftc":
            self.assertEqual(explicit.identity, self.toolchain.identity)
        self.toolchain.require_unchanged()
        self.test_support_and_generated_tests_link_into_one_runnable_executable()

    def test_competing_helper_or_main_entrypoints_refuse_without_publication(self):
        for path, content in (
            ("tools/cleanup.swift", 'print("independent cleanup")\n'),
            ("tools/cleanup.swift", "@main struct Cleanup { static func main() {} }\n"),
            ("main.swift", "@main struct Application { static func main() {} }\n"),
        ):
            with self.subTest(path=path, content=content):
                target = self.source / "source" / path
                original = target.read_bytes() if target.exists() else None
                target.write_text(content)
                before = canonical_tree_digest(self.source)
                try:
                    with self.assertRaises(BuildError) as error:
                        self.build()
                    self.assertEqual(
                        error.exception.code, "builder.swift_generated_source_rejected"
                    )
                    self.assertEqual(canonical_tree_digest(self.source), before)
                    self.assertEqual(
                        list((self.root / "artifacts").rglob("build-manifest.json")), []
                    )
                finally:
                    if original is None:
                        target.unlink()
                    else:
                        target.write_bytes(original)
