"""Real protobuf closure and message validation; no network transport."""

import hashlib
import unittest
from dataclasses import replace
from unittest import mock

from literate_ai.adapters._grpc_call_cases import (
    GrpcCallCase,
    GrpcCardinality,
    GrpcErrorDetail,
    GrpcStatus,
)
from literate_ai.adapters._grpc_descriptors import GrpcDescriptorClosure
from literate_ai.contracts import ContentIdentity


def identity(payload):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(payload).hexdigest())


class GrpcDescriptorBoundaryTests(unittest.TestCase):
    def test_identity_and_byte_bounds_precede_optional_imports(self):
        for payload, expected in (
            (b"x", identity(b"y")),
            (b"", identity(b"")),
            (bytearray(b"x"), identity(b"x")),
            (b"x" * (4 * 1024 * 1024 + 1), identity(b"x")),
        ):
            with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                with mock.patch(
                    "builtins.__import__", side_effect=AssertionError("no imports")
                ):
                    GrpcDescriptorClosure(payload, expected)

    def test_missing_optional_runtime_refuses(self):
        with mock.patch("builtins.__import__", side_effect=ImportError("unavailable")):
            with self.assertRaisesRegex(ValueError, "toolchain is unavailable"):
                GrpcDescriptorClosure(b"x", identity(b"x"))


class GrpcDescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from google.protobuf import descriptor_pb2, descriptor_pool
        except ImportError:
            raise unittest.SkipTest(
                "requires the grpc-acceptance protobuf runtime"
            ) from None
        cls.pb = descriptor_pb2
        cls.pool = descriptor_pool

    def descriptors(self):
        types = self.pb.FileDescriptorProto(
            name="types.proto", package="sample.v1", syntax="proto2"
        )
        for name in ("Input", "Output", "Failure"):
            message = types.message_type.add(name=name)
            message.field.add(name="value", number=1, type=5, label=2)
        service = self.pb.FileDescriptorProto(
            name="service.proto", package="sample.v1", syntax="proto2"
        )
        service.dependency.append("types.proto")
        definition = service.service.add(name="Service")
        for name, shape in zip(("UU", "US", "SU", "SS"), GrpcCardinality, strict=True):
            definition.method.add(
                name=name,
                input_type=".sample.v1.Input",
                output_type=".sample.v1.Output",
                client_streaming=shape.value.startswith("stream"),
                server_streaming=shape.value.endswith("stream"),
            )
        return self.pb.FileDescriptorSet(file=[service, types])

    def closure(self, document=None):
        payload = (
            document if document is not None else self.descriptors()
        ).SerializeToString()
        return GrpcDescriptorClosure(payload, identity(payload))

    def case(self, **changes):
        args = dict(
            method="/sample.v1.Service/UU",
            cardinality=GrpcCardinality.UNARY_UNARY,
            requests=(b"\x08\x01",),
            responses=(b"\x08\x02",),
            status=GrpcStatus.OK,
            timeout_milliseconds=1000,
        )
        args.update(changes)
        return GrpcCallCase(**args)

    def test_unordered_complete_closure_resolves_all_rpc_shapes(self):
        closure = self.closure()
        for name, shape in zip(("UU", "US", "SU", "SS"), GrpcCardinality, strict=True):
            with self.subTest(shape=shape):
                binding = closure.validate_case(
                    self.case(method="/sample.v1.Service/" + name, cardinality=shape)
                )
                self.assertEqual(binding.input_type, "sample.v1.Input")
                self.assertEqual(binding.output_type, "sample.v1.Output")
                self.assertEqual(binding.cardinality, shape)
                self.assertEqual(binding.descriptor_identity, closure.identity)

    def test_missing_duplicate_cyclic_and_invalid_definitions_refuse(self):
        missing = self.descriptors()
        del missing.file[1]
        duplicate = self.descriptors()
        duplicate.file.add().CopyFrom(duplicate.file[0])
        cycle = self.descriptors()
        cycle.file[1].dependency.append("service.proto")
        invalid = self.descriptors()
        invalid.file[0].service[0].method[0].input_type = ".absent.Type"
        repeated = self.descriptors()
        repeated.file[0].dependency.append("types.proto")
        for document in (missing, duplicate, cycle, invalid, repeated):
            with (
                self.subTest(descriptors=len(document.file)),
                self.assertRaises(ValueError),
            ):
                self.closure(document)
        for payload in (b"\x80", b"\x10\x00"):
            with self.assertRaises(ValueError):
                GrpcDescriptorClosure(payload, identity(payload))

    def test_descriptor_file_count_is_bounded(self):
        document = self.pb.FileDescriptorSet()
        for index in range(129):
            document.file.add(name=f"file{index}.proto", syntax="proto3")
        with self.assertRaisesRegex(ValueError, "file count"):
            self.closure(document)

    def test_no_global_descriptor_fallback(self):
        self.assertIsNotNone(
            self.pool.Default().FindMessageTypeByName(
                "google.protobuf.FileDescriptorSet"
            )
        )
        with self.assertRaises(ValueError):
            self.closure().validate_message("google.protobuf.FileDescriptorSet", b"")

    def test_missing_method_and_shape_mismatch_refuse(self):
        closure = self.closure()
        for case in (
            self.case(method="/sample.v1.Service/Absent"),
            self.case(cardinality=GrpcCardinality.UNARY_STREAM),
        ):
            with self.assertRaises(ValueError):
                closure.validate_case(case)

    def test_malformed_missing_required_unknown_and_wrong_wire_fields_refuse(self):
        closure = self.closure()
        for payload in (b"\x80", b"", b"\x08\x01\x10\x01", b"\x0a\x01x"):
            for field in ("requests", "responses"):
                with (
                    self.subTest(payload=payload, field=field),
                    self.assertRaises(ValueError),
                ):
                    closure.validate_case(self.case(**{field: (payload,)}))

    def test_error_details_resolve_and_validate_their_declared_type(self):
        closure = self.closure()
        case = self.case(
            status=GrpcStatus.INVALID_ARGUMENT,
            responses=(),
            error_details=(GrpcErrorDetail("sample.v1.Failure", b"\x08\x01"),),
        )
        closure.validate_case(case)
        for detail in (
            GrpcErrorDetail("sample.v1.Absent", b""),
            GrpcErrorDetail("sample.v1.Failure", b""),
        ):
            with self.assertRaises(ValueError):
                closure.validate_case(replace(case, error_details=(detail,)))

    def test_descriptor_order_changes_exact_identity(self):
        document = self.descriptors()
        original = document.SerializeToString()
        document.file.reverse()
        changed = document.SerializeToString()
        self.assertNotEqual(original, changed)
        with self.assertRaisesRegex(ValueError, "declared identity"):
            GrpcDescriptorClosure(changed, identity(original))
