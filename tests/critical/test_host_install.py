"""Contracts for composed Flavor host-toolchain installation."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters import host_install
from literate_ai.adapters.host_install import (
    HostInstallError,
)
from literate_ai.bootstrap.host_install_requirements import (
    HostManagedArtifact,
)


def _artifact(archive: Path, *, digest: str | None = None) -> HostManagedArtifact:
    return HostManagedArtifact(
        "urn:test:artifact",
        "fixture agent",
        "1.0.0",
        ("codex",),
        "https://example.invalid/fixture.tar.gz",
        "sha256",
        digest or hashlib.sha256(archive.read_bytes()).hexdigest(),
        "tar.gz",
        "bundle",
        (".",),
        "agent-source",
        "codex",
    )


class HostInstallContractTests(unittest.TestCase):
    def test_managed_artifact_verifies_digest_and_normalizes_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "fixture.tar.gz"
            content = b"fixture executable"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("bundle/agent-source")
                info.size = len(content)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(content))
            artifact = _artifact(archive)

            def download(_url: str, destination: Path) -> None:
                shutil.copy2(archive, destination)

            installed = host_install._install_managed_artifact(
                artifact, tool_root=root / "tools", downloader=download
            )
            self.assertEqual((installed / "codex").read_bytes(), content)
            self.assertTrue(
                host_install._artifact_manifest_matches(installed, artifact)
            )

            manifest_path = installed / ".literate-ai-artifact.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("required_runtime_paths")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(
                host_install._artifact_manifest_matches(installed, artifact)
            )

    def test_managed_artifact_rejects_digest_mismatch_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.tar.gz"
            with tarfile.open(safe, "w:gz") as bundle:
                info = tarfile.TarInfo("bundle/agent-source")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))

            def download_safe(_url: str, destination: Path) -> None:
                shutil.copy2(safe, destination)

            with self.assertRaises(HostInstallError) as digest_error:
                host_install._install_managed_artifact(
                    _artifact(safe, digest="0" * 64),
                    tool_root=root / "digest-tools",
                    downloader=download_safe,
                )
            self.assertEqual(
                digest_error.exception.code, "host-install.artifact-digest-mismatch"
            )

            unsafe = root / "unsafe.tar.gz"
            with tarfile.open(unsafe, "w:gz") as bundle:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))

            def download_unsafe(_url: str, destination: Path) -> None:
                shutil.copy2(unsafe, destination)

            with self.assertRaises(HostInstallError) as traversal_error:
                host_install._install_managed_artifact(
                    _artifact(unsafe),
                    tool_root=root / "traversal-tools",
                    downloader=download_unsafe,
                )
            self.assertEqual(
                traversal_error.exception.code, "host-install.archive-traversal"
            )


if __name__ == "__main__":
    unittest.main()
