"""Default native dispatch drives a real child service and always shuts it down."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    load_ipc_surface_conformance_acceptance,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts import canonical_identity
from tests.support.fixtures_test_ipc_surface_acceptance import _identity, _lock, _valid_document

_SERVER = r"""
import signal, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import grpc
from google.protobuf import descriptor_pb2, descriptor_pool
from grpc_reflection.v1alpha import reflection

assert sys.argv[1] == "--litai-serve"
port = int(sys.argv[2])
marker = Path(sys.argv[3])
mode = sys.argv[4]
pool = descriptor_pool.DescriptorPool()
for file in descriptor_pb2.FileDescriptorSet.FromString(
    Path(__file__).with_suffix(".pb").read_bytes()
).file:
    pool.AddSerializedFile(file.SerializeToString())
executor = ThreadPoolExecutor(max_workers=4)
server = grpc.server(executor)


def unary(request, context):
    return b"\x08\x03" if mode == "wrong" else b"\x08\x02"


def stream(request, context):
    if mode == "slow":
        while context.is_active():
            time.sleep(0.005)
        return
    yield unary(request, context)


handlers = {}
for name, shape in [
    ("UU", "unary_unary"),
    ("US", "unary_stream"),
    ("SU", "stream_unary"),
    ("SS", "stream_stream"),
]:

    def apply_unary(request, context, shape=shape):
        if shape.startswith("stream"):
            list(request)
        return unary(request, context)

    def apply_stream(request, context, shape=shape):
        if shape.startswith("stream"):
            list(request)
        yield from stream(request, context)

    handlers[name] = getattr(grpc, shape + "_rpc_method_handler")(
        apply_stream if shape.endswith("stream") else apply_unary
    )
server.add_generic_rpc_handlers(
    (grpc.method_handlers_generic_handler("sample.Service", handlers),)
)
reflection.enable_server_reflection(
    ("sample.Service", reflection.SERVICE_NAME), server, pool=pool
)
assert server.add_insecure_port("127.0.0.1:" + str(port)) == port


