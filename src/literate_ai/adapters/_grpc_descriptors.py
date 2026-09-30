"""Descriptor-bound validation for native cases, with an optional protobuf runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from literate_ai.contracts import ContentIdentity, canonical_identity

from ._grpc_call_cases import MAX_MESSAGE_BYTES, GrpcCallCase, GrpcCardinality

MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
MAX_DESCRIPTOR_FILES = 128


@dataclass(frozen=True, slots=True)
class GrpcMethodBinding:
    descriptor_identity: ContentIdentity
    input_type: str
    output_type: str
    cardinality: GrpcCardinality


@dataclass(frozen=True, slots=True)
class GrpcReflectionMatch:
    declared_identity: ContentIdentity
    observed_identity: ContentIdentity
    normalized_identity: ContentIdentity
    file_names: tuple[str, ...]
    protobuf_runtime: str


class GrpcDescriptorClosure:
    """A complete declared pool, never the process-global protobuf registry.

    The identity binds original bytes. This does not claim cross-version canonical
    protobuf serialization. Reflection comparison sorts filenames and uses the
    same runtime to encode both sides and omit explicit default JSON names; repeated
    declaration order and custom names are preserved.
    """

    def __init__(self, payload: bytes, expected_identity: ContentIdentity):
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_DESCRIPTOR_BYTES:
            raise ValueError("descriptor set must contain bounded immutable bytes")
        if not isinstance(expected_identity, ContentIdentity):
            raise ValueError("descriptor identity must be typed")
        if expected_identity.uri != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise ValueError("descriptor bytes do not match their declared identity")
        try:
            from google.protobuf import (
                __version__,
                descriptor_pb2,
                descriptor_pool,
                message_factory,
            )
            from google.protobuf.internal import api_implementation
        except ImportError as exc:
            raise ValueError("gRPC protobuf toolchain is unavailable") from exc

        descriptors = descriptor_pb2.FileDescriptorSet()
        try:
            descriptors.ParseFromString(payload)
        except Exception as exc:
            raise ValueError("descriptor set is malformed") from exc
        if not 0 < len(descriptors.file) <= MAX_DESCRIPTOR_FILES:
            raise ValueError("descriptor set has an invalid file count")
        wrapper = descriptor_pb2.FileDescriptorSet()
        wrapper.CopyFrom(descriptors)
        wrapper.ClearField("file")
        if wrapper.SerializeToString():
            raise ValueError("descriptor set contains undeclared wrapper fields")
        files = {}
        for file in descriptors.file:
            if (
                not file.name
                or len(file.name) > 512
                or "\x00" in file.name
                or file.name in files
            ):
                raise ValueError("descriptor file names must be bounded and unique")
            if len(set(file.dependency)) != len(file.dependency):
                raise ValueError("descriptor dependencies must be unique")
            files[file.name] = file
        if any(
            dependency not in files
            for f in files.values()
            for dependency in f.dependency
        ):
            raise ValueError("descriptor dependency closure is incomplete")
        pool = descriptor_pool.DescriptorPool()
        loaded = set()
        while len(loaded) != len(files):
            ready = sorted(
                name
                for name, file in files.items()
                if name not in loaded and set(file.dependency) <= loaded
            )
            if not ready:
                raise ValueError("descriptor dependency closure contains a cycle")
            for name in ready:
                try:
                    pool.AddSerializedFile(files[name].SerializeToString())
                except Exception as exc:
                    raise ValueError("descriptor definition is invalid") from exc
                loaded.add(name)
        self._pool = pool
        self._message_class = message_factory.GetMessageClass
        self._identity = expected_identity
        self._descriptor_type = descriptor_pb2.FileDescriptorProto
        self._descriptor_pool_type = descriptor_pool.DescriptorPool
        self._default_json_names: dict[str, str] = {}
        self._protobuf_runtime = f"{__version__}:{api_implementation.Type()}"
        self._files = {
            name: self._normalized_file(file) for name, file in sorted(files.items())
        }

    def _normalized_file(self, file) -> bytes:
        normalized = self._descriptor_type()
        normalized.CopyFrom(file)
        pending = list(normalized.message_type)
        fields = list(normalized.extension)
        while pending:
            message = pending.pop()
            fields.extend(message.field)
            fields.extend(message.extension)
            pending.extend(message.nested_type)
        for field in fields:
            if not field.HasField("json_name"):
                continue
            if field.name not in self._default_json_names:
                # Ask the bound runtime for the implicit name; never infer it from
                # the declaration's possibly custom json_name or erase other fields.
                probe = self._descriptor_type(name="json_name.proto", syntax="proto3")
                probe.message_type.add(name="Name").field.add(
                    name=field.name, number=1, label=1, type=5
                )
                pool = self._descriptor_pool_type()
                pool.AddSerializedFile(probe.SerializeToString())
                self._default_json_names[field.name] = (
                    pool.FindMessageTypeByName("Name").fields[0].json_name
                )
            if field.json_name == self._default_json_names[field.name]:
                field.ClearField("json_name")
        return normalized.SerializeToString(deterministic=True)

    @property
    def identity(self) -> ContentIdentity:
        return self._identity

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(self._files)

    def match_reflection(self, payloads: tuple[bytes, ...]) -> GrpcReflectionMatch:
        """Compare one complete collected reflection closure, with no network I/O.

        Per-file declaration order, options and source information are preserved.
        Default JSON-name presence and outer file order are irrelevant. Duplicate
        filenames refuse even when their bytes agree; transport must request a
        complete unambiguous closure before handing it to this comparator.
        """
        if type(payloads) is not tuple or not 0 < len(payloads) <= MAX_DESCRIPTOR_FILES:
            raise ValueError("reflection requires a bounded immutable file sequence")
        if any(type(p) is not bytes or not p for p in payloads):
            raise ValueError("reflection descriptors must contain immutable bytes")
        if sum(len(p) for p in payloads) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("reflection descriptor bytes exceed the closure limit")
        files = {}
        for payload in payloads:
            file = self._descriptor_type()
            try:
                file.ParseFromString(payload)
                if not file.IsInitialized():
                    raise ValueError("descriptor fields are missing")
                normalized = self._normalized_file(file)
            except Exception as exc:
                raise ValueError("reflection descriptor is malformed") from exc
            if file.name not in self._files:
                raise ValueError("reflection contains an undeclared descriptor file")
            if file.name in files:
                raise ValueError("reflection descriptor filenames must be unique")
            files[file.name] = normalized
        if files != self._files:
            raise ValueError("reflection differs from the complete declared closure")
        names = tuple(sorted(files))
        return GrpcReflectionMatch(
            self.identity,
            canonical_identity(
                {
                    "schema": "literate-ai/grpc-reflection-bytes@1",
                    "files": [
                        "sha256:" + hashlib.sha256(p).hexdigest() for p in payloads
                    ],
                }
            ),
            canonical_identity(
                {
                    "schema": "literate-ai/grpc-descriptor-normalization@2",
                    "protobuf_runtime": self._protobuf_runtime,
                    "files": [
                        [name, "sha256:" + hashlib.sha256(files[name]).hexdigest()]
                        for name in names
                    ],
                }
            ),
            names,
            self._protobuf_runtime,
        )

    def validate_message(self, type_name: str, payload: bytes) -> None:
        if not isinstance(type_name, str) or not type_name or len(type_name) > 512:
            raise ValueError("protobuf message type name is invalid")
        if type(payload) is not bytes or len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("protobuf message must contain bounded immutable bytes")
        try:
            message = self._message_class(self._pool.FindMessageTypeByName(type_name))()
            message.ParseFromString(payload)
            if not message.IsInitialized():
                raise ValueError("required protobuf fields are absent")
            original = message.SerializeToString(deterministic=True)
            message.DiscardUnknownFields()
            if original != message.SerializeToString(deterministic=True):
                raise ValueError("protobuf message contains undeclared fields")
        except Exception as exc:
            raise ValueError(
                "message does not conform to its declared protobuf type"
            ) from exc

    def validate_case(self, case: GrpcCallCase) -> GrpcMethodBinding:
        if type(case) is not GrpcCallCase:
            raise ValueError("gRPC case must be typed")
        service, method_name = case.method[1:].split("/")
        try:
            method = self._pool.FindMethodByName(service + "." + method_name)
        except KeyError as exc:
            raise ValueError(
                "gRPC method is absent from the declared descriptor"
            ) from exc
        shape = GrpcCardinality(
            ("stream" if method.client_streaming else "unary")
            + "-"
            + ("stream" if method.server_streaming else "unary")
        )
        if case.cardinality is not shape:
            raise ValueError("gRPC cardinality differs from the declared method")
        for payload in case.requests:
            self.validate_message(method.input_type.full_name, payload)
        for payload in case.responses:
            self.validate_message(method.output_type.full_name, payload)
        for detail in case.error_details:
            self.validate_message(detail.type_name, detail.message)
        return GrpcMethodBinding(
            self.identity,
            method.input_type.full_name,
            method.output_type.full_name,
            shape,
        )
