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
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    IPC_SURFACE_SCHEMA,
    SERVICE_SCHEMA,
    ComponentAcceptanceError,
    IpcSurfaceConformanceAcceptance,
    load_ipc_surface_conformance_acceptance,
    load_persistent_service_acceptance,
    oracle_path,
    resolve_component_acceptance_oracle,
)
from literate_ai.adapters.ipc_surface_acceptance import (
    HttpIpcSurfaceProbe,
    IpcRequestCaseObservation,
    IpcSurfaceAcceptanceError,
    IpcSurfaceObservation,
    IpcSurfaceProbe,
    decide_ipc_surface_conformance,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
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


class IpcHttpExpectationTests(unittest.TestCase):
    def _probe(self, **expectations):
        path, holder = _write(
            _valid_document(request_cases=[{"path": "/value", **expectations}])
        )
        self.addCleanup(holder.cleanup)
        return load_ipc_surface_conformance_acceptance(path, "surface").request_cases[0]

    def _fetch(self, probe, *, body=b'{"value":7}', status=200, headers=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.status = status
        response.headers = {} if headers is None else headers
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.open_service_request",
            return_value=response,
        ) as request:
            result = LocalStandardLifecyclePorts._http_get_body(
                "http://127.0.0.1:12345", probe
            )
        request.assert_called_once()
        response.read.assert_called_once_with(probe.response_limit_bytes + 1)
        return result

    def test_declared_http_expectations_refuse_incorrect_responses(self):
        for expectations, response in (
            ({"expected_status": 201}, {}),
            ({"expected_headers": {"X-Result": "required"}}, {}),
            ({"expected_json": {"value": 8}}, {}),
            ({"expected_json": {"value": 7}}, {"body": b"not-json"}),
            ({"expected_text_contains": ["required"]}, {}),
            ({"expected_sse_events": [{"data": "required"}]}, {}),
        ):
            with self.subTest(expectations=expectations):
                with self.assertRaises(LocalStandardLifecycleError):
                    self._fetch(self._probe(**expectations), **response)

    def test_matching_http_expectations_return_original_bytes(self):
        payload = b'{"value":7}'
        probe = self._probe(
            expected_status=201,
            expected_headers={"X-Result": "valid"},
            expected_json={"value": 7},
        )
        self.assertEqual(
            self._fetch(probe, body=payload, status=201, headers={"x-result": "valid"}),
            payload,
        )

    def test_unavailable_connection_remains_a_readiness_observation(self):
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.open_service_request",
            side_effect=ConnectionRefusedError,
        ):
            self.assertIsNone(
                LocalStandardLifecyclePorts._http_get_body(
                    "http://127.0.0.1:12345", self._probe()
                )
            )


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

    def test_request_observations_must_cover_declared_cases_exactly(self) -> None:
        contract = self._contract()
        for cases in (
            (),
            (IpcRequestCaseObservation(1, True, "sha256:response"),),
            (IpcRequestCaseObservation(False, True, "sha256:response"),),
            (
                IpcRequestCaseObservation(0, True, "sha256:first"),
                IpcRequestCaseObservation(0, True, "sha256:second"),
            ),
        ):
            with self.subTest(cases=cases):
                observation = IpcSurfaceObservation(
                    description_present=True,
                    served_description_identity=_DECLARED_SCHEMA,
                    request_cases=cases,
                )
                with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
                    decide_ipc_surface_conformance(contract, observation)
                self.assertEqual(
                    caught.exception.code, "ipc_surface_acceptance.case_coverage"
                )

    def test_multiple_request_cases_require_complete_ordered_observations(self) -> None:
        original = self._contract()
        contract = replace(original, request_cases=original.request_cases * 2)
        first = IpcRequestCaseObservation(0, True, "sha256:first")
        second = IpcRequestCaseObservation(1, True, "sha256:second")
        for cases in ((first,), (first, first), (second, first)):
            with self.subTest(cases=cases):
                observation = IpcSurfaceObservation(True, _DECLARED_SCHEMA, cases)
                with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
                    decide_ipc_surface_conformance(contract, observation)
                self.assertEqual(
                    caught.exception.code, "ipc_surface_acceptance.case_coverage"
                )
        decision = decide_ipc_surface_conformance(
            contract, IpcSurfaceObservation(True, _DECLARED_SCHEMA, (first, second))
        )
        self.assertEqual(decision["outcome"], "accepted")
        self.assertEqual(len(decision["request_cases"]), 2)

    def test_response_validation_requires_a_true_boolean(self) -> None:
        contract = self._contract()
        for validated in (1, "true", None):
            with self.subTest(validated=validated):
                observation = IpcSurfaceObservation(
                    description_present=True,
                    served_description_identity=_DECLARED_SCHEMA,
                    request_cases=(
                        IpcRequestCaseObservation(0, validated, "sha256:response"),
                    ),
                )
                with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
                    decide_ipc_surface_conformance(contract, observation)
                self.assertEqual(
                    caught.exception.code, "ipc_surface_acceptance.response_invalid"
                )

    def test_served_description_mismatch_fails_closed(self) -> None:
        contract = self._contract()
        observation = IpcSurfaceObservation(
            description_present=True,
            served_description_identity="urn:example:something-else@1",
            request_cases=(IpcRequestCaseObservation(0, True, "sha256:resp"),),
        )
        with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
            decide_ipc_surface_conformance(contract, observation)
        self.assertEqual(
            caught.exception.code, "ipc_surface_acceptance.description_mismatch"
        )

    def test_non_validating_response_fails_closed(self) -> None:
        contract = self._contract()
        observation = IpcSurfaceObservation(
            description_present=True,
            served_description_identity=_DECLARED_SCHEMA,
            request_cases=(
                IpcRequestCaseObservation(
                    0, False, "sha256:resp", detail="response omitted required field"
                ),
            ),
        )
        with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
            decide_ipc_surface_conformance(contract, observation)
        self.assertEqual(
            caught.exception.code, "ipc_surface_acceptance.response_invalid"
        )

    def test_missing_served_description_fails_closed(self) -> None:
        contract = self._contract()
        observation = IpcSurfaceObservation(
            description_present=False,
            served_description_identity="",
        )
        with self.assertRaises(IpcSurfaceAcceptanceError) as caught:
            decide_ipc_surface_conformance(contract, observation)
        self.assertEqual(
            caught.exception.code, "ipc_surface_acceptance.description_missing"
        )

    def test_http_probe_maps_fetch_bytes_to_observation(self) -> None:
        # The port drives fetch + validate; the pure decision then accepts. This
        # proves the whole port is testable with a canned fetch and no server.
        contract = self._contract()
        served = json.dumps({"openapi": "3.1.0"}).encode()

        def fetch(base_url: str, probe: object) -> bytes | None:
            return served if probe is contract.description_probe else b'{"id": 1}'

        probe = HttpIpcSurfaceProbe(
            fetch=fetch,
            describe_identity=lambda _bytes: _DECLARED_SCHEMA,
        )
        observation = probe.observe("http://127.0.0.1:1", contract)
        decision = decide_ipc_surface_conformance(contract, observation)
        self.assertEqual(decision["outcome"], "accepted")
        self.assertTrue(observation.request_cases[0].validated)

    def test_http_probe_reports_absent_description(self) -> None:
        contract = self._contract()
        probe = HttpIpcSurfaceProbe(fetch=lambda _url, _probe: None)
        observation = probe.observe("http://127.0.0.1:1", contract)
        self.assertFalse(observation.description_present)
        with self.assertRaises(IpcSurfaceAcceptanceError):
            decide_ipc_surface_conformance(contract, observation)


