"""Concrete command BUILD/CAS custody for exact-input artifact coalescing.

This conservative adapter deliberately keys the complete admitted BUILD input.
A cross-release semantic projection cannot replace that key until the build
artifact evidence contract supports new plan/source/SBOM admission explicitly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from literate_ai.adapters.action_build import admit_build_action
from literate_ai.adapters.action_build_result import read_build_result
from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
)
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.blobs import BlobRef
from literate_ai.storage import FileSystemCAS


class CommandBuildArtifactExecutor:
    """Produce through real command/SSH dispatch; reopen CAS for every request.

    The transport factory supplies each request's existing exact input records
    and durable result recorder. Artifact records and full dependency/archive
    bytes live in the verified CAS. Current BUILD grants and evidence are never
    rewritten. A separately retained admission record binds each request to the
    original native build evidence and immutable archive.
    """

    def __init__(
        self,
        transport_for: Callable[
            [LifecycleActionDispatchRequest], CommandLifecycleActionDispatcher
        ],
        cas: FileSystemCAS,
        *,
        blob_source=None,
    ):
        if not callable(transport_for) or not isinstance(cas, FileSystemCAS):
            raise TypeError(
                "command artifact executor requires transport and CAS custody"
            )
        self.transport_for = transport_for
        self.cas = cas
        self.blob_source = blob_source
        self._lock = threading.Lock()
        self._payloads: dict[ContentIdentity, BlobRef] = {}

    def _inputs(self, request):
        transport = self.transport_for(request)
        if not isinstance(transport, CommandLifecycleActionDispatcher):
            raise TypeError("artifact execution requires the owned command transport")
        transport.validate_request(request)
        records = transport.input_records(request)
        admit_build_action(
            request,
            transport.deadline,
            records,
            expected_worker_identity=request.worker.worker_identity,
        )
        identity = request.predecessor_result_identities[0]
        return transport, identity, records[identity]

    def action_key(self, request):
        return self._inputs(request)[1]

    def produce(self, request):
        transport, input_identity, input_record = self._inputs(request)
        started, started_utc = time.monotonic_ns(), datetime.now(UTC).isoformat()
        failure = None
        termination = None
        try:
            outcome, content = transport.dispatch_record(request)
            if outcome.failure_code is not None:
                raise ActionWireError(outcome.failure_code, "worker BUILD failed")
            if content is None or outcome.result_identity is None:
                raise ActionWireError(
                    "action_build.result_missing", "worker BUILD result missing"
                )
            read_build_result(
                content=content,
                result_identity=outcome.result_identity,
                input_record=input_record,
                input_identity=input_identity,
                deadline=transport.deadline,
                cas=self.cas,
                blob_source=None
                if self.blob_source is None
                else lambda ref: self.blob_source(request, ref),
                require_current=transport.deadline.remaining,
            )
            transport.validate_request(request)
            reference = self.cas.put_bytes(content)
            identity = ContentIdentity.parse_uri(reference.identity)
            if identity != outcome.result_identity:
                raise ActionWireError(
                    "action_build.result_invalid", "result custody differs"
                )
            with self._lock:
                self._payloads[identity] = reference
            return identity
        except BaseException as exc:
            failure = getattr(exc, "code", None) or type(exc).__name__
            termination = getattr(exc, "termination_evidence", None)
            raise
        finally:
            self._retain(
                transport,
                {
                    "schema": "literate-ai/command-build-attempt@1",
                    "request_identity": request.identity.uri,
                    "worker_identity": request.worker.worker_identity.uri,
                    "input_identity": input_identity.uri,
                    "started_utc": started_utc,
                    "ended_utc": datetime.now(UTC).isoformat(),
                    "wall_time_ns": time.monotonic_ns() - started,
                    "outcome": "passed" if failure is None else "failed",
                    "failure_code": failure,
                    "termination_evidence_identity": None
                    if termination is None
                    else termination.evidence_identity.uri,
                },
            )

    def _retain(self, transport, value):
        content = canonical_json_bytes(value)
        reference = self.cas.put_bytes(content)
        transport.record_result(ContentIdentity.parse_uri(reference.identity), content)

    def admit(self, request, artifact):
        transport, input_identity, input_record = self._inputs(request)
        with self._lock:
            reference = self._payloads.get(artifact)
        if reference is None:
            raise ActionWireError(
                "action_build.payload_missing", "verified BUILD payload is unavailable"
            )
        content = self.cas.get_bytes(reference)
        _, result, _, _, _ = read_build_result(
            content=content,
            result_identity=artifact,
            input_record=input_record,
            input_identity=input_identity,
            deadline=transport.deadline,
            cas=self.cas,
            require_current=transport.deadline.remaining,
        )
        transport.validate_request(request)
        self._retain(
            transport,
            {
                "schema": "literate-ai/command-build-artifact-admission@1",
                "admission_location": "controller",
                "native_execution_repeated": False,
                "request_identity": request.identity.uri,
                "worker_identity": request.worker.worker_identity.uri,
                "input_identity": input_identity.uri,
                "result_identity": artifact.uri,
                "artifact_archive": result.artifact_archive.to_dict(),
                "build_evidence_identity": result.evidence.identity.uri,
                "admitted_utc": datetime.now(UTC).isoformat(),
            },
        )
        transport.record_result(artifact, content)
        return LifecycleActionDispatchOutcome(request.identity, artifact)

    def cancel(self, request):
        self.transport_for(request).cancel(request)
