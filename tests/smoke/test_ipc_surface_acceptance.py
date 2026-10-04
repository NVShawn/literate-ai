"""Fail-closed IPC-surface conformance acceptance (ADR 0029 / IPC-SURFACE-001).

The framework core here is protocol-neutral: MCP, REST, and gRPC are adapters that
set the ``protocol`` tag and supply a ``description_probe`` + declared schema, and
plug into the same contract, loader, decision, and lifecycle dispatch. These tests
prove the machinery, so the adapters cannot drift from it.

The decision logic (given a served-description + response observation, does it
pass or fail?) is proven with a hand-built observation and no live server, exactly
like ``decide_browser_acceptance``. A lifecycle-dispatch test with a hand-written
serve script and a fake description-fetch port proves the persistent-service path
routes to IPC conformance when the oracle declares the IPC schema, and still runs
the plain service probe otherwise.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    IPC_SURFACE_SCHEMA,
    IpcSurfaceConformanceAcceptance,
    load_ipc_surface_conformance_acceptance,
)
from literate_ai.adapters.ipc_surface_acceptance import (
    IpcRequestCaseObservation,
    IpcSurfaceAcceptanceError,
    IpcSurfaceObservation,
    IpcSurfaceProbe,
    decide_ipc_surface_conformance,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
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


def _write(document: dict[str, object]) -> tuple[Path, tempfile.TemporaryDirectory]:
    holder = tempfile.TemporaryDirectory()
    path = Path(holder.name) / "surface.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, holder


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


class IpcSurfaceDecisionTests(unittest.TestCase):
    """The engine-free conformance decision, proven without a live server."""

    def _contract(self) -> IpcSurfaceConformanceAcceptance:
        path, holder = _write(_valid_document())
        try:
            return load_ipc_surface_conformance_acceptance(path, "surface")
        finally:
            holder.cleanup()

    def test_description_matches_and_responses_validate_passes(self) -> None:
        contract = self._contract()
        observation = IpcSurfaceObservation(
            description_present=True,
            served_description_identity=_DECLARED_SCHEMA,
            request_cases=(IpcRequestCaseObservation(0, True, "sha256:resp"),),
        )
        decision = decide_ipc_surface_conformance(contract, observation)
        self.assertEqual(decision["outcome"], "accepted")
        self.assertEqual(decision["protocol"], "rest")
        self.assertEqual(decision["declared_schema_identity"], _DECLARED_SCHEMA)
        self.assertEqual(decision["served_description_identity"], _DECLARED_SCHEMA)
        self.assertEqual(decision["surface_version"], "1.2.3")
        # The compatibility promise is reused verbatim, not reinvented.
        self.assertEqual(decision["compatibility"]["policy"], "semver-stable")

    def test_mismatched_or_missing_served_description_fails_closed(self) -> None:
        contract = self._contract()
        for observation, code in (
            (
                IpcSurfaceObservation(
                    description_present=True,
                    served_description_identity="urn:example:something-else@1",
                    request_cases=(IpcRequestCaseObservation(0, True, "sha256:resp"),),
                ),
                "ipc_surface_acceptance.description_mismatch",
            ),
            (
                IpcSurfaceObservation(
                    description_present=False, served_description_identity=""
                ),
                "ipc_surface_acceptance.description_missing",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
                    decide_ipc_surface_conformance(contract, observation)
                self.assertEqual(caught.exception.code, code)


class FakeIpcSurfaceProbe(IpcSurfaceProbe):
    """A scripted probe standing in for a REST/gRPC/MCP adapter (no toolchain)."""

    def __init__(self, served_identity: str, all_validate: bool = True) -> None:
        self._served_identity = served_identity
        self._all_validate = all_validate
        self.observed = False

    @property
    def tool_identity(self) -> ContentIdentity:
        return canonical_identity(
            {"schema": "literate-ai/ipc-surface-probe@1", "engine": "fake"}
        )

    def observe(self, base_url: str, contract: IpcSurfaceConformanceAcceptance):
        self.observed = True
        return IpcSurfaceObservation(
            description_present=True,
            served_description_identity=self._served_identity,
            request_cases=tuple(
                IpcRequestCaseObservation(index, self._all_validate, "sha256:resp")
                for index in range(len(contract.request_cases))
            ),
        )


class IpcSurfaceLifecycleDispatchTests(unittest.TestCase):
    """The persistent-service path routes to IPC conformance via a fake surface."""

    def _serve_script(self, package_root: Path, marker: Path) -> Path:
        service = package_root / "surface.py"
        service.write_text(
            textwrap.dedent(
                """
                import signal, sys
                from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
                from pathlib import Path

                assert sys.argv[1] == '--litai-serve', sys.argv
                port, marker = int(sys.argv[2]), Path(sys.argv[3])
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{}')
                    def log_message(self, *args):
                        pass
                server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
                def stop(*args):
                    marker.write_text('stopped')
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, stop)
                if hasattr(signal, 'SIGBREAK'):
                    signal.signal(signal.SIGBREAK, stop)
                marker.with_suffix(".ready").write_text("ready")
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                """
            ),
            encoding="utf-8",
        )
        return service

    def test_persistent_service_routes_to_ipc_conformance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            marker = root / "stopped"
            service = self._serve_script(package_root, marker)
            contract_path = root / "surface.json"
            contract_path.write_text(
                json.dumps(
                    _valid_document(
                        process={
                            "arguments": ["{port}", str(marker)],
                            "startup_timeout_seconds": 120,
                        },
                        description_probe={"path": "/openapi.json"},
                        request_cases=[{"path": "/widgets/1"}],
                    )
                ),
                encoding="utf-8",
            )
            contract = load_ipc_surface_conformance_acceptance(contract_path, "surface")
            probe = FakeIpcSurfaceProbe(served_identity=_DECLARED_SCHEMA)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
                ipc_surface_probe=probe,
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(
                identity=_identity("surface-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            with (
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
            ):
                self.assertIsNotNone(
                    ports.accept_project_independently(
                        _lock("sha256:spec"),
                        mock.sentinel.execution_plan,
                        mock.sentinel.project_build_plan,
                        plan,
                        SimpleNamespace(identity=_identity("surface-result")),
                        _identity("root-test"),
                        _identity("package-execution"),
                    )
                )
            self.assertTrue(probe.observed)
            self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")


if __name__ == "__main__":
    unittest.main()
