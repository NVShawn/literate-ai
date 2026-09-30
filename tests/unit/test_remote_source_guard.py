from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from literate_ai import remote_source_guard
from literate_ai.remote_source_guard import (
    SourceGuardError,
    git_tree_identity,
    source_tree_identity,
)

extract_source_archive = getattr(remote_source_guard, "extract_source_archive", None)
materialize_git_tree = getattr(remote_source_guard, "materialize_git_tree", None)

_MANIFEST_SCHEMA = "literate-ai/canonical-source-manifest@1"
_INVALID_IDENTITY = "sha256:" + "0" * 64


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _manifest_identity(entries: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(entries, key=lambda item: item["path"])
    content = json.dumps(
        {"schema": _MANIFEST_SCHEMA, "entries": ordered},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _digest(content)


def _file_entry(path: str, content: bytes, *, executable: bool = False):
    return {
        "content_identity": _digest(content),
        "executable": executable,
        "kind": "file",
        "path": path,
        "size": len(content),
    }


def _symlink_entry(path: str, target: str):
    return {
        "kind": "symlink",
        "path": path,
        "target_identity": _digest(os.fsencode(target)),
    }


def _tar_file(
    name: str, content: bytes = b"content\n"
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.size = len(content)
    return member, content


def _tar_symlink(name: str, target: str) -> tuple[tarfile.TarInfo, None]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = target
    member.mode = 0o777
    return member, None


def _write_archive(
    path: Path, members: Iterable[tuple[tarfile.TarInfo, bytes | None]]
) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for member, content in members:
            archive.addfile(
                member,
                None if content is None else io.BytesIO(content),
            )


def _hypothetical_archive_identity(
    members: tuple[tuple[tarfile.TarInfo, bytes | None], ...],
) -> str:
    """Bind unsafe bytes so rejection cannot be an expected-identity mismatch."""

    entries: list[dict[str, Any]] = []
    for member, content in members:
        path = member.name.removeprefix("literate-ai/")
        if member.isreg() and content is not None:
            entries.append(
                _file_entry(path, content, executable=bool(member.mode & 0o111))
            )
        elif member.issym():
            entries.append(_symlink_entry(path, member.linkname))
        else:
            return _INVALID_IDENTITY
    return _manifest_identity(entries)


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def _repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "source-guard@example.invalid")
    _git(repository, "config", "user.name", "Source Guard Test")
    return repository


def _commit(repository: Path, message: str) -> str:
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD").decode("ascii").strip()


def _add_git_symlink(repository: Path, path: str, target: bytes) -> None:
    object_id = _git(repository, "hash-object", "-w", "--stdin", input_bytes=target)
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        "120000",
        object_id.decode("ascii").strip(),
        path,
    )


def _symlinks_available(root: Path) -> bool:
    target = root / "symlink-capability-target"
    link = root / "symlink-capability-link"
    target.write_bytes(b"probe")
    try:
        link.symlink_to(target.name)
        return link.is_symlink() and os.readlink(link) == target.name
    except OSError:
        return False
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


@unittest.skipUnless(
    callable(extract_source_archive), "extract_source_archive is not implemented yet"
)
class SourceArchiveMaterializationTests(unittest.TestCase):
    def _reject_without_writes(
        self,
        members: Iterable[tuple[tarfile.TarInfo, bytes | None]],
    ) -> None:
        frozen_members = tuple(members)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            destination = root / "destination"
            _write_archive(archive, frozen_members)

            with self.assertRaises(SourceGuardError):
                extract_source_archive(
                    archive,
                    destination,
                    _hypothetical_archive_identity(frozen_members),
                )

            self.assertFalse(
                destination.exists(),
                "unsafe archives must be rejected before destination creation",
            )

    def test_internal_and_contained_parent_symlinks_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if not _symlinks_available(root):
                self.skipTest("host cannot create symbolic links")
            archive = root / "source.tar.gz"
            destination = root / "destination"
            content = b"authoritative bytes\n"
            members = (
                _tar_file("literate-ai/shared.txt", content),
                _tar_symlink("literate-ai/current.txt", "shared.txt"),
                _tar_symlink("literate-ai/nested/current.txt", "../shared.txt"),
            )
            expected = _manifest_identity(
                (
                    _file_entry("shared.txt", content),
                    _symlink_entry("current.txt", "shared.txt"),
                    _symlink_entry("nested/current.txt", "../shared.txt"),
                )
            )
            _write_archive(archive, members)

            extract_source_archive(archive, destination, expected)

            materialized = destination / "literate-ai"
            self.assertEqual(source_tree_identity(materialized), expected)
            self.assertEqual(os.readlink(materialized / "current.txt"), "shared.txt")
            self.assertEqual(
                os.readlink(materialized / "nested" / "current.txt"),
                "../shared.txt",
            )

    def test_archive_byte_limit_refuses_before_destination_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            destination = root / "destination"
            members = (_tar_file("literate-ai/source.txt", b"bounded\n"),)
            _write_archive(archive, members)

            with self.assertRaisesRegex(SourceGuardError, "transport bound"):
                remote_source_guard.extract_source_archive_with_manifest(
                    archive,
                    destination,
                    _hypothetical_archive_identity(members),
                    max_archive_bytes=archive.stat().st_size - 1,
                )

            self.assertFalse(destination.exists())

    def test_entry_limit_refuses_before_destination_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            destination = root / "destination"
            members = (
                _tar_file("literate-ai/first.txt", b"first\n"),
                _tar_file("literate-ai/second.txt", b"second\n"),
            )
            _write_archive(archive, members)

            with self.assertRaisesRegex(SourceGuardError, "entry bound"):
                remote_source_guard.extract_source_archive_with_manifest(
                    archive,
                    destination,
                    _hypothetical_archive_identity(members),
                    max_entries=1,
                )

            self.assertFalse(destination.exists())

    def test_content_limit_refuses_before_destination_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            destination = root / "destination"
            content = b"larger-than-limit\n"
            members = (_tar_file("literate-ai/source.txt", content),)
            _write_archive(archive, members)

            with self.assertRaisesRegex(SourceGuardError, "content bound"):
                remote_source_guard.extract_source_archive_with_manifest(
                    archive,
                    destination,
                    _hypothetical_archive_identity(members),
                    max_total_bytes=len(content) - 1,
                )

            self.assertFalse(destination.exists())

    def test_nonportable_or_escaping_member_paths_are_rejected_before_writes(
        self,
    ) -> None:
        names = (
            "/absolute.txt",
            "../escaping.txt",
            "literate-ai/../escaping.txt",
            "literate-ai\\backslash.txt",
            "C:/drive-qualified.txt",
            "C:\\drive-qualified.txt",
            "//server/share/unc.txt",
            "\\\\server\\share\\unc.txt",
        )
        for name in names:
            with self.subTest(name=name):
                self._reject_without_writes((_tar_file(name),))

    def test_nonportable_or_escaping_symlink_targets_are_rejected_before_writes(
        self,
    ) -> None:
        targets = (
            "/absolute",
            "../../escaping",
            "..\\escaping",
            "C:/drive-qualified",
            "C:\\drive-qualified",
            "//server/share/unc",
            "\\\\server\\share\\unc",
        )
        for target in targets:
            with self.subTest(target=target):
                self._reject_without_writes(
                    (_tar_symlink("literate-ai/nested/link", target),)
                )

    def test_duplicate_and_casefold_colliding_members_are_rejected_before_writes(
        self,
    ) -> None:
        cases = (
            (
                _tar_file("literate-ai/duplicate.txt", b"first"),
                _tar_file("literate-ai/duplicate.txt", b"second"),
            ),
            (
                _tar_file("literate-ai/README.txt", b"first"),
                _tar_file("literate-ai/readme.txt", b"second"),
            ),
            (
                _tar_file("literate-ai/Directory/first.txt", b"first"),
                _tar_file("literate-ai/directory/second.txt", b"second"),
            ),
        )
        for members in cases:
            with self.subTest(paths=[member.name for member, _content in members]):
                self._reject_without_writes(members)

    def test_hardlinks_devices_and_fifos_are_rejected_before_writes(self) -> None:
        hardlink = tarfile.TarInfo("literate-ai/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "literate-ai/file.txt"
        special_types = (
            ("hardlink", hardlink),
            ("character-device", tarfile.TarInfo("literate-ai/character-device")),
            ("block-device", tarfile.TarInfo("literate-ai/block-device")),
            ("fifo", tarfile.TarInfo("literate-ai/fifo")),
        )
        special_types[1][1].type = tarfile.CHRTYPE
        special_types[2][1].type = tarfile.BLKTYPE
        special_types[3][1].type = tarfile.FIFOTYPE
        for label, member in special_types:
            with self.subTest(kind=label):
                self._reject_without_writes(((member, None),))

    def test_member_beneath_symlink_is_rejected_before_writes(self) -> None:
        self._reject_without_writes(
            (
                _tar_symlink("literate-ai/alias", "real"),
                _tar_file("literate-ai/alias/payload.txt", b"must not be written"),
            )
        )


@unittest.skipUnless(
    shutil.which("git") and callable(materialize_git_tree),
    "Git or materialize_git_tree is unavailable",
)
class GitTreeMaterializationTests(unittest.TestCase):
    def test_internal_and_contained_parent_symlinks_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if not _symlinks_available(root):
                self.skipTest("host cannot create symbolic links")
            repository = _repository(root)
            (repository / "shared.txt").write_bytes(b"authoritative bytes\n")
            _git(repository, "add", "shared.txt")
            _add_git_symlink(repository, "current.txt", b"shared.txt")
            _add_git_symlink(repository, "nested/current.txt", b"../shared.txt")
            revision = _commit(repository, "safe symlinks")
            expected = git_tree_identity(repository, revision)
            destination = root / "destination"

            materialize_git_tree(repository, revision, destination, expected)

            self.assertEqual(source_tree_identity(destination), expected)
            self.assertEqual(os.readlink(destination / "current.txt"), "shared.txt")
            self.assertEqual(
                os.readlink(destination / "nested" / "current.txt"),
                "../shared.txt",
            )

    def test_nonportable_symlink_targets_are_rejected_before_writes(self) -> None:
        targets = (
            b"/absolute",
            b"../../escaping",
            b"..\\escaping",
            b"C:/drive-qualified",
            b"C:\\drive-qualified",
            b"//server/share/unc",
            b"\\\\server\\share\\unc",
        )
        for index, target in enumerate(targets):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    repository = _repository(root)
                    _add_git_symlink(repository, "nested/link", target)
                    revision = _commit(repository, f"unsafe symlink {index}")
                    destination = root / "destination"

                    with self.assertRaises(SourceGuardError):
                        materialize_git_tree(
                            repository,
                            revision,
                            destination,
                            _manifest_identity(
                                (_symlink_entry("nested/link", os.fsdecode(target)),)
                            ),
                        )

                    self.assertFalse(destination.exists())

    def test_export_ignore_and_substitution_do_not_change_authoritative_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _repository(root)
            (repository / ".gitattributes").write_text(
                "retained.txt export-ignore\nexpanded.txt export-subst\n",
                encoding="utf-8",
            )
            (repository / "retained.txt").write_bytes(b"must remain present\n")
            (repository / "expanded.txt").write_bytes(b"$Format:%H$\n")
            _git(repository, "add", ".")
            revision = _commit(repository, "archive attributes are not authority")
            expected = git_tree_identity(repository, revision)
            destination = root / "destination"

            materialize_git_tree(repository, revision, destination, expected)

            self.assertEqual(
                (destination / "retained.txt").read_bytes(), b"must remain present\n"
            )
            self.assertEqual(
                (destination / "expanded.txt").read_bytes(), b"$Format:%H$\n"
            )
            self.assertEqual(source_tree_identity(destination), expected)

    def test_gitlinks_fail_explicitly_before_destination_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = _repository(root)
            (repository / "root.txt").write_bytes(b"root\n")
            _git(repository, "add", "root.txt")
            dependency_commit = _commit(repository, "dependency target")
            _git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                "160000",
                dependency_commit,
                "dependency",
            )
            revision = _commit(repository, "gitlink")
            destination = root / "destination"

            with self.assertRaisesRegex(SourceGuardError, "gitlink|submodule"):
                git_tree_identity(repository, revision)
            with self.assertRaisesRegex(SourceGuardError, "gitlink|submodule"):
                materialize_git_tree(
                    repository, revision, destination, _INVALID_IDENTITY
                )

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
