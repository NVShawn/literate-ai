"""Detect-first, user-local provisioning for mandatory framework tools."""

from __future__ import annotations

import hashlib
import os
import platform
import posixpath
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import certifi

from literate_ai.adapters.user_paths import UserPathError, resolve_host_paths

NODE_VERSION = "24.19.0"
_NODE_DISTRIBUTIONS = {
    ("darwin", "arm64"): (
        "node-v24.19.0-darwin-arm64.tar.gz",
        "8294b7aa9b03997481c06babf1e8b270c859358f27da57a11509afe537ac381d",
    ),
    ("darwin", "x64"): (
        "node-v24.19.0-darwin-x64.tar.gz",
        "d1b5e999db158c62fe8f7267a4476b035d8bd93b1a605bac24a3f0dd166e3316",
    ),
    ("linux", "arm64"): (
        "node-v24.19.0-linux-arm64.tar.xz",
        "01443c1e1a29e531ccad5a46fefa6df490d2189c49f7955904aecdbb0fe86fdc",
    ),
    ("linux", "x64"): (
        "node-v24.19.0-linux-x64.tar.xz",
        "14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647",
    ),
    ("windows", "arm64"): (
        "node-v24.19.0-win-arm64.zip",
        "8502f4a50b458d4cc38ed8f2001556c2cd239d464920f74017926ccb1e1c157f",
    ),
    ("windows", "x64"): (
        "node-v24.19.0-win-x64.zip",
        "57f71ab3652e797d84acddc79c81cc9ff1c6ddb2a1974cdb83f00fee9bff4c73",
    ),
}


