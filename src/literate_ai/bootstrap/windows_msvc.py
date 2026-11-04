"""Bounded, dependency-free discovery of a vendor-installed native MSVC toolset."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from .windows_images import _identity, _opening_identity, observe_pe_architecture

_VERSION = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,6}){2,3}")
_NATIVE_DIRECTORIES = {"arm64": "arm64", "x86_64": "x64"}
_TARGET_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x64": "x86_64",
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x86": "x86",
}
_COMPILER_TARGET = re.compile(
    r"(?i)[\\/]VC[\\/]Tools[\\/]MSVC[\\/][0-9.]+[\\/]bin[\\/]"
    r"Host(?:arm64|x64|x86)[\\/](arm64|x64|x86)[\\/]cl\.exe$"
)
_BANNER_TARGET = re.compile(
    r"^Microsoft \(R\) C/C\+\+ Optimizing Compiler Version [0-9.]+ for (\S+)$"
)


class WindowsMsvcDiscoveryError(ValueError):
    """An installed vendor toolset cannot be observed safely."""


def selected_msvc_target(
    compiler: Path, version_output: str, environment: Mapping[str, str]
) -> str:
    """Bind compilation target independently of the compiler's host PE machine."""

    if len(version_output) > 2 * 1024 * 1024:
        raise WindowsMsvcDiscoveryError("MSVC version observation exceeds its bound")
    targets: set[str] = set()
    if match := _COMPILER_TARGET.search(str(compiler)):
        targets.add(_TARGET_ALIASES[match[1].casefold()])
    for line in version_output.splitlines():
        match = _BANNER_TARGET.fullmatch(line.strip())
        raw = match[1].casefold() if match is not None else None
        if raw is None and line.startswith("Target: "):
            raw = line.removeprefix("Target: ").strip().split("-", 1)[0].casefold()
        if raw is not None:
            target = _TARGET_ALIASES.get(raw)
            if target is None:
                raise WindowsMsvcDiscoveryError(
                    "MSVC compilation target is unsupported"
                )
            targets.add(target)
    if len(targets) != 1:
        raise WindowsMsvcDiscoveryError(
            "MSVC compilation target is unproven or conflicting"
        )
    target = next(iter(targets))
    configured = {name.casefold(): value for name, value in environment.items()}
    context = configured.get("vscmd_arg_tgt_arch", "").strip().casefold()
    if context and _TARGET_ALIASES.get(context) != target:
        raise WindowsMsvcDiscoveryError(
            "MSVC developer target conflicts with selected compiler"
        )
    return target


def _plain_directory(path: Path) -> bool:
    for parent in (path, *path.parents):
        try:
            observed = parent.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(observed.st_mode) or (
            getattr(observed, "st_file_attributes", 0) & 0x400
        ):
            raise WindowsMsvcDiscoveryError("Visual Studio directory is redirected")
    return True


def _default_toolset_version(path: Path) -> str | None:
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not 1 <= before.st_size <= 128
    ):
        raise WindowsMsvcDiscoveryError("Visual Studio toolset metadata is unsafe")
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_NOFOLLOW", "O_NONBLOCK", "O_NOINHERIT"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _opening_identity(before) != _opening_identity(opened):
            raise WindowsMsvcDiscoveryError(
                "Visual Studio toolset changed while opening"
            )
        content = os.read(descriptor, 129)
        if (
            len(content) != before.st_size
            or _identity(opened) != _identity(os.fstat(descriptor))
            or _identity(before) != _identity(path.stat(follow_symlinks=False))
        ):
            raise WindowsMsvcDiscoveryError(
                "Visual Studio toolset changed while reading"
            )
    finally:
        os.close(descriptor)
    try:
        version = content.decode("ascii").strip()
    except UnicodeError as exc:
        raise WindowsMsvcDiscoveryError(
            "Visual Studio toolset version is invalid"
        ) from exc
    if _VERSION.fullmatch(version) is None:
        raise WindowsMsvcDiscoveryError("Visual Studio toolset version is invalid")
    return version


def discover_native_msvc_compiler(
    architecture: str, environment: Mapping[str, str]
) -> Path | None:
    """Find a native compiler under finite standard vendor installation roots.

    Custom installations remain explicit through CXX or PATH. Reading the vendor's
    default-toolset file avoids recursive searches, subprocesses, and dependencies
    that are unavailable during the cold host bootstrap.
    """

    native = _NATIVE_DIRECTORIES.get(architecture)
    if native is None:
        return None
    configured = {name.casefold(): value for name, value in environment.items()}
    roots = tuple(
        dict.fromkeys(
            Path(value)
            for name in ("programfiles(x86)", "programfiles", "programw6432")
            if (value := configured.get(name)) and Path(value).is_absolute()
        )
    )
    # At most 24 roots and one bounded metadata read per root. The declared
    # BuildTools edition takes priority; no directory enumeration is performed.
    for year in ("2022", "2019"):
        for edition in ("BuildTools", "Community", "Professional", "Enterprise"):
            for root in roots:
                vc = root / "Microsoft Visual Studio" / year / edition / "VC"
                if not _plain_directory(vc):
                    continue
                metadata = vc / "Auxiliary/Build/Microsoft.VCToolsVersion.default.txt"
                if not _plain_directory(metadata.parent):
                    continue
                version = _default_toolset_version(metadata)
                if version is None:
                    continue
                compiler = (
                    vc
                    / "Tools/MSVC"
                    / version
                    / "bin"
                    / f"Host{native}"
                    / native
                    / "cl.exe"
                )
                if not _plain_directory(compiler.parent):
                    continue
                try:
                    observed = observe_pe_architecture(compiler)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as exc:
                    raise WindowsMsvcDiscoveryError(
                        "Installed Visual Studio compiler is unverified"
                    ) from exc
                if observed != architecture:
                    raise WindowsMsvcDiscoveryError(
                        "Installed Visual Studio compiler is not native for this target"
                    )
                return compiler
    return None
