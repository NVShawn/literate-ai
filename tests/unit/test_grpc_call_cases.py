"""Native case bounds before protobuf decoding or transport selection."""

import unittest
from dataclasses import FrozenInstanceError, replace

from literate_ai.adapters._grpc_call_cases import (
    MAX_CASE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    GrpcCallCase,
    GrpcCardinality,
    GrpcErrorDetail,
    GrpcStatus,
)


class GrpcCallCaseTests(unittest.TestCase):
    def case(self, **changes):
        values = dict(
            method="/sample.v1.Service/Call",
            cardinality=GrpcCardinality.UNARY_UNARY,
            requests=(b"",),
            responses=(b"",),
            status=GrpcStatus.OK,
            timeout_milliseconds=1000,
        )
        values.update(changes)
        return GrpcCallCase(**values)

    def test_all_four_shapes_preserve_order_and_empty_messages(self):
        for shape in GrpcCardinality:
            with self.subTest(shape=shape):
                requests = (
                    (b"", b"\x08\x01") if shape.value.startswith("stream") else (b"",)
                )
                responses = (
                    (b"\x08\x02", b"") if shape.value.endswith("stream") else (b"",)
                )
                case = self.case(
                    cardinality=shape, requests=requests, responses=responses
                )
                self.assertEqual(case.requests, requests)
                self.assertEqual(case.responses, responses)
                with self.assertRaises(FrozenInstanceError):
                    case.requests = ()

    def test_empty_streams_and_partial_stream_failure_are_representable(self):
        self.case(cardinality=GrpcCardinality.STREAM_STREAM, requests=(), responses=())
        self.case(cardinality=GrpcCardinality.UNARY_STREAM, responses=())
        detail = GrpcErrorDetail("sample.v1.Failure", b"\x08\x01")
        case = self.case(
            cardinality=GrpcCardinality.UNARY_STREAM,
            responses=(b"\x08\x01", b"\x08\x02"),
            status=GrpcStatus.INVALID_ARGUMENT,
            status_message="invalid item",
            error_details=(detail,),
        )
        self.assertEqual(case.error_details, (detail,))

    def test_unary_cardinality_is_enforced_in_both_directions(self):
        for changes in (
            {"requests": ()},
            {"requests": (b"", b"")},
            {"responses": ()},
            {"responses": (b"", b"")},
            {"status": GrpcStatus.NOT_FOUND},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.case(**changes)
        self.case(status=GrpcStatus.NOT_FOUND, responses=())
        self.case(cardinality=GrpcCardinality.STREAM_UNARY, requests=())

    def test_ambiguous_mutable_and_unbounded_cases_refuse(self):
        for changes in (
            {"method": "https://example.test/Call"},
            {"method": "/Service/Call?x=1"},
            {"method": "/Service/Call\n"},
            {"method": "/S/" + "x" * 1024},
            {"cardinality": "unary-unary"},
            {"status": "OK"},
            {"requests": [b""]},
            {"requests": (bytearray(),)},
            {"responses": (b"x" * (MAX_MESSAGE_BYTES + 1),)},
            {"requests": (b"",) * (MAX_MESSAGES + 1)},
            {"error_details": []},
            {"error_details": (b"",)},
            {"status_message": "\x00"},
            {"status_message": "x" * 4097},
        ):
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError):
                self.case(**changes)

    def test_deadlines_reject_boolean_float_nonfinite_and_out_of_range(self):
        for timeout in (True, False, 0, -1, 120001, 1.0, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.case(timeout_milliseconds=timeout)
        self.case(timeout_milliseconds=1)
        self.case(timeout_milliseconds=120000)

    def test_aggregate_budget_covers_both_directions_and_error_details(self):
        payload = b"x" * MAX_MESSAGE_BYTES
        case = self.case(
            cardinality=GrpcCardinality.STREAM_STREAM,
            requests=(payload,) * (MAX_CASE_BYTES // MAX_MESSAGE_BYTES - 1),
            responses=(payload,),
            status=GrpcStatus.INTERNAL,
        )
        with self.assertRaisesRegex(ValueError, "aggregate"):
            replace(case, error_details=(GrpcErrorDetail("Failure", b"x"),))

    def test_error_details_are_typed_bounded_and_absent_on_success(self):
        detail = GrpcErrorDetail("sample.v1.Failure", b"")
        for type_name, message in (
            ("bad/type", b""),
            ("Failure", bytearray()),
            ("Failure", b"x" * (MAX_MESSAGE_BYTES + 1)),
        ):
            with self.subTest(type_name=type_name), self.assertRaises(ValueError):
                GrpcErrorDetail(type_name, message)
        with self.assertRaises(ValueError):
            self.case(error_details=(detail,))
        with self.assertRaises(ValueError):
            self.case(
                status=GrpcStatus.INTERNAL, responses=(), error_details=(detail,) * 33
            )
        self.case(status_message="completed")
