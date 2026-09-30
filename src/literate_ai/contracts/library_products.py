"""Typed library product projection shared by rebuild, custody and dispatch.

This preserves the existing rebuild envelope. Its identity binds the projection;
it is not an acceptance receipt or authorization to import or execute the package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail
from .cpp_libraries import CppLibraryLayout
from .executable_components.artifacts import ArtifactExport
from .executable_components.commands import LibraryImportSurface
from .identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class LibraryArtifactProduct:
    artifact_export: ArtifactExport
    import_surface: LibraryImportSurface
    native_layout: CppLibraryLayout | None = None

    SCHEMA: ClassVar[str] = "literate-ai/library-rebuild-artifact@1"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_export, ArtifactExport)
            or self.artifact_export.role != "library"
        ):
            fail(
                "LibraryArtifactProduct.artifact_export",
                "must be a typed library-role ArtifactExport",
            )
        if not isinstance(self.import_surface, LibraryImportSurface):
            fail(
                "LibraryArtifactProduct.import_surface",
                "must be a typed LibraryImportSurface",
            )

        if self.import_surface.language == "cpp":
            if not isinstance(self.native_layout, CppLibraryLayout):
                fail(
                    "LibraryArtifactProduct.native_layout",
                    "C++ requires a native layout",
                )
            for capability in self.import_surface.capabilities:
                if "include/" + capability.module not in self.native_layout.headers:
                    fail(
                        "LibraryArtifactProduct.import_surface",
                        "C++ capability header is absent from the public closure",
                    )
        elif self.native_layout is not None:
            fail("LibraryArtifactProduct.native_layout", "requires the C++ language")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": self.SCHEMA,
            "artifact_export": self.artifact_export.to_dict(),
            "import_surface": self.import_surface.to_dict(),
        }
        if self.native_layout is not None:
            value["native_layout"] = self.native_layout.to_dict()
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LibraryArtifactProduct"
    ) -> LibraryArtifactProduct:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"artifact_export", "import_surface"}),
            optional=frozenset({"native_layout"}),
        )
        return cls(
            ArtifactExport.from_dict(
                data["artifact_export"], path=f"{path}.artifact_export"
            ),
            LibraryImportSurface.from_dict(
                data["import_surface"], path=f"{path}.import_surface"
            ),
            (
                CppLibraryLayout.from_dict(
                    data["native_layout"], path=f"{path}.native_layout"
                )
                if "native_layout" in data
                else None
            ),
        )


def library_worker_manifest(
    identity: ContentIdentity,
    product: LibraryArtifactProduct,
    toolchains: tuple[ContentIdentity, ...],
) -> dict[str, Any]:
    """Canonical worker custody binding, shared by producer and result validation."""

    if not isinstance(product, LibraryArtifactProduct):
        fail("LibraryWorkerManifest.product", "requires typed product authority")
    if not isinstance(identity, ContentIdentity):
        fail("LibraryWorkerManifest.artifact_identity", "requires a typed identity")
    if not toolchains or any(
        not isinstance(item, ContentIdentity) for item in toolchains
    ):
        fail(
            "LibraryWorkerManifest.toolchains", "requires observed toolchain identities"
        )
    return {
        "schema": "literate-ai/worker-library-artifact@1",
        "artifact_identity": identity.to_dict(),
        "library_artifact": product.to_dict(),
        "toolchain_identities": [item.to_dict() for item in toolchains],
    }
