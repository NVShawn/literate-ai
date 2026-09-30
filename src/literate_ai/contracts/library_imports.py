"""Reviewed native import names without resolver-owned interface identities."""

import re
from dataclasses import dataclass

from ._validation import fail, fields, string_tuple, string_value
from .cpp_libraries import is_cpp_name, validate_cpp_header
from .executable_components._common import portable_name, tuple_value


@dataclass(frozen=True, slots=True)
class AuthoredLibraryImport:
    language: str
    package: str
    capability: str
    module: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        string_value(self.language, "AuthoredLibraryImport.language")
        if self.language not in {"python", "javascript", "rust", "cpp"}:
            fail("AuthoredLibraryImport.language", "unsupported library language")
        portable_name(self.capability, "AuthoredLibraryImport.capability")
        string_value(self.package, "AuthoredLibraryImport.package", max_length=255)
        string_value(self.module, "AuthoredLibraryImport.module", max_length=512)
        identifier = r"[A-Za-z_][A-Za-z0-9_]*"
        package_pattern = (
            r"[A-Za-z_][A-Za-z0-9_-]*" if self.language == "javascript" else identifier
        )
        if re.fullmatch(package_pattern, self.package) is None:
            fail("AuthoredLibraryImport.package", "invalid native package name")
        if self.language == "cpp":
            validate_cpp_header(self.module, label="AuthoredLibraryImport.module")
            if not self.module.startswith(self.package + "/"):
                fail(
                    "AuthoredLibraryImport.module",
                    "header must be below the named package",
                )
        else:
            separator = {"python": ".", "javascript": "/", "rust": "::"}[self.language]
            suffix = self.module.removeprefix(self.package)
            if not self.module.startswith(self.package) or (
                suffix
                and re.fullmatch(
                    r"(?:" + re.escape(separator) + identifier + r")+", suffix
                )
                is None
            ):
                fail(
                    "AuthoredLibraryImport.module",
                    "must remain inside the named package",
                )
            if self.language == "javascript" and not suffix:
                fail(
                    "AuthoredLibraryImport.module",
                    "requires an explicit package subpath",
                )
        symbols = string_tuple(
            tuple_value(self.symbols, "AuthoredLibraryImport.symbols"),
            "AuthoredLibraryImport.symbols",
        )
        if (
            not 1 <= len(symbols) <= 256
            or symbols != tuple(sorted(set(symbols)))
            or any(
                not is_cpp_name(symbol)
                if self.language == "cpp"
                else re.fullmatch(identifier, symbol) is None
                for symbol in symbols
            )
        ):
            fail(
                "AuthoredLibraryImport.symbols", "requires canonical native identifiers"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "package": self.package,
            "capability": self.capability,
            "module": self.module,
            "symbols": list(self.symbols),
        }

    @classmethod
    def from_dict(cls, value, *, path="AuthoredLibraryImport"):
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"language", "package", "capability", "module", "symbols"}
            ),
        )
        return cls(
            data["language"],
            data["package"],
            data["capability"],
            data["module"],
            string_tuple(data["symbols"], f"{path}.symbols"),
        )


def validate_library_imports(
    declarations: tuple[AuthoredLibraryImport, ...],
    *,
    kind: str,
    capabilities: tuple[str, ...],
) -> None:
    """Require a complete, canonical import map for each declared language."""
    if (
        not isinstance(declarations, tuple)
        or len(declarations) > 256
        or any(not isinstance(item, AuthoredLibraryImport) for item in declarations)
    ):
        fail("library_imports", "requires bounded typed declarations")
    keys = tuple((item.language, item.capability) for item in declarations)
    if keys != tuple(sorted(set(keys))):
        fail("library_imports", "requires unique language/capability order")
    if not declarations:
        return
    if kind != "library":
        fail("library_imports", "requires a library")
    for language in {item.language for item in declarations}:
        selected = tuple(item for item in declarations if item.language == language)
        if {item.capability for item in selected} != set(capabilities) or len(
            {item.package for item in selected}
        ) != 1:
            fail(
                "library_imports",
                "requires every capability in one package per language",
            )