class ToolBootstrapError(RuntimeError):
    """A required tool could not be provisioned into managed user storage."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def managed_tool_root() -> Path:
    try:
        return Path(resolve_host_paths().managed_tool_root)
    except UserPathError as exc:
        raise ToolBootstrapError(
            "project.tool_bootstrap_root_invalid", exc.message
        ) from exc


def _host_node_distribution() -> tuple[str, str, str]:
    system = platform.system().casefold()
    system = "windows" if system == "windows" else system
    machine = platform.machine().casefold()
    architecture = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }.get(machine)
    distribution = _NODE_DISTRIBUTIONS.get((system, architecture or ""))
    if distribution is None:
        raise ToolBootstrapError(
            "project.tool_bootstrap_platform_unsupported",
            f"automatic Node.js bootstrap does not support {system}/{machine}",
        )
    filename, digest = distribution
    return (
        filename,
        digest,
        filename.removesuffix(".zip").removesuffix(".tar.gz").removesuffix(".tar.xz"),
    )


def managed_node_root(root: Path | None = None) -> Path:
    tool_root = root or managed_tool_root()
    filename, _digest, archive_root = _host_node_distribution()
    del filename
    return tool_root / "node" / NODE_VERSION / archive_root


def managed_npm_command(root: Path | None = None) -> Path:
    node_root = managed_node_root(root)
    return node_root / ("npm.cmd" if os.name == "nt" else "bin/npm")


def _managed_npm_invocation(root: Path | None = None) -> tuple[str, str]:
    node_root = managed_node_root(root)
    if os.name == "nt":
        node = node_root / "node.exe"
        npm_cli = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    else:
        node = node_root / "bin" / "node"
        npm_cli = node_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not node.is_file() or not npm_cli.is_file():
        raise ToolBootstrapError(
            "project.tool_bootstrap_incomplete",
            "managed Node.js does not contain its exact npm execution closure",
        )
    return str(node), str(npm_cli)


def _safe_archive_name(name: str, expected_root: str) -> None:
    if "\\" in name or ":" in name:
        raise ToolBootstrapError(
            "project.tool_bootstrap_archive_unsafe",
            "the pinned Node.js archive contains an unsafe path",
        )
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ToolBootstrapError(
            "project.tool_bootstrap_archive_unsafe",
            "the pinned Node.js archive contains an unsafe path",
        )
    if path.parts[0] != expected_root:
        raise ToolBootstrapError(
            "project.tool_bootstrap_archive_invalid",
            "the pinned Node.js archive has an unexpected root directory",
        )


def _extract_node_archive(archive: Path, destination: Path, expected_root: str) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                _safe_archive_name(member.filename, expected_root)
                mode = member.external_attr >> 16
                if (mode & 0o170000) == 0o120000:
                    raise ToolBootstrapError(
                        "project.tool_bootstrap_archive_unsafe",
                        "the pinned Node.js zip contains a symbolic link",
                    )
            bundle.extractall(destination)
        return
    with tarfile.open(archive, mode="r:*") as bundle:
        for member in bundle.getmembers():
            _safe_archive_name(member.name, expected_root)
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if not link.is_absolute():
                    link = PurePosixPath(member.name).parent / link
                normalized = PurePosixPath(posixpath.normpath(str(link)))
                if normalized.is_absolute() or (
                    not normalized.parts or normalized.parts[0] != expected_root
                ):
                    raise ToolBootstrapError(
                        "project.tool_bootstrap_archive_unsafe",
                        "the pinned Node.js archive contains an unsafe link",
                    )
        bundle.extractall(destination, filter="data")


def provision_node(*, root: Path | None = None) -> dict[str, object]:
    """Install a checksum-pinned Node/npm transport in LitAI-managed storage."""

    tool_root = root or managed_tool_root()
    filename, expected_digest, archive_root = _host_node_distribution()
    destination = managed_node_root(tool_root)
    npm = managed_npm_command(tool_root)
    if npm.is_file():
        return {
            "tool": "node",
            "version": NODE_VERSION,
            "state": "reused-managed",
            "command": str(npm),
        }
    version_root = destination.parent
    if version_root.exists():
        raise ToolBootstrapError(
            "project.tool_bootstrap_incomplete",
            f"managed Node.js directory is incomplete: {version_root}",
        )
    version_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".node-bootstrap-", dir=version_root.parent))
    archive = staging / filename
    try:
        request = urllib.request.Request(
            f"https://nodejs.org/dist/v{NODE_VERSION}/{filename}",
            headers={"User-Agent": "literate-ai-tool-bootstrap"},
        )
        try:
            tls = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, timeout=60, context=tls) as response:
                with archive.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
        except (OSError, urllib.error.URLError) as exc:
            raise ToolBootstrapError(
                "project.tool_bootstrap_download_failed",
                "the pinned Node.js distribution could not be downloaded",
            ) from exc
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_digest = digest.hexdigest()
        if actual_digest != expected_digest:
            raise ToolBootstrapError(
                "project.tool_bootstrap_digest_mismatch",
                "the downloaded Node.js distribution failed its pinned SHA-256 check",
            )
        extracted = staging / "extracted"
        extracted.mkdir()
        try:
            _extract_node_archive(archive, extracted, archive_root)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ToolBootstrapError(
                "project.tool_bootstrap_archive_invalid",
                "the pinned Node.js distribution could not be safely extracted",
            ) from exc
        staged_root = extracted / archive_root
        staged_npm = staged_root / ("npm.cmd" if os.name == "nt" else "bin/npm")
        if os.name == "nt":
            staged_node = staged_root / "node.exe"
            staged_npm_cli = staged_root / "node_modules/npm/bin/npm-cli.js"
        else:
            staged_node = staged_root / "bin/node"
            staged_npm_cli = staged_root / "lib/node_modules/npm/bin/npm-cli.js"
        if not all(
            path.is_file() for path in (staged_npm, staged_node, staged_npm_cli)
        ):
            raise ToolBootstrapError(
                "project.tool_bootstrap_archive_invalid",
                "the pinned Node.js distribution does not contain its npm closure",
            )
        publication = staging / "publication"
        publication.mkdir()
        staged_root.replace(publication / archive_root)
        publication.replace(version_root)
        return {
            "tool": "node",
            "version": NODE_VERSION,
            "state": "installed-managed",
            "command": str(npm),
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "NODE_VERSION",
    "ToolBootstrapError",
    "managed_node_root",
    "managed_npm_command",
    "managed_tool_root",
    "provision_node",
]
