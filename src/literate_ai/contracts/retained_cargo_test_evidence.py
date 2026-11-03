"""Explicit target coverage claims; command references are not trusted execution."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import fail, fields, list_value
from .identity import ContentIdentity
from .retained_cargo_test_protocol import (
    RetainedCargoCustomProtocol,
    RetainedCargoExcludedProtocol,
    RetainedCargoIgnoredCase,
    RetainedCargoLibtestProtocol,
)
from .retained_cargo_tests import RetainedCargoTestTarget
from .retained_cargo_tests_v2 import RetainedCargoTestInventoryV2


@dataclass(frozen=True, slots=True)
class RetainedCargoTargetEvidenceV2:
    package_root: str
    target_name: str
    target_kinds: tuple[str, ...]
    kind: str
    passed_cases: tuple[str, ...]
    ignored_cases: tuple[RetainedCargoIgnoredCase, ...]
    command_observation_identities: tuple[ContentIdentity, ...]
    permission_identity: ContentIdentity | None

    FIELDS = frozenset(
        {
            "package_root",
            "target_name",
            "target_kinds",
            "kind",
            "passed_cases",
            "ignored_cases",
            "command_observation_identities",
            "permission_identity",
        }
    )

    def __post_init__(self):
        RetainedCargoTestTarget(
            self.package_root, self.target_name, self.target_kinds, ()
        )
        if not isinstance(self.kind, str) or self.kind not in {
            "libtest",
            "custom-harness",
            "excluded",
        }:
            fail(
                self.__class__.__name__, "requires an explicit coverage classification"
            )
        RetainedCargoLibtestProtocol(self.passed_cases, self.ignored_cases)
        observations = self.command_observation_identities
        count = {"libtest": 2, "custom-harness": 1, "excluded": 0}[self.kind]
        if (
            not isinstance(observations, tuple)
            or len(observations) != count
            or any(not isinstance(x, ContentIdentity) for x in observations)
            or len(set(observations)) != count
        ):
            fail(self.__class__.__name__, "requires distinct exact phase observations")
        if self.kind != "libtest" and (self.passed_cases or self.ignored_cases):
            fail(self.__class__.__name__, "only libtest can claim named case outcomes")
        if self.kind == "excluded":
            if not isinstance(self.permission_identity, ContentIdentity):
                fail(
                    self.__class__.__name__,
                    "requires independently verified permission",
                )
        elif self.permission_identity is not None:
            fail(self.__class__.__name__, "only exclusions carry permission references")

    @property
    def key(self):
        return self.package_root, self.target_name, self.target_kinds

    def to_dict(self):
        return {
            "package_root": self.package_root,
            "target_name": self.target_name,
            "target_kinds": list(self.target_kinds),
            "kind": self.kind,
            "passed_cases": list(self.passed_cases),
            "ignored_cases": [x.to_dict() for x in self.ignored_cases],
            "command_observation_identities": [
                x.to_dict() for x in self.command_observation_identities
            ],
            "permission_identity": self.permission_identity.to_dict()
            if self.permission_identity
            else None,
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        kinds = list_value(data["target_kinds"], cls.__name__)
        passed = list_value(data["passed_cases"], cls.__name__)
        ignored = list_value(data["ignored_cases"], cls.__name__)
        observations = list_value(data["command_observation_identities"], cls.__name__)
        if (
            len(kinds) > 16
            or len(passed) + len(ignored) > 100000
            or len(observations) > 2
        ):
            fail(cls.__name__, "target evidence allowance exceeded")
        return cls(
            data["package_root"],
            data["target_name"],
            tuple(kinds),
            data["kind"],
            tuple(passed),
            tuple(RetainedCargoIgnoredCase.from_dict(x) for x in ignored),
            tuple(ContentIdentity.from_dict(x) for x in observations),
            ContentIdentity.from_dict(data["permission_identity"])
            if data["permission_identity"] is not None
            else None,
        )


def require_retained_cargo_coverage(outcomes, inventory: RetainedCargoTestInventoryV2):
    """Compare claims to review, without granting execution or exclusion authority."""
    if type(inventory) is not RetainedCargoTestInventoryV2:
        fail("retained.tests.coverage", "requires the matching v2 inventory")
    if tuple(x.key for x in outcomes) != tuple(x.key for x in inventory.targets):
        fail("retained.tests.coverage", "requires every reviewed target exactly once")
    for outcome, target in zip(outcomes, inventory.targets, strict=True):
        protocol = target.protocol
        if isinstance(protocol, RetainedCargoLibtestProtocol):
            if (
                outcome.kind != "libtest"
                or outcome.passed_cases != protocol.must_run
                or outcome.ignored_cases != protocol.ignored
            ):
                fail(
                    "retained.tests.coverage",
                    "named libtest outcomes differ from review",
                )
        elif isinstance(protocol, RetainedCargoCustomProtocol):
            if outcome.kind != "custom-harness":
                fail(
                    "retained.tests.coverage",
                    "custom command coverage differs from review",
                )
        elif isinstance(protocol, RetainedCargoExcludedProtocol):
            if (
                outcome.kind != "excluded"
                or outcome.permission_identity != protocol.permission_identity
            ):
                fail(
                    "retained.tests.coverage",
                    "exclusion permission differs from review",
                )
