"""Qualified artifacts cannot be substituted between checking and publication."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts import canonical_identity
from literate_ai.project_releases import (
    ProjectReleaseError,
    _verify_release_file_assets,
)
from literate_ai.release_files import SCHEMA, file_identity, validate_release_files


class ReleaseFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.wheel = self.root / "package.whl"
        self.wheel.write_bytes(b"qualified bytes")
        self.manifest = {
            "schema": SCHEMA,
            "revision": "a" * 40,
            "version": "1.1.0",
            "files": [
                {
                    "role": "wheel",
                    "path": "package.whl",
                    "size": 15,
                    "identity": file_identity(self.wheel),
                }
            ],
        }
        self.seal(self.manifest)

    def seal(self, value: dict) -> None:
        value.pop("identity", None)
        value["identity"] = canonical_identity(value).uri

    def validate(self, value: dict) -> dict:
        return validate_release_files(
            self.root,
            value,
            revision="a" * 40,
            version="1.1.0",
            required_roles=("wheel",),
        )

    def test_checked_files_pass_and_same_size_substitution_fails(self) -> None:
        self.assertEqual(self.validate(self.manifest), self.manifest)
        self.wheel.write_bytes(b"substitute data")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.validate(self.manifest)

    def test_resealed_revision_role_and_path_substitution_fail(self) -> None:
        for field, value in (("revision", "b" * 40), ("version", "1.2.0")):
            changed = copy.deepcopy(self.manifest)
            changed[field] = value
            self.seal(changed)
            with self.assertRaises(ValueError):
                self.validate(changed)
        for field, value in (
            ("role", "unexpected"),
            ("path", "../package.whl"),
            ("size", True),
        ):
            changed = copy.deepcopy(self.manifest)
            changed["files"][0][field] = value
            self.seal(changed)
            with self.assertRaises(ValueError):
                self.validate(changed)

    def test_missing_and_duplicate_files_fail(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["files"].append(changed["files"][0])
        self.seal(changed)
        with self.assertRaises(ValueError):
            self.validate(changed)
        self.wheel.unlink()
        with self.assertRaises(ValueError):
            self.validate(self.manifest)

    def test_remote_verification_hashes_downloaded_bytes(self) -> None:
        for content, accepted in (
            (b"qualified bytes", True),
            (b"substitute data", False),
        ):

            def download(argv, content=content, **kwargs):
                destination = Path(argv[argv.index("--dir") + 1])
                (destination / "package.whl").write_bytes(content)

            with mock.patch(
                "literate_ai.project_releases.subprocess.run", side_effect=download
            ):
                if accepted:
                    _verify_release_file_assets(
                        self.root, "owner/repo", "v1.1.0", self.manifest
                    )
                else:
                    with self.assertRaises(ProjectReleaseError) as raised:
                        _verify_release_file_assets(
                            self.root, "owner/repo", "v1.1.0", self.manifest
                        )
                    self.assertEqual(
                        raised.exception.code, "release.published_artifact_mismatch"
                    )
