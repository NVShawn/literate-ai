"""Independent behavioral-surface inventory contracts for inverse translation."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from literate_ai.source_to_specification.contracts import (
    EvidenceReference,
    SourceToSpecificationError,
    canonical_digest,
)
from literate_ai.source_to_specification.literal_data import (
    literal_table_symbol_suffixes,
)

BEHAVIORAL_SURFACE_SCHEMA = (
    "urn:literate-ai:schema:v1:behavioral-surface-inventory-item"
)
BEHAVIORAL_SURFACE_INVENTORY_SCHEMA = (
    "urn:literate-ai:schema:v1:behavioral-surface-inventory"
)


class SurfaceIntelligence(Protocol):
    authority_source_snapshot_id: str
    provider_id: str
    provider_version: str
    executable_identity: str
    evidence: tuple[object, ...]
    relationships: tuple[object, ...]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceToSpecificationError(
            "surface_inventory.text_invalid", f"{label} must be a non-empty string"
        )
    return value.strip()


def _identity(value: object, label: str) -> str:
    normalized = _text(value, label)
    if not normalized.startswith("sha256:") or len(normalized) != 71:
        raise SourceToSpecificationError(
            "surface_inventory.identity_invalid",
            f"{label} must be an exact sha256 content identity",
        )
    try:
        int(normalized[7:], 16)
    except ValueError as exc:
        raise SourceToSpecificationError(
            "surface_inventory.identity_invalid",
            f"{label} must be an exact sha256 content identity",
        ) from exc
    return normalized


class BehavioralInterfaceKind(StrEnum):
    ENTRYPOINT = "entrypoint"
    EXPORT = "export"
    IO_PROTOCOL = "io-protocol"
    CONFIGURATION = "configuration"
    ERROR = "error"
    STATE = "state"
    ORDERING = "ordering"
    NORMALIZATION = "normalization"
    TEST_OBSERVED = "test-observed"
    COMPONENT_BOUNDARY = "component-boundary"
    DEPENDENCY_EDGE = "dependency-edge"
    LITERAL_DATA = "literal-data"


class BehavioralSurfaceRequirement(StrEnum):
    REQUIRED = "required"
    ADVISORY = "advisory"


class BehavioralSurfaceDisposition(StrEnum):
    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class BehavioralSurfaceInventoryItem:
    """One detector-owned surface; translators cannot alter its stable identity."""

    surface_id: str
    detector_identity: str
    language: str
    interface_kind: BehavioralInterfaceKind
    path: str
    symbol: str
    evidence: tuple[EvidenceReference, ...]
    requirement: BehavioralSurfaceRequirement
    disposition: BehavioralSurfaceDisposition
    mapped_observation_ids: tuple[str, ...] = ()
    exclusion_reason: str | None = None
    exclusion_review_identity: str | None = None

    SCHEMA: ClassVar[str] = BEHAVIORAL_SURFACE_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.surface_id, "surface_id")
        _identity(self.detector_identity, "detector_identity")
        _text(self.language, "language")
        if not isinstance(self.interface_kind, BehavioralInterfaceKind):
            raise SourceToSpecificationError(
                "surface_inventory.kind_invalid",
                "interface_kind must be a BehavioralInterfaceKind",
            )
        _text(self.path, "path")
        if not isinstance(self.symbol, str):
            raise SourceToSpecificationError(
                "surface_inventory.symbol_invalid", "symbol must be a string"
            )
        if not self.evidence or any(
            not isinstance(item, EvidenceReference) for item in self.evidence
        ):
            raise SourceToSpecificationError(
                "surface_inventory.evidence_missing",
                "every surface requires exact evidence",
            )
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if evidence_ids != tuple(sorted(set(evidence_ids))):
            raise SourceToSpecificationError(
                "surface_inventory.evidence_noncanonical",
                "surface evidence must be unique and canonically ordered",
            )
        if not isinstance(
            self.requirement, BehavioralSurfaceRequirement
        ) or not isinstance(self.disposition, BehavioralSurfaceDisposition):
            raise SourceToSpecificationError(
                "surface_inventory.status_invalid",
                "surface requirement and disposition must be typed",
            )
        if self.mapped_observation_ids != tuple(
            sorted(set(self.mapped_observation_ids))
        ) or any(not item for item in self.mapped_observation_ids):
            raise SourceToSpecificationError(
                "surface_inventory.mapping_noncanonical",
                "mapped observation IDs must be unique and canonically ordered",
            )
        mapped = self.disposition is BehavioralSurfaceDisposition.MAPPED
        excluded = self.disposition is BehavioralSurfaceDisposition.EXCLUDED
        if mapped != bool(self.mapped_observation_ids):
            raise SourceToSpecificationError(
                "surface_inventory.mapping_inconsistent",
                "mapped disposition requires one or more exact observations",
            )
        if excluded != (
            self.exclusion_reason is not None
            and self.exclusion_review_identity is not None
        ):
            raise SourceToSpecificationError(
                "surface_inventory.exclusion_unreviewed",
                "exclusion requires both a reviewed reason and review identity",
            )
        if excluded:
            _text(self.exclusion_reason, "exclusion_reason")
            _identity(self.exclusion_review_identity, "exclusion_review_identity")
        if self.surface_id != canonical_digest(self.identity_material()):
            raise SourceToSpecificationError(
                "surface_inventory.surface_identity_mismatch",
                "surface ID does not bind its detector-owned semantic location",
            )

    @classmethod
    def create(
        cls,
        *,
        detector_identity: str,
        language: str,
        interface_kind: BehavioralInterfaceKind,
        path: str,
        symbol: str,
        evidence: Iterable[EvidenceReference],
        requirement: BehavioralSurfaceRequirement,
        disposition: BehavioralSurfaceDisposition = (
            BehavioralSurfaceDisposition.UNMAPPED
        ),
        mapped_observation_ids: Iterable[str] = (),
        exclusion_reason: str | None = None,
        exclusion_review_identity: str | None = None,
    ) -> BehavioralSurfaceInventoryItem:
        exact_evidence = tuple(sorted(evidence, key=lambda item: item.evidence_id))
        material = {
            "detector_identity": detector_identity,
            "language": language,
            "interface_kind": interface_kind.value,
            "path": path,
            "symbol": symbol,
            "evidence_ids": [item.evidence_id for item in exact_evidence],
            "requirement": requirement.value,
        }
        return cls(
            canonical_digest(material),
            detector_identity,
            language,
            interface_kind,
            path,
            symbol,
            exact_evidence,
            requirement,
            disposition,
            tuple(sorted(set(mapped_observation_ids))),
            exclusion_reason,
            exclusion_review_identity,
        )

    def identity_material(self) -> dict[str, object]:
        return {
            "detector_identity": self.detector_identity,
            "language": self.language,
            "interface_kind": self.interface_kind.value,
            "path": self.path,
            "symbol": self.symbol,
            "evidence_ids": [item.evidence_id for item in self.evidence],
            "requirement": self.requirement.value,
        }

    def mapped(self, observation_ids: Iterable[str]) -> BehavioralSurfaceInventoryItem:
        exact = tuple(sorted(set(observation_ids)))
        return replace(
            self,
            disposition=BehavioralSurfaceDisposition.MAPPED,
            mapped_observation_ids=exact,
            exclusion_reason=None,
            exclusion_review_identity=None,
        )

    def excluded(
        self, *, reason: str, review_identity: str
    ) -> BehavioralSurfaceInventoryItem:
        return replace(
            self,
            disposition=BehavioralSurfaceDisposition.EXCLUDED,
            mapped_observation_ids=(),
            exclusion_reason=reason,
            exclusion_review_identity=review_identity,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "surface_id": self.surface_id,
            **self.identity_material(),
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source_snapshot_id": item.source_snapshot_id,
                    "content_digest": item.content_digest,
                    "path": item.path,
                    "symbol": item.symbol,
                }
                for item in self.evidence
            ],
            "disposition": self.disposition.value,
            "mapped_observation_ids": list(self.mapped_observation_ids),
            "exclusion_reason": self.exclusion_reason,
            "exclusion_review_identity": self.exclusion_review_identity,
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "BehavioralSurfaceInventoryItem"
    ) -> BehavioralSurfaceInventoryItem:
        if not isinstance(value, dict):
            raise SourceToSpecificationError(
                "surface_inventory.item_invalid", f"{path} must be an object"
            )
        expected = {
            "schema",
            "surface_id",
            "detector_identity",
            "language",
            "interface_kind",
            "path",
            "symbol",
            "evidence_ids",
            "requirement",
            "evidence",
            "disposition",
            "mapped_observation_ids",
            "exclusion_reason",
            "exclusion_review_identity",
        }
        if set(value) != expected or value.get("schema") != cls.SCHEMA:
            raise SourceToSpecificationError(
                "surface_inventory.item_schema_invalid",
                f"{path} has an unsupported schema or field set",
            )
        raw_evidence = value["evidence"]
        if not isinstance(raw_evidence, list):
            raise SourceToSpecificationError(
                "surface_inventory.evidence_invalid",
                f"{path}.evidence must be an array",
            )
        evidence: list[EvidenceReference] = []
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict) or set(item) != {
                "evidence_id",
                "source_snapshot_id",
                "content_digest",
                "path",
                "symbol",
            }:
                raise SourceToSpecificationError(
                    "surface_inventory.evidence_invalid",
                    f"{path}.evidence[{index}] must be an exact evidence reference",
                )
            evidence.append(
                EvidenceReference(
                    item["evidence_id"],
                    item["source_snapshot_id"],
                    item["content_digest"],
                    item["path"],
                    item["symbol"],
                )
            )
        evidence_ids = value["evidence_ids"]
        mapped = value["mapped_observation_ids"]
        if not isinstance(evidence_ids, list) or not isinstance(mapped, list):
            raise SourceToSpecificationError(
                "surface_inventory.array_invalid",
                f"{path} evidence and mapping IDs must be arrays",
            )
        item = cls(
            value["surface_id"],
            value["detector_identity"],
            value["language"],
            BehavioralInterfaceKind(value["interface_kind"]),
            value["path"],
            value["symbol"],
            tuple(evidence),
            BehavioralSurfaceRequirement(value["requirement"]),
            BehavioralSurfaceDisposition(value["disposition"]),
            tuple(mapped),
            value["exclusion_reason"],
            value["exclusion_review_identity"],
        )
        if evidence_ids != [entry.evidence_id for entry in item.evidence]:
            raise SourceToSpecificationError(
                "surface_inventory.evidence_identity_mismatch",
                f"{path}.evidence_ids differ from exact evidence",
            )
        return item


@dataclass(frozen=True, slots=True)
class BehavioralSurfaceInventory:
    """Canonical detector result, optionally reconciled by a distinct translator."""

    source_snapshot_identity: str
    collector_identity: str
    translator_identity: str | None
    surfaces: tuple[BehavioralSurfaceInventoryItem, ...]

    SCHEMA: ClassVar[str] = BEHAVIORAL_SURFACE_INVENTORY_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.source_snapshot_identity, "source_snapshot_identity")
        _identity(self.collector_identity, "collector_identity")
        if self.translator_identity is not None:
            _identity(self.translator_identity, "translator_identity")
            if self.translator_identity == self.collector_identity:
                raise SourceToSpecificationError(
                    "surface_inventory.provider_not_independent",
                    "surface collector and translator identities must be distinct",
                )
        if not self.surfaces:
            raise SourceToSpecificationError(
                "surface_inventory.empty", "behavioral surface inventory is empty"
            )
        ids = tuple(item.surface_id for item in self.surfaces)
        if ids != tuple(sorted(set(ids))):
            raise SourceToSpecificationError(
                "surface_inventory.noncanonical",
                "surface inventory must be unique and canonically ordered",
            )
        if self.translator_identity is not None and any(
            item.detector_identity == self.translator_identity
            and item.requirement is not BehavioralSurfaceRequirement.ADVISORY
            for item in self.surfaces
        ):
            raise SourceToSpecificationError(
                "surface_inventory.translator_claims_required",
                "translator-proposed surfaces must remain advisory",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def blocking_surface_ids(self) -> tuple[str, ...]:
        return tuple(
            item.surface_id
            for item in self.surfaces
            if item.requirement is BehavioralSurfaceRequirement.REQUIRED
            and item.disposition is BehavioralSurfaceDisposition.UNMAPPED
        )

    def require_complete(self) -> None:
        if self.blocking_surface_ids:
            blocking = tuple(
                item
                for item in self.surfaces
                if item.surface_id in frozenset(self.blocking_surface_ids)
            )
            raise SourceToSpecificationError(
                "surface_inventory.required_unmapped",
                "required behavioral surfaces remain unmapped: "
                + ", ".join(
                    f"{item.surface_id} ({item.interface_kind.value} "
                    f"{item.path}:{item.symbol})"
                    for item in blocking
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_snapshot_identity": self.source_snapshot_identity,
            "collector_identity": self.collector_identity,
            "translator_identity": self.translator_identity,
            "surfaces": [item.to_dict() for item in self.surfaces],
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "BehavioralSurfaceInventory"
    ) -> BehavioralSurfaceInventory:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "source_snapshot_identity",
            "collector_identity",
            "translator_identity",
            "surfaces",
        }:
            raise SourceToSpecificationError(
                "surface_inventory.schema_invalid",
                f"{path} must be an exact behavioral surface inventory",
            )
        if value.get("schema") != cls.SCHEMA or not isinstance(value["surfaces"], list):
            raise SourceToSpecificationError(
                "surface_inventory.schema_invalid",
                f"{path} has an unsupported schema or surfaces value",
            )
        return cls(
            value["source_snapshot_identity"],
            value["collector_identity"],
            value["translator_identity"],
            tuple(
                BehavioralSurfaceInventoryItem.from_dict(
                    item, path=f"{path}.surfaces[{index}]"
                )
                for index, item in enumerate(value["surfaces"])
            ),
        )


def source_intelligence_surface_collector_identity(
    intelligence: SurfaceIntelligence,
) -> str:
    """Identify the deterministic projection separately from its index provider."""

    return canonical_digest(
        {
            "schema": "literate-ai/behavioral-surface-collector@1",
            "provider_id": intelligence.provider_id,
            "provider_version": intelligence.provider_version,
            "executable_identity": intelligence.executable_identity,
            "projection": "public-symbol-dependency-ordering-and-normalization-v3",
        }
    )


def _interface_kind(
    *, path: str, symbol: str, kind: str, content: str
) -> BehavioralInterfaceKind:
    material = " ".join((path, symbol, kind, content[:4096])).casefold()
    basename = path.rsplit("/", 1)[-1].casefold()
    if "test" in kind.casefold() or basename.startswith("test") or ".test." in basename:
        return BehavioralInterfaceKind.TEST_OBSERVED
    if symbol.casefold() in {"main", "run", "serve", "start"} or "__main__" in material:
        return BehavioralInterfaceKind.ENTRYPOINT
    if symbol.endswith(("Error", "Exception")) or any(
        token in material for token in (" raise ", " throw ", " except ", " catch (")
    ):
        return BehavioralInterfaceKind.ERROR
    if any(
        token in material for token in ("config", "setting", "environment variable")
    ):
        return BehavioralInterfaceKind.CONFIGURATION
    if any(
        token in material
        for token in ("request", "response", "stdin", "stdout", "http")
    ):
        return BehavioralInterfaceKind.IO_PROTOCOL
    if any(
        token in material for token in ("state", "transaction", "persist", "database")
    ):
        return BehavioralInterfaceKind.STATE
    return BehavioralInterfaceKind.EXPORT


_INTERFACE_FACETS: dict[BehavioralInterfaceKind, frozenset[str]] = {
    BehavioralInterfaceKind.ENTRYPOINT: frozenset(
        {"entrypoint", "entrypoints", "api", "public-api", "operations"}
    ),
    BehavioralInterfaceKind.EXPORT: frozenset(
        {"api", "public-api", "api-surface", "data-contracts", "entrypoints"}
    ),
    BehavioralInterfaceKind.IO_PROTOCOL: frozenset(
        {"api", "public-api", "data-contracts", "behavior", "entrypoints"}
    ),
    BehavioralInterfaceKind.CONFIGURATION: frozenset(
        {"configuration", "operations", "deployment"}
    ),
    BehavioralInterfaceKind.ERROR: frozenset(
        {"errors", "error", "api", "public-api", "behavior", "security"}
    ),
    BehavioralInterfaceKind.STATE: frozenset(
        {"state", "behavior", "operations", "persistence", "concurrency"}
    ),
    BehavioralInterfaceKind.ORDERING: frozenset(
        {"behavior", "invariants", "runtime-semantics", "tests"}
    ),
    BehavioralInterfaceKind.NORMALIZATION: frozenset(
        {"behavior", "invariants", "runtime-semantics", "io-protocol", "tests"}
    ),
    BehavioralInterfaceKind.TEST_OBSERVED: frozenset(
        {"tests", "behavior", "negative-paths", "compatibility"}
    ),
    BehavioralInterfaceKind.COMPONENT_BOUNDARY: frozenset(
        {"architecture", "dependencies", "entrypoints", "public-api"}
    ),
    BehavioralInterfaceKind.DEPENDENCY_EDGE: frozenset(
        {"architecture", "dependencies", "build-graph", "deployment"}
    ),
    BehavioralInterfaceKind.LITERAL_DATA: frozenset(
        {"data-contracts", "behavior", "state", "api", "public-api"}
    ),
}


def observation_facet_maps_interface(
    facet: str, interface_kind: BehavioralInterfaceKind
) -> bool:
    """Require semantic-facet agreement in addition to shared evidence."""

    normalized = _text(facet, "observation facet").casefold().replace("_", "-")
    return normalized in _INTERFACE_FACETS[interface_kind]


def interface_kind_for_observation_facet(facet: str) -> BehavioralInterfaceKind:
    """Choose one conservative interface kind for a model-only advisory proposal."""

    normalized = _text(facet, "observation facet").casefold().replace("_", "-")
    priority = (
        BehavioralInterfaceKind.ENTRYPOINT,
        BehavioralInterfaceKind.ERROR,
        BehavioralInterfaceKind.CONFIGURATION,
        BehavioralInterfaceKind.STATE,
        BehavioralInterfaceKind.ORDERING,
        BehavioralInterfaceKind.NORMALIZATION,
        BehavioralInterfaceKind.IO_PROTOCOL,
        BehavioralInterfaceKind.TEST_OBSERVED,
        BehavioralInterfaceKind.DEPENDENCY_EDGE,
        BehavioralInterfaceKind.COMPONENT_BOUNDARY,
        BehavioralInterfaceKind.EXPORT,
    )
    return next(
        (kind for kind in priority if normalized in _INTERFACE_FACETS[kind]),
        BehavioralInterfaceKind.EXPORT,
    )


def create_model_surface_proposal(
    *,
    translator_identity: str,
    language: str,
    observation_id: str,
    facet: str,
    evidence: Iterable[EvidenceReference],
) -> BehavioralSurfaceInventoryItem:
    """Retain a model-only addition as advisory, never detector-owned authority."""

    exact_evidence = tuple(sorted(evidence, key=lambda item: item.evidence_id))
    if not exact_evidence:
        raise SourceToSpecificationError(
            "surface_inventory.model_proposal_evidence_missing",
            "model surface proposals require exact evidence",
        )
    paths = tuple(sorted({item.path for item in exact_evidence}))
    path = paths[0] if len(paths) == 1 else "<multiple-evidence-paths>"
    return BehavioralSurfaceInventoryItem.create(
        detector_identity=translator_identity,
        language=language,
        interface_kind=interface_kind_for_observation_facet(facet),
        path=path,
        symbol=f"model-proposal:{_text(observation_id, 'observation_id')}",
        evidence=exact_evidence,
        requirement=BehavioralSurfaceRequirement.ADVISORY,
    )


def _dependency_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", value)
    return None if match is None else match.group(1)


def _is_test_source_path(path: str, language: str) -> bool:
    """Keep test implementation evidence out of the required product surface set.

    Tests remain in the admitted evidence partition and may support an observation. They
    classes, functions, or exports.
    """

    normalized = path.replace("\\", "/").casefold()
    parts = tuple(part for part in normalized.split("/") if part)
    if any(part in {"test", "tests", "__tests__"} for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if language == "python":
        return (
            name == "conftest.py"
            or name.startswith("test_")
            or name.endswith("_test.py")
        )
    if language in {"javascript", "typescript"}:
        return bool(re.search(r"(?:^|\.)(?:test|spec)\.(?:[cm]?[jt]sx?)$", name))
    if language == "rust":
        return name.endswith("_test.rs") or name.endswith("_tests.rs")
    if language == "cpp":
        stem = re.sub(r"\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx)$", "", name)
        return stem.endswith("_test") or stem.endswith("_tests")
    return False


def _manifest_surface_keys(
    *, path: str, language: str, content: str
) -> tuple[tuple[BehavioralInterfaceKind, str], ...]:
    """Return conservative, well-known manifest declarations without model inference."""

    name = path.rsplit("/", 1)[-1]
    keys: set[tuple[BehavioralInterfaceKind, str]] = set()
    try:
        if name == "package.json" and language == "javascript":
            value = json.loads(content)
            if not isinstance(value, dict):
                return ()
            for field in ("main", "module"):
                if isinstance(value.get(field), str):
                    keys.add(
                        (BehavioralInterfaceKind.ENTRYPOINT, f"{field}:{value[field]}")
                    )
            binary = value.get("bin")
            if isinstance(binary, str):
                keys.add((BehavioralInterfaceKind.ENTRYPOINT, f"bin:{binary}"))
            elif isinstance(binary, dict):
                keys.update(
                    (BehavioralInterfaceKind.ENTRYPOINT, f"bin:{key}")
                    for key, target in binary.items()
                    if isinstance(key, str) and isinstance(target, str)
                )
            exported = value.get("exports")
            if isinstance(exported, (str, list, dict)):
                keys.add((BehavioralInterfaceKind.EXPORT, "package-exports"))
            for field in ("dependencies", "peerDependencies", "optionalDependencies"):
                dependencies = value.get(field)
                if isinstance(dependencies, dict):
                    keys.update(
                        (BehavioralInterfaceKind.DEPENDENCY_EDGE, f"dependency:{key}")
                        for key in dependencies
                        if isinstance(key, str) and key
                    )
        elif name == "pyproject.toml" and language == "python":
            value = tomllib.loads(content)
            project = value.get("project", {})
            if not isinstance(project, dict):
                return ()
            for field in ("scripts", "gui-scripts"):
                scripts = project.get(field)
                if isinstance(scripts, dict):
                    keys.update(
                        (BehavioralInterfaceKind.ENTRYPOINT, f"{field}:{key}")
                        for key, target in scripts.items()
                        if isinstance(key, str) and isinstance(target, str)
                    )
            dependencies = project.get("dependencies")
            if isinstance(dependencies, list):
                keys.update(
                    (
                        BehavioralInterfaceKind.DEPENDENCY_EDGE,
                        f"dependency:{dependency}",
                    )
                    for item in dependencies
                    if (dependency := _dependency_name(item)) is not None
                )
        elif name == "Cargo.toml" and language == "rust":
            value = tomllib.loads(content)
            package = value.get("package")
            if isinstance(package, dict) and isinstance(package.get("name"), str):
                keys.add(
                    (
                        BehavioralInterfaceKind.COMPONENT_BOUNDARY,
                        f"crate:{package['name']}",
                    )
                )
            if isinstance(value.get("lib"), dict):
                keys.add((BehavioralInterfaceKind.EXPORT, "crate-library"))
            binaries = value.get("bin")
            if isinstance(binaries, list):
                keys.update(
                    (BehavioralInterfaceKind.ENTRYPOINT, f"bin:{item['name']}")
                    for item in binaries
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                )
            dependencies = value.get("dependencies")
            if isinstance(dependencies, dict):
                keys.update(
                    (BehavioralInterfaceKind.DEPENDENCY_EDGE, f"dependency:{key}")
                    for key in dependencies
                    if isinstance(key, str) and key
                )
        elif name == "MODULE.bazel":
            keys.update(
                (BehavioralInterfaceKind.DEPENDENCY_EDGE, f"bazel-module:{module}")
                for module in re.findall(
                    r"bazel_dep\s*\(\s*name\s*=\s*[\"']([^\"']+)[\"']", content
                )
            )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError):
        return ()
    return tuple(sorted(keys, key=lambda item: (item[0].value, item[1])))


def _ordering_surface_symbols(
    *, language: str, symbol: str, content: str
) -> tuple[str, ...]:
    """Flag concrete comparison constructs for a distinct model coverage audit.

    These are locations, not inferred behavior.  The language translator must still
    recover direction, comparison domain, normalization, stability, and independence
    from encounter order from the exact cited source evidence.
    """

    patterns = {
        "python": (
            ("sequence-sort", r"\bsorted\s*\(|\.sort\s*\("),
            ("selection-tie-break", r"\b(?:min|max)\s*\([^\n]*\bkey\s*="),
        ),
        "javascript": (
            ("sequence-sort", r"\.sort\s*\("),
            ("locale-comparison", r"\.localeCompare\s*\("),
        ),
        "typescript": (
            ("sequence-sort", r"\.sort\s*\("),
            ("locale-comparison", r"\.localeCompare\s*\("),
        ),
        "rust": (
            ("sequence-sort", r"\.sort(?:_by|_by_key|_unstable(?:_by|_by_key)?)?\s*\("),
            ("selection-tie-break", r"\.(?:min|max)_by(?:_key)?\s*\("),
        ),
        "cpp": (
            ("sequence-sort", r"\bstd::(?:stable_)?sort\s*\("),
            ("selection-tie-break", r"\bstd::(?:min|max)_element\s*\("),
        ),
    }
    return tuple(
        f"{symbol}#ordering:{name}"
        for name, pattern in patterns.get(language, ())
        if re.search(pattern, content)
    )


def _normalization_surface_symbols(
    *, language: str, symbol: str, content: str
) -> tuple[str, ...]:
    """Locate explicit value normalization for a distinct model coverage audit."""

    patterns = {
        "python": (
            ("trim", r"\.strip\s*\("),
            ("case-fold", r"\.(?:lower|upper|casefold)\s*\("),
            ("pattern-rewrite", r"\bre\.(?:sub|subn)\s*\("),
        ),
        "javascript": (
            ("trim", r"\.trim\s*\("),
            ("case-fold", r"\.to(?:Lower|Upper)Case\s*\("),
            ("pattern-rewrite", r"\.replace(?:All)?\s*\("),
        ),
        "typescript": (
            ("trim", r"\.trim\s*\("),
            ("case-fold", r"\.to(?:Lower|Upper)Case\s*\("),
            ("pattern-rewrite", r"\.replace(?:All)?\s*\("),
        ),
        "rust": (
            ("trim", r"\.trim(?:_matches|_start|_end)?\s*\("),
            ("case-fold", r"\.to_(?:lower|upper)case\s*\("),
            ("pattern-rewrite", r"\.replace\s*\("),
        ),
        "cpp": (
            ("trim", r"\b(?:trim|strip)\s*\("),
            ("case-fold", r"\bstd::to(?:lower|upper)\s*\("),
            ("pattern-rewrite", r"\bstd::regex_replace\s*\("),
        ),
    }
    return tuple(
        f"{symbol}#normalization:{name}"
        for name, pattern in patterns.get(language, ())
        if re.search(pattern, content)
    )


def collect_behavioral_surface_inventory(
    intelligence: SurfaceIntelligence,
) -> BehavioralSurfaceInventory:
    """Project exact pre-translation source evidence into stable required surfaces.

    The projection intentionally contains no model output. A later reconciliation step
    may map observations or record reviewed exclusions without changing any surface ID.
    """

    collector = source_intelligence_surface_collector_identity(intelligence)
    grouped: dict[
        tuple[str, BehavioralInterfaceKind, str, str], list[EvidenceReference]
    ] = {}
    evidence_by_location: dict[tuple[str, str], list[EvidenceReference]] = {}
    for item in intelligence.evidence:
        reference = getattr(item, "reference", None)
        if not isinstance(reference, EvidenceReference):
            continue
        language = _text(getattr(item, "language", None), "evidence language")
        kind = _text(getattr(item, "kind", None), "evidence kind")
        content = getattr(item, "content", "")
        if not isinstance(content, str):
            raise SourceToSpecificationError(
                "surface_inventory.evidence_content_invalid",
                "surface evidence content must be text",
            )
        if _is_test_source_path(reference.path, language):
            continue
        if not reference.symbol:
            for interface_kind, symbol in _manifest_surface_keys(
                path=reference.path, language=language, content=content
            ):
                grouped.setdefault(
                    (language, interface_kind, reference.path, symbol), []
                ).append(reference)
            continue
        interface_kind = _interface_kind(
            path=reference.path,
            symbol=reference.symbol,
            kind=kind,
            content=content,
        )
        grouped.setdefault(
            (language, interface_kind, reference.path, reference.symbol), []
        ).append(reference)
        for ordering_symbol in _ordering_surface_symbols(
            language=language,
            symbol=reference.symbol,
            content=content,
        ):
            grouped.setdefault(
                (
                    language,
                    BehavioralInterfaceKind.ORDERING,
                    reference.path,
                    ordering_symbol,
                ),
                [],
            ).append(reference)
        for normalization_symbol in _normalization_surface_symbols(
            language=language,
            symbol=reference.symbol,
            content=content,
        ):
            grouped.setdefault(
                (
                    language,
                    BehavioralInterfaceKind.NORMALIZATION,
                    reference.path,
                    normalization_symbol,
                ),
                [],
            ).append(reference)
        for literal_symbol in literal_table_symbol_suffixes(
            language=language,
            content=content,
        ):
            grouped.setdefault(
                (
                    language,
                    BehavioralInterfaceKind.LITERAL_DATA,
                    reference.path,
                    f"{reference.symbol}#{literal_symbol}",
                ),
                [],
            ).append(reference)
        evidence_by_location.setdefault((reference.path, reference.symbol), []).append(
            reference
        )
    for relationship in intelligence.relationships:
        relationship_kind = str(getattr(relationship, "kind", "")).casefold()
        if relationship_kind not in {
            "depends-on",
            "dependency",
            "imports",
            "requires",
            "uses-package",
        }:
            continue
        source_path = str(getattr(relationship, "source_path", ""))
        source_symbol = str(getattr(relationship, "source_symbol", ""))
        exact = evidence_by_location.get((source_path, source_symbol), ())
        if not exact:
            continue
        language = _text(
            getattr(relationship, "source_language", None),
            "relationship source language",
        )
        target = _text(
            getattr(relationship, "target_symbol", None),
            "relationship target symbol",
        )
        grouped.setdefault(
            (
                language,
                BehavioralInterfaceKind.DEPENDENCY_EDGE,
                source_path,
                f"{source_symbol}->{target}",
            ),
            [],
        ).extend(exact)
    surfaces = tuple(
        sorted(
            (
                BehavioralSurfaceInventoryItem.create(
                    detector_identity=collector,
                    language=language,
                    interface_kind=kind,
                    path=path,
                    symbol=symbol,
                    evidence=references,
                    requirement=BehavioralSurfaceRequirement.REQUIRED,
                )
                for (language, kind, path, symbol), references in grouped.items()
            ),
            key=lambda item: item.surface_id,
        )
    )
    if not surfaces:
        raise SourceToSpecificationError(
            "surface_inventory.unsupported",
            "deterministic source intelligence discovered no public behavioral surface",
        )
    return BehavioralSurfaceInventory(
        intelligence.authority_source_snapshot_id,
        collector,
        None,
        surfaces,
    )


def reconcile_behavioral_surface_inventory(
    detected: BehavioralSurfaceInventory,
    *,
    translator_identity: str,
    observations: Mapping[str, tuple[str, Iterable[str]]],
    verifier_surfaces: Iterable[BehavioralSurfaceInventoryItem] = (),
    model_proposals: Iterable[BehavioralSurfaceInventoryItem] = (),
    reviewed_exclusions: Mapping[str, tuple[str, str]] | None = None,
) -> BehavioralSurfaceInventory:
    """Merge independent and advisory surfaces without translator deletion rights."""

    if not isinstance(detected, BehavioralSurfaceInventory):
        raise TypeError("detected must be a BehavioralSurfaceInventory")
    if detected.translator_identity is not None:
        raise SourceToSpecificationError(
            "surface_inventory.already_reconciled",
            "only an unreconciled detector inventory may be merged",
        )
    exact_translator = _identity(translator_identity, "translator_identity")
    verifier = tuple(verifier_surfaces)
    proposals = tuple(model_proposals)
    if any(
        not isinstance(item, BehavioralSurfaceInventoryItem)
        or item.detector_identity == exact_translator
        or item.requirement is not BehavioralSurfaceRequirement.REQUIRED
        for item in verifier
    ):
        raise SourceToSpecificationError(
            "surface_inventory.verifier_surface_invalid",
            "verifier surfaces must be required and independently detected",
        )
    if any(
        not isinstance(item, BehavioralSurfaceInventoryItem)
        or item.detector_identity != exact_translator
        or item.requirement is not BehavioralSurfaceRequirement.ADVISORY
        for item in proposals
    ):
        raise SourceToSpecificationError(
            "surface_inventory.model_proposal_invalid",
            "model proposals must be advisory and bind the exact translator",
        )
    merged: dict[str, BehavioralSurfaceInventoryItem] = {}
    for item in (*detected.surfaces, *verifier, *proposals):
        existing = merged.get(item.surface_id)
        if (
            existing is not None
            and existing.identity_material() != item.identity_material()
        ):
            raise SourceToSpecificationError(
                "surface_inventory.surface_conflict",
                "the same surface ID has conflicting detector material",
            )
        merged[item.surface_id] = item
    normalized_observations: dict[str, tuple[str, frozenset[str]]] = {}
    for observation_id, value in observations.items():
        if (
            not isinstance(observation_id, str)
            or not observation_id
            or not isinstance(value, tuple)
            or len(value) != 2
        ):
            raise SourceToSpecificationError(
                "surface_inventory.observation_invalid",
                "observation mappings must contain an ID, facet, and evidence IDs",
            )
        facet, evidence_ids = value
        normalized_observations[observation_id] = (
            _text(facet, "observation facet"),
            frozenset(_text(item, "observation evidence ID") for item in evidence_ids),
        )
    exclusions = {} if reviewed_exclusions is None else dict(reviewed_exclusions)
    unknown_exclusions = set(exclusions) - set(merged)
    if unknown_exclusions:
        raise SourceToSpecificationError(
            "surface_inventory.exclusion_unknown",
            "reviewed exclusion names an unknown surface",
        )
    reconciled = []
    for surface_id, surface in merged.items():
        exclusion = exclusions.get(surface_id)
        if exclusion is not None:
            if not isinstance(exclusion, tuple) or len(exclusion) != 2:
                raise SourceToSpecificationError(
                    "surface_inventory.exclusion_invalid",
                    "reviewed exclusions require a reason and review identity",
                )
            reconciled.append(
                surface.excluded(reason=exclusion[0], review_identity=exclusion[1])
            )
            continue
        surface_evidence = {item.evidence_id for item in surface.evidence}
        mapped = tuple(
            sorted(
                observation_id
                for observation_id, (
                    facet,
                    evidence_ids,
                ) in normalized_observations.items()
                if surface_evidence & evidence_ids
                and observation_facet_maps_interface(facet, surface.interface_kind)
            )
        )
        reconciled.append(
            surface.mapped(mapped)
            if mapped
            else replace(
                surface,
                disposition=BehavioralSurfaceDisposition.UNMAPPED,
                mapped_observation_ids=(),
                exclusion_reason=None,
                exclusion_review_identity=None,
            )
        )
    detector_identities = tuple(
        sorted({item.detector_identity for item in (*detected.surfaces, *verifier)})
    )
    collector_identity = (
        detected.collector_identity
        if detector_identities == (detected.collector_identity,)
        else canonical_digest(
            {
                "schema": "literate-ai/behavioral-surface-collector-set@1",
                "collectors": list(detector_identities),
            }
        )
    )
    return BehavioralSurfaceInventory(
        detected.source_snapshot_identity,
        collector_identity,
        exact_translator,
        tuple(sorted(reconciled, key=lambda item: item.surface_id)),
    )


__all__ = [
    "BEHAVIORAL_SURFACE_INVENTORY_SCHEMA",
    "BEHAVIORAL_SURFACE_SCHEMA",
    "BehavioralInterfaceKind",
    "BehavioralSurfaceDisposition",
    "BehavioralSurfaceInventory",
    "BehavioralSurfaceInventoryItem",
    "BehavioralSurfaceRequirement",
    "SurfaceIntelligence",
    "collect_behavioral_surface_inventory",
    "create_model_surface_proposal",
    "interface_kind_for_observation_facet",
    "observation_facet_maps_interface",
    "reconcile_behavioral_surface_inventory",
    "source_intelligence_surface_collector_identity",
]
