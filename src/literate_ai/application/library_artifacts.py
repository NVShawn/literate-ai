"""Pure projection of reviewed library capability authority into import names."""

from __future__ import annotations

from literate_ai.contracts import ContentIdentity, CppLibraryLayout
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.library_imports import AuthoredLibraryImport


def project_library_import_surface(
    component_name: str,
    language: str,
    capabilities: tuple[tuple[str, ContentIdentity], ...],
    *,
    declarations: tuple[AuthoredLibraryImport, ...] = (),
) -> LibraryImportSurface:
    """Project reviewed imports or the established convention shared by adapters."""

    if not isinstance(declarations, tuple) or any(
        not isinstance(item, AuthoredLibraryImport) for item in declarations
    ):
        raise ValueError("native imports require typed declarations")
    selected = tuple(item for item in declarations if item.language == language)
    if selected:
        interfaces = dict(capabilities)
        if (
            len(interfaces) != len(capabilities)
            or tuple(item.capability for item in selected) != tuple(sorted(interfaces))
            or len({item.package for item in selected}) != 1
        ):
            raise ValueError(
                "native import declarations must cover every current interface"
            )
        return LibraryImportSurface(
            language,
            selected[0].package,
            tuple(
                LibraryCapabilityImport(
                    item.capability,
                    interfaces[item.capability],
                    item.module,
                    item.symbols,
                )
                for item in selected
            ),
        )
    package = component_name.replace("-", "_")
    imports = []
    for capability, interface_identity in capabilities:
        prefix = f"{component_name}."
        qualified = capability.startswith(prefix)
        relative = capability.removeprefix(prefix)
        segments = tuple(part.replace("-", "_") for part in relative.split("."))
        symbol = segments[-1]
        if language == "python":
            module = f"{package}.{symbol}"
        elif language == "javascript":
            module = f"{package}/{symbol}"
        elif language == "rust":
            if qualified and len(segments) > 1:
                module = "::".join((package, *segments[:-1]))
                raw_symbol = relative.rsplit(".", 1)[-1]
                if raw_symbol.startswith("type-"):
                    symbol = "".join(
                        part.title()
                        for part in raw_symbol.removeprefix("type-").split("-")
                    )
                elif raw_symbol.startswith("fn-"):
                    symbol = raw_symbol.removeprefix("fn-").replace("-", "_")
            else:
                module = f"{package}::{symbol}"
        else:
            raise ValueError(f"importable libraries do not yet support {language!r}")
        imports.append(
            LibraryCapabilityImport(capability, interface_identity, module, (symbol,))
        )
    return LibraryImportSurface(language, package, tuple(imports))


def project_cpp_library_layout(
    surface: LibraryImportSurface, *, kind: str, platform: str
) -> CppLibraryLayout:
    """Resolve the selected product kind into the target's native file roles."""
    if surface.language != "cpp":
        raise ValueError("native C++ layout requires C++ import authority")
    if platform not in {"linux", "macos", "windows"}:
        raise ValueError("unsupported C++ native product platform")
    if kind not in {"static", "shared"}:
        raise ValueError("C++ product kind must be static or shared")
    headers = tuple(sorted({"include/" + item.module for item in surface.capabilities}))
    package = surface.package
    if platform == "windows":
        link = f"lib/{package}.lib"
        runtime = (f"bin/{package}.dll",) if kind == "shared" else ()
    else:
        suffix = (
            ".a" if kind == "static" else ".dylib" if platform == "macos" else ".so"
        )
        link = f"lib/lib{package}{suffix}"
        runtime = (link,) if kind == "shared" else ()
    return CppLibraryLayout(kind, headers, (link,), runtime)


__all__ = ["project_library_import_surface", "project_cpp_library_layout"]
