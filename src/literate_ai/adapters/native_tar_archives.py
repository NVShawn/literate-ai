"""Bounded native libarchive rewriting of canonical explicit TAR headers.

This mechanical tool binding grants no platform, retained producer, PACKAGE or
LINK authority. The FreeBSD owner additionally requires actual native pkg/tool
scope and independent archive/set verification. In particular @archive copying
preserves a supplied hardlink header rather than inferring it from staged inodes.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.freebsd_packaging import _expand_single_xz_stream
from literate_ai.adapters.retained_project_inputs import _stream
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity


def _executable_identity(path):
    size, mode, digest = _stream(path.parent, path.name, 256 * 1024 * 1024)
    if not size or mode != 0o755:
        raise PackagingError("Native libarchive tool requires a bounded executable")
    return digest


@dataclass(frozen=True, slots=True)
class NativeTarToolBinding:
    executable: Path
    executable_digest: str
    version: str

    @property
    def environment(self):
        return {
            "PATH": str(self.executable.parent),
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
        }

    @classmethod
    def discover(cls, command="bsdtar"):
        selected = shutil.which(command)
        if selected is None:
            raise PackagingError(
                "Native libarchive tar is unavailable; no installation is authorized"
            )
        executable = Path(selected).resolve(strict=True)
        provisional = cls(executable, _executable_identity(executable), "")
        version = provisional._version()
        result = cls(executable, provisional.executable_digest, version)
        result.require_current()
        return result

    def _version(self):
        result = run_bounded_process(
            (str(self.executable), "--version"),
            cwd=None,
            environment=self.environment,
            timeout_seconds=15,
            stdout_limit_bytes=4096,
            stderr_limit_bytes=4096,
            error_prefix="native.libarchive.probe",
            terminate_descendants=True,
        )
        version = result.stdout.decode("utf-8", errors="strict").strip()
        if (
            result.returncode
            or "\n" in version
            or not version.startswith("bsdtar ")
            or re.search(r"\blibarchive [0-9][0-9A-Za-z.+_-]*\b", version) is None
        ):
            raise PackagingError(
                "Native tar must report its bsdtar and libarchive versions"
            )
        return version

    def require_current(self):
        if (
            _executable_identity(self.executable) != self.executable_digest
            or self._version() != self.version
        ):
            raise PackagingError("Native libarchive tar tool changed")

    def to_dict(self):
        return {
            "schema": "literate-ai/native-libarchive-tar-tool@1",
            "executable_sha256": self.executable_digest,
            "version": self.version,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


def rewrite_native_tar(
    tool: NativeTarToolBinding,
    source: Path,
    expected_input: BlobRef,
    output: BinaryIO,
    *,
    maximum_input_bytes: int,
    maximum_output_bytes: int,
    compressed: bool = True,
    timeout_seconds: int = 300,
    maximum_expanded_bytes: int | None = None,
):
    """Copy exact archive entries through real libarchive with bounded file output.

    The caller owns a new private output file and its cleanup. This function does
    not extract paths or publish archives, and its result is not native pkg proof.
    """
    if not isinstance(tool, NativeTarToolBinding) or not isinstance(
        expected_input, BlobRef
    ):
        raise TypeError("Native archive rewrite requires typed tool and input identity")
    for value in (maximum_input_bytes, maximum_output_bytes, timeout_seconds):
        if type(value) is not int or not 1 <= value < 2**63:
            raise ValueError("Native archive rewrite requires finite resource limits")
    if maximum_expanded_bytes is None:
        maximum_expanded_bytes = maximum_input_bytes + 10240
    if type(maximum_expanded_bytes) is not int or maximum_expanded_bytes < 1:
        raise ValueError("Native archive rewrite requires a finite expansion bound")
    if (
        type(compressed) is not bool
        or expected_input.media_type != "application/x-tar"
        or not 0 < expected_input.size <= maximum_input_bytes
    ):
        raise PackagingError(
            "Native archive input differs from its bounded TAR selection"
        )
    if (
        not isinstance(output, (io.BufferedIOBase, io.RawIOBase))
        or not output.writable()
        or not output.readable()
        or not output.seekable()
    ):
        raise PackagingError("Native archive output requires a binary file")
    output_node = os.fstat(output.fileno())
    if (
        not stat.S_ISREG(output_node.st_mode)
        or output_node.st_nlink > 1
        or output.tell() != 0
        or output_node.st_size != 0
    ):
        raise PackagingError("Native archive output must be a new empty private file")
    source = Path(source).absolute()

    def current_input():
        size, _mode, digest = _stream(source.parent, source.name, maximum_input_bytes)
        if (size, digest) != (expected_input.size, expected_input.digest):
            raise PackagingError("Native archive input custody changed")

    current_input()
    tool.require_current()
    result = run_bounded_process(
        (
            str(tool.executable),
            "-c",
            "-P",
            "--format=pax",
            *(
                ("-J", "--options=xz:threads=1,xz:compression-level=9")
                if compressed
                else ()
            ),
            "-f",
            "-",
            "@" + str(source),
        ),
        cwd=source.parent,
        environment=tool.environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=maximum_output_bytes,
        stderr_limit_bytes=65536,
        error_prefix="native.libarchive.rewrite",
        terminate_descendants=True,
        stdout_sink=output,
    )
    tool.require_current()
    current_input()
    if result.returncode:
        raise PackagingError(
            "Native libarchive archive rewrite failed: "
            + result.stderr[-4000:].decode(errors="replace")
        )
    output.flush()
    if compressed:
        # libarchive pads stdout/FIFO output to its 20-block record. Normalize
        # only bounded, four-byte-aligned zero padding during construction.
        # XZ's actual footer ends in YZ, never a zero byte. The canonical strict
        # frame checker then validates the complete remaining stream, so extra
        # frames/data remain refusals, never a verification exemption.
        size = os.fstat(output.fileno()).st_size
        output.seek(max(0, size - 10241))
        tail = output.read(10241)
        padding = len(tail) - len(tail.rstrip(b"\x00"))
        if padding > 10240 or padding % 4:
            raise PackagingError("Native archive formatter padding exceeds policy")
        if padding:
            output.truncate(size - padding)
        output.seek(0)
        _expand_single_xz_stream(output, None, size - padding, maximum_expanded_bytes)
        output.seek(0, os.SEEK_END)
    after = os.fstat(output.fileno())
    if (
        after.st_nlink > 1
        or after.st_size != output.tell()
        or after.st_size > maximum_output_bytes
    ):
        raise PackagingError("Native archive output custody or byte bound changed")
    return result
