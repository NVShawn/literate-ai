"""Provision exact upstream crates for real offline Rust verifier journeys.

This helper creates test inputs, not framework source owners or acceptance
evidence. Call it once per fixture class, then copy that seed for each journey.
No network, ambient Cargo execution, or user-cache mutation occurs here.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

# Exact cached upstream .crate archives. Runtime native Cargo must independently
# consume the resulting directory source and validate its locked checksums.
CRATES = {
    "serde_json-1.0.151": (
        156556,
        "c841b55ecdae098c80dcae9cf767f6f8a0c2cdb3416bbef72181df4d0fe73f14",
    ),
    "serde_core-1.0.228": (
        63111,
        "41d385c7d4ca58e59fc732af25c3983b67ac852c1a25000afe1175de458b67ad",
    ),
    "serde-1.0.228": (
        83652,
        "9a8e94ea7f378bd32cbbd37198a4a91436180c5bb472411e48b5ec2e2124ae9e",
    ),
    "serde_derive-1.0.228": (
        59605,
        "d540f220d3187173da220f885ab66608367b6574e925011a9353e4badda91d79",
    ),
    "itoa-1.0.18": (
        15935,
        "8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682",
    ),
    "memchr-2.8.0": (
        97780,
        "f8ca58f447f06ed17d5fc4043ce1b10dd205e060fb3ce5b979b8ed8e59ff3f79",
    ),
    "zmij-1.0.21": (
        26665,
        "b8848ee67ecc8aedbaf3e4122217aff892639231befc6a1b58d29fff4c2cabaa",
    ),
    "syn-2.0.117": (
        306011,
        "e665b8803e7b1d2a727f4023456bbbbe74da67099c585258af0ad9c5013b9b99",
    ),
    "quote-1.0.45": (
        31564,
        "41f2619966050689382d2b44f664f4bc593e129785a36d6ee376ddf37259b924",
    ),
    "proc-macro2-1.0.106": (
        59765,
        "8fd00f0bb2e90d81d1044c2b32617f68fcb9fa3bb7640c23e9c748e53fb30934",
    ),
    "unicode-ident-1.0.24": (
        49298,
        "e6e4313cd5fcd3dad5cafa179702e2b244f760991f45397d14d4ebf38247da75",
    ),
}


def prepare_vendor(destination: Path, archive_directory: Path) -> None:
    """Expand only the exact finite real archives into a fresh test-owned root."""
    destination.mkdir(mode=0o700)
    total = count = 0
    for package, (size, digest) in sorted(CRATES.items()):
        path = archive_directory / (package + ".crate")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise ValueError("exact fixture crate archive required")
            raw = source.read(size + 1)
            after = os.fstat(source.fileno())

        def signature(node):
            return (
                node.st_dev,
                node.st_ino,
                node.st_mode,
                node.st_nlink,
                node.st_size,
                node.st_mtime_ns,
                node.st_ctime_ns,
            )

        if (
            signature(before) != signature(after)
            or len(raw) != size
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise ValueError("fixture crate archive bytes changed")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive:
                count += 1
                relative = PurePosixPath(member.name)
                if (
                    count > 4096
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in member.name
                    or not relative.parts
                    or relative.parts[0] != package
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError("unsafe or excessive fixture crate namespace")
                output = destination.joinpath(*relative.parts)
                if member.isdir():
                    output.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                total += member.size
                if member.size > 2 * 1024**2 or total > 32 * 1024**2:
                    raise ValueError("fixture crate extraction budget exceeded")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("fixture crate regular member required")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError("fixture crate member size differs")
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with output.open("xb") as target:
                    target.write(content)
                files[PurePosixPath(*relative.parts[1:]).as_posix()] = hashlib.sha256(
                    content
                ).hexdigest()
        with (destination / package / ".cargo-checksum.json").open("x") as checksum:
            json.dump({"files": files, "package": digest}, checksum, sort_keys=True)
