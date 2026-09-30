"""Bind lifecycle-verified Python payload observations to source BOM references."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from literate_ai.contracts import ContentIdentity

from .acquisition import _source_package_identity
from .python_source import PythonSourceAuthority
from .types import DependencyObservationError, HostDependencyObservation


@dataclass(frozen=True)
class PythonDependencyEvidence:
    """A trusted lifecycle callback's freshly revalidated payload evidence.

    Not deserializable build metadata or authorization. The callback must verify
    the source, target, build binding and files against an independently retained
    identity before returning this record, on every resolution/cache reuse.
    """

    source: PythonSourceAuthority
    identity: ContentIdentity
    observation: HostDependencyObservation


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-resolution-invalid", message)


def _properties(component: Mapping[str, object], name: str) -> set[str]:
    return {
        item["value"]
        for item in component.get("properties", [])
        if isinstance(item, Mapping)
        and item.get("name") == name
        and isinstance(item.get("value"), str)
    }


def project_python_evidence(
    source_content: bytes,
    *,
    root_ref: str,
    source: PythonSourceAuthority,
    evidence: PythonDependencyEvidence,
) -> HostDependencyObservation:
    """Require exact closure and remap package coordinates, never guess BOM refs."""
    if (
        not isinstance(evidence, PythonDependencyEvidence)
        or evidence.source != source
        or not isinstance(evidence.identity, ContentIdentity)
        or not isinstance(evidence.observation, HostDependencyObservation)
    ):
        _fail("Python resolution lacks source-bound installed evidence")
    expected = {package.name: package for package in source.lock.packages}
    observed = {}
    ref_to_name = {"@root": "@root"}
    for component in evidence.observation.components:
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "pypi":
            _fail("Python installation contains an unexpected package coordinate")
        name = coordinate[1]
        ref = component.get("bom-ref")
        if (
            name not in expected
            or name in observed
            or not isinstance(ref, str)
            or ref in ref_to_name
            or component.get("version") != expected[name].version
        ):
            _fail("Python installation inventory differs from the exact lock")
        observed[name] = component
        ref_to_name[ref] = name
    if set(observed) != set(expected):
        _fail("Python installation omits locked packages")
    try:
        edges = {
            (ref_to_name[parent], ref_to_name[child])
            for parent, child in evidence.observation.edges
        }
    except KeyError as exc:
        raise DependencyObservationError(
            "dependencies.python-resolution-invalid",
            "Python installation edge references an unknown package",
        ) from exc
    if edges != set(source.lock.edges):
        _fail("Python installation edges differ from the selected lock graph")

    projected = []
    refs = {"@root": root_ref}
    for component in json.loads(source_content).get("components", []):
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "pypi":
            continue
        name = coordinate[1]
        if name not in observed or name in refs:
            _fail("Source BOM has an unobserved or duplicate Python package")
        candidate = observed[name]
        alias_key = "literate-ai:python-top-level-import"
        if not _properties(component, alias_key) <= _properties(candidate, alias_key):
            _fail("Source BOM claims Python imports absent from installed payloads")
        tree_key = "literate-ai:python-installed-tree-identity"
        if not _properties(component, tree_key) <= _properties(candidate, tree_key):
            _fail("Source BOM claims a different installed Python payload identity")
        ref = component.get("bom-ref")
        if not isinstance(ref, str) or ref in refs.values():
            _fail("Source BOM Python references are ambiguous")
        refs[name] = ref
        projected.append({**candidate, "bom-ref": ref, "name": component["name"]})
    if set(refs) != {"@root", *expected}:
        _fail("Source BOM omits an installed Python package")
    return HostDependencyObservation(
        tuple(projected),
        tuple(sorted((refs[parent], refs[child]) for parent, child in edges)),
    )
