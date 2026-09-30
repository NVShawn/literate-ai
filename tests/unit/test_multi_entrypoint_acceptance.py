"""Verifier-owned oracle routing for multi-entrypoint Components."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_acceptance import (
    ORACLE_SCHEMA,
    ComponentAcceptanceError,
    ComponentAcceptanceOracleBundle,
    FilesystemComponentAcceptanceOracle,
    oracle_path,
    resolve_component_acceptance_oracle,
)
from literate_ai.contracts import canonical_identity


def _lock():
    revision = canonical_identity({"fixture": "multi-entrypoint-acceptance"})
    return SimpleNamespace(
        root_revision=revision,
        nodes=(
            SimpleNamespace(
                revision=SimpleNamespace(
                    identity=revision,
                    specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                    definition=SimpleNamespace(
                        coordinate=SimpleNamespace(name="multi-service"),
                        entrypoints=(
                            SimpleNamespace(
                                name="api",
                                kind="portable-application",
                                resolved_deployment_unit="api-unit",
                            ),
                            SimpleNamespace(
                                name="worker",
                                kind="portable-application",
                                resolved_deployment_unit="worker-unit",
                            ),
                        ),
                    ),
                )
            ),
        ),
    )


def _write_oracle(path: Path, case_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": ORACLE_SCHEMA,
                "specification_set_identity": "sha256:spec",
                "cases": [
                    {
                        "case_id": case_id,
                        "arguments": [],
                        "expected_result": {"ok": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class MultiEntrypointAcceptanceTests(unittest.TestCase):
    def test_resolver_requires_and_binds_one_oracle_per_deployment_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_oracle(oracle_path(root, "multi-service", "api"), "api-case")
            _write_oracle(oracle_path(root, "multi-service", "worker"), "worker-case")

            resolved = resolve_component_acceptance_oracle(
                root, "components/multi-service", _lock()
            )

            self.assertIsInstance(resolved, ComponentAcceptanceOracleBundle)
            assert isinstance(resolved, ComponentAcceptanceOracleBundle)
            self.assertEqual(
                tuple(item.deployment_unit for item in resolved.bindings),
                ("api-unit", "worker-unit"),
            )
            self.assertIsInstance(
                resolved.for_deployment_unit("api-unit").oracle,
                FilesystemComponentAcceptanceOracle,
            )
            self.assertNotEqual(
                resolved.bindings[0].identity,
                resolved.bindings[1].identity,
            )

    def test_missing_or_unsafe_entrypoint_oracle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_oracle(oracle_path(root, "multi-service", "api"), "api-case")
            with self.assertRaisesRegex(ComponentAcceptanceError, "no acceptance"):
                resolve_component_acceptance_oracle(
                    root, "components/multi-service", _lock()
                )
        with self.assertRaises(ComponentAcceptanceError) as caught:
            oracle_path(Path("/tmp/project"), "multi-service", "../outside")
        self.assertEqual(caught.exception.code, "component_acceptance.path_invalid")


if __name__ == "__main__":
    unittest.main()
