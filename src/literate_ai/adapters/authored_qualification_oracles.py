"""Keep original verifier instances and exact independent inputs current."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from literate_ai.adapters.authored_qualification_authority import (
    CurrentAuthoredQualificationAuthority,
)
from literate_ai.adapters.component_acceptance import (
    LibraryAcceptance,
    load_versioned_library_acceptance,
)
from literate_ai.adapters.rust_library_acceptance import (
    RustLibraryAcceptance,
    capture_loaded_rust_library_verifier_inputs,
)
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts.authored_qualification_profiles import (
    AuthoredQualificationProfile,
)
from literate_ai.contracts.flavors import FlavorAxis
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.contracts.product_json import product_json_identity
from literate_ai.projects import PinnedInputClosure
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationCaseSurfaceBinding,
    QualificationVerifierCaseMap,
)


class AuthoredQualificationOracleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AuthoredQualificationOracleBinding:
    component_revision_identity: ContentIdentity
    oracle: LibraryAcceptance = field(repr=False, compare=False)
    oracle_identity: ContentIdentity
    case_map: QualificationVerifierCaseMap
    rust_verifier_inputs_identity: ContentIdentity | None


@dataclass(frozen=True, slots=True)
class CurrentAuthoredQualificationOracles:
    """Current verifier inputs, never a decoded grant or generated acceptance.

    Retain the originally loaded Rust v2 objects: replacing or reconstructing a
    dataclass must not mint its private verifier custody. The later native
    execution boundary still joins the exact tools, package and execution rights.
    """

    authority: CurrentAuthoredQualificationAuthority
    profile: AuthoredQualificationProfile
    bindings: tuple[AuthoredQualificationOracleBinding, ...]
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    @contextmanager
    def capture_current(self):
        self.authority.require_unchanged()
        self._inputs.require_unchanged()
        with ExitStack() as stack:
            for binding in self.bindings:
                if binding.oracle.identity != binding.oracle_identity:
                    raise AuthoredQualificationOracleError(
                        "qualification.authored.oracle_changed",
                        "a verifier-owned oracle changed after selection",
                    )
                if type(binding.oracle) is RustLibraryAcceptance:
                    scope = stack.enter_context(
                        capture_loaded_rust_library_verifier_inputs(binding.oracle)
                    )
                    if scope.identity != binding.rust_verifier_inputs_identity:
                        raise AuthoredQualificationOracleError(
                            "qualification.authored.verifier_inputs_changed",
                            "original Rust verifier input custody changed",
                        )
            yield self
            self._inputs.require_unchanged()
            self.authority.require_unchanged()
            for binding in self.bindings:
                if binding.oracle.identity != binding.oracle_identity:
                    raise AuthoredQualificationOracleError(
                        "qualification.authored.oracle_changed",
                        "a verifier-owned oracle changed during qualification",
                    )

    def require_unchanged(self) -> None:
        with self.capture_current():
            pass

    @property
    def identity(self) -> ContentIdentity:
        self.require_unchanged()
        return canonical_identity(
            {
                "schema": "literate-ai/current-authored-qualification-oracles@1",
                "origin_identity": self.authority.origin.identity.uri,
                "profile_identity": self.profile.identity.uri,
                "input_closure_identity": self._inputs.identity,
                "bindings": [
                    {
                        "component_revision_identity": (
                            item.component_revision_identity.uri
                        ),
                        "oracle_identity": item.oracle_identity.uri,
                        "case_map_identity": item.case_map.identity.uri,
                        "rust_verifier_inputs_identity": (
                            item.rust_verifier_inputs_identity.uri
                            if item.rust_verifier_inputs_identity is not None
                            else None
                        ),
                    }
                    for item in self.bindings
                ],
            }
        )


def _unique_object(rows):
    result = dict(rows)
    if len(result) != len(rows):
        raise ValueError("qualification.authored.duplicate_authority_field")
    return result


def _nonfinite(_value):
    raise ValueError("qualification.authored.nonfinite_authority_value")


def _library_surface(node):
    authoring = node.revision.definition
    if authoring.resolved_kind != "library":
        raise AuthoredQualificationOracleError(
            "qualification.authored.library_oracle_required",
            "this verifier composition requires a library oracle for every node",
        )
    languages = tuple(
        item.value
        for slot in node.target_flavor_selection.slots
        if slot.slot.axis is FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM
        for item in slot.selected
    )
    if len(languages) != 1:
        raise AuthoredQualificationOracleError(
            "qualification.authored.language_ambiguous",
            "every library requires one exact selected implementation language",
        )
    interfaces = {
        item.capability: item.interface_identity for item in node.interface_bindings
    }
    if set(interfaces) != {item.name for item in authoring.provides}:
        raise AuthoredQualificationOracleError(
            "qualification.authored.interface_incomplete",
            "every provided library capability requires its exact public interface",
        )
    return project_library_import_surface(
        node.revision.coordinate.name,
        languages[0],
        tuple((item.name, interfaces[item.name]) for item in authoring.provides),
        declarations=authoring.library_imports,
    )


def read_current_authored_qualification_oracles(
    authority: CurrentAuthoredQualificationAuthority,
    profile_path: str = "litai/qualification.json",
    *,
    maximum_file_bytes: int = 16 * 1024 * 1024,
) -> CurrentAuthoredQualificationOracles:
    """Seal all selected private oracles before any generation or execution.

    New framework-owned inputs stay under litai/. Coordinate-qualified oracle
    names prevent namespaces with the same Component name from sharing a file.
    The legacy source-derived profile, oracle locations and readers are unchanged.
    """

    if not isinstance(authority, CurrentAuthoredQualificationAuthority):
        raise TypeError("current authored authority must be independently reopened")
    if type(maximum_file_bytes) is not int or maximum_file_bytes <= 0:
        raise ValueError("qualification.authored.file_limit_invalid")
    canonical_relative_posix_path(profile_path, label="authored qualification profile")
    if PurePosixPath(profile_path).parts[0] != "litai":
        raise AuthoredQualificationOracleError(
            "qualification.authored.profile_outside_litai",
            "authored qualification profiles must be inside litai/",
        )
    authority.require_unchanged()
    root = authority.project.root
    nodes = tuple(
        sorted(
            authority.locked.authority.lock.nodes,
            key=lambda n: n.revision.coordinate.uri,
        )
    )
    limits = PinnedInputClosure.for_project(authority.project)
    inputs = PinnedInputClosure(
        maximum_files=min(1 + 2 * len(nodes), limits.maximum_files),
        maximum_file_bytes=min(maximum_file_bytes, limits.maximum_file_bytes),
        maximum_total_bytes=limits.maximum_total_bytes,
    )
    content = inputs.pin(
        root / profile_path, boundary=root, label="authored qualification profile"
    )
    profile = AuthoredQualificationProfile.from_dict(
        json.loads(content, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    )
    if tuple(item.component for item in profile.oracles) != tuple(
        node.revision.coordinate for node in nodes
    ):
        raise AuthoredQualificationOracleError(
            "qualification.authored.oracle_membership_mismatch",
            "profile must select exactly one oracle for every locked Component",
        )
    origin_identity = authority.origin.identity
    bindings = []
    for node, selected in zip(nodes, profile.oracles, strict=True):
        coordinate = node.revision.coordinate
        path = (
            root
            / "litai/verification/acceptance"
            / coordinate.namespace
            / f"{coordinate.name}.json"
        )

        def read_bytes(selected_path, *, oracle_path=path):
            relative = selected_path.relative_to(root).as_posix()
            content = inputs.pin(selected_path, boundary=root, label=relative)
            if selected_path == oracle_path:
                # Preserve legacy readers while refusing ambiguous new authored
                # authority, including duplicate keys inside private answers.
                json.loads(
                    content,
                    object_pairs_hook=_unique_object,
                    parse_constant=_nonfinite,
                )
            return content

        surface = _library_surface(node)
        oracle = load_versioned_library_acceptance(
            path, coordinate.name, read_bytes=read_bytes
        )
        expected_interfaces = tuple(
            sorted(
                (item.identity for item in node.revision.public_interfaces),
                key=lambda i: i.uri,
            )
        )
        if (
            oracle.identity != selected.oracle_identity
            or oracle.specification_set_identity
            != node.revision.specification_set_identity
            or oracle.public_interface_identities != expected_interfaces
            or oracle.import_surface_identity != surface.identity
            or oracle.language != surface.language
        ):
            raise AuthoredQualificationOracleError(
                "qualification.authored.oracle_authority_mismatch",
                "selected oracle differs from current locked library authority",
            )
        if {case.capability for case in oracle.cases} != {
            item.capability for item in surface.capabilities
        }:
            raise AuthoredQualificationOracleError(
                "qualification.authored.oracle_coverage_incomplete",
                "independent oracle cases must cover every exact public capability",
            )
        verifier = canonical_identity(
            {
                "provider": "authored-independent-library-oracle@1",
                "origin_identity": origin_identity.uri,
                "profile_identity": profile.identity.uri,
                "component_revision_identity": node.revision.identity.uri,
                "oracle_identity": oracle.identity.uri,
            }
        )
        case_map = QualificationVerifierCaseMap(
            verifier,
            tuple(
                QualificationCaseSurfaceBinding(
                    case.case_id,
                    product_json_identity(
                        {
                            "case_id": case.case_id,
                            "capability": case.capability,
                            "arguments": case.arguments,
                            "expected_result": case.expected_result,
                        }
                    ),
                    (case.capability,),
                )
                for case in oracle.cases
            ),
        )
        rust_inputs = None
        if type(oracle) is RustLibraryAcceptance:
            with capture_loaded_rust_library_verifier_inputs(oracle) as scope:
                rust_inputs = scope.identity
        bindings.append(
            AuthoredQualificationOracleBinding(
                node.revision.identity, oracle, oracle.identity, case_map, rust_inputs
            )
        )
    result = CurrentAuthoredQualificationOracles(
        authority, profile, tuple(bindings), inputs
    )
    result.require_unchanged()
    return result
