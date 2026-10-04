"""Native public IPC documents bind protobuf declarations without HTTP probes."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters._grpc_oracle import GrpcBoundCallCase, GrpcReflectionProbe
from literate_ai.adapters.component_acceptance import (
    IPC_SURFACE_NATIVE_SCHEMA,
    ComponentAcceptanceError,
    load_ipc_surface_conformance_acceptance,
    oracle_path,
    resolve_component_acceptance_oracle,
)
from tests.support import fixtures_test_grpc_descriptors as descriptor_fixtures
from tests.support.fixtures_test_ipc_surface_acceptance import _service_lock, _valid_document


class GrpcOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        descriptor_fixtures.GrpcDescriptorTests.setUpClass()
        cls.fixture = descriptor_fixtures.GrpcDescriptorTests()

    def document(self):
        payload = self.fixture.descriptors().SerializeToString()
        cases = []
        for name, shape in [
            ("UU", "unary-unary"),
            ("US", "unary-stream"),
            ("SU", "stream-unary"),
            ("SS", "stream-stream"),
        ]:
            cases.append(
                dict(
                    method="/sample.v1.Service/" + name,
                    cardinality=shape,
                    input_type="sample.v1.Input",
                    output_type="sample.v1.Output",
                    requests=["0801"],
                    responses=["0802"],
                    status="OK",
                    status_message="",
                    timeout_milliseconds=1000,
                    error_details=[],
                )
            )
        return _valid_document(
            schema=IPC_SURFACE_NATIVE_SCHEMA,
            protocol="grpc",
            declared_schema_identity="sha256:" + hashlib.sha256(payload).hexdigest(),
            description_probe=dict(
                descriptor_set_hex=payload.hex(), timeout_milliseconds=1000
            ),
            request_cases=cases,
        )

    def load(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "oracle.json"
            p.write_text(json.dumps(document))
            return load_ipc_surface_conformance_acceptance(p, "surface")

    def test_all_four_shapes_load_with_bound_types(self):
        contract = self.load(self.document())
        self.assertIsInstance(contract.description_probe, GrpcReflectionProbe)
        self.assertEqual(contract.description_probe.timeout_seconds, 1)
        self.assertEqual(len(contract.request_cases), 4)
        for case in contract.request_cases:
            self.assertIsInstance(case, GrpcBoundCallCase)
            self.assertEqual(case.input_type, "sample.v1.Input")
            self.assertEqual(case.call.responses, (b"\x08\x02",))
        self.assertEqual(contract.identity, self.load(self.document()).identity)

    def test_public_resolution_selects_native_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = oracle_path(root, "surface")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.document()))
            contract = resolve_component_acceptance_oracle(
                root, "spec", _service_lock("surface")
            )
            self.assertIsInstance(contract.description_probe, GrpcReflectionProbe)
            self.assertEqual(contract.identity, self.load(self.document()).identity)

    def test_missing_native_runtime_refuses_before_socket_or_launch(self):
        from literate_ai.adapters.ipc_surface_acceptance import (
            IpcSurfaceAcceptanceError,
        )
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalSourceTreeRegistry,
            LocalStandardLifecycleError,
            LocalStandardLifecyclePorts,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(tmp),
                contracts=(),
            )
            with (
                mock.patch(
                    "socket.socket", side_effect=AssertionError("must not allocate")
                ),
                mock.patch(
                    "literate_ai.adapters.grpc_surface_acceptance.GrpcIpcSurfaceProbe",
                    side_effect=IpcSurfaceAcceptanceError(
                        "ipc_surface_acceptance.grpc_unavailable", "unavailable"
                    ),
                ),
                self.assertRaisesRegex(LocalStandardLifecycleError, "grpc_unavailable"),
            ):
                ports._accept_ipc_surface_conformance(
                    None, None, None, self.load(self.document()), None, None
                )

    def test_rest_identity_is_unchanged(self):
        self.assertEqual(
            self.load(_valid_document()).identity.uri,
            "sha256:a63e7b483e4d75d407a0f6ca9a8501d3fba993c4be9677a4bb23c7f041306063",
        )

    def test_shape_types_payload_status_and_deadlines_refuse(self):
        changes = [
            ("method", "/sample.v1.Service/Absent"),
            ("cardinality", "stream-stream"),
            ("input_type", "sample.v1.Output"),
            ("output_type", "sample.v1.Input"),
            ("requests", ["80"]),
            ("responses", [""]),
            ("requests", ["08011001"]),
            ("requests", ["08 01"]),
            ("responses", ["080A"]),
            ("status", "bogus"),
            ("timeout_milliseconds", True),
            ("timeout_milliseconds", 0),
            ("timeout_milliseconds", 120001),
            ("extra", True),
            ("error_details", None),
        ]
        for field, value in changes:
            d = self.document()
            d["request_cases"][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ComponentAcceptanceError),
            ):
                self.load(d)

    def test_descriptor_identity_protocol_and_fields_refuse(self):
        for field, value in [
            ("protocol", "rest"),
            ("declared_schema_identity", "sha256:" + "0" * 64),
            ("extra", True),
        ]:
            d = self.document()
            d[field] = value
            with self.subTest(field=field), self.assertRaises(ComponentAcceptanceError):
                self.load(d)
        for field, value in [
            ("descriptor_set_hex", "80"),
            ("timeout_milliseconds", False),
            ("path", "/reflection"),
        ]:
            d = self.document()
            d["description_probe"][field] = value
            with self.subTest(field=field), self.assertRaises(ComponentAcceptanceError):
                self.load(d)

    def test_error_details_and_partial_streams_are_typed_and_identity_bound(self):
        d = self.document()
        c = d["request_cases"][1]
        c.update(
            status="INVALID_ARGUMENT",
            status_message="invalid",
            error_details=[dict(type_name="sample.v1.Failure", message="0801")],
        )
        contract = self.load(d)
        self.assertEqual(
            contract.request_cases[1].call.error_details[0].message, b"\x08\x01"
        )
        changed = copy.deepcopy(d)
        changed["request_cases"][1]["status_message"] = "different"
        self.assertNotEqual(contract.identity, self.load(changed).identity)
        for field, value in [("type_name", "absent.Type"), ("message", "80")]:
            changed = copy.deepcopy(d)
            changed["request_cases"][1]["error_details"][0][field] = value
            with self.assertRaises(ComponentAcceptanceError):
                self.load(changed)

    def test_document_limit_and_missing_optional_runtime_refuse(self):
        d = self.document()
        d["description_probe"]["descriptor_set_hex"] = "00" * (1024 * 1024)
        with self.assertRaisesRegex(ComponentAcceptanceError, "maximum size"):
            self.load(d)
        d = self.document()
        real_import = __import__

        def unavailable(name, *args, **kwargs):
            if name.startswith("google.protobuf"):
                raise ImportError("unavailable")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch("builtins.__import__", side_effect=unavailable),
            self.assertRaisesRegex(
                ComponentAcceptanceError, "toolchain is unavailable"
            ),
        ):
            self.load(d)
