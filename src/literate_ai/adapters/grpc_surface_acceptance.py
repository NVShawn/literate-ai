"""Bounded loopback gRPC transport for native IPC contracts.

The lifecycle selects this adapter for native IPC @2 gRPC declarations.
"""

from __future__ import annotations

import math
import time
from importlib.metadata import version
from urllib.parse import urlsplit

from literate_ai.adapters._grpc_call_cases import (
    MAX_CASE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
)
from literate_ai.adapters._grpc_descriptors import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_FILES,
    GrpcDescriptorClosure,
)
from literate_ai.adapters._grpc_oracle import GrpcBoundCallCase, GrpcReflectionProbe
from literate_ai.adapters.ipc_surface_acceptance import (
    IpcRequestCaseObservation,
    IpcSurfaceAcceptanceError,
    IpcSurfaceObservation,
    IpcSurfaceProbe,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)


def _refuse(message):
    raise IpcSurfaceAcceptanceError("ipc_surface_acceptance.grpc_refused", message)


def _target(base_url):
    try:
        value = urlsplit(base_url)
        if (
            value.scheme != "http"
            or value.hostname != "127.0.0.1"
            or value.username is not None
            or value.password is not None
            or value.path
            or value.query
            or value.fragment
            or value.port is None
            or value.port == 0
        ):
            raise ValueError("not a lifecycle loopback address")
        return f"127.0.0.1:{value.port}"
    except ValueError as exc:
        _refuse(str(exc))


def _remaining(deadline, maximum):
    remaining = min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        _refuse("gRPC observation exceeded its remaining process budget")
    return remaining


