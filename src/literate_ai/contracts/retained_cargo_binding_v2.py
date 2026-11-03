"""Version-matched reviewed Cargo import intent, never live importer authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail
from .blobs import BlobRef
from .identity import canonical_identity
from .retained_cargo_policy import RetainedLibraryGatePolicyV2
from .retained_cargo_tests_v2 import RetainedCargoTestInventoryV2
from .retained_cargo_workspace_v2 import RetainedCargoWorkspacePlanV2
from .retained_libraries import RetainedLibraryBinding


@dataclass(frozen=True, slots=True)
class RetainedCargoBindingV2:
    """Bind exact policy/inventory files as well as the existing provider closure.

    Receivers must reopen all three JSON blobs under byte custody and parse
    their explicit v2 codecs before calling require_declarations. That method
    checks declared identities only; it cannot prove that any blob was read,
    that a provider is trusted or that exclusions are independently permitted.
    """

    common: RetainedLibraryBinding
    gate_policy: BlobRef
    test_inventory: BlobRef

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-binding@2"

    def __post_init__(self):
        if type(self.common) is not RetainedLibraryBinding:
            fail(
                self.__class__.__name__,
                "requires the exact provider binding declarations",
            )
        for value in (
            self.common.workspace_plan,
            self.gate_policy,
            self.test_inventory,
        ):
            if (
                type(value) is not BlobRef
                or value.media_type != "application/json"
                or not 0 < value.size <= 256 * 1024 * 1024
            ):
                fail(
                    self.__class__.__name__,
                    "requires finite nonempty reviewed JSON blobs",
                )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def require_declarations(
        self,
        plan: RetainedCargoWorkspacePlanV2,
        policy: RetainedLibraryGatePolicyV2,
        inventory: RetainedCargoTestInventoryV2,
    ) -> None:
        if (
            type(plan) is not RetainedCargoWorkspacePlanV2
            or type(policy) is not RetainedLibraryGatePolicyV2
            or type(inventory) is not RetainedCargoTestInventoryV2
        ):
            fail(
                self.__class__.__name__,
                "requires matching v2 plan, policy and inventory",
            )
        plan.require_gate_policy(policy)
        inventory.require_graph(plan.graph)
        if (
            policy.common.importer_project_id != self.common.importer_project_id
            or inventory.importer_project_id != self.common.importer_project_id
            or inventory.workspace_plan_identity != plan.identity
            or inventory.gate_policy_identity != policy.identity
        ):
            fail(self.__class__.__name__, "reviewed declaration identities differ")

    def to_dict(self):
        result = self.common.to_dict()
        result.update(
            schema=self.SCHEMA,
            gate_policy=self.gate_policy.to_dict(),
            test_inventory=self.test_inventory.to_dict(),
        )
        return result

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=RetainedLibraryBinding._FIELDS | {"gate_policy", "test_inventory"},
        )
        common = {key: data[key] for key in RetainedLibraryBinding._FIELDS}
        common["schema"] = RetainedLibraryBinding.SCHEMA
        return cls(
            RetainedLibraryBinding.from_dict(common),
            BlobRef.from_dict(data["gate_policy"]),
            BlobRef.from_dict(data["test_inventory"]),
        )
