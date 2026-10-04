from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from literate_ai import remote_source_guard
from literate_ai.remote_source_guard import (
    SourceGuardError,
)

extract_source_archive = getattr(remote_source_guard, "extract_source_archive", None)

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


if __name__ == "__main__":
    unittest.main()
