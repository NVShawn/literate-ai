"""Portable SDK generation metadata, without execution or admission authority."""

import hashlib
from dataclasses import dataclass

from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity


@dataclass(frozen=True, slots=True)
class RecipeNativeSdkDependency:
    dependency_id: str
    consumer_revision: ContentIdentity
    input_identity: ContentIdentity
    target_identity: ContentIdentity
    selection_identity: ContentIdentity
    source_lock_identity: ContentIdentity
    source_build_plan_identity: ContentIdentity
    sdk_snapshot_identity: ContentIdentity
    import_surface: LibraryImportSurface
    integration_contract: ContentReference
    integration_content: str

    def __post_init__(self) -> None:
        if not isinstance(self.dependency_id, str) or not self.dependency_id:
            raise ValueError("SDK generation dependency requires a dependency ID")
        for name in (
            "consumer_revision",
            "input_identity",
            "target_identity",
            "selection_identity",
            "source_lock_identity",
            "source_build_plan_identity",
            "sdk_snapshot_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError("SDK generation dependency requires exact identities")
        if not isinstance(self.import_surface, LibraryImportSurface):
            raise TypeError("SDK generation dependency requires an import surface")
        reference = self.integration_contract
        if (
            not isinstance(reference, ContentReference)
            or reference.kind != "integration-contract"
            or not isinstance(self.integration_content, str)
        ):
            raise ValueError(
                "SDK generation dependency requires a public integration contract"
            )
        actual = (
            "sha256:"
            + hashlib.sha256(self.integration_content.encode("utf-8")).hexdigest()
        )
        if actual != reference.identity.uri or any(
            item.interface_identity != reference.identity
            for item in self.import_surface.capabilities
        ):
            raise ValueError(
                "SDK generation import surface differs from its public contract"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/recipe-native-sdk-dependency@1",
            "dependency_id": self.dependency_id,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "consumer_revision",
                    "input_identity",
                    "target_identity",
                    "selection_identity",
                    "source_lock_identity",
                    "source_build_plan_identity",
                    "sdk_snapshot_identity",
                )
            },
            "import_surface": self.import_surface.to_dict(),
            "integration_contract": self.integration_contract.to_dict(),
            "integration_content": self.integration_content,
        }
