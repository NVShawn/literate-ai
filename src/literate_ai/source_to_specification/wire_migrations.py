"""Deterministic, fail-closed migration into current inverse wire contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from literate_ai.schema_catalog import (
    SUPPORTED_SCHEMA_CATALOGS,
    SchemaCatalogError,
    schema_catalog_root,
)

V1_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v1:source-to-specification-result"
)
V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v2:source-to-specification-result"
)
V2_COMPONENT_GRAPH_DRAFT_SCHEMA = "urn:literate-ai:schema:v2:component-graph-draft"
V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v3:source-to-specification-result"
)
V3_COMPONENT_GRAPH_DRAFT_SCHEMA = "urn:literate-ai:schema:v3:component-graph-draft"
V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA = (
    "urn:literate-ai:schema:v2:component-authority-projection"
)

_V1_RESULT_FIELDS = frozenset(
    {
        "request",
        "observations",
        "draft",
        "coverage",
        "uncertainty",
        "run",
        "component_definition_draft",
    }
)
_UNRELEASED_RESULT_FIELDS = _V1_RESULT_FIELDS | {"component_graph_draft"}
_V2_RESULT_FIELDS = _UNRELEASED_RESULT_FIELDS | {"schema"}
_GRAPH_FIELDS = frozenset({"source_snapshot_id", "root_coordinate", "nodes", "edges"})
_V2_GRAPH_FIELDS = _GRAPH_FIELDS | {"schema"}
_V3_GRAPH_FIELDS = _V2_GRAPH_FIELDS
_V2_AUTHORITY_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "component_coordinate",
        "component_revision_identity",
        "state",
        "transition",
        "source_snapshot_identity",
        "specification_set_identity",
        "target_lock_identity",
        "generation_closure",
        "verifier_identity",
        "policy_identity",
        "evidence_identities",
        "provenance_reference_identity",
        "prior_projection_identity",
    }
)
_LEGACY_RESULT_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@1"
_V2_RESULT_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@2"

_V1_QUALIFICATION_SCHEMAS = {
    "urn:literate-ai:schema:v1:regenerative-qualification-policy": (
        "urn:literate-ai:schema:v2:regenerative-qualification-policy",
        frozenset(
            {
                "schema",
                "policy_id",
                "minimum_clean_runs",
                "required_target_profile_ids",
                "required_surface_ids",
                "allow_skipped_tests",
            }
        ),
    ),
    "urn:literate-ai:schema:v1:clean-regeneration-evidence": (
        "urn:literate-ai:schema:v2:clean-regeneration-evidence",
        frozenset(
            {
                "schema",
                "run_id",
                "run_attestation_id",
                "source_snapshot_id",
                "specification_set_id",
                "target_profile_id",
                "flavor_lock_id",
                "generation_recipe_id",
                "generated_tree_id",
                "build_result_id",
                "generated_test_result_id",
                "independent_parity_result_id",
                "covered_surface_ids",
                "source_excluded_from_generation",
                "empty_workspace",
                "generated_source_cache_hit",
                "build_passed",
                "generated_tests_passed",
                "independent_parity_passed",
                "generated_tests_total",
                "generated_tests_succeeded",
                "generated_tests_failed",
                "generated_tests_skipped",
            }
        ),
    ),
    "urn:literate-ai:schema:v1:regenerative-qualification-decision": (
        "urn:literate-ai:schema:v2:regenerative-qualification-decision",
        frozenset(
            {
                "schema",
                "source_snapshot_id",
                "specification_set_id",
                "policy_id",
                "policy_identity",
                "evidence_ids",
                "authority",
                "blockers",
            }
        ),
    ),
    "urn:literate-ai:schema:v1:operational-qualification-attestation": (
        "urn:literate-ai:schema:v2:operational-qualification-attestation",
        frozenset(
            {
                "schema",
                "plan",
                "source_snapshot_id",
                "regenerator_identity",
                "regeneration",
                "parity_verifier_identity",
                "parity_request",
                "parity",
                "attestor_identity",
                "empty_workspace",
                "exact_generation_inputs",
            }
        ),
    ),
    "urn:literate-ai:schema:v1:local-qualification-signature": (
        "urn:literate-ai:schema:v2:local-qualification-signature",
        frozenset({"schema", "identity", "signer", "payload_identity", "signature"}),
    ),
}


class WireMigrationError(ValueError):
    """A wire document cannot be identified or migrated without invention."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _catalog_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise OSError("schema catalog entries must be regular files")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _wire_schema_registry_for_catalogs(
    catalogs: tuple[tuple[str, str, str], ...],
) -> Registry:
    registry = Registry()
    try:
        for _version, root_text, fingerprint in catalogs:
            root = Path(root_text)
            for path in sorted(root.glob("*.schema.json")):
                document = json.loads(path.read_bytes())
                identifier = document.get("$id")
                if not isinstance(identifier, str):
                    raise WireMigrationError(
                        "wire.schema_catalog_invalid",
                        f"schema lacks a root identity: {path.name}",
                    )
                registry = registry.with_resource(
                    identifier, Resource.from_contents(document)
                )
            if _catalog_fingerprint(root) != fingerprint:
                raise OSError("schema catalog changed while its registry was built")
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SchemaCatalogError,
    ) as exc:
        raise WireMigrationError(
            "wire.schema_catalog_invalid",
            "versioned wire schemas could not be loaded",
        ) from exc
    return registry.crawl()


