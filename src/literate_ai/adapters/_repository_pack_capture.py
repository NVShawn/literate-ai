"""Bounded, self-contained Git history export from disposable proof storage."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from .repository_orchestration import OrchestrationInventoryError

_REFERENCE = "refs/litai-export/target"


def _fail():
    raise OrchestrationInventoryError(
        "publication_pack_invalid",
        "published object pack is incomplete, invalid or oversized",
    ) from None


@dataclass(frozen=True, slots=True)
class RepositoryPackPolicy:
    maximum_pack_bytes: int = 64 * 1024 * 1024
    maximum_index_bytes: int = 32 * 1024 * 1024
    maximum_objects: int = 1_000_000

    def __post_init__(self):
        for value, limit in (
            (self.maximum_pack_bytes, 256 * 1024 * 1024),
            (self.maximum_index_bytes, 64 * 1024 * 1024),
            (self.maximum_objects, 2_000_000),
        ):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError(
                    "pack policy requires positive integers within hard bounds"
                )


@dataclass(frozen=True, slots=True)
class RepositoryObjectPack:
    commit: str
    pack: bytes
    index: bytes
    policy: RepositoryPackPolicy

    def __post_init__(self):
        from literate_ai.contracts.repository_refresh import RepositoryRefreshTarget

        RepositoryRefreshTarget("pack-target", self.commit)
        if not isinstance(self.policy, RepositoryPackPolicy):
            raise TypeError("object pack requires a typed capture policy")
        width = len(self.commit) // 2
        digest = hashlib.sha1 if width == 20 else hashlib.sha256
        if (
            not isinstance(self.pack, bytes)
            or not isinstance(self.index, bytes)
            or not 12 + width <= len(self.pack) <= self.policy.maximum_pack_bytes
            or not 1032 + width * 2
            <= len(self.index)
            <= self.policy.maximum_index_bytes
            or self.pack[:4] != b"PACK"
            or int.from_bytes(self.pack[4:8]) not in (2, 3)
            or not 1 <= self.object_count <= self.policy.maximum_objects
            or digest(self.pack[:-width]).digest() != self.pack[-width:]
            or self.index[:8] != b"\xfftOc\0\0\0\2"
            or self.index[-2 * width : -width] != self.pack[-width:]
            or digest(self.index[:-width]).digest() != self.index[-width:]
        ):
            _fail()
        fanout = struct.unpack("!256I", self.index[8:1032])
        if (
            tuple(sorted(fanout)) != fanout
            or fanout[-1] != self.object_count
            # The bounded pack is smaller than 2 GiB, so no 64-bit offset table.
            or len(self.index) != 1032 + self.object_count * (width + 8) + width * 2
        ):
            _fail()
        # Native index-pack validates records/offsets/connectivity before this
        # transport record is issued; the record alone is not admission authority.

    @property
    def object_count(self):
        return int.from_bytes(self.pack[8:12])

    @property
    def pack_id(self):
        return self.pack[-len(self.commit) // 2 :].hex()


def capture_repository_pack(run, repository, commit, policy):
    run("update-ref", _REFERENCE, commit)
    bundle = run(
        "bundle",
        "create",
        "--version=3",
        "--quiet",
        "-",
        _REFERENCE,
        stdout_limit_bytes=policy.maximum_pack_bytes + 4096,
    ).stdout
    algorithm = "sha1" if len(commit) == 40 else "sha256"
    expected = (
        "# v3 git bundle\n@object-format="
        + algorithm
        + "\n"
        + commit
        + " "
        + _REFERENCE
        + "\n\n"
    ).encode()
    # Exact header excludes prerequisites, filters and extra exported refs.
    if not bundle.startswith(expected):
        _fail()
    pack = bundle[len(expected) :]
    if (
        not 12 <= len(pack) <= policy.maximum_pack_bytes
        or pack[:4] != b"PACK"
        or not 1 <= int.from_bytes(pack[8:12]) <= policy.maximum_objects
    ):
        _fail()
    path = repository / "export.pack"
    with path.open("xb") as stream:
        stream.write(pack)
    # Validate in a NEW empty object database, not the fetched store: borrowed
    # objects must never make an incomplete exported pack appear connected.
    empty = repository / "export-check"
    run("init", "--bare", "--template=", "--object-format=" + algorithm, str(empty))
    index = repository / "export.idx"
    checked = run(
        "--git-dir=" + str(empty),
        "index-pack",
        "--strict",
        "--check-self-contained-and-connected",
        "--index-version=2",
        "--no-rev-index",
        "--threads=1",
        "--max-input-size=" + str(policy.maximum_pack_bytes),
        "-o",
        str(index),
        str(path),
    ).stdout
    with index.open("rb") as stream:
        index_content = stream.read(policy.maximum_index_bytes + 1)
    result = RepositoryObjectPack(commit, pack, index_content, policy)
    if checked != result.pack_id.encode() + b"\n":
        _fail()
    return result
