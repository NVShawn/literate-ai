"""Reviewed retained Cargo case inventory; parsing never grants admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, list_value, string_value
from .cargo_workspace import CargoWorkspaceExpectation
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths


def validate_retained_test_cases(values, maximum_cases=100000):
    if (
        type(maximum_cases) is not int
        or maximum_cases < 1
        or not isinstance(values, tuple)
        or len(values) > maximum_cases
        or any(
            not isinstance(v, str)
            or not v
            or len(v) > 1024
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v)
            for v in values
        )
        or values != tuple(sorted(set(values)))
    ):
        fail("retained.tests.cases", "requires bounded, sorted, unique case names")


def retained_cargo_test_targets(graph: CargoWorkspaceExpectation):
    """Targets selected by workspace/all-targets, including false test flags."""
    if not isinstance(graph, CargoWorkspaceExpectation):
        fail("retained.tests.graph", "requires a reviewed Cargo graph")
    return tuple(
        (package, target)
        for package in graph.packages
        if package.root in graph.members
        for target in package.targets
        if "custom-build" not in target.kinds
        and set(target.required_features) <= set(package.features)
    )


@dataclass(frozen=True, slots=True)
class RetainedCargoTestTarget:
    package_root: str
    target_name: str
    target_kinds: tuple[str, ...]
    cases: tuple[str, ...]

    def __post_init__(self):
        string_value(self.package_root, "retained.tests.package_root", max_length=1024)
        if self.package_root != ".":
            canonical_relative_posix_paths(
                (self.package_root,), label="test package root"
            )
        string_value(self.target_name, "retained.tests.target_name", max_length=256)
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.target_name):
            fail("retained.tests.target_name", "requires a printable target name")
        if (
            not isinstance(self.target_kinds, tuple)
            or not 1 <= len(self.target_kinds) <= 16
        ):
            fail("retained.tests.target_kinds", "requires bounded target kinds")
        for kind in self.target_kinds:
            string_value(kind, "retained.tests.target_kind", max_length=128)
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in kind):
                fail("retained.tests.target_kind", "requires a printable target kind")
        if self.target_kinds != tuple(sorted(set(self.target_kinds))):
            fail("retained.tests.target_kinds", "requires sorted unique target kinds")
        validate_retained_test_cases(self.cases)

    @property
    def key(self):
        return self.package_root, self.target_name, self.target_kinds

    def to_dict(self):
        return dict(
            package_root=self.package_root,
            target_name=self.target_name,
            target_kinds=list(self.target_kinds),
            cases=list(self.cases),
        )

    @classmethod
    def from_dict(cls, value, *, path="RetainedCargoTestTarget"):
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"package_root", "target_name", "target_kinds", "cases"}
            ),
        )
        kinds = list_value(data["target_kinds"], f"{path}.target_kinds")
        cases = list_value(data["cases"], f"{path}.cases")
        if len(kinds) > 16 or len(cases) > 100000:
            fail(path, "test inventory bounds exceeded")
        return cls(
            data["package_root"], data["target_name"], tuple(kinds), tuple(cases)
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoTestInventory:
    importer_project_id: str
    workspace_plan_identity: ContentIdentity
    gate_policy_identity: ContentIdentity
    targets: tuple[RetainedCargoTestTarget, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-test-inventory@1"
    MAX_TARGETS: ClassVar[int] = 4096
    MAX_CASES: ClassVar[int] = 100000

    def __post_init__(self):
        string_value(
            self.importer_project_id,
            "retained.tests.importer_project_id",
            max_length=256,
        )
        if any(ord(c) < 32 or ord(c) == 127 for c in self.importer_project_id):
            fail(
                "retained.tests.importer_project_id",
                "requires a printable project identifier",
            )
        for identity in (self.workspace_plan_identity, self.gate_policy_identity):
            if not isinstance(identity, ContentIdentity):
                fail("retained.tests.identity", "requires a content identity")
        if (
            not isinstance(self.targets, tuple)
            or not 1 <= len(self.targets) <= self.MAX_TARGETS
            or any(not isinstance(t, RetainedCargoTestTarget) for t in self.targets)
        ):
            fail("retained.tests.targets", "requires bounded typed test targets")
        keys = tuple(t.key for t in self.targets)
        if keys != tuple(sorted(set(keys))):
            fail("retained.tests.targets", "requires sorted unique targets")
        if not 1 <= sum(len(t.cases) for t in self.targets) <= self.MAX_CASES:
            fail("retained.tests.cases", "requires 1 to 100000 cases overall")

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def require_graph(self, graph: CargoWorkspaceExpectation):
        selected = retained_cargo_test_targets(graph)
        expected = {(p.root, t.name, tuple(sorted(t.kinds))) for p, t in selected}
        if len(selected) != len(expected) or {t.key for t in self.targets} != expected:
            fail("retained.tests.targets", "must cover every selected reviewed target")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "importer_project_id": self.importer_project_id,
            "workspace_plan_identity": self.workspace_plan_identity.to_dict(),
            "gate_policy_identity": self.gate_policy_identity.to_dict(),
            "targets": [target.to_dict() for target in self.targets],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path="RetainedCargoTestInventory"):
        data = contract_fields(
            value,
            path=path,
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
        targets = list_value(data["targets"], f"{path}.targets")
        if not 1 <= len(targets) <= cls.MAX_TARGETS:
            fail(f"{path}.targets", "requires 1 to 4096 targets")
        return cls(
            data["importer_project_id"],
            ContentIdentity.from_dict(
                data["workspace_plan_identity"], path=f"{path}.workspace_plan_identity"
            ),
            ContentIdentity.from_dict(
                data["gate_policy_identity"], path=f"{path}.gate_policy_identity"
            ),
            tuple(
                RetainedCargoTestTarget.from_dict(t, path=f"{path}.targets[{i}]")
                for i, t in enumerate(targets)
            ),
        )
