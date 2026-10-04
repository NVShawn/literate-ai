"""Shared test fixtures extracted from test_library_products."""

from __future__ import annotations

from literate_ai.contracts import (
    ArtifactExport,
    BlobRef,
    LibraryArtifactProduct,
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)


def library_product(language: str = "python") -> LibraryArtifactProduct:
    identities = [canonical_identity({"fixture": index}) for index in range(8)]
    media = "application/octet-stream"
    artifact = ArtifactExport(
        "library",
        identities[0],
        "library",
        identities[1],
        identities[2],
        media,
        identities[3],
        identities[4],
        identities[5],
        identities[6],
        (identities[7],),
        BlobRef("a" * 64, 1, media_type=media),
    )
    surface = LibraryImportSurface(
        language,
        "fixture",
        (LibraryCapabilityImport("fixture.logic", identities[0], "fixture", ("run",)),),
    )
    return LibraryArtifactProduct(artifact, surface)