class IpcSurfaceLoaderTests(unittest.TestCase):
    """Strict validation mirroring the service/browser loader tests."""

    def _load(self, **overrides: object) -> IpcSurfaceConformanceAcceptance:
        path, holder = _write(_valid_document(**overrides))
        try:
            return load_ipc_surface_conformance_acceptance(path, "surface")
        finally:
            holder.cleanup()

    def test_valid_document_loads(self) -> None:
        contract = self._load()
        self.assertEqual(contract.protocol, "rest")
        self.assertEqual(contract.declared_schema_identity, _DECLARED_SCHEMA)
        self.assertEqual(contract.surface_version, "1.2.3")
        self.assertEqual(contract.compatibility.compatible_versions, ">=1.0.0 <2.0.0")
        self.assertEqual(len(contract.request_cases), 1)

    def test_unknown_field_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(surprise=True)

    def test_empty_protocol_tag_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(protocol="")

    def test_missing_declared_schema_identity_fails_closed(self) -> None:
        document = _valid_document()
        del document["declared_schema_identity"]
        path, holder = _write(document)
        try:
            with self.assertRaises(ComponentAcceptanceError):
                load_ipc_surface_conformance_acceptance(path, "surface")
        finally:
            holder.cleanup()

    def test_bad_surface_version_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(surface_version="1.2")

    def test_bad_compatibility_promise_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(compatibility={"policy": "not-a-policy"})

    def test_unbound_specification_identity_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(specification_set_identity="")

    def test_empty_request_cases_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(request_cases=[])

    def test_wrong_schema_fails_closed(self) -> None:
        with self.assertRaises(ComponentAcceptanceError):
            self._load(schema=SERVICE_SCHEMA)

    def test_require_current_rejects_specification_drift(self) -> None:
        contract = self._load(specification_set_identity="sha256:old")
        with self.assertRaisesRegex(ComponentAcceptanceError, "review and rebind"):
            contract.require_current(_lock("sha256:new"))

    def test_require_current_accepts_matching_specification(self) -> None:
        contract = self._load(specification_set_identity="sha256:spec")
        contract.require_current(_lock("sha256:spec"))


