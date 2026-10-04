"""Bazel conformance decorates, but never impersonates, a native builder."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
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
    executable_file_digest,
)
from literate_ai.adapters.builders import bazel as bazel_builder_module
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.contracts import generated_source_tree_identity
from literate_ai.security import (
    AuthorizationRevocationSet,
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


class BazelToolchainDiscoveryTests(unittest.TestCase):
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

    def request_and_authorization(self):
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
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=_Delegate.builder_id,
            toolchain_digest=DIGEST_A,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("native-executable", BAZEL_DEPENDENCY_EVIDENCE_OUTPUT),
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

    def adapter(self, root: Path, delegate: _Delegate):
        return BazelConformanceBuildAdapter(
            delegate,
            self.toolchain,
            root / "composite-artifacts",
            AuthorizationRevocationSet(),
            clock=lambda: NOW + timedelta(minutes=1),
        )

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


if __name__ == "__main__":
    unittest.main()
