"""Versioned complete Cargo test classification, without execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, list_value, string_value
from .identity import ContentIdentity, canonical_identity
from .retained_cargo_patches import CargoWorkspaceExpectationV2
from .retained_cargo_test_protocol import (
    RetainedCargoCustomProtocol,
    RetainedCargoExcludedProtocol,
    RetainedCargoLibtestProtocol,
    retained_cargo_protocol_from_dict,
)
from .retained_cargo_tests import RetainedCargoTestTarget, retained_cargo_test_targets


@dataclass(frozen=True, slots=True)
class RetainedCargoTestTargetV2:
    package_root: str
    target_name: str
    target_kinds: tuple[str, ...]
    harness: bool
    protocol: (
        RetainedCargoLibtestProtocol
        | RetainedCargoCustomProtocol
        | RetainedCargoExcludedProtocol
    )

    def __post_init__(self):
        RetainedCargoTestTarget(
            self.package_root, self.target_name, self.target_kinds, ()
        )
        if type(self.harness) is not bool:
            fail(self.__class__.__name__, "requires the exact manifest harness flag")
        if type(self.protocol) not in {
            RetainedCargoLibtestProtocol,
            RetainedCargoCustomProtocol,
            RetainedCargoExcludedProtocol,
        }:
            fail(self.__class__.__name__, "requires a reviewed test protocol")
        if isinstance(self.protocol, RetainedCargoLibtestProtocol) and not self.harness:
            fail(self.__class__.__name__, "libtest requires the manifest harness")
        if isinstance(self.protocol, RetainedCargoCustomProtocol) and self.harness:
            fail(self.__class__.__name__, "custom harness requires harness=false")

    @property
    def key(self):
        return self.package_root, self.target_name, self.target_kinds

    def to_dict(self):
        return {
            "package_root": self.package_root,
            "target_name": self.target_name,
            "target_kinds": list(self.target_kinds),
            "harness": self.harness,
            "protocol": self.protocol.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset(
                {"package_root", "target_name", "target_kinds", "harness", "protocol"}
            ),
        )
        kinds = list_value(data["target_kinds"], cls.__name__)
        if len(kinds) > 16:
            fail(cls.__name__, "target kind allowance exceeded")
        return cls(
            data["package_root"],
            data["target_name"],
            tuple(kinds),
            data["harness"],
            retained_cargo_protocol_from_dict(data["protocol"]),
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoTestInventoryV2:
    importer_project_id: str
    workspace_plan_identity: ContentIdentity
    gate_policy_identity: ContentIdentity
    targets: tuple[RetainedCargoTestTargetV2, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-test-inventory@2"
    MAX_TARGETS: ClassVar[int] = 4096
    MAX_CASES: ClassVar[int] = 100000
    MAX_CUSTOM_INPUTS: ClassVar[int] = 4096

    def __post_init__(self):
        string_value(self.importer_project_id, self.__class__.__name__, max_length=256)
        if any(ord(c) < 32 or ord(c) == 127 for c in self.importer_project_id):
            fail(self.__class__.__name__, "requires a printable importer identifier")
        if any(
            not isinstance(x, ContentIdentity)
            for x in (
                self.workspace_plan_identity,
                self.gate_policy_identity,
            )
        ):
            fail(self.__class__.__name__, "requires exact plan and gate identities")
        if (
            not isinstance(self.targets, tuple)
            or not 1 <= len(self.targets) <= self.MAX_TARGETS
            or any(type(x) is not RetainedCargoTestTargetV2 for x in self.targets)
        ):
            fail(self.__class__.__name__, "requires bounded typed targets")
        keys = tuple(x.key for x in self.targets)
        if keys != tuple(sorted(set(keys))):
            fail(self.__class__.__name__, "requires unique targets in canonical order")
        if (
            sum(
                x.protocol.case_count
                for x in self.targets
                if isinstance(x.protocol, RetainedCargoLibtestProtocol)
            )
            > self.MAX_CASES
        ):
            fail(self.__class__.__name__, "aggregate case allowance exceeded")
        if (
            sum(
                len(x.protocol.inputs)
                for x in self.targets
                if isinstance(x.protocol, RetainedCargoCustomProtocol)
            )
            > self.MAX_CUSTOM_INPUTS
        ):
            fail(self.__class__.__name__, "aggregate custom input allowance exceeded")
        # Empty libtest targets and exclusions are representable. They cannot
        # substitute for at least one reviewed operation that actually executes.
        if not any(
            isinstance(x.protocol, RetainedCargoCustomProtocol)
            or (
                isinstance(x.protocol, RetainedCargoLibtestProtocol)
                and bool(x.protocol.must_run)
            )
            for x in self.targets
        ):
            fail(
                self.__class__.__name__, "requires positive reviewed execution coverage"
            )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def require_graph(self, graph: CargoWorkspaceExpectationV2):
        if type(graph) is not CargoWorkspaceExpectationV2:
            fail(self.__class__.__name__, "requires the version-matched Cargo graph")
        selected = retained_cargo_test_targets(graph.common)
        expected = {(p.root, t.name, tuple(sorted(t.kinds))) for p, t in selected}
        if len(selected) != len(expected) or {x.key for x in self.targets} != expected:
            fail(
                self.__class__.__name__,
                "must classify every selected target exactly once",
            )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "importer_project_id": self.importer_project_id,
            "workspace_plan_identity": self.workspace_plan_identity.to_dict(),
            "gate_policy_identity": self.gate_policy_identity.to_dict(),
            "targets": [x.to_dict() for x in self.targets],
        }

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "importer_project_id",
                    "workspace_plan_identity",
                    "gate_policy_identity",
                    "targets",
                }
            ),
        )
        targets = list_value(data["targets"], cls.__name__)
        if not 1 <= len(targets) <= cls.MAX_TARGETS:
            fail(cls.__name__, "target allowance exceeded")
        # Refuse aggregate expansion before constructing per-target case/input DTOs.
        cases = inputs = 0
        for target in targets:
            if not isinstance(target, dict) or not isinstance(
                target.get("protocol"), dict
            ):
                fail(cls.__name__, "requires tagged target protocols")
            protocol = target["protocol"]
            if protocol.get("kind") == "libtest":
                cases += len(list_value(protocol.get("must_run"), cls.__name__))
                cases += len(list_value(protocol.get("ignored"), cls.__name__))
            elif protocol.get("kind") == "custom-harness":
                inputs += len(list_value(protocol.get("inputs"), cls.__name__))
            if cases > cls.MAX_CASES or inputs > cls.MAX_CUSTOM_INPUTS:
                fail(cls.__name__, "aggregate inventory allowance exceeded")
        return cls(
            data["importer_project_id"],
            ContentIdentity.from_dict(data["workspace_plan_identity"]),
            ContentIdentity.from_dict(data["gate_policy_identity"]),
            tuple(RetainedCargoTestTargetV2.from_dict(x) for x in targets),
        )