class IpcSurfaceOracleResolutionTests(unittest.TestCase):
    """A persistent-service selects the IPC oracle by its document's schema."""

    def test_persistent_service_document_declaring_ipc_loads_ipc_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            path = oracle_path(project_root, "surface")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(_valid_document()), encoding="utf-8")
            lock = _service_lock("surface")
            oracle = resolve_component_acceptance_oracle(project_root, "spec", lock)
        self.assertIsInstance(oracle, IpcSurfaceConformanceAcceptance)

    def test_persistent_service_without_ipc_schema_loads_service_probe(self) -> None:
        from literate_ai.adapters.component_acceptance import (
            PersistentServiceAcceptance,
        )

        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            path = oracle_path(project_root, "surface")
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {},
                        "readiness": {"path": "/health"},
                        "requests": [{"path": "/value"}],
                    }
                ),
                encoding="utf-8",
            )
            lock = _service_lock("surface")
            oracle = resolve_component_acceptance_oracle(project_root, "spec", lock)
        self.assertIsInstance(oracle, PersistentServiceAcceptance)


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

    def test_default_transport_refuses_non_rest_protocol_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
            )
            for protocol in ("grpc", "mcp", "custom-protocol"):
                path, holder = _write(_valid_document(protocol=protocol))
                self.addCleanup(holder.cleanup)
                contract = load_ipc_surface_conformance_acceptance(path, "surface")
                with (
                    self.subTest(protocol=protocol),
                    mock.patch(
                        "literate_ai.adapters.lifecycle.standard_local.socket.socket",
                        side_effect=AssertionError(
                            "refusal must precede socket allocation"
                        ),
                    ) as socket,
                    mock.patch.object(ports, "_packaged_service_command") as launch,
                ):
                    with self.assertRaisesRegex(
                        LocalStandardLifecycleError,
                        "requires an explicit protocol probe",
                    ):
                        ports._accept_ipc_surface_conformance(
                            None,
                            None,
                            None,
                            contract,
                            _identity("root-test"),
                            _identity("package-execution"),
                        )
                    socket.assert_not_called()
                    launch.assert_not_called()

    def test_non_rest_probe_without_readiness_refuses_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                ipc_surface_probe=FakeIpcSurfaceProbe(_DECLARED_SCHEMA),
            )
            for protocol in ("grpc", "mcp", "custom-protocol"):
                path, holder = _write(_valid_document(protocol=protocol))
                self.addCleanup(holder.cleanup)
                contract = load_ipc_surface_conformance_acceptance(path, "surface")
                with (
                    self.subTest(protocol=protocol),
                    mock.patch(
                        "literate_ai.adapters.lifecycle.standard_local.socket.socket",
                        side_effect=AssertionError(
                            "refusal must precede socket allocation"
                        ),
                    ) as socket,
                    mock.patch.object(ports, "_packaged_service_command") as launch,
                ):
                    with self.assertRaisesRegex(
                        LocalStandardLifecycleError,
                        "requires explicit protocol readiness",
                    ):
                        ports._accept_ipc_surface_conformance(
                            None,
                            None,
                            None,
                            contract,
                            _identity("root-test"),
                            _identity("package-execution"),
                        )
                    socket.assert_not_called()
                    launch.assert_not_called()

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
        self._assert_readiness_dispatch()

    def test_non_rest_readiness_uses_explicit_protocol_probe(self):
        self._assert_readiness_dispatch("grpc", "ready")

    def test_non_rest_readiness_rejects_invalid_result_and_stops(self):
        self._assert_readiness_dispatch("grpc", "invalid", "readiness.*boolean")

    def test_non_rest_readiness_clamps_float_rounding_to_declared_budget(self):
        self._assert_readiness_dispatch(
            "grpc",
            "invalid",
            "readiness.*boolean",
            monotonic_origin=8.711582158575643,
        )

    def test_non_rest_readiness_rejects_late_success_and_stops(self):
        self._assert_readiness_dispatch("grpc", "late", "deadline")

    def test_non_rest_readiness_propagates_typed_refusal_and_stops(self):
        self._assert_readiness_dispatch("grpc", "error", "fixture.unavailable")

    def test_late_observation_is_refused_and_service_stopped(self):
        self._assert_readiness_dispatch(
            "grpc", "ready", "exceeded its process deadline", late_observation=True
        )

    def test_observation_receives_remaining_process_budget(self):
        self._assert_readiness_dispatch("grpc", "ready", budgeted=True)

    def test_budgeted_observation_refusal_stops_service(self):
        self._assert_readiness_dispatch(
            "grpc", "ready", "fixture.budget", budgeted=True, budget_error=True
        )

    def test_observe_only_protocol_adapter_remains_supported(self):
        self._assert_readiness_dispatch("grpc", "ready", legacy_probe=True)

    def _assert_readiness_dispatch(
        self,
        protocol="rest",
        readiness=None,
        expected_error=None,
        *,
        late_observation=False,
        budgeted=False,
        budget_error=False,
        legacy_probe=False,
        monotonic_origin=None,
    ):
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
            contract = replace(contract, protocol=protocol)
            monotonic = time.monotonic
            monotonic_started = monotonic()
            monotonic_calls = [0]
            elapsed_offset = [0.0]
            budget_elapsed_seconds = 7.0
            # Subtracting large monotonic float values can differ by a few ulps.
            budget_rounding_tolerance_seconds = 1e-9
            budget_seen = []
            probe = FakeIpcSurfaceProbe(served_identity=_DECLARED_SCHEMA)
            if readiness is not None:
                owner = self

                class ProtocolProbe(FakeIpcSurfaceProbe):
                    def observe_with_timeout(
                        self, base_url, contract, *, timeout_seconds
                    ):
                        budget_seen.append(timeout_seconds)
                        owner.assertGreater(timeout_seconds, 0)
                        owner.assertLessEqual(
                            timeout_seconds,
                            contract.process_timeout_seconds
                            - (budget_elapsed_seconds if budgeted else 0.0)
                            + budget_rounding_tolerance_seconds,
                        )
                        if budget_error:
                            raise IpcSurfaceAcceptanceError(
                                "fixture.budget", "expired observation budget"
                            )
                        return self.observe(base_url, contract)

                    def observe(self, base_url, contract):
                        observation = super().observe(base_url, contract)
                        if late_observation:
                            elapsed_offset[0] = contract.process_timeout_seconds + 1
                        return observation

                    def is_ready(self, base_url, contract, *, timeout_seconds):
                        owner.assertGreater(timeout_seconds, 0)
                        owner.assertLessEqual(
                            timeout_seconds, contract.startup_timeout_seconds
                        )
                        if not marker.with_suffix(".ready").exists():
                            return False
                        if readiness == "late":
                            elapsed_offset[0] += timeout_seconds + 0.01
                        if readiness == "error":
                            raise IpcSurfaceAcceptanceError(
                                "fixture.unavailable", "unavailable"
                            )
                        if budgeted:
                            readiness_budget = min(
                                contract.startup_timeout_seconds,
                                contract.process_timeout_seconds,
                            )
                            actual_elapsed = readiness_budget - timeout_seconds
                            # Make the next observation see seven total elapsed
                            # seconds. Adding seven to actual startup time makes
                            # this fixture depend on runner speed.
                            elapsed_offset[0] = budget_elapsed_seconds - actual_elapsed
                        return 1 if readiness == "invalid" else True

                probe = ProtocolProbe(served_identity=_DECLARED_SCHEMA)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
                ipc_surface_probe=(
                    SimpleNamespace(
                        tool_identity=probe.tool_identity,
                        observe=probe.observe,
                        is_ready=probe.is_ready,
                    )
                    if legacy_probe
                    else probe
                ),
            )
            custody = SimpleNamespace(
                root=package_root, tree_identity=local_tree_identity(package_root)
            )
            plan = SimpleNamespace(
                identity=_identity("surface-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("surface-result"))
            lock = _lock("sha256:spec")

            def observed_monotonic():
                if monotonic_origin is None:
                    return monotonic() + elapsed_offset[0]
                monotonic_calls[0] += 1
                elapsed = (
                    0.0 if monotonic_calls[0] <= 3 else monotonic() - monotonic_started
                )
                return monotonic_origin + elapsed + elapsed_offset[0]

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
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.time.monotonic",
                    side_effect=observed_monotonic,
                ),
            ):

                def accept():
                    return ports.accept_project_independently(
                        lock,
                        mock.sentinel.execution_plan,
                        mock.sentinel.project_build_plan,
                        plan,
                        result,
                        _identity("root-test"),
                        _identity("package-execution"),
                    )

                with mock.patch.object(
                    ports, "_http_get_body", wraps=ports._http_get_body
                ) as http:
                    if readiness is not None:
                        http.side_effect = AssertionError(
                            "non-REST readiness must not use HTTP"
                        )
                    if expected_error:
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError, expected_error
                        ):
                            accept()
                        self.assertEqual(probe.observed, late_observation)
                    else:
                        self.assertIsNotNone(accept())
                        self.assertTrue(probe.observed)
                    if readiness is not None:
                        http.assert_not_called()
            if budgeted:
                self.assertEqual(len(budget_seen), 1)
                self.assertLessEqual(
                    budget_seen[0],
                    contract.process_timeout_seconds
                    - budget_elapsed_seconds
                    + budget_rounding_tolerance_seconds,
                )
            if legacy_probe:
                self.assertEqual(budget_seen, [])
            self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")

    def test_persistent_service_still_uses_plain_probe_without_ipc_oracle(
        self,
    ) -> None:
        # With a plain persistent-service oracle the dispatch must NOT take the IPC
        # path: it routes to _accept_persistent_service. Proven by asserting the IPC
        # accept method is never touched while the service accept method is.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SERVICE_SCHEMA,
                        "specification_set_identity": "sha256:spec",
                        "process": {},
                        "readiness": {"path": "/health"},
                        "requests": [{"path": "/value"}],
                    }
                ),
                encoding="utf-8",
            )
            contract = load_persistent_service_acceptance(path, "service")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary) / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
            )
            plan = SimpleNamespace(
                identity=_identity("service-plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("service-result"))
            lock = _lock("sha256:spec")
            with (
                mock.patch.object(
                    ports,
                    "_accept_persistent_service",
                    return_value=_identity("service-evidence"),
                ) as service_accept,
                mock.patch.object(
                    ports, "_accept_ipc_surface_conformance"
                ) as ipc_accept,
                mock.patch.object(
                    ports,
                    "project_package_custody",
                    return_value=SimpleNamespace(root=Path(temporary)),
                ),
            ):
                ports.accept_project_independently(
                    lock,
                    mock.sentinel.execution_plan,
                    mock.sentinel.project_build_plan,
                    plan,
                    result,
                    _identity("root-test"),
                    _identity("package-execution"),
                )
            service_accept.assert_called_once()
            ipc_accept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
