"""Real descriptor reflection comparison without a network transport."""

import unittest
from dataclasses import FrozenInstanceError

from literate_ai.adapters._grpc_descriptors import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_FILES,
    GrpcDescriptorClosure,
)
from tests.unit import test_grpc_descriptors as fixtures


class GrpcReflectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.GrpcDescriptorTests.setUpClass()
        cls.fixture = fixtures.GrpcDescriptorTests()
        cls.pb = cls.fixture.pb

    def reflected(self, document=None):
        return tuple(
            f.SerializeToString() for f in (document or self.fixture.descriptors()).file
        )

    def test_unordered_files_match_without_losing_raw_observation_identity(self):
        closure = self.fixture.closure()
        payloads = self.reflected()
        original = closure.match_reflection(payloads)
        reordered = closure.match_reflection(tuple(reversed(payloads)))
        self.assertEqual(original.declared_identity, closure.identity)
        self.assertEqual(original.normalized_identity, reordered.normalized_identity)
        self.assertNotEqual(original.observed_identity, reordered.observed_identity)
        self.assertEqual(original.file_names, ("service.proto", "types.proto"))
        declaration = self.fixture.descriptors()
        declaration.file.reverse()
        rebound = self.fixture.closure(declaration).match_reflection(payloads)
        self.assertNotEqual(original.declared_identity, rebound.declared_identity)
        self.assertEqual(original.normalized_identity, rebound.normalized_identity)
        self.assertTrue(original.protobuf_runtime.startswith("7.36.2:"))
        with self.assertRaises(FrozenInstanceError):
            original.file_names = ()

    def test_wire_field_order_normalizes_in_the_same_runtime(self):
        document = self.fixture.descriptors()
        file = document.file[0]
        name = self.pb.FileDescriptorProto(name=file.name)
        file.ClearField("name")
        reordered = file.SerializeToString() + name.SerializeToString()
        closure = self.fixture.closure()
        original = closure.match_reflection(self.reflected())
        observed = closure.match_reflection(
            (reordered, document.file[1].SerializeToString())
        )
        self.assertEqual(original.normalized_identity, observed.normalized_identity)
        self.assertNotEqual(original.observed_identity, observed.observed_identity)

    def test_default_json_name_presence_is_semantically_equivalent(self):
        omitted = self.fixture.descriptors()
        field = omitted.file[1].message_type[0].field[0]
        field.name = "item_value"
        explicit = self.pb.FileDescriptorSet()
        explicit.CopyFrom(omitted)
        explicit.file[1].message_type[0].field[0].json_name = "itemValue"
        closure = self.fixture.closure(explicit)
        before = explicit.SerializeToString()
        match = closure.match_reflection(self.reflected(omitted))
        original = closure.match_reflection(self.reflected(explicit))
        self.assertEqual(match.normalized_identity, original.normalized_identity)
        self.assertNotEqual(match.observed_identity, original.observed_identity)
        self.assertEqual(closure.identity, fixtures.identity(before))
        self.assertEqual(explicit.SerializeToString(), before)
        self.fixture.closure(omitted).match_reflection(self.reflected(explicit))

    def test_default_json_names_cover_nested_fields_and_extensions(self):
        file = self.pb.FileDescriptorProto(
            name="names.proto", package="names", syntax="proto2"
        )
        outer = file.message_type.add(name="Outer")
        outer.extension_range.add(start=1, end=10)
        outer.nested_type.add(name="Inner").field.add(
            name="inner_value", number=1, label=1, type=5, json_name="innerValue"
        )
        file.extension.add(
            name="extra_value",
            number=1,
            label=1,
            type=5,
            extendee=".names.Outer",
            json_name="extraValue",
        )
        outer.extension.add(
            name="nested_value",
            number=2,
            label=1,
            type=5,
            extendee=".names.Outer",
            json_name="nestedValue",
        )
        document = self.pb.FileDescriptorSet(file=[file])
        closure = self.fixture.closure(document)
        file.message_type[0].nested_type[0].field[0].ClearField("json_name")
        file.extension[0].ClearField("json_name")
        file.message_type[0].extension[0].ClearField("json_name")
        closure.match_reflection((file.SerializeToString(),))
        file.message_type[0].extension[0].json_name = "changed"
        with self.assertRaises(ValueError):
            closure.match_reflection((file.SerializeToString(),))

    def test_custom_json_name_cannot_be_omitted_or_changed(self):
        declared = self.fixture.descriptors()
        declared.file[1].message_type[0].field[0].json_name = "customValue"
        closure = self.fixture.closure(declared)
        closure.match_reflection(self.reflected(declared))
        for name in (None, "value", "anotherValue"):
            changed = self.pb.FileDescriptorSet()
            changed.CopyFrom(declared)
            field = changed.file[1].message_type[0].field[0]
            if name is None:
                field.ClearField("json_name")
            else:
                field.json_name = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                closure.match_reflection(self.reflected(changed))

    def test_missing_extra_duplicate_and_conflicting_files_refuse(self):
        closure = self.fixture.closure()
        payloads = self.reflected()
        changed = self.fixture.descriptors()
        changed.file[0].package = "changed"
        foreign = self.pb.FileDescriptorProto(name="extra.proto").SerializeToString()
        for observed in (
            payloads[:1],
            payloads + (foreign,),
            payloads + (payloads[0],),
            payloads + (changed.file[0].SerializeToString(),),
        ):
            with self.subTest(observed=len(observed)), self.assertRaises(ValueError):
                closure.match_reflection(observed)

    def test_every_declared_field_including_options_and_source_info_is_bound(self):
        changes = [
            lambda d: setattr(d.file[0].service[0].method[0], "server_streaming", True),
            lambda d: setattr(d.file[1].message_type[0].field[0], "type", 9),
            lambda d: setattr(d.file[0].options, "java_package", "changed"),
            lambda d: d.file[0].source_code_info.location.add(
                leading_comments="changed"
            ),
            lambda d: d.file[0].dependency.append("absent.proto"),
            lambda d: d.file[0].service[0].method.reverse(),
        ]
        closure = self.fixture.closure()
        for index, change in enumerate(changes):
            document = self.fixture.descriptors()
            change(document)
            with self.subTest(change=index), self.assertRaises(ValueError):
                closure.match_reflection(self.reflected(document))

    def test_malformed_unknown_and_empty_descriptors_refuse(self):
        closure = self.fixture.closure()
        payloads = self.reflected()
        for payload in (b"\x80", b"", payloads[0] + b"\xf8\x07\x01"):
            with self.subTest(payload=payload[-3:]), self.assertRaises(ValueError):
                closure.match_reflection((payload, payloads[1]))

    def test_collection_types_counts_and_bytes_are_bounded(self):
        closure = self.fixture.closure()
        payloads = self.reflected()
        for observed in (
            (),
            list(payloads),
            (bytearray(payloads[0]),),
            (payloads[0],) * (MAX_DESCRIPTOR_FILES + 1),
            (b"x" * (MAX_DESCRIPTOR_BYTES + 1),),
        ):
            with (
                self.subTest(kind=type(observed).__name__, count=len(observed)),
                self.assertRaises(ValueError),
            ):
                closure.match_reflection(observed)

    def test_unknown_descriptor_set_wrapper_fields_cannot_escape_comparison(self):
        payload = self.fixture.descriptors().SerializeToString() + b"\xf8\x07\x01"
        with self.assertRaisesRegex(ValueError, "wrapper fields"):
            GrpcDescriptorClosure(payload, fixtures.identity(payload))
