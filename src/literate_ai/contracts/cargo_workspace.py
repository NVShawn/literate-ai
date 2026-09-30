"""Reviewed Cargo graph values shared by wire plans and native observation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._validation import fields, list_value


class CargoWorkspaceGraphError(ValueError):
    """A sanitized refusal, without producer-controlled paths or diagnostics."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CargoWorkspaceGraphError("cargo.workspace." + code)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _relative(value: object, *, root: bool = False) -> bool:
    if value == ".":
        return root
    return (
        _text(value)
        and "\\" not in value
        and ":" not in value
        and not value.startswith("/")
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _strings(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and all(_text(item) for item in value)
        and len(set(value)) == len(value)
    )


@dataclass(frozen=True, slots=True)
class CargoTargetExpectation:
    name: str
    kinds: tuple[str, ...]
    crate_types: tuple[str, ...]
    source: str  # workspace-relative, including tests outside the package root
    edition: str
    test: bool
    doctest: bool
    required_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(
            _text(self.name)
            and _strings(self.kinds)
            and bool(self.kinds)
            and _strings(self.crate_types)
            and bool(self.crate_types)
            and _relative(self.source)
            and _text(self.edition)
            and type(self.test) is bool
            and type(self.doctest) is bool
            and _strings(self.required_features),
            "invalid_target_expectation",
        )


@dataclass(frozen=True, slots=True)
class CargoPackageExpectation:
    root: str
    name: str
    version: str
    features: tuple[str, ...]
    targets: tuple[CargoTargetExpectation, ...]

    def __post_init__(self) -> None:
        _require(
            _relative(self.root, root=True)
            and _text(self.name)
            and _text(self.version)
            and _strings(self.features)
            and isinstance(self.targets, tuple)
            and bool(self.targets)
            and all(isinstance(item, CargoTargetExpectation) for item in self.targets),
            "invalid_package_expectation",
        )
        _require(len(set(self.targets)) == len(self.targets), "duplicate_target")


@dataclass(frozen=True, slots=True)
class CargoDependencyExpectation:
    consumer: str
    provider: str
    alias: str  # manifest key; resolved_name is Cargo's library/renamed target name
    resolved_name: str
    kind: str | None
    target: str | None
    features: tuple[str, ...]
    uses_default_features: bool
    optional: bool
    version_requirement: str
    resolved: bool  # expected presence in metadata; not cfg evaluation

    def __post_init__(self) -> None:
        _require(
            _relative(self.consumer, root=True)
            and _relative(self.provider, root=True)
            and _text(self.alias)
            and _text(self.resolved_name)
            and self.kind in (None, "dev", "build")
            and (self.target is None or _text(self.target))
            and _strings(self.features)
            and type(self.uses_default_features) is bool
            and type(self.optional) is bool
            and _text(self.version_requirement)
            and type(self.resolved) is bool,
            "invalid_dependency_expectation",
        )


@dataclass(frozen=True, slots=True)
class CargoWorkspaceExpectation:
    packages: tuple[CargoPackageExpectation, ...]
    dependencies: tuple[CargoDependencyExpectation, ...]
    members: tuple[str, ...]
    default_members: tuple[str, ...]
    root_package: str | None  # None for a virtual workspace
    output_directory: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.packages, tuple)
            and bool(self.packages)
            and all(isinstance(p, CargoPackageExpectation) for p in self.packages)
            and isinstance(self.dependencies, tuple)
            and all(
                isinstance(d, CargoDependencyExpectation) for d in self.dependencies
            )
            and _strings(self.members)
            and _strings(self.default_members),
            "invalid_expectation",
        )
        roots = {p.root for p in self.packages}
        _require(len(roots) == len(self.packages), "duplicate_package_root")
        _require(
            bool(self.members)
            and set(self.members) <= roots
            and set(self.default_members) <= set(self.members)
            and (
                self.root_package is None
                or (_text(self.root_package) and self.root_package in roots)
            )
            and all(
                d.consumer in roots and d.provider in roots for d in self.dependencies
            ),
            "unknown_expected_package",
        )
        keys = [(d.consumer, d.alias, d.kind, d.target) for d in self.dependencies]
        _require(len(set(keys)) == len(keys), "duplicate_dependency")
        edges = [
            (d.consumer, d.provider, d.resolved_name, d.kind, d.target)
            for d in self.dependencies
            if d.resolved
        ]
        _require(len(set(edges)) == len(edges), "duplicate_resolved_dependency")
        _require(
            _relative(self.output_directory)
            and all(
                root == "."
                or not (
                    self.output_directory == root
                    or self.output_directory.startswith(root + "/")
                    or root.startswith(self.output_directory + "/")
                )
                for root in roots
            ),
            "output_package_overlap",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [
                {
                    "root": p.root,
                    "name": p.name,
                    "version": p.version,
                    "features": list(p.features),
                    "targets": [
                        {
                            "name": t.name,
                            "kinds": list(t.kinds),
                            "crate_types": list(t.crate_types),
                            "source": t.source,
                            "edition": t.edition,
                            "test": t.test,
                            "doctest": t.doctest,
                            "required_features": list(t.required_features),
                        }
                        for t in p.targets
                    ],
                }
                for p in self.packages
            ],
            "dependencies": [
                {
                    "consumer": d.consumer,
                    "provider": d.provider,
                    "alias": d.alias,
                    "resolved_name": d.resolved_name,
                    "kind": d.kind,
                    "target": d.target,
                    "features": list(d.features),
                    "uses_default_features": d.uses_default_features,
                    "optional": d.optional,
                    "version_requirement": d.version_requirement,
                    "resolved": d.resolved,
                }
                for d in self.dependencies
            ],
            "members": list(self.members),
            "default_members": list(self.default_members),
            "root_package": self.root_package,
            "output_directory": self.output_directory,
        }

    @classmethod
    def from_dict(cls, value: Any) -> CargoWorkspaceExpectation:
        data = fields(
            value,
            path="CargoWorkspaceExpectation",
            required=frozenset(
                {
                    "packages",
                    "dependencies",
                    "members",
                    "default_members",
                    "root_package",
                    "output_directory",
                }
            ),
        )
        packages = []
        for raw in _bounded_array(data["packages"]):
            p = fields(
                raw,
                path="CargoPackageExpectation",
                required=frozenset(
                    {
                        "root",
                        "name",
                        "version",
                        "features",
                        "targets",
                    }
                ),
            )
            targets = []
            for raw_target in _bounded_array(p["targets"]):
                t = dict(
                    fields(
                        raw_target,
                        path="CargoTargetExpectation",
                        required=frozenset(
                            {
                                "name",
                                "kinds",
                                "crate_types",
                                "source",
                                "edition",
                                "test",
                                "doctest",
                                "required_features",
                            }
                        ),
                    )
                )
                for key in ("kinds", "crate_types", "required_features"):
                    t[key] = _bounded_array(t[key])
                targets.append(CargoTargetExpectation(**t))
            packages.append(
                CargoPackageExpectation(
                    p["root"],
                    p["name"],
                    p["version"],
                    _bounded_array(p["features"]),
                    tuple(targets),
                )
            )
        dependencies = []
        for raw in _bounded_array(data["dependencies"]):
            d = dict(
                fields(
                    raw,
                    path="CargoDependencyExpectation",
                    required=frozenset(
                        {
                            "consumer",
                            "provider",
                            "alias",
                            "resolved_name",
                            "kind",
                            "target",
                            "features",
                            "uses_default_features",
                            "optional",
                            "version_requirement",
                            "resolved",
                        }
                    ),
                )
            )
            d["features"] = _bounded_array(d["features"])
            dependencies.append(CargoDependencyExpectation(**d))
        return cls(
            tuple(packages),
            tuple(dependencies),
            _bounded_array(data["members"]),
            _bounded_array(data["default_members"]),
            data["root_package"],
            data["output_directory"],
        )


def _bounded_array(value: Any) -> tuple[Any, ...]:
    items = list_value(value, "CargoWorkspaceExpectation.array")
    _require(len(items) <= 4096, "expectation_limit")
    return tuple(items)