class GrpcIpcSurfaceProbe(IpcSurfaceProbe):
    def __init__(self):
        try:
            import grpc
            from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
            from grpc_status import rpc_status
        except ImportError as exc:
            raise IpcSurfaceAcceptanceError(
                "ipc_surface_acceptance.grpc_unavailable",
                "native gRPC acceptance dependencies are unavailable",
            ) from exc
        self._grpc = grpc
        self._reflection = reflection_pb2
        self._reflection_stub = reflection_pb2_grpc.ServerReflectionStub
        self._status = rpc_status
        self._identity = canonical_identity(
            {
                "adapter": "literate-ai/native-grpc-probe@1",
                "runtime": {
                    name: version(name)
                    for name in (
                        "grpcio",
                        "grpcio-reflection",
                        "grpcio-status",
                        "protobuf",
                        "googleapis-common-protos",
                    )
                },
            }
        )

    @property
    def tool_identity(self):
        return self._identity

    def _channel(self, base_url):
        return self._grpc.insecure_channel(
            _target(base_url),
            options=(
                ("grpc.max_receive_message_length", MAX_DESCRIPTOR_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_metadata_size", 65536),
                ("grpc.enable_http_proxy", 0),
            ),
        )

    def _contract(self, contract):
        if (
            contract.protocol != "grpc"
            or not isinstance(contract.description_probe, GrpcReflectionProbe)
            or not contract.request_cases
            or any(not isinstance(c, GrpcBoundCallCase) for c in contract.request_cases)
        ):
            _refuse("native gRPC transport requires an IPC @2 contract")
        closure = GrpcDescriptorClosure(
            contract.description_probe.descriptor_set,
            ContentIdentity.parse_uri(contract.declared_schema_identity),
        )
        for bound in contract.request_cases:
            binding = closure.validate_case(bound.call)
            if (
                binding.input_type != bound.input_type
                or binding.output_type != bound.output_type
            ):
                _refuse("gRPC case types disagree with the descriptor")
        return closure

    def is_ready(self, base_url, contract, *, timeout_seconds):
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            _refuse("readiness requires a positive finite budget")
        deadline = time.monotonic() + min(
            timeout_seconds, contract.startup_timeout_seconds
        )
        self._contract(contract)
        with self._channel(base_url) as channel:
            future = self._grpc.channel_ready_future(channel)
            try:
                future.result(timeout=_remaining(deadline, timeout_seconds))
                return True
            except self._grpc.FutureTimeoutError:
                return False
            finally:
                future.cancel()

    def observe(self, base_url, contract):
        return self.observe_with_timeout(
            base_url, contract, timeout_seconds=contract.process_timeout_seconds
        )

    def _reflect(self, channel, closure, deadline):
        names = closure.file_names
        requests = [
            self._reflection.ServerReflectionRequest(file_by_filename=name)
            for name in names
        ]
        call = self._reflection_stub(channel).ServerReflectionInfo(
            iter(requests), timeout=_remaining(deadline, 120), wait_for_ready=False
        )
        files = {}
        raw = []
        total = 0
        count = 0
        try:
            for response in call:
                _remaining(deadline, 120)
                if (
                    count >= len(names)
                    or response.original_request != requests[count]
                    or response.WhichOneof("message_response")
                    != "file_descriptor_response"
                ):
                    _refuse("reflection response does not match its declared request")
                count += 1
                batch = set()
                batch_raw = []
                payloads = response.file_descriptor_response.file_descriptor_proto
                if len(payloads) > MAX_DESCRIPTOR_FILES:
                    _refuse("reflection response exceeds its file count")
                for payload in payloads:
                    total += len(payload)
                    if total > MAX_DESCRIPTOR_BYTES:
                        _refuse("reflection response exceeds its byte budget")
                    from google.protobuf import descriptor_pb2

                    file = descriptor_pb2.FileDescriptorProto.FromString(payload)
                    if file.name not in names or file.name in batch:
                        _refuse("reflection contains extra or duplicate files")
                    batch.add(file.name)
                    if file.name in files and files[file.name] != payload:
                        _refuse("reflection repeats a conflicting file")
                    files[file.name] = payload
                    batch_raw.append(payload.hex())
                raw.append({"file_by_filename": names[count - 1], "files": batch_raw})
                if names[count - 1] not in files:
                    _refuse("reflection omitted the requested file")
            if count != len(names):
                _refuse("reflection did not answer every request")
            match = closure.match_reflection(
                tuple(files[name] for name in sorted(files))
            )
            return match, canonical_json_bytes(
                {"schema": "literate-ai/grpc-reflection-wire@1", "responses": raw}
            )
        finally:
            call.cancel()

    def _drive(self, channel, closure, bound, deadline, index, remaining_bytes):
        case = bound.call
        declared_timeout = case.timeout_milliseconds / 1000
        timeout = _remaining(deadline, declared_timeout)
        # grpc-core may surface DEADLINE_EXCEEDED just before Python's
        # monotonic deadline (notably on Windows). Remember which deadline
        # supplied the call timeout so a process-budget expiry cannot become
        # an ordinary, potentially acceptable case observation.
        process_budget_limits_call = timeout < declared_timeout
        method = getattr(channel, case.cardinality.value.replace("-", "_"))(case.method)
        requests = (
            iter(case.requests)
            if case.cardinality.value.startswith("stream")
            else case.requests[0]
        )
        responses = []
        call = None
        total = 0
        try:
            try:
                if case.cardinality.value.endswith("unary"):
                    response, call = method.with_call(
                        requests, timeout=timeout, wait_for_ready=False
                    )
                    responses.append(response)
                else:
                    call = method(requests, timeout=timeout, wait_for_ready=False)
                    for response in call:
                        _remaining(deadline, 120)
                        total += len(response)
                        if (
                            len(response) > MAX_MESSAGE_BYTES
                            or total > MAX_CASE_BYTES
                            or total > remaining_bytes[0]
                            or len(responses) >= MAX_MESSAGES
                            or len(responses) >= len(case.responses)
                        ):
                            _refuse("gRPC response stream exceeds its declared bounds")
                        responses.append(response)
            except self._grpc.RpcError as exc:
                call = exc
            if call is None:
                _refuse("gRPC call returned no terminal status")
            if (
                process_budget_limits_call
                and call.code() == self._grpc.StatusCode.DEADLINE_EXCEEDED
            ):
                _refuse("gRPC observation exceeded its remaining process budget")
            _remaining(deadline, 120)
            total = sum(len(response) for response in responses)
            details = []
            status = self._status.from_call(call)
            if status is not None:
                if len(status.details) > 32:
                    _refuse("gRPC error detail count exceeds its bound")
                for detail in status.details:
                    name = detail.type_url.rsplit("/", 1)[-1]
                    closure.validate_message(name, detail.value)
                    total += len(detail.value)
                    details.append((name, detail.value))
            for response in responses:
                closure.validate_message(bound.output_type, response)
            remaining_bytes[0] -= total
            if total > MAX_CASE_BYTES or remaining_bytes[0] < 0:
                _refuse("gRPC observed payload exceeds its byte budget")
            code = call.code().name
            text = call.details()
            if not isinstance(text, str) or len(text) > 4096:
                _refuse("gRPC status text exceeds its bound")
            observed = {
                "responses": [p.hex() for p in responses],
                "status": code,
                "status_message": text,
                "error_details": [[n, p.hex()] for n, p in details],
            }
            valid = (
                tuple(responses) == case.responses
                and code == case.status.value
                and text == case.status_message
                and tuple(details)
                == tuple((d.type_name, d.message) for d in case.error_details)
            )
            observation = IpcRequestCaseObservation(
                index,
                valid,
                canonical_identity(observed).uri,
                ""
                if valid
                else "gRPC responses or terminal status differ from the oracle",
            )
            return observation, observed
        finally:
            if call is not None:
                call.cancel()

    def observe_with_timeout(self, base_url, contract, *, timeout_seconds):
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            _refuse("observation requires a positive finite budget")
        deadline = time.monotonic() + min(
            timeout_seconds, contract.process_timeout_seconds
        )
        try:
            closure = self._contract(contract)
            with self._channel(base_url) as channel:
                match, raw = self._reflect(
                    channel,
                    closure,
                    min(
                        deadline,
                        time.monotonic() + contract.description_probe.timeout_seconds,
                    ),
                )
                remaining_bytes = [MAX_CASE_BYTES]
                driven = tuple(
                    self._drive(
                        channel, closure, case, deadline, index, remaining_bytes
                    )
                    for index, case in enumerate(contract.request_cases)
                )
                _remaining(deadline, 120)
                detail = canonical_json_bytes(
                    {
                        "schema": "literate-ai/grpc-observation-detail@1",
                        "normalized_descriptor_identity": match.normalized_identity.uri,
                        "cases": [raw_case for _, raw_case in driven],
                    }
                ).decode("utf-8")
                _remaining(deadline, 120)
                return IpcSurfaceObservation(
                    True,
                    match.declared_identity.uri,
                    tuple(observation for observation, _ in driven),
                    raw,
                    detail,
                )
        except IpcSurfaceAcceptanceError:
            raise
        except Exception as exc:
            raise IpcSurfaceAcceptanceError(
                "ipc_surface_acceptance.grpc_failed",
                f"native gRPC observation failed: {type(exc).__name__}",
            ) from exc
