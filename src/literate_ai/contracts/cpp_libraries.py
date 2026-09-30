"""Declared compiled C++ product closure; not evidence of build or acceptance."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, string_tuple, string_value
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths

_CPP_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")


def is_cpp_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 512
        and _CPP_NAME.fullmatch(value) is not None
    )


def validate_cpp_header(value: str, *, label: str) -> None:
    string_value(value, label, max_length=512)
    try:
        canonical_relative_posix_paths((value,), label=label)
    except (TypeError, ValueError) as exc:
        fail(label, str(exc))
    if re.fullmatch(r"[A-Za-z0-9_./-]+", value) is None:
        fail(label, "requires a portable C++ include path")


@dataclass(frozen=True, slots=True)
class CppLibraryLayout:
    """Consumer-visible files in one sealed native library directory.

    Paths are relative to the export. Public includes resolve below ``include/``.
    The same binary may have both link and runtime roles on ELF/Mach-O targets.
    Target and ABI identities belong to the enclosing ArtifactExport, not filenames.
    """

    kind: str
    headers: tuple[str, ...]
    link_files: tuple[str, ...]
    runtime_files: tuple[str, ...] = ()

    SCHEMA: ClassVar[str] = "literate-ai/cpp-library-layout@1"

    def __post_init__(self) -> None:
        string_value(self.kind, "CppLibraryLayout.kind")
        if self.kind not in {"static", "shared"}:
            fail("CppLibraryLayout.kind", "requires static or shared production")
        for name in ("headers", "link_files", "runtime_files"):
            values = getattr(self, name)
            label = f"CppLibraryLayout.{name}"
            if not isinstance(values, tuple) or len(values) > 16384:
                fail(label, "requires a bounded tuple of paths")
            for value in values:
                string_value(value, label, max_length=512)
            if values != tuple(sorted(set(values))):
                fail(label, "requires unique paths in canonical order")
            try:
                canonical_relative_posix_paths(values, label=label)
            except (TypeError, ValueError) as exc:
                fail(label, str(exc))
        if not self.headers or not self.link_files:
            fail("CppLibraryLayout", "requires public headers and linkable binaries")
        if any(not path.startswith("include/") for path in self.headers):
            fail("CppLibraryLayout.headers", "must be files below include/")
        if any(not path.startswith("lib/") for path in self.link_files):
            fail("CppLibraryLayout.link_files", "must be files below lib/")
        if any(not path.startswith(("lib/", "bin/")) for path in self.runtime_files):
            fail("CppLibraryLayout.runtime_files", "must be below lib/ or bin/")
        if self.kind == "static" and self.runtime_files:
            fail(
                "CppLibraryLayout.runtime_files", "static products have no own runtime"
            )
        if self.kind == "shared" and not self.runtime_files:
            fail(
                "CppLibraryLayout.runtime_files",
                "shared products require runtime files",
            )
        try:
            canonical_relative_posix_paths(self.files, label="CppLibraryLayout.files")
        except (TypeError, ValueError) as exc:
            fail("CppLibraryLayout.files", str(exc))

    @property
    def files(self) -> tuple[str, ...]:
        """Exact union of public header, linker and loader inputs."""
        return tuple(
            sorted(set((*self.headers, *self.link_files, *self.runtime_files)))
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "headers": list(self.headers),
            "link_files": list(self.link_files),
            "runtime_files": list(self.runtime_files),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CppLibraryLayout"
    ) -> CppLibraryLayout:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"kind", "headers", "link_files", "runtime_files"}),
        )
        return cls(
            string_value(data["kind"], f"{path}.kind"),
            string_tuple(data["headers"], f"{path}.headers"),
            string_tuple(data["link_files"], f"{path}.link_files"),
            string_tuple(data["runtime_files"], f"{path}.runtime_files"),
        )
