from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "skills" / "agent" / "checkout-git-repository" / "scripts" / "checkout.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("litai_repository_checkout", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("repository checkout skill cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-c", "protocol.file.allow=always", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@unittest.skipUnless(shutil.which("git-lfs"), "Git LFS is required for checkout tests")
class RepositoryCheckoutSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Checkouts only read the origin, so one shared fixture is safe; every
        # test materializes into its own temporary destination.
        fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(fixture.cleanup)
        cls.origin, cls.revision = cls._repository(Path(fixture.name))

    @staticmethod
    def _repository(root: Path) -> tuple[str, str]:
        submodule_source = root / "submodule-source"
        submodule_origin = root / "submodule-origin.git"
        submodule_source.mkdir()
        _git(submodule_source, "init", "--initial-branch=main")
        _git(submodule_source, "config", "user.name", "Literate AI Tests")
        _git(submodule_source, "config", "user.email", "tests@example.invalid")
        _git(submodule_source, "lfs", "install", "--local")
        _git(submodule_source, "lfs", "track", "*.bin")
        submodule_source.joinpath("nested.bin").write_bytes(b"nested hydration\n")
        _git(submodule_source, "add", ".gitattributes", "nested.bin")
        _git(submodule_source, "commit", "-m", "submodule fixture")
        subprocess.run(
            ("git", "init", "--bare", "--initial-branch=main", str(submodule_origin)),
            check=True,
            capture_output=True,
        )
        _git(submodule_source, "remote", "add", "origin", str(submodule_origin))
        _git(submodule_source, "push", "origin", "main")
        _git(submodule_source, "lfs", "push", "--all", "origin")

        source = root / "source"
        origin = root / "origin.git"
        source.mkdir()
        _git(source, "init", "--initial-branch=main")
        _git(source, "config", "user.name", "Literate AI Tests")
        _git(source, "config", "user.email", "tests@example.invalid")
        _git(source, "lfs", "install", "--local")
        _git(source, "lfs", "track", "*.bin")
        source.joinpath("payload.bin").write_bytes(b"hydrated content\n")
        source.joinpath("excluded.bin").write_bytes(b"intentionally stored pointer\n")
        _git(source, "add", "excluded.bin")
        with source.joinpath(".gitattributes").open("a", encoding="utf-8") as stream:
            stream.write("excluded.bin -filter -diff -merge -text\n")
        source.joinpath("README.md").write_text("checkout fixture\n", encoding="utf-8")
        _git(
            source,
            "submodule",
            "add",
            submodule_origin.resolve().as_uri(),
            "dependencies/nested",
        )
        _git(
            source,
            "add",
            ".gitattributes",
            ".gitmodules",
            "payload.bin",
            "README.md",
            "dependencies/nested",
        )
        _git(source, "commit", "-m", "fixture")
        revision = _git(source, "rev-parse", "HEAD")
        _git(source, "tag", "fixture-tag")
        subprocess.run(
            ("git", "init", "--bare", "--initial-branch=main", str(origin)),
            check=True,
            capture_output=True,
        )
        _git(source, "remote", "add", "origin", str(origin))
        _git(source, "push", "origin", "main", "fixture-tag")
        _git(source, "lfs", "push", "--all", "origin")
        return origin.resolve().as_uri(), revision

    def test_branch_tag_and_exact_sha_are_shallow_and_lfs_complete(self) -> None:
        checkout = _module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin, revision = self.origin, self.revision
            for index, requested in enumerate(("main", "fixture-tag", revision)):
                with self.subTest(requested=requested):
                    destination = root / f"checkout-{index}"
                    report = checkout.materialize(
                        str(origin), requested, destination, history_depth=1
                    )
                    self.assertEqual(report["revision"], revision)
                    self.assertTrue(report["shallow"])
                    self.assertTrue(report["clean"])
                    self.assertEqual(report["repositories"], 2)
                    self.assertEqual(
                        destination.joinpath("payload.bin").read_bytes(),
                        b"hydrated content\n",
                    )
                    self.assertFalse(
                        destination.joinpath("payload.bin")
                        .read_bytes()
                        .startswith(checkout._POINTER_HEADER)
                    )
                    self.assertTrue(
                        destination.joinpath("excluded.bin")
                        .read_bytes()
                        .startswith(checkout._POINTER_HEADER)
                    )
                    self.assertGreaterEqual(report["restored_excluded_lfs_pointers"], 0)
                    self.assertEqual(
                        destination.joinpath(
                            "dependencies", "nested", "nested.bin"
                        ).read_bytes(),
                        b"nested hydration\n",
                    )

    def test_unresolved_current_tree_lfs_pointer_fails_closed(self) -> None:
        checkout = _module()
        original = checkout._git_lfs

        def leave_pointer(repository, *arguments, environment):
            if arguments[:1] in {("pull",), ("fsck",)}:
                return b""
            return original(repository, *arguments, environment=environment)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = self.origin
            with mock.patch.object(checkout, "_git_lfs", side_effect=leave_pointer):
                with self.assertRaises(checkout.CheckoutError) as caught:
                    checkout.materialize(
                        str(origin), "main", root / "checkout", history_depth=1
                    )
        self.assertEqual(caught.exception.code, "repository-checkout.lfs-unresolved")

    def test_missing_git_lfs_has_a_specific_failure(self) -> None:
        checkout = _module()
        original = checkout._run

        def unavailable(*arguments, **kwargs):
            if arguments[:3] == ("git", "lfs", "version"):
                raise checkout.CheckoutError(
                    "repository-checkout.command-failed", "missing"
                )
            return original(*arguments, **kwargs)

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(checkout, "_run", side_effect=unavailable),
        ):
            with self.assertRaises(checkout.CheckoutError) as caught:
                checkout.materialize(
                    "https://example.invalid/repository.git",
                    "main",
                    Path(temporary) / "checkout",
                )
        self.assertEqual(
            caught.exception.code, "repository-checkout.git-lfs-unavailable"
        )


if __name__ == "__main__":
    unittest.main()
