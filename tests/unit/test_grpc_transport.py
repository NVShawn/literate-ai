"""Real loopback RPCs through native acceptance; no generated application code."""

import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_acceptance import (
    load_ipc_surface_conformance_acceptance,
)
from literate_ai.adapters.grpc_surface_acceptance import GrpcIpcSurfaceProbe
from literate_ai.adapters.ipc_surface_acceptance import (
    IpcSurfaceAcceptanceError,
    decide_ipc_surface_conformance,
)
from tests.unit.test_ipc_surface_acceptance import _valid_document


class GrpcTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import grpc
            from google.protobuf import descriptor_pb2, descriptor_pool
            from google.rpc import status_pb2
            from grpc_reflection.v1alpha import reflection, reflection_pb2_grpc
            from grpc_status import rpc_status
        except ImportError:
            raise unittest.SkipTest(
                "requires grpc-acceptance optional runtime"
            ) from None
        cls.grpc = grpc
        cls.pb = descriptor_pb2
        cls.pool_type = descriptor_pool.DescriptorPool
        cls.reflection_grpc = reflection_pb2_grpc
        cls.reflection = reflection
        cls.rpc_status = rpc_status
        cls.status_pb2 = status_pb2

    def setUp(self):
        self.mode = "normal"
        self.invocations = []
        file = self.pb.FileDescriptorProto(
            name="native.proto", package="native", syntax="proto3"
        )
        types = self.pb.FileDescriptorProto(
            name="types.proto", package="native", syntax="proto3"
        )
        file.dependency.append("types.proto")
        for name in ["Input", "Output", "Failure"]:
            types.message_type.add(name=name).field.add(
                name="value", number=1, type=5, label=1
            )
        service = file.service.add(name="Surface")
        for name, client, server in [
            ("UU", False, False),
            ("US", False, True),
            ("SU", True, False),
            ("SS", True, True),
        ]:
            service.method.add(
                name=name,
                input_type=".native.Input",
                output_type=".native.Output",
                client_streaming=client,
                server_streaming=server,
            )
        self.pool = self.pool_type()
        types_descriptor = self.pool.AddSerializedFile(types.SerializeToString())
        descriptor = self.pool.AddSerializedFile(file.SerializeToString())
        canonical = self.pb.FileDescriptorProto.FromString(descriptor.serialized_pb)
        self.payload = self.pb.FileDescriptorSet(
            file=[
                canonical,
                self.pb.FileDescriptorProto.FromString(types_descriptor.serialized_pb),
            ]
        ).SerializeToString()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.server = self.grpc.server(self.executor)
        handlers = {}
        for name, shape in [
            ("UU", "unary_unary"),
            ("US", "unary_stream"),
            ("SU", "stream_unary"),
            ("SS", "stream_stream"),
        ]:
            factory = getattr(self.grpc, shape + "_rpc_method_handler")
            if shape.endswith("stream"):

                def stream(request, context, name=name):
                    self.invocations.append(name)
                    if name == "SS":
                        list(request)
                    if self.mode == "slow":
                        while context.is_active():
                            time.sleep(0.005)
                        return
                    if self.mode == "error":
                        yield b"\x08\x02"
                        status = self.status_pb2.Status(code=3, message="invalid")
                        status.details.add(
                            type_url="type.googleapis.com/native.Failure",
                            value=b"\x08\x01",
                        )
                        context.abort_with_status(self.rpc_status.to_status(status))
                    if self.mode == "missing":
                        return
                    if self.mode == "reordered":
                        yield b"\x08\x03"
                    yield b"\x08\x02"
                    if self.mode == "extra":
                        yield b"\x08\x03"
            else:

                def unary(request, context, name=name):
                    self.invocations.append(name)
                    if name == "SU":
                        list(request)
                    if self.mode == "invalid":
                        return b"\x80"
                    if self.mode == "wrong":
                        return b"\x08\x03"
                    return b"\x08\x02"

            handlers[name] = factory(stream if shape.endswith("stream") else unary)
        self.server.add_generic_rpc_handlers(
            (self.grpc.method_handlers_generic_handler("native.Surface", handlers),)
        )
        owner = self

        class Reflection(self.reflection.ReflectionServicer):
            def ServerReflectionInfo(self, requests, context):
                for response in super().ServerReflectionInfo(requests, context):
                    if owner.mode == "reflection-missing":
                        return
                    if owner.mode == "reflection-duplicate":
                        response.file_descriptor_response.file_descriptor_proto.append(
                            response.file_descriptor_response.file_descriptor_proto[0]
                        )
                    if owner.mode == "reflection-extra":
                        response.file_descriptor_response.file_descriptor_proto.append(
                            owner.pb.FileDescriptorProto(
                                name="extra.proto"
                            ).SerializeToString()
                        )
                    yield response

        self.reflection_grpc.add_ServerReflectionServicer_to_server(
            Reflection(
                ("native.Surface", self.reflection.SERVICE_NAME), pool=self.pool
            ),
            self.server,
        )
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.url = f"http://127.0.0.1:{port}"
        self.addCleanup(self.executor.shutdown, wait=True)
        self.addCleanup(lambda: self.server.stop(0).wait(5))
        self.probe = GrpcIpcSurfaceProbe()

    def contract(self, names=("UU", "US", "SU", "SS")):
        shapes = {
            "UU": "unary-unary",
            "US": "unary-stream",
            "SU": "stream-unary",
            "SS": "stream-stream",
        }
        cases = [
            dict(
                method="/native.Surface/" + name,
                cardinality=shapes[name],
                input_type="native.Input",
                output_type="native.Output",
                requests=["0801"],
                responses=["0802"],
                status="OK",
                status_message="",
                timeout_milliseconds=1000,
                error_details=[],
            )
            for name in names
        ]
        document = _valid_document(
            schema="literate-ai/ipc-surface-conformance-acceptance@2",
            protocol="grpc",
            declared_schema_identity="sha256:"
            + hashlib.sha256(self.payload).hexdigest(),
            description_probe={
                "descriptor_set_hex": self.payload.hex(),
                "timeout_milliseconds": 1000,
            },
            request_cases=cases,
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "oracle.json"
            p.write_text(json.dumps(document))
            return load_ipc_surface_conformance_acceptance(p, "surface")

    def observe(self, contract=None, timeout=3):
        return self.probe.observe_with_timeout(
            self.url, contract or self.contract(), timeout_seconds=timeout
        )

    def test_all_four_rpc_shapes_and_native_reflection_pass(self):
        contract = self.contract()
        self.assertTrue(self.probe.is_ready(self.url, contract, timeout_seconds=1))
        observation = self.observe(contract)
        self.assertEqual(
            decide_ipc_surface_conformance(contract, observation)["outcome"], "accepted"
        )
        self.assertEqual(self.invocations, ["UU", "US", "SU", "SS"])
        self.assertTrue(observation.served_description_bytes)
        raw = json.loads(observation.protocol_detail)
        self.assertTrue(raw["normalized_descriptor_identity"].startswith("sha256:"))
        self.assertEqual(raw["cases"][0]["responses"], ["0802"])
        from literate_ai.contracts import canonical_identity

        for case, observed in zip(raw["cases"], observation.request_cases, strict=True):
            self.assertEqual(canonical_identity(case).uri, observed.response_identity)
        reflected = json.loads(observation.served_description_bytes)
        self.assertEqual(
            [r["file_by_filename"] for r in reflected["responses"]],
            ["native.proto", "types.proto"],
        )

    def test_typed_error_details_after_partial_stream_pass(self):
        from literate_ai.adapters._grpc_call_cases import GrpcErrorDetail, GrpcStatus

        self.mode = "error"
        contract = self.contract(("US",))
        bound = contract.request_cases[0]
        contract = replace(
            contract,
            request_cases=(
                replace(
                    bound,
                    call=replace(
                        bound.call,
                        status=GrpcStatus.INVALID_ARGUMENT,
                        status_message="invalid",
                        error_details=(GrpcErrorDetail("native.Failure", b"\x08\x01"),),
                    ),
                ),
            ),
        )
        self.assertEqual(
            decide_ipc_surface_conformance(contract, self.observe(contract))["outcome"],
            "accepted",
        )

    def test_wrong_response_and_status_refuse_decision(self):
        for mode in ["wrong", "error"]:
            self.mode = mode
            contract = self.contract(("UU",) if mode == "wrong" else ("US",))
            with self.subTest(mode=mode), self.assertRaises(IpcSurfaceAcceptanceError):
                decide_ipc_surface_conformance(contract, self.observe(contract))

    def test_malformed_response_and_extra_stream_are_refused(self):
        for mode, name in [("invalid", "UU"), ("extra", "US")]:
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(IpcSurfaceAcceptanceError):
                self.observe(self.contract((name,)))

    def test_deadline_cancels_stream_instead_of_accepting_partial_results(self):
        self.mode = "slow"
        contract = self.contract(("US",))
        start = time.monotonic()
        with self.assertRaises(IpcSurfaceAcceptanceError):
            self.observe(contract, timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)

    def test_reflection_drift_refuses_before_operation_calls(self):
        document = self.pb.FileDescriptorSet.FromString(self.payload)
        document.file[0].options.java_package = "changed"
        self.payload = document.SerializeToString()
        with self.assertRaises(IpcSurfaceAcceptanceError):
            self.observe()
        self.assertEqual(self.invocations, [])

    def test_missing_duplicate_and_extra_reflection_files_refuse(self):
        for mode in ("reflection-missing", "reflection-duplicate", "reflection-extra"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(IpcSurfaceAcceptanceError):
                self.observe()
        self.assertEqual(self.invocations, [])

    def test_missing_and_reordered_response_sequences_refuse(self):
        for mode in ("missing", "reordered"):
            self.mode = mode
            contract = self.contract(("US",))
            if mode == "reordered":
                bound = contract.request_cases[0]
                contract = replace(
                    contract,
                    request_cases=(
                        replace(
                            bound,
                            call=replace(
                                bound.call, responses=(b"\x08\x02", b"\x08\x03")
                            ),
                        ),
                    ),
                )
            with self.subTest(mode=mode), self.assertRaises(IpcSurfaceAcceptanceError):
                decide_ipc_surface_conformance(contract, self.observe(contract))

    def test_declared_call_deadline_is_enforced_with_total_time_remaining(self):
        from literate_ai.adapters._grpc_call_cases import GrpcStatus

        self.mode = "slow"
        contract = self.contract(("US",))
        bound = contract.request_cases[0]
        contract = replace(
            contract,
            request_cases=(
                replace(
                    bound,
                    call=replace(
                        bound.call,
                        responses=(),
                        status=GrpcStatus.DEADLINE_EXCEEDED,
                        status_message="Deadline Exceeded",
                        timeout_milliseconds=50,
                    ),
                ),
            ),
        )
        start = time.monotonic()
        observation = self.observe(contract, timeout=3)
        self.assertEqual(
            decide_ipc_surface_conformance(contract, observation)["outcome"], "accepted"
        )
        self.assertLess(time.monotonic() - start, 2)

    def test_non_loopback_targets_and_invalid_budgets_refuse(self):
        contract = self.contract()
        for target in [
            "http://example.invalid:123",
            "http://127.0.0.1:123/path",
            "http://user@127.0.0.1:123",
        ]:
            with (
                self.subTest(target=target),
                self.assertRaises(IpcSurfaceAcceptanceError),
            ):
                self.probe.observe_with_timeout(target, contract, timeout_seconds=1)
        for budget in [0, -1, True, "1", float("inf"), float("nan")]:
            with (
                self.subTest(budget=budget),
                self.assertRaises(IpcSurfaceAcceptanceError),
            ):
                self.observe(timeout=budget)