def _wire_schema_registry() -> Registry:
    try:
        catalogs = tuple(
            (
                version,
                str(root := schema_catalog_root(version)),
                _catalog_fingerprint(root),
            )
            for version in SUPPORTED_SCHEMA_CATALOGS
        )
        return _wire_schema_registry_for_catalogs(catalogs)
    except (FileNotFoundError, OSError, SchemaCatalogError) as exc:
        raise WireMigrationError(
            "wire.schema_catalog_invalid",
            "versioned wire schemas could not be loaded",
        ) from exc


def _validate_wire_document(
    document: Mapping[str, Any], schema_uri: str, *, code: str
) -> None:
    # jsonschema compiles format grammars on import; load it only to validate.
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError

    registry = _wire_schema_registry()
    try:
        schema = registry.contents(schema_uri)
        Draft202012Validator(schema, registry=registry).validate(document)
    except (ValidationError, SchemaError, Unresolvable, KeyError, LookupError) as exc:
        raise WireMigrationError(code, f"wire document violates {schema_uri}") from exc


def _document(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise WireMigrationError("wire.document_invalid", f"{label} must be an object")
    try:
        return copy.deepcopy(dict(value))
    except (TypeError, ValueError) as exc:
        raise WireMigrationError(
            "wire.document_invalid", f"{label} cannot be copied safely"
        ) from exc


def _migration_uncertainty(document: Mapping[str, Any]) -> dict[str, Any]:
    request = document.get("request")
    if not isinstance(request, Mapping):
        raise WireMigrationError(
            "wire.v1_result_invalid", "v1 result request must be an object"
        )
    request_id = request.get("request_id")
    snapshot = request.get("source_snapshot_id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(snapshot, str)
    ):
        raise WireMigrationError(
            "wire.v1_result_invalid", "v1 result request identity is invalid"
        )
    material = json.dumps(
        {"request_id": request_id, "source_snapshot_id": snapshot},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(material).hexdigest()[:24]
    return {
        "uncertainty_id": f"migration:missing-component-graph:{suffix}",
        "kind": "missing-evidence",
        "message": (
            "The source document predates the Component graph contract; migration "
            "did not infer or invent a graph."
        ),
        "observation_ids": [],
        "surface_ids": [],
        "blocking": True,
    }


def _record_missing_graph(document: dict[str, Any]) -> None:
    uncertainty = document.get("uncertainty")
    if not isinstance(uncertainty, dict) or not isinstance(
        uncertainty.get("items"), list
    ):
        raise WireMigrationError(
            "wire.v1_result_invalid", "v1 result uncertainty ledger is invalid"
        )
    migration_item = _migration_uncertainty(document)
    existing = [
        item
        for item in uncertainty["items"]
        if isinstance(item, Mapping)
        and item.get("uncertainty_id") == migration_item["uncertainty_id"]
    ]
    if existing and existing != [migration_item]:
        raise WireMigrationError(
            "wire.migration_identity_collision",
            "v1 result already uses the deterministic migration uncertainty ID",
        )
    if not existing:
        uncertainty["items"].append(migration_item)


def _bind_graph_schema(document: dict[str, Any]) -> None:
    graph = document.get("component_graph_draft")
    if graph is None:
        _record_missing_graph(document)
        return
    if not isinstance(graph, dict) or set(graph) != _GRAPH_FIELDS:
        raise WireMigrationError(
            "wire.unreleased_result_invalid",
            "unreleased Component graph has an invalid or ambiguous shape",
        )
    document["component_graph_draft"] = {
        "schema": V2_COMPONENT_GRAPH_DRAFT_SCHEMA,
        **graph,
    }


def migrate_v1_source_to_specification_result(value: Any) -> dict[str, Any]:
    """Migrate the exact published v1 result; never synthesize a missing graph."""

    document = _document(value, label="v1 source-to-specification result")
    if set(document) != _V1_RESULT_FIELDS:
        raise WireMigrationError(
            "wire.v1_result_invalid",
            "document is not the exact published v1 result shape",
        )
    _validate_wire_document(
        document,
        V1_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
        code="wire.v1_result_invalid",
    )
    _record_missing_graph(document)
    document["component_graph_draft"] = None
    migrated = {"schema": V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, **document}
    _validate_wire_document(
        migrated,
        V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
        code="wire.v2_result_invalid",
    )
    return migrated


def adapt_unreleased_post_v011_source_to_specification_result(
    value: Any,
) -> dict[str, Any]:
    """Recognize only the known, never-published post-v0.1.1 result shape."""

    document = _document(value, label="unreleased source-to-specification result")
    if set(document) != _UNRELEASED_RESULT_FIELDS:
        raise WireMigrationError(
            "wire.unreleased_result_invalid",
            "document is not the recognized post-v0.1.1 development shape",
        )
    _bind_graph_schema(document)
    adapted = {"schema": V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, **document}
    _validate_wire_document(
        adapted,
        V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
        code="wire.unreleased_result_invalid",
    )
    return adapted


def normalize_source_to_specification_result(value: Any) -> dict[str, Any]:
    """Return one canonical v2 result or fail closed on unknown wire versions."""

    document = _document(value, label="source-to-specification result")
    schema = document.get("schema")
    if schema is None:
        fields = set(document)
        if fields == _V1_RESULT_FIELDS:
            return migrate_v1_source_to_specification_result(document)
        if fields == _UNRELEASED_RESULT_FIELDS:
            return adapt_unreleased_post_v011_source_to_specification_result(document)
        raise WireMigrationError(
            "wire.result_shape_ambiguous",
            "unversioned result does not match a supported historical shape",
        )
    if schema == V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA:
        if set(document) != _V2_RESULT_FIELDS:
            raise WireMigrationError(
                "wire.v3_result_invalid", "v3 result has an invalid record shape"
            )
        graph = document.get("component_graph_draft")
        if graph is not None and (
            not isinstance(graph, Mapping)
            or set(graph) != _V3_GRAPH_FIELDS
            or graph.get("schema") != V3_COMPONENT_GRAPH_DRAFT_SCHEMA
        ):
            raise WireMigrationError(
                "wire.v3_graph_invalid", "v3 result has an invalid Component graph"
            )
        _validate_wire_document(
            document,
            V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
            code="wire.v3_result_invalid",
        )
        return document
    if schema != V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA:
        raise WireMigrationError(
            "wire.version_unsupported", f"unsupported result schema: {schema!r}"
        )
    if set(document) != _V2_RESULT_FIELDS:
        raise WireMigrationError(
            "wire.v2_result_invalid", "v2 result has an invalid record shape"
        )
    graph = document.get("component_graph_draft")
    if graph is not None and (
        not isinstance(graph, Mapping)
        or set(graph) != _V2_GRAPH_FIELDS
        or graph.get("schema") != V2_COMPONENT_GRAPH_DRAFT_SCHEMA
    ):
        raise WireMigrationError(
            "wire.v2_graph_invalid", "v2 result has an invalid Component graph"
        )
    _validate_wire_document(
        document,
        V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
        code="wire.v2_result_invalid",
    )
    return document


def adapt_unreleased_post_v011_qualification_document(
    value: Any,
) -> dict[str, Any]:
    """Rebind one exact, never-published post-v0.1.1 qualification record."""

    document = _document(value, label="v1 qualification record")
    schema = document.get("schema")
    if not isinstance(schema, str) or schema not in _V1_QUALIFICATION_SCHEMAS:
        raise WireMigrationError(
            "wire.qualification_version_unsupported",
            "qualification record is not a recognized post-v0.1.1 resource",
        )
    output_schema, fields = _V1_QUALIFICATION_SCHEMAS[schema]
    if set(document) != fields:
        raise WireMigrationError(
            "wire.qualification_record_invalid",
            "v1 qualification record does not match its exact published shape",
        )
    if schema == "urn:literate-ai:schema:v1:regenerative-qualification-decision":
        document["claimed_authority"] = document.pop("authority")
        document["effective_authority"] = "source-baseline"
    document["schema"] = output_schema
    _validate_wire_document(
        document,
        output_schema,
        code="wire.qualification_record_invalid",
    )
    return document


def normalize_component_authority_projection(value: Any) -> dict[str, Any]:
    """Accept only the v2 authority projection; legacy claims lack its evidence.

    ``accepted-specification-set@1`` records asserted an authority scope, but they did
    not bind the complete evidence closure required by this contract.  Converting one
    automatically would invent authority, so there is intentionally no v1 adapter.
    """

    document = _document(value, label="Component authority projection")
    schema = document.get("schema")
    if schema != V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA:
        raise WireMigrationError(
            "wire.authority_projection_version_unsupported",
            f"unsupported Component authority projection schema: {schema!r}",
        )
    if set(document) != _V2_AUTHORITY_PROJECTION_FIELDS:
        raise WireMigrationError(
            "wire.authority_projection_invalid",
            "v2 Component authority projection has an invalid record shape",
        )
    transitions = {
        "source-authoritative": {"source-inventory"},
        "spec-assisted": {"spec-derivation"},
        "derived-source-retained": {
            "human-acceptance",
            "qualification-invalidation",
        },
        "regeneratively-qualified-fungible": {"regenerative-qualification"},
    }
    state = document.get("state")
    prior = document.get("prior_projection_identity")
    specification = document.get("specification_set_identity")
    component_revision = document.get("component_revision_identity")
    target_lock = document.get("target_lock_identity")
    generation_closure = document.get("generation_closure")
    if (
        state not in transitions
        or document.get("transition") not in transitions[state]
        or (
            state == "source-authoritative"
            and any(
                value is not None
                for value in (
                    prior,
                    specification,
                    component_revision,
                    target_lock,
                    generation_closure,
                    document.get("verifier_identity"),
                    document.get("policy_identity"),
                )
            )
        )
        or (
            state != "source-authoritative"
            and (
                not isinstance(prior, str)
                or not isinstance(specification, str)
                or not isinstance(component_revision, str)
            )
        )
        or (
            state in {"spec-assisted", "derived-source-retained"}
            and any(
                value is not None
                for value in (
                    target_lock,
                    generation_closure,
                    document.get("verifier_identity"),
                    document.get("policy_identity"),
                )
            )
        )
        or (
            state == "regeneratively-qualified-fungible"
            and (
                not isinstance(target_lock, str)
                or not isinstance(generation_closure, dict)
                or not isinstance(document.get("verifier_identity"), str)
                or not isinstance(document.get("policy_identity"), str)
            )
        )
    ):
        raise WireMigrationError(
            "wire.authority_projection_invalid",
            "v2 Component authority projection state and transition are inconsistent",
        )
    _validate_wire_document(
        document,
        V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA,
        code="wire.authority_projection_invalid",
    )
    return document


def _migrated_file_document(value: Any) -> dict[str, Any]:
    document = _document(value, label="source-to-specification migration input")
    schema = document.get("schema")
    if schema == _LEGACY_RESULT_BUNDLE_SCHEMA:
        result = document.get("result")
        document["result"] = normalize_source_to_specification_result(result)
        document["schema"] = _V2_RESULT_BUNDLE_SCHEMA
        return document
    if schema == _V2_RESULT_BUNDLE_SCHEMA:
        result = document.get("result")
        document["result"] = normalize_source_to_specification_result(result)
        return document
    return normalize_source_to_specification_result(document)


def _replace_for_commit(source: Path, target: Path) -> None:
    os.replace(source, target)


def migrate_source_to_specification_result_files(
    paths: list[str | Path] | tuple[str | Path, ...],
) -> tuple[Path, ...]:
    """Migrate a set of result files with preflight staging and exact rollback.

    All inputs are parsed and migrated before the first destination is replaced.  If a
    commit replacement fails, every already-replaced file is restored from its exact
    original bytes before the failure is reported.
    """

    if not paths:
        raise WireMigrationError(
            "wire.file_migration_empty", "at least one migration file is required"
        )
    originals: dict[Path, bytes] = {}
    staged: dict[Path, Path] = {}
    resolved_paths: set[Path] = set()
    try:
        for raw_path in paths:
            unresolved = Path(raw_path)
            if unresolved.is_symlink() or not unresolved.is_file():
                raise WireMigrationError(
                    "wire.file_migration_input_invalid",
                    f"migration input must be a regular file: {unresolved}",
                )
            path = unresolved.resolve(strict=True)
            if path in resolved_paths:
                raise WireMigrationError(
                    "wire.file_migration_input_duplicate",
                    f"migration input is duplicated: {unresolved}",
                )
            resolved_paths.add(path)
            content = path.read_bytes()
            try:
                value = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WireMigrationError(
                    "wire.file_migration_json_invalid",
                    f"migration input is not valid UTF-8 JSON: {unresolved}",
                ) from exc
            migrated = _migrated_file_document(value)
            output = (
                json.dumps(migrated, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
            handle, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.migration-", dir=path.parent
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(output)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
            originals[path] = content
            staged[path] = temporary_path

        committed: list[Path] = []
        try:
            for path in originals:
                _replace_for_commit(staged[path], path)
                committed.append(path)
        except BaseException as commit_error:
            try:
                for path in reversed(committed):
                    handle, temporary = tempfile.mkstemp(
                        prefix=f".{path.name}.rollback-", dir=path.parent
                    )
                    rollback_path = Path(temporary)
                    try:
                        with os.fdopen(handle, "wb") as stream:
                            stream.write(originals[path])
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(rollback_path, path)
                    finally:
                        rollback_path.unlink(missing_ok=True)
            except BaseException as rollback_error:
                raise WireMigrationError(
                    "wire.file_migration_rollback_failed",
                    "migration failed and exact rollback could not be completed",
                ) from rollback_error
            raise WireMigrationError(
                "wire.file_migration_commit_failed",
                "migration failed; every replaced file was restored exactly",
            ) from commit_error
        return tuple(originals)
    finally:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)


__all__ = [
    "V2_COMPONENT_AUTHORITY_PROJECTION_SCHEMA",
    "V1_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA",
    "V2_COMPONENT_GRAPH_DRAFT_SCHEMA",
    "V2_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA",
    "V3_COMPONENT_GRAPH_DRAFT_SCHEMA",
    "V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA",
    "WireMigrationError",
    "adapt_unreleased_post_v011_qualification_document",
    "adapt_unreleased_post_v011_source_to_specification_result",
    "migrate_v1_source_to_specification_result",
    "migrate_source_to_specification_result_files",
    "normalize_component_authority_projection",
    "normalize_source_to_specification_result",
]
