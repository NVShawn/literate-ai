"""Stage a pinned Hex plugin for native CI qualification.

No user Mix archive directory is read or modified. Native discovery separately
checks the plugin's loadability and binds its files to the selected toolchain.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

URL = "https://builds.hex.pm/installs/1.16.0/hex-2.5.1.ez"
SHA256 = "2eadc51d9300a134db10859cfce41b4754d44f65bd843ebe359e87e3476c6e2b"
LIMIT = 8 * 1024 * 1024
SOURCE_COMMIT = "a43131c26aeca06064959297d737991163f5ac5d"
SOURCE_URL = f"https://codeload.github.com/hexpm/hex/tar.gz/{SOURCE_COMMIT}"
SOURCE_SHA256 = "386936947eec8023eeff5c6ee936ea093a7a67592b93bc66eabd32eb9628c7a2"


def stage(output: Path, archive: bytes) -> Path:
    """Verify first, then replace only this helper's private plugin subtree."""
    if len(archive) > LIMIT or hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError("Hex archive digest mismatch; refusing extraction")
    return _stage_verified(output, archive)


def _stage_verified(output: Path, archive: bytes) -> Path:
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        files = []
        total = 0
        for member in package.infolist():
            path = PurePosixPath(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.filename
                or not path.parts
                or path.parts[0] != "hex-2.5.1"
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe Hex archive member")
            total += member.file_size
            if total > LIMIT * 4:
                raise ValueError("Hex archive expands beyond its bound")
            if not member.is_dir():
                files.append((path, package.read(member)))
    if not any(str(path) == "hex-2.5.1/ebin/hex.app" for path, _ in files):
        raise ValueError("Pinned archive is missing Hex application metadata")
    output.mkdir(parents=True, exist_ok=True)
    plugin = output / "hex-2.5.1"
    if plugin.is_symlink():
        raise ValueError("Hex staging directory must not be a symlink")
    if plugin.exists():
        shutil.rmtree(plugin)
    for relative, content in files:
        destination = output.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    ebin = plugin / "ebin"
    if not (ebin / "hex.app").is_file():
        raise ValueError("Pinned archive is missing Hex application metadata")
    return ebin.resolve()


def source_files(archive: bytes) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Authenticate and bound the complete source before writing or executing."""
    if len(archive) > LIMIT or hashlib.sha256(archive).hexdigest() != SOURCE_SHA256:
        raise ValueError("Hex source digest mismatch; refusing extraction")
    files = []
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for member in package:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not path.parts
                or path.parts[0] != f"hex-{SOURCE_COMMIT}"
                or not (member.isdir() or member.isfile())
                or path in seen
                or any(":" in part for part in path.parts)
            ):
                raise ValueError("Unsafe Hex source member")
            seen.add(path)
            total += member.size
            if len(seen) > 4096 or total > LIMIT * 4:
                raise ValueError("Hex source expands beyond its bound")
            if member.isfile():
                stream = package.extractfile(member)
                if stream is None:
                    raise ValueError("Missing Hex source member")
                files.append((PurePosixPath(*path.parts[1:]), stream.read()))
    if not any(str(path) == "mix.exs" for path, _ in files):
        raise ValueError("Pinned source is missing Mix project metadata")
    return tuple(files)


def build_source(output: Path, archive: bytes) -> Path:
    """Build authenticated source with the bound native Mix command, offline."""
    files = source_files(archive)
    from literate_ai.adapters.builders.elixir import discover_elixir_toolchain
    from literate_ai.adapters.builders.mix import discover_mix_toolchain

    with tempfile.TemporaryDirectory(prefix="litai-hex-source-") as directory:
        scratch = Path(directory)
        project = scratch / "source"
        project.mkdir()
        for relative, content in files:
            destination = project.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        environment = dict(os.environ)
        for key in ("MIX_HOME", "MIX_ARCHIVES", "HEX_HOME", "MIX_BUILD_PATH"):
            value = scratch / key.lower()
            value.mkdir()
            environment[key] = str(value)
        environment["MIX_ENV"] = "prod"
        environment["HEX_OFFLINE"] = "1"
        tool = discover_mix_toolchain(
            discover_elixir_toolchain(environment), environment
        )
        tool.require_unchanged(environment)
        built_archive = scratch / "hex-2.5.1.ez"
        subprocess.run(
            [*tool.command, "archive.build", "-o", str(built_archive)],
            cwd=project,
            env=environment,
            check=True,
            timeout=300,
        )
        tool.require_unchanged(environment)
        with built_archive.open("rb") as source:
            built = source.read(LIMIT + 1)
        if len(built) > LIMIT:
            raise ValueError("Built Hex archive exceeds its bound")
        return _stage_verified(output, built)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="reuse a digest-verified archive")
    parser.add_argument("--build-from-source", action="store_true")
    args = parser.parse_args()
    if args.archive is None:
        url = SOURCE_URL if args.build_from_source else URL
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read(LIMIT + 1)
    else:
        with args.archive.open("rb") as source:
            archive = source.read(LIMIT + 1)
    staging = build_source if args.build_from_source else stage
    print(staging(args.output.resolve(), archive))


if __name__ == "__main__":
    main()
