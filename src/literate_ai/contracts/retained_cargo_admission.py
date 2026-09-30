"""Reviewed source retirement and durable retained-Cargo admission evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, list_value, string_value
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths


@dataclass(frozen=True, slots=True)
class RetainedCargoSourceRetirement:
    """Importer-reviewed roots that must be absent before consumer admission."""

    importer_project_id: str
    binding_identity: ContentIdentity
    retired_roots: tuple[str, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-source-retirement@1"

    def __post_init__(self) -> None:
        string_value(
            self.importer_project_id,
            "RetainedCargoSourceRetirement.importer_project_id",
            max_length=256,
        )
        if any(ord(char) < 32 or ord(char) == 127 for char in self.importer_project_id):
            fail(
                "RetainedCargoSourceRetirement.importer_project_id",
                "requires a printable project identifier",
            )
        if not isinstance(self.binding_identity, ContentIdentity):
            fail(
                "RetainedCargoSourceRetirement.binding_identity",
                "requires a content identity",
            )
        if (
            not isinstance(self.retired_roots, tuple)
            or not 1 <= len(self.retired_roots) <= 4096
        ):
            fail(
                "RetainedCargoSourceRetirement.retired_roots",
                "requires 1 to 4096 paths",
            )
        canonical_relative_posix_paths(
            self.retired_roots, label="retired Cargo source roots"
        )
        if self.retired_roots != tuple(sorted(set(self.retired_roots))):
            fail(
                "RetainedCargoSourceRetirement.retired_roots",
                "requires sorted unique paths",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "importer_project_id": self.importer_project_id,
            "binding_identity": self.binding_identity.to_dict(),
            "retired_roots": list(self.retired_roots),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RetainedCargoSourceRetirement"
    ) -> RetainedCargoSourceRetirement:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"importer_project_id", "binding_identity", "retired_roots"}
            ),
        )
        roots = list_value(data["retired_roots"], f"{path}.retired_roots")
        return cls(
            string_value(
                data["importer_project_id"],
                f"{path}.importer_project_id",
                max_length=256,
            ),
            ContentIdentity.from_dict(
                data["binding_identity"], path=f"{path}.binding_identity"
            ),
            tuple(roots),
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoAdmissionReceipt:
    """Passing evidence for one exact importer state and reviewed retirement."""

    binding_identity: ContentIdentity
    workspace_plan_identity: ContentIdentity
    gate_policy_identity: ContentIdentity
    test_inventory_identity: ContentIdentity
    source_retirement_identity: ContentIdentity
    consumer_inputs_identity: ContentIdentity
    qualification_identity: ContentIdentity
    command_observation_identities: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-admission-receipt@1"

    def __post_init__(self) -> None:
        for name in (
            "binding_identity",
            "workspace_plan_identity",
            "gate_policy_identity",
            "test_inventory_identity",
            "source_retirement_identity",
            "consumer_inputs_identity",
            "qualification_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"RetainedCargoAdmissionReceipt.{name}", "requires an identity")
        values = self.command_observation_identities
        if (
            not isinstance(values, tuple)
            or not 1 <= len(values) <= 100000
            or any(not isinstance(item, ContentIdentity) for item in values)
        ):
            fail(
                "RetainedCargoAdmissionReceipt.command_observation_identities",
                "requires bounded command identities",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "binding_identity": self.binding_identity.to_dict(),
            "workspace_plan_identity": self.workspace_plan_identity.to_dict(),
            "gate_policy_identity": self.gate_policy_identity.to_dict(),
            "test_inventory_identity": self.test_inventory_identity.to_dict(),
            "source_retirement_identity": self.source_retirement_identity.to_dict(),
            "consumer_inputs_identity": self.consumer_inputs_identity.to_dict(),
            "qualification_identity": self.qualification_identity.to_dict(),
            "command_observation_identities": [
                item.to_dict() for item in self.command_observation_identities
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RetainedCargoAdmissionReceipt"
    ) -> RetainedCargoAdmissionReceipt:
        names = (
            "binding_identity",
            "workspace_plan_identity",
            "gate_policy_identity",
            "test_inventory_identity",
            "source_retirement_identity",
            "consumer_inputs_identity",
            "qualification_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset((*names, "command_observation_identities")),
        )
        observations = list_value(
            data["command_observation_identities"],
            f"{path}.command_observation_identities",
        )
        return cls(
            *(
                ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in names
            ),
            tuple(
                ContentIdentity.from_dict(
                    item, path=f"{path}.command_observation_identities[{index}]"
                )
                for index, item in enumerate(observations)
            ),
        )
