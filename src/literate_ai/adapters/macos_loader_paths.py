"""Explicit macOS library/framework path overrides, before native resolution.

This models four path controls. It is not a complete dyld process policy: binary
load-command settings, image suffixes, versioned overrides and inserted libraries
require their own observation. Unmodeled DYLD controls are refused here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.contracts.identity import canonical_identity

_KEYS = (
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
)


def _validate_roots(groups):
    if any(not isinstance(group, tuple) for group in groups):
        raise ValueError("macos.loader.paths-invalid")
    if sum(len(group) for group in groups) > 128:
        raise ValueError("macos.loader.path-limit")
    for group in groups:
        for value in group:
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or value.startswith("//")
                or len(value) > 4096
                or "\x00" in value
                or ":" in value
                or ".." in PurePosixPath(value).parts
                or str(PurePosixPath(value)) != value
            ):
                raise ValueError("macos.loader.paths-invalid")


def _framework_suffix(path):
    parts = path.split("/")
    for index in range(len(parts) - 2, -1, -1):
        part = parts[index]
        if not part.endswith(".framework"):
            continue
        name = part.removesuffix(".framework")
        rest = parts[index + 1 :]
        if name and (
            rest == [name]
            or (
                len(rest) == 3 and rest[0] == "Versions" and rest[1] and rest[2] == name
            )
        ):
            return "/".join(parts[index:])
    return None


@dataclass(frozen=True)
class MacOsLoaderPaths:
    library: tuple[str, ...] = ()
    framework: tuple[str, ...] = ()
    fallback_library: tuple[str, ...] = ()
    fallback_framework: tuple[str, ...] = ()

    def __post_init__(self):
        _validate_roots(self.groups)

    @property
    def groups(self):
        return (
            self.library,
            self.framework,
            self.fallback_library,
            self.fallback_framework,
        )

    def to_environment(self):
        return {
            key: ":".join(group)
            for key, group in zip(_KEYS, self.groups, strict=True)
            if group
        }

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/macos-loader-paths@1",
                "paths": {
                    key: list(group)
                    for key, group in zip(_KEYS, self.groups, strict=True)
                },
            }
        )

    @classmethod
    def from_environment(cls, environment):
        if not isinstance(environment, Mapping) or len(environment) > 2048:
            raise ValueError("macos.loader.environment-invalid")
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("macos.loader.environment-invalid")
            if key.startswith("DYLD_") and key not in _KEYS:
                raise ValueError("macos.loader.control-unsupported")
        groups = []
        for key in _KEYS:
            value = environment.get(key)
            if value is None:
                groups.append(())
            else:
                if len(value) > 128 * 4097:
                    raise ValueError("macos.loader.path-limit")
                groups.append(tuple(value.split(":")))
        return cls(*groups)

    def candidates(self, load_path):
        """Return ordered override and fallback paths around the original lookup.

        Expand @loader_path/@rpath and select compatible native images separately.
        Framework recognition retains its version directory and nested framework
        suffix; ordinary library overrides never substitute a framework leaf.
        """
        if (
            not isinstance(load_path, str)
            or not load_path
            or len(load_path) > 4096
            or "\x00" in load_path
            or load_path.endswith("/")
            or ".." in PurePosixPath(load_path).parts
        ):
            raise ValueError("macos.loader.import-invalid")
        framework = _framework_suffix(load_path)
        suffix = framework or load_path.rsplit("/", 1)[-1]
        before, after = (
            (self.framework, self.fallback_framework)
            if framework
            else (self.library, self.fallback_library)
        )
        return tuple(str(PurePosixPath(root) / suffix) for root in before), tuple(
            str(PurePosixPath(root) / suffix) for root in after
        )
