"""Explicit FreeBSD library search paths; other environment controls refuse."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.contracts.identity import canonical_identity


@dataclass(frozen=True, slots=True)
class FreeBsdLoaderPaths:
    library: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.library, tuple) or len(self.library) > 128:
            raise ValueError("freebsd.loader.path-limit")
        for value in self.library:
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or value.startswith("//")
                or len(value) > 4096
                or any(ord(c) < 32 or c in ":;$" for c in value)
                or ".." in PurePosixPath(value).parts
                or str(PurePosixPath(value)) != value
            ):
                raise ValueError("freebsd.loader.paths-invalid")

    @classmethod
    def from_environment(cls, environment):
        if not isinstance(environment, Mapping) or len(environment) > 2048:
            raise ValueError("freebsd.loader.environment-invalid")
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("freebsd.loader.environment-invalid")
            if key.startswith("LD_") and key != "LD_LIBRARY_PATH":
                raise ValueError("freebsd.loader.control-unsupported")
        value = environment.get("LD_LIBRARY_PATH", "")
        if len(value) > 128 * 4097:
            raise ValueError("freebsd.loader.path-limit")
        return cls(tuple(value.split(":")) if value else ())

    def to_environment(self):
        return {"LD_LIBRARY_PATH": ":".join(self.library)} if self.library else {}

    @property
    def identity(self):
        return canonical_identity(
            {"schema": "literate-ai/freebsd-loader-paths@1", "library": self.library}
        )