def stop(*args):
    server.stop(0).wait(3)
    executor.shutdown(wait=True)
    marker.write_text("stopped")
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
server.start()
server.wait_for_termination()
"""


class GrpcLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import grpc  # noqa: F401
            from google.protobuf import descriptor_pb2, descriptor_pool
        except ImportError:
            raise unittest.SkipTest("requires grpc-acceptance runtime") from None
        cls.pb = descriptor_pb2
        cls.pool = descriptor_pool

    def document(self):
        file = self.pb.FileDescriptorProto(
            name="service.proto", package="sample", syntax="proto3"
        )
        for name in ["Input", "Output"]:
            file.message_type.add(name=name).field.add(
                name="value", number=1, type=5, label=1
            )
        service = file.service.add(name="Service")
        cases = []
        for name, shape in [
            ("UU", "unary-unary"),
            ("US", "unary-stream"),
            ("SU", "stream-unary"),
            ("SS", "stream-stream"),
        ]:
            service.method.add(
                name=name,
                input_type=".sample.Input",
                output_type=".sample.Output",
                client_streaming=shape.startswith("stream"),
                server_streaming=shape.endswith("stream"),
            )
            cases.append(
                dict(
                    method="/sample.Service/" + name,
                    cardinality=shape,
                    input_type="sample.Input",
                    output_type="sample.Output",
                    requests=["0801"],
                    responses=["0802"],
                    status="OK",
                    status_message="",
                    timeout_milliseconds=1000,
                    error_details=[],
                )
            )
        canonical = (
            self.pool.DescriptorPool()
            .AddSerializedFile(file.SerializeToString())
            .serialized_pb
        )
        payload = self.pb.FileDescriptorSet(
            file=[self.pb.FileDescriptorProto.FromString(canonical)]
        ).SerializeToString()
        document = _valid_document(
            schema="literate-ai/ipc-surface-conformance-acceptance@2",
            protocol="grpc",
            declared_schema_identity="sha256:" + hashlib.sha256(payload).hexdigest(),
            description_probe={
                "descriptor_set_hex": payload.hex(),
                "timeout_milliseconds": 1000,
            },
            request_cases=cases,
        )
        return document, payload

    def run_lifecycle(self, mode, expected_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            package.mkdir()
            marker = root / "stopped"
            service = package / "service.py"
            service.write_text(_SERVER)
            document, payload = self.document()
            service.with_suffix(".pb").write_bytes(payload)
            document["process"]["arguments"] = ["{port}", str(marker), mode]
            if mode == "slow":
                document["request_cases"][1]["timeout_milliseconds"] = 50
            path = root / "oracle.json"
            path.write_text(json.dumps(document))
            contract = load_ipc_surface_conformance_acceptance(path, "surface")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=contract,
            )
            recorder = QualificationEvidenceRecorder(
                max_bytes=32 * 1024 * 1024, max_records=100
            )
            ports.retain_evidence_with(recorder)
            custody = SimpleNamespace(
                root=package, tree_identity=local_tree_identity(package)
            )
            plan = SimpleNamespace(
                identity=_identity("plan"),
                entrypoints=(SimpleNamespace(kind="persistent-service"),),
            )
            result = SimpleNamespace(identity=_identity("result"))
            launched = []
            popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = popen(*args, **kwargs)
                launched.append(process)
                return process

            with (
                mock.patch(
                    "literate_ai.adapters.lifecycle.standard_local.subprocess.Popen",
                    side_effect=launch,
                ),
                mock.patch.object(
                    ports, "project_package_custody", return_value=custody
                ),
                mock.patch.object(
                    ports,
                    "_packaged_argv",
                    return_value=(sys.executable, str(service), "--litai-smoke"),
                ),
                mock.patch.object(ports, "_packaged_environment", return_value={}),
                mock.patch.object(
                    ports,
                    "_http_get_body",
                    side_effect=AssertionError("native acceptance must not call HTTP"),
                ),
            ):

                def accept():
                    return ports.accept_project_independently(
                        _lock("sha256:spec"),
                        mock.sentinel.execution_plan,
                        mock.sentinel.build_plan,
                        plan,
                        result,
                        _identity("test"),
                        _identity("execution"),
                    )

                if expected_error:
                    with self.assertRaisesRegex(
                        LocalStandardLifecycleError, expected_error
                    ):
                        accept()
                else:
                    evidence = accept()
                    reader = QualificationEvidenceReader(
                        recorder.entries, max_bytes=32 * 1024 * 1024, max_records=100
                    )
                    record = reader.read_json(evidence)
                    native = record["native_observation"]
                    reflected = json.loads(
                        bytes.fromhex(native["served_description_hex"])
                    )
                    self.assertTrue(reflected["responses"])
                    details = json.loads(native["protocol_detail"])
                    for raw, observed in zip(
                        details["cases"],
                        record["conformance"]["request_cases"],
                        strict=True,
                    ):
                        self.assertEqual(
                            canonical_identity(raw).uri, observed["response_identity"]
                        )

            self.assertTrue(launched)
            self.assertTrue(all(process.poll() is not None for process in launched))
            # Windows job termination need not execute a Python signal handler.
            # Process exit is asserted on every platform above.
            if os.name != "nt":
                self.assertEqual(marker.read_text(), "stopped")
            self.assertEqual(local_tree_identity(package), custody.tree_identity)

    def test_default_native_dispatch_accepts_all_shapes_and_stops_child(self):
        self.run_lifecycle("normal")

    def test_default_native_dispatch_refuses_wrong_response_and_stops_child(self):
        self.run_lifecycle("wrong", "response_invalid")

    def test_default_native_dispatch_refuses_deadline_and_stops_child(self):
        self.run_lifecycle("slow", "response_invalid")
