from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_ipc_surface_acceptance``."""









from types import SimpleNamespace

from unittest import mock

from literate_ai.adapters.component_acceptance import (
    IPC_SURFACE_SCHEMA,
)



from literate_ai.contracts import ContentIdentity, canonical_identity

_DECLARED_SCHEMA = "urn:example:rest-surface@1"

def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"ipc-surface-test": label})

def _valid_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": IPC_SURFACE_SCHEMA,
        "specification_set_identity": "sha256:spec",
        "protocol": "rest",
        "declared_schema_identity": _DECLARED_SCHEMA,
        "surface_version": "1.2.3",
        "compatibility": {
            "policy": "semver-stable",
            "compatible_versions": ">=1.0.0 <2.0.0",
            "statement": "additive changes only within the 1.x line",
        },
        "process": {"arguments": ["{port}"], "environment": {}},
        "description_probe": {"path": "/openapi.json"},
        "request_cases": [{"path": "/widgets/1", "expected_status": 200}],
    }
    document.update(overrides)
    return document

def _lock(specification_uri: str) -> mock.Mock:
    lock = mock.Mock()
    lock.root_revision = _identity("root")
    lock.nodes = (
        SimpleNamespace(
            revision=SimpleNamespace(
                identity=lock.root_revision,
                specification_set_identity=SimpleNamespace(uri=specification_uri),
            )
        ),
    )
    return lock

def _service_lock(component_name: str) -> mock.Mock:
    lock = mock.Mock()
    lock.root_revision = _identity("root")
    lock.nodes = (
        SimpleNamespace(
            revision=SimpleNamespace(
                identity=lock.root_revision,
                specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                definition=SimpleNamespace(
                    coordinate=SimpleNamespace(name=component_name),
                    entrypoints=(SimpleNamespace(kind="persistent-service"),),
                ),
            )
        ),
    )
    return lock

