"""Retain exact run products before qualification scratch is discarded.

This capture checks integrity and membership. It is not importer admission or a
complete portable qualification bundle: current authority, independent verifier
records and importer trust must still be reopened before consumption.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from threading import Lock

from literate_ai.adapters.component_acceptance import LibraryAcceptance
from literate_ai.contracts import ComponentLock
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.product_json import (
    product_json_bytes,
    product_json_identity,
    product_json_values_equal,
)
from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet
from literate_ai.contracts.standard_lifecycle_membership import StandardAggregateReceipt
from literate_ai.contracts.standard_lifecycle_policy import (
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
)
from literate_ai.contracts.standard_post_source_evidence import (
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestCaseEvidence,
    StandardGeneratedTestExecutionEvidence,
)
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from literate_ai.contracts.testing import ProjectTestReceipt
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
    QualificationLifecycleResult,
    QualificationLifecycleRunEvidence,
    QualificationLifecycleRunner,
)


class QualificationCaptureError(ValueError):
    """A run cannot supply the exact requested product/evidence capture."""


class QualificationEvidenceRecorder:
    """Bounded immutable payload retention under existing content identities."""

    def __init__(self, *, max_bytes: int, max_records: int) -> None:
        if any(
            type(value) is not int or value <= 0 for value in (max_bytes, max_records)
        ):
            raise QualificationCaptureError("qualification.capture.limit-invalid")
        self.max_bytes = max_bytes
        self.max_records = max_records
        self._retained_bytes = 0
        self._lock = Lock()
        self._records: dict[ContentIdentity, bytes] = {}

    def remember_json(self, value: object) -> ContentIdentity:
        payload = canonical_json_bytes(value)
        return self.remember_bytes(payload)

    def remember_bytes(self, payload: bytes) -> ContentIdentity:
        if not isinstance(payload, bytes):
            raise QualificationCaptureError("qualification.capture.bytes-required")
        identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(payload).hexdigest()
        )
        self._remember(identity, payload)
        return identity

    def _remember(self, identity: ContentIdentity, payload: bytes) -> None:
        # Component builds run concurrently. Deduplication, both bounds and the
        # insertion must be one operation across every producer in this run.
        with self._lock:
            previous = self._records.get(identity)
            if previous is not None:
                if previous != payload:
                    raise QualificationCaptureError(
                        "qualification.capture.identity-conflict"
                    )
                return
            if len(self._records) >= self.max_records:
                raise QualificationCaptureError("qualification.capture.record-limit")
            if self._retained_bytes + len(payload) > self.max_bytes:
                raise QualificationCaptureError("qualification.capture.byte-limit")
            self._records[identity] = payload
            self._retained_bytes += len(payload)

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    @property
    def entries(self) -> tuple[tuple[ContentIdentity, bytes], ...]:
        with self._lock:
            return tuple(sorted(self._records.items(), key=lambda item: item[0].uri))


class QualificationEvidenceReader:
    """Bounded exact record reopening; this grants no importer trust."""

    def __init__(self, entries, *, max_bytes: int, max_records: int) -> None:
        if any(
            type(value) is not int or value <= 0 for value in (max_bytes, max_records)
        ):
            raise QualificationCaptureError("qualification.capture.limit-invalid")
        if not isinstance(entries, tuple) or len(entries) > max_records:
            raise QualificationCaptureError("qualification.capture.record-limit")
        total = 0
        previous = ""
        for entry in entries:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], ContentIdentity)
                or not isinstance(entry[1], bytes)
            ):
                raise QualificationCaptureError("qualification.capture.records-invalid")
            identity, payload = entry
            if identity.uri <= previous:
                raise QualificationCaptureError(
                    "qualification.capture.records-noncanonical"
                )
            previous = identity.uri
            total += len(payload)
            if total > max_bytes:
                raise QualificationCaptureError("qualification.capture.byte-limit")
        # Bounds and unique canonical membership precede hashing/allocation.
        for identity, payload in entries:
            if hashlib.sha256(payload).hexdigest() != identity.digest:
                raise QualificationCaptureError("qualification.capture.record-mismatch")
        self._records = dict(entries)
        self.max_bytes = max_bytes

    def read_bytes(self, identity: ContentIdentity) -> bytes:
        if not isinstance(identity, ContentIdentity):
            raise QualificationCaptureError("qualification.capture.identity-invalid")
        try:
            return self._records[identity]
        except KeyError as exc:
            raise QualificationCaptureError(
                "qualification.capture.record-missing"
            ) from exc

    def read_json(self, identity: ContentIdentity) -> object:
        payload = self.read_bytes(identity)
        try:
            value = json.loads(payload)
            if canonical_json_bytes(value) != payload:
                raise QualificationCaptureError(
                    "qualification.capture.json-noncanonical"
                )
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise QualificationCaptureError(
                "qualification.capture.json-invalid"
            ) from exc
        return value


@dataclass(frozen=True, slots=True)
class QualificationRunCapture:
    run: QualificationLifecycleRunEvidence
    exports: RetainedLibraryExportSet
    acceptances: tuple[StandardComponentAcceptanceEvidence, ...]
    blobs: tuple[tuple[BlobRef, bytes], ...]


def capture_qualification_run(
    run: QualificationLifecycleRunEvidence,
    exports: RetainedLibraryExportSet,
    acceptances: tuple[StandardComponentAcceptanceEvidence, ...],
    *,
    read_blob: Callable[[BlobRef], bytes],
    max_bytes: int,
) -> QualificationRunCapture:
    """Copy immutable bytes only after checking all run/product memberships.

    The trusted lifecycle caller supplies its actual accepted records and blob reader.
    Limits are checked across unique references before the first read. No paths
    are opened, archives extracted, packages executed or authority published here.
    """

    if type(max_bytes) is not int or max_bytes <= 0:
        raise QualificationCaptureError("qualification.capture.limit-invalid")
    if (
        not isinstance(run, QualificationLifecycleRunEvidence)
        or not isinstance(exports, RetainedLibraryExportSet)
        or not isinstance(acceptances, tuple)
        or not acceptances
        or any(
            not isinstance(item, StandardComponentAcceptanceEvidence)
            for item in acceptances
        )
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")

    def ordered(values):
        return tuple(sorted(values, key=lambda item: item.uri))

    identities = tuple(item.identity for item in acceptances)
    components = tuple(item.component_revision for item in acceptances)
    if (
        identities != ordered(set(identities))
        or len(set(components)) != len(components)
        or identities != run.acceptance_evidence_identities
        or ordered(item.build.identity for item in acceptances)
        != run.build_evidence_identities
        or ordered(item.build.source_tree_identity for item in acceptances)
        != run.source_tree_identities
        or ordered(item.build.resolved_sbom.bom_identity for item in acceptances)
        != run.resolved_sbom_identities
        or ordered(item.generated_tests.identity for item in acceptances)
        != run.generated_test_evidence_identities
        or ordered(item.generated_test_suite_identity for item in acceptances)
        != run.generated_test_suite_identities
        or ordered(
            case.case_identity
            for item in acceptances
            for case in item.generated_tests.cases
        )
        != run.generated_test_case_identities
        or sum(len(item.generated_tests.cases) for item in acceptances)
        != run.generated_test_total
    ):
        raise QualificationCaptureError("qualification.capture.run-mismatch")

    built = {
        export.identity: export for item in acceptances for export in item.build.exports
    }
    graph_exports = {
        export.identity: export
        for manifest in exports.graph.manifests
        for export in manifest.exports
    }
    if built != graph_exports or any(
        item.target_identity != run.target_profile_identity
        for item in graph_exports.values()
    ):
        raise QualificationCaptureError("qualification.capture.export-mismatch")

    references = {item.blob for item in graph_exports.values()}
    if sum(item.size for item in references) > max_bytes:
        raise QualificationCaptureError("qualification.capture.byte-limit")
    retained = []
    for reference in sorted(
        references, key=lambda item: (item.identity, item.media_type, item.size)
    ):
        content = read_blob(reference)
        if (
            not isinstance(content, bytes)
            or len(content) != reference.size
            or hashlib.sha256(content).hexdigest() != reference.digest
        ):
            raise QualificationCaptureError("qualification.capture.blob-mismatch")
        retained.append((reference, content))
    return QualificationRunCapture(run, exports, acceptances, tuple(retained))


def capture_qualification_source_records(recorder, *, candidate, cas) -> None:
    """Retain the existing CAS source bundle before qualification cleanup."""

    import stat

    from literate_ai.contracts.executable_components.source_generation import (
        GeneratedSourceCandidate,
    )
    from literate_ai.storage import FileSystemCAS, StorageError

    if (
        not isinstance(recorder, QualificationEvidenceRecorder)
        or not isinstance(candidate, GeneratedSourceCandidate)
        or not isinstance(cas, FileSystemCAS)
    ):
        raise QualificationCaptureError("qualification.capture.source-records-invalid")

    def read(reference):
        if reference.size > recorder.max_bytes:
            raise QualificationCaptureError("qualification.capture.byte-limit")
        payload = cas.get_bytes(reference)
        if recorder.remember_bytes(payload).uri != reference.identity:
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        return payload

    def read_identity(identity):
        # Identity-only authority references lack sizes. CAS rechecks the
        # observed bound, regular file type and digest on its own descriptor.
        path = cas.path_for(BlobRef(identity.digest, 0))
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise QualificationCaptureError(
                "qualification.capture.source-records-invalid"
            )
        return read(BlobRef(identity.digest, metadata.st_size))

    def document(identity):
        payload = read_identity(identity)
        value = json.loads(payload)
        if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
            raise QualificationCaptureError(
                "qualification.capture.source-records-invalid"
            )
        return value

    try:
        manifest_bytes = read_identity(candidate.source_manifest_identity)
        _verify_qualification_source_records(
            candidate, manifest_bytes, read, recorder.max_bytes
        )
        manifest = json.loads(manifest_bytes)
        invocation = document(
            ContentIdentity.from_dict(manifest["invocation_identity"])
        )
        document(ContentIdentity.from_dict(invocation["execution_plan_identity"]))
        stage = document(
            ContentIdentity.parse_uri(
                BlobRef.from_dict(manifest["stage_output_record"]).identity
            )
        )
        document(ContentIdentity.from_dict(stage["stage_request_identity"]))
        document(ContentIdentity.from_dict(stage["route_decision_identity"]))
    except QualificationCaptureError:
        raise
    except (
        StorageError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.source-records-invalid"
        ) from exc


def verify_qualification_source_records(reader, *, candidate) -> None:
    """Reopen exact source files without a filesystem or generation provider."""

    from literate_ai.contracts.executable_components.source_generation import (
        GeneratedSourceCandidate,
    )

    if not isinstance(reader, QualificationEvidenceReader) or not isinstance(
        candidate, GeneratedSourceCandidate
    ):
        raise QualificationCaptureError("qualification.capture.source-records-invalid")

    def read(reference):
        if reference.size > reader.max_bytes:
            raise QualificationCaptureError("qualification.capture.byte-limit")
        payload = reader.read_bytes(ContentIdentity.parse_uri(reference.identity))
        if len(payload) != reference.size:
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        return payload

    _verify_qualification_source_records(
        candidate,
        reader.read_bytes(candidate.source_manifest_identity),
        read,
        reader.max_bytes,
    )


def verify_qualification_generation_records(reader, *, source_output) -> None:
    """Bind the retained final stage and invocation to accepted provenance."""

    from literate_ai.application.models import route_decision_dict
    from literate_ai.contracts import SourceGenerationRunOutput
    from literate_ai.models.routing import ModelRouteDecision

    if not isinstance(reader, QualificationEvidenceReader) or not isinstance(
        source_output, SourceGenerationRunOutput
    ):
        raise QualificationCaptureError("qualification.capture.generation-invalid")
    candidate = source_output.candidate
    provenance = source_output.provenance

    def same(identity, expected):
        if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
            expected
        ):
            raise QualificationCaptureError("qualification.capture.generation-mismatch")

    try:
        manifest = reader.read_json(candidate.source_manifest_identity)
        stage_id = ContentIdentity.parse_uri(
            BlobRef.from_dict(manifest["stage_output_record"]).identity
        )
        stage = reader.read_json(stage_id)
        route_id = ContentIdentity.from_dict(stage["route_decision_identity"])
        if provenance.model_stage_output_identities != (
            stage_id,
        ) or provenance.route_decision_identities != (route_id,):
            raise QualificationCaptureError("qualification.capture.generation-mismatch")
        invocation_id = ContentIdentity.from_dict(manifest["invocation_identity"])
        invocation = reader.read_json(invocation_id)
        plan_id = ContentIdentity.from_dict(invocation["execution_plan_identity"])
        request = invocation["stage_request"]
        same(
            invocation_id,
            {
                "execution_plan_identity": plan_id.to_dict(),
                "stage_request": request,
                "application_root_revision_identity": (
                    provenance.application_root_revision_identity.to_dict()
                ),
                "readiness_identity": provenance.readiness_identity.to_dict(),
            },
        )
        request_id = canonical_identity(request)
        same(request_id, request)
        plan = reader.read_json(plan_id)
        stages, routes = plan["model_stages"], plan["route_decisions"]
        if (
            not isinstance(stages, list)
            or not stages
            or not isinstance(routes, list)
            or len(stages) != len(routes)
            or stages[-1]["produces_tree"] is not True
            or any(item["produces_tree"] is not False for item in stages[:-1])
            or not isinstance(request, dict)
            or not isinstance(request.get("prior_stage_outputs"), dict)
            or request["stage_id"] != stages[-1]["stage_id"]
        ):
            raise QualificationCaptureError("qualification.capture.generation-mismatch")
        ContentIdentity.from_dict(request["input_identity"])
        route = ModelRouteDecision.from_dict(reader.read_json(route_id))
        same(route_id, route.to_dict())
        if canonical_json_bytes(routes[-1]) != canonical_json_bytes(
            route_decision_dict(route)
        ):
            raise QualificationCaptureError("qualification.capture.generation-mismatch")
        provider = stage.get("provider_evidence_identity")
        provider_ids = (
            () if provider is None else (ContentIdentity.parse_uri(provider),)
        )
        if provider_ids != provenance.provider_evidence_identities:
            raise QualificationCaptureError("qualification.capture.generation-mismatch")
        if (
            not isinstance(stage["coding_cli"], str)
            or not stage["coding_cli"]
            or (stage["model"] is not None and not isinstance(stage["model"], str))
        ):
            raise QualificationCaptureError("qualification.capture.generation-invalid")
        tool = stage["coding_cli_tool_binding_identity"]
        if tool is not None:
            ContentIdentity.parse_uri(tool)
        same(
            stage_id,
            {
                "schema": "literate-ai/coding-cli-stage-output-record@1",
                "execution_plan_identity": plan_id.to_dict(),
                "stage_request_identity": request_id.to_dict(),
                "planned_request_identity": (
                    candidate.planned_coding_cli_request_identity.to_dict()
                ),
                "stage_id": stages[-1]["stage_id"],
                "route_decision_identity": route_id.to_dict(),
                "tree_identity": candidate.tree_identity.to_dict(),
                "tree_record": manifest["tree_record"],
                "coding_cli": stage["coding_cli"],
                "model": stage["model"],
                "coding_cli_tool_binding_identity": tool,
                **(
                    {"provider_evidence_identity": provider}
                    if provider is not None
                    else {}
                ),
            },
        )
    except QualificationCaptureError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        RecursionError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.generation-invalid"
        ) from exc


def _verify_qualification_source_records(candidate, manifest_bytes, read, max_bytes):
    from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH
    from literate_ai.contracts.executable_components.packages import (
        SourceBundleClosure,
        SourceBundleFile,
    )
    from literate_ai.contracts.executable_components.source_generation import (
        GeneratedSourceCandidate,
    )
    from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
    from literate_ai.storage import StorageError

    if not isinstance(candidate, GeneratedSourceCandidate):
        raise QualificationCaptureError("qualification.capture.source-records-invalid")
    try:
        manifest = json.loads(manifest_bytes)
        candidate_fields = (
            "component_revision",
            "source_generation_request_identity",
            "planned_coding_cli_request_identity",
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "prompt_identity",
            "recipe_identity",
            "workspace_allocation_identity",
            "tree_identity",
        )
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != set(candidate_fields)
            | {
                "schema",
                "invocation_identity",
                "tree_record",
                "stage_output_record",
                "generated_test_suite_identity",
                "source_bom",
            }
            or manifest["schema"] != "literate-ai/generated-source-manifest-record@1"
            or canonical_json_bytes(manifest) != manifest_bytes
            or any(
                manifest[name] != getattr(candidate, name).to_dict()
                for name in candidate_fields
            )
            or manifest["generated_test_suite_identity"]
            != candidate.generated_test_suite_identity.uri
            or ContentIdentity.from_dict(manifest["source_bom"]["bom_identity"])
            != candidate.source_bom_identity
        ):
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        tree_ref = BlobRef.from_dict(manifest["tree_record"])
        if tree_ref.identity != candidate.source_bundle_identity.uri:
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        tree = json.loads(read(tree_ref))
        closure = SourceBundleClosure(
            tree_ref,
            ContentIdentity.from_dict(tree["tree_identity"]),
            tuple(SourceBundleFile.from_dict(item) for item in tree["files"]),
        )
        semantic_tree = canonical_identity(
            [
                {
                    "path": item.path,
                    "size": item.blob.size,
                    "digest": item.blob.identity,
                }
                for item in closure.files
            ]
        )
        if (
            semantic_tree != closure.tree_identity
            or semantic_tree != candidate.tree_identity
        ):
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        if (
            sum({item.blob.identity: item.blob.size for item in closure.files}.values())
            > max_bytes
        ):
            raise QualificationCaptureError("qualification.capture.byte-limit")
        for item in closure.files:
            read(item.blob)
        file_ids = {item.path: item.blob.identity for item in closure.files}
        if (
            file_ids.get(CYCLONEDX_SOURCE_SBOM_PATH)
            != candidate.source_bom_identity.uri
            or file_ids.get(GENERATED_TEST_SUITE_PATH)
            != candidate.generated_test_suite_identity.uri
        ):
            raise QualificationCaptureError(
                "qualification.capture.source-records-mismatch"
            )
        read(BlobRef.from_dict(manifest["stage_output_record"]))
    except QualificationCaptureError:
        raise
    except (
        StorageError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.source-records-invalid"
        ) from exc


def capture_lifecycle_records(
    lifecycle, recorder: QualificationEvidenceRecorder
) -> None:
    """Retain the exact assembled lifecycle/package records, without admission."""

    from literate_ai.application.standard_project_lifecycle import (
        StandardProjectLifecycleResult,
    )

    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise QualificationCaptureError("qualification.capture.lifecycle-invalid")
    if not lifecycle.successful or lifecycle.root_integration is None:
        raise QualificationCaptureError("qualification.capture.lifecycle-incomplete")
    documents = [(lifecycle.identity, lifecycle.identity_document())]
    for node in lifecycle.node_results:
        documents.append((node.identity, node.identity_document()))
    if lifecycle.project_build_plan is None:
        raise QualificationCaptureError("qualification.capture.lifecycle-incomplete")
    documents.append(
        (
            lifecycle.project_build_plan.identity,
            lifecycle.project_build_plan.identity_document(),
        )
    )
    integration = lifecycle.root_integration
    for record in (
        lifecycle.generation_schedule,
        *lifecycle.project_build_plan.components,
        *(node.source_cache_membership for node in lifecycle.node_results),
        *lifecycle.context_benchmark_records,
        lifecycle.context_cache_report,
        *lifecycle.candidate_attempt_chains,
        lifecycle.lifecycle_membership,
        lifecycle.aggregate_receipt,
        integration,
        integration.artifact_graph,
        integration.link_plan,
        integration.package_plan,
        integration.package_result,
    ):
        if record is None:
            raise QualificationCaptureError(
                "qualification.capture.lifecycle-incomplete"
            )
        documents.append((record.identity, record.to_dict()))
    # Preflight all semantic identities before retaining any of these documents.
    if any(
        canonical_identity(document) != expected for expected, document in documents
    ):
        raise QualificationCaptureError("qualification.capture.record-mismatch")
    for _, document in documents:
        recorder.remember_json(document)


def reopen_qualification_lifecycle(
    reader: QualificationEvidenceReader,
    lifecycle_identity: ContentIdentity,
):
    """Reconstruct retained lifecycle records through their existing validators.

    Identity documents reference immutable records instead of embedding every
    object. Reopen those exact records and reapply the same lifecycle invariants
    used by the producer. This establishes no current authority or admission.
    """

    from literate_ai.application.standard_project_lifecycle import (
        StandardComponentBuildPlan,
        StandardNodeLifecycleResult,
        StandardProjectBuildPlan,
        StandardProjectLifecycleError,
        StandardProjectLifecycleResult,
        StandardSourceCacheMembership,
    )
    from literate_ai.contracts.executable_components import (
        ArtifactExport,
        CandidateAttemptChain,
        ComponentContextBenchmarkRecord,
        ForwardGenerationContextCacheReport,
        SourceGenerationNodeResult,
        SourceGenerationRunOutput,
        SourceGenerationScheduleResult,
    )
    from literate_ai.contracts.standard_lifecycle_membership import (
        StandardNodeFailureEvidence,
        StandardProjectLifecycleMembership,
    )
    from literate_ai.contracts.standard_post_source_evidence import (
        StandardBuildEvidence,
        StandardExecutionEvidence,
        StandardGeneratedTestExecutionEvidence,
    )

    if not isinstance(reader, QualificationEvidenceReader):
        raise QualificationCaptureError("qualification.capture.reader-invalid")
    if not isinstance(lifecycle_identity, ContentIdentity):
        raise QualificationCaptureError("qualification.capture.identity-invalid")
    decoded = {}

    def identity(value):
        return None if value is None else ContentIdentity.parse_uri(value)

    def read(reference, decode):
        if reference is None:
            return None
        pinned = ContentIdentity.parse_uri(reference)
        key = (pinned, decode)
        if key in decoded:
            return decoded[key]
        record = decode(reader.read_json(pinned))
        if record.identity != pinned:
            raise QualificationCaptureError("qualification.capture.record-mismatch")
        decoded[key] = record
        return record

    def optional(value, decode):
        return None if value is None else decode(value)

    def node(data):
        if data["schema"] != "literate-ai/standard-node-lifecycle-result@4":
            raise QualificationCaptureError("qualification.capture.lifecycle-invalid")
        arguments = {
            name: identity(data[name])
            for name in (
                "component_revision",
                "build_plan_identity",
                "index_identity",
                "authorization_identity",
                "build_identity",
                "test_identity",
                "execution_identity",
                "acceptance_identity",
                "source_cache_publication_identity",
                "source_admission_identity",
            )
        }
        arguments.update(
            source_generation=SourceGenerationNodeResult.from_dict(
                data["source_generation"]
            ),
            source_output=optional(
                data["source_output"], SourceGenerationRunOutput.from_dict
            ),
            exports=tuple(ArtifactExport.from_dict(item) for item in data["exports"]),
            source_cache_membership=read(
                data["source_cache_membership_identity"],
                StandardSourceCacheMembership.from_dict,
            ),
            failure_code=data["failure_code"],
            failure_evidence=optional(
                data["failure_evidence"], StandardNodeFailureEvidence.from_dict
            ),
            build_evidence=optional(
                data["build_evidence"], StandardBuildEvidence.from_dict
            ),
            generated_test_evidence=optional(
                data["generated_test_evidence"],
                StandardGeneratedTestExecutionEvidence.from_dict,
            ),
            execution_evidence=optional(
                data["execution_evidence"], StandardExecutionEvidence.from_dict
            ),
            acceptance_evidence=optional(
                data["acceptance_evidence"],
                StandardComponentAcceptanceEvidence.from_dict,
            ),
        )
        return StandardNodeLifecycleResult(**arguments)

    def build_plan(data):
        if data["schema"] != "literate-ai/standard-project-build-plan@1":
            raise QualificationCaptureError("qualification.capture.lifecycle-invalid")
        return StandardProjectBuildPlan(
            identity(data["execution_plan_identity"]),
            tuple(
                read(item, StandardComponentBuildPlan.from_dict)
                for item in data["components"]
            ),
        )

    def lifecycle(data):
        if data["schema"] != "literate-ai/standard-project-lifecycle-result@4":
            raise QualificationCaptureError("qualification.capture.lifecycle-invalid")
        return StandardProjectLifecycleResult(
            execution_plan_identity=identity(data["execution_plan_identity"]),
            validation_identity=identity(data["validation_identity"]),
            project_build_plan_identity=identity(data["project_build_plan_identity"]),
            generation_schedule=read(
                data["generation_schedule_identity"],
                SourceGenerationScheduleResult.from_dict,
            ),
            node_results=tuple(read(item, node) for item in data["node_results"]),
            lifecycle_membership=read(
                data["lifecycle_membership_identity"],
                StandardProjectLifecycleMembership.from_dict,
            ),
            admission_identity=identity(data["admission_identity"]),
            aggregate_receipt=read(
                data["aggregate_receipt_identity"], StandardAggregateReceipt.from_dict
            ),
            receipt_identity=identity(data["receipt_identity"]),
            project_build_plan=read(data["project_build_plan_identity"], build_plan),
            root_integration_evidence_identity=identity(
                data["root_integration_evidence_identity"]
            ),
            root_integration=read(
                data["root_integration_evidence_identity"],
                StandardRootIntegrationEvidence.from_dict,
            ),
            context_prompt_journal_identities=tuple(
                identity(item) for item in data["context_prompt_journal_identities"]
            ),
            context_benchmark_records=tuple(
                read(item, ComponentContextBenchmarkRecord.from_dict)
                for item in data["context_benchmark_record_identities"]
            ),
            context_cache_report=read(
                data["context_cache_report_identity"],
                ForwardGenerationContextCacheReport.from_dict,
            ),
            candidate_attempt_chains=tuple(
                read(item, CandidateAttemptChain.from_dict)
                for item in data["candidate_attempt_chain_identities"]
            ),
        )

    try:
        return read(lifecycle_identity.uri, lifecycle)
    except QualificationCaptureError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        StandardProjectLifecycleError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.lifecycle-invalid"
        ) from exc


def reopen_qualification_products(
    reader: QualificationEvidenceReader,
    *,
    qualification_identity: ContentIdentity,
    run_identity: ContentIdentity,
    exports_identity: ContentIdentity,
    max_package_bytes: int,
    component_lock: ComponentLock,
    oracle: LibraryAcceptance,
    current_recipes: Mapping[ContentIdentity, object],
    current_commands: Mapping[ContentIdentity, object],
    current_profile: object,
    current_driver: object,
) -> QualificationRunCapture:
    """Reopen exact run/product membership from a pinned qualification result.

    This checks complete typed run membership, root/receipt binding and immutable
    package integrity and independent library acceptance against caller-reopened
    lock/oracle authority. Remaining stage observations and importer trust are
    required checks; the returned capture grants no consumption authority.
    """

    if not isinstance(reader, QualificationEvidenceReader):
        raise QualificationCaptureError("qualification.capture.reader-invalid")
    if not isinstance(component_lock, ComponentLock) or not isinstance(
        current_recipes, Mapping
    ):
        raise QualificationCaptureError("qualification.capture.suite-authority-invalid")
    from literate_ai.contracts.executable_components import ComponentCommandContract

    if not isinstance(current_commands, Mapping):
        raise QualificationCaptureError(
            "qualification.capture.command-authority-invalid"
        )
    commands = dict(current_commands)
    if set(commands) != {
        node.revision.identity for node in component_lock.nodes
    } or any(
        not isinstance(contract, ComponentCommandContract)
        or contract.component_revision != revision
        for revision, contract in commands.items()
    ):
        raise QualificationCaptureError(
            "qualification.capture.command-authority-invalid"
        )
    recipes = dict(current_recipes)
    if set(recipes) != {node.revision.identity for node in component_lock.nodes}:
        raise QualificationCaptureError("qualification.capture.suite-authority-invalid")
    result = QualificationLifecycleResult.from_dict(
        reader.read_json(qualification_identity)
    )
    selected = tuple(run for run in result.runs if run.run_identity == run_identity)
    if len(selected) != 1:
        raise QualificationCaptureError("qualification.capture.run-missing")
    run = selected[0]
    exports = RetainedLibraryExportSet.from_dict(reader.read_json(exports_identity))
    root = reopen_qualification_root(reader, run)
    if root.artifact_graph != exports.graph or root.link_plan != exports.link_plan:
        raise QualificationCaptureError("qualification.capture.root-export-mismatch")
    root_product = next(
        (
            product
            for product in exports.libraries
            if product.artifact_export.identity
            == root.package_plan.root_artifact_identity
        ),
        None,
    )
    if root_product is None:
        raise QualificationCaptureError("qualification.capture.root-export-mismatch")
    for member in result.runs:
        execution = reopen_qualification_run(reader, member, current_commands=commands)
        if {
            node.component_revision for node in execution.lifecycle.node_results
        } != set(recipes):
            raise QualificationCaptureError(
                "qualification.capture.suite-authority-invalid"
            )
        plans = {
            plan.identity: plan
            for plan in execution.lifecycle.project_build_plan.components
        }
        for node in execution.lifecycle.node_results:
            verify_qualification_command_authority(
                reader,
                contract=commands[node.component_revision],
                authorization_identity=node.authorization_identity,
                plan=plans[node.build_plan_identity],
            )
            verify_qualification_source_records(
                reader, candidate=node.source_output.candidate
            )
            verify_qualification_generation_records(
                reader, source_output=node.source_output
            )
            verify_qualification_boms(
                reader, component_lock=component_lock, build=node.build_evidence
            )
            verify_qualification_generated_suite(
                reader,
                recipe=recipes[node.component_revision],
                component_lock_identity=component_lock.identity,
                tests=node.generated_test_evidence,
            )
            recipe_identity = ContentIdentity.parse_uri(
                recipes[node.component_revision].identity
            )
            if (
                node.source_output is None
                or node.source_output.candidate.recipe_identity != recipe_identity
                or node.source_output.provenance.recipe_identity != recipe_identity
            ):
                raise QualificationCaptureError(
                    "qualification.capture.suite-source-mismatch"
                )
        member_root = execution.lifecycle.root_integration
        root_node = next(
            node
            for node in execution.lifecycle.node_results
            if node.component_revision
            == member_root.package_plan.root_component_revision
        )
        verify_qualification_packaged_library_processes(
            reader, root=member_root, tests=root_node.generated_test_evidence
        )
        member_export = next(
            export
            for manifest in member_root.artifact_graph.manifests
            for export in manifest.exports
            if export.identity == member_root.package_plan.root_artifact_identity
        )
        verify_qualification_library_acceptance(
            reader,
            root=member_root,
            product=LibraryArtifactProduct(
                member_export, root_product.import_surface, root_product.native_layout
            ),
            component_lock=component_lock,
            oracle=oracle,
        )
    from literate_ai.contracts.projects import StandardProjectLifecycleDriver

    if not isinstance(current_driver, StandardProjectLifecycleDriver):
        raise QualificationCaptureError(
            "qualification.capture.standard-authority-invalid"
        )
    if any(
        member.driver_identity != current_driver.identity
        or member.lifecycle_policy_identity != current_driver.policy_identity
        or member.framework_distribution_identity
        != current_driver.framework_distribution_identity
        for member in result.runs
    ):
        raise QualificationCaptureError(
            "qualification.capture.standard-authority-mismatch"
        )
    verify_qualification_parity(reader, result=result, profile=current_profile)
    acceptances = tuple(
        StandardComponentAcceptanceEvidence.from_dict(reader.read_json(identity))
        for identity in run.acceptance_evidence_identities
    )
    return capture_qualification_run(
        run,
        exports,
        acceptances,
        read_blob=lambda reference: reader.read_bytes(
            ContentIdentity.parse_uri(reference.identity)
        ),
        max_bytes=max_package_bytes,
    )


def verify_qualification_parity(
    reader: QualificationEvidenceReader,
    *,
    result: QualificationLifecycleResult,
    profile: object,
) -> None:
    """Recompute every retained parity result against current profile authority."""

    from literate_ai.adapters.qualification_authority import (
        qualification_verifier_case_map,
        qualification_verifier_record,
    )
    from literate_ai.source_to_specification.contracts import canonical_digest
    from literate_ai.source_to_specification.host_qualification import (
        LocalQualificationProfile,
    )
    from literate_ai.source_to_specification.inventory import source_inventory_from_dict
    from literate_ai.source_to_specification.qualification_lifecycle import (
        parse_qualification_json_result,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(result, QualificationLifecycleResult)
        or not isinstance(profile, LocalQualificationProfile)
        or len(result.runs) < profile.minimum_clean_runs
    ):
        raise QualificationCaptureError(
            "qualification.capture.parity-authority-invalid"
        )

    def same(identity, document):
        if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
            document
        ):
            raise QualificationCaptureError(
                "qualification.capture.parity-record-mismatch"
            )

    try:
        source_identity = result.runs[0].source_snapshot_identity
        inventory = source_inventory_from_dict(reader.read_json(source_identity))
        if inventory.identity != source_identity.uri:
            raise QualificationCaptureError(
                "qualification.capture.baseline-inventory-mismatch"
            )
        provider = qualification_verifier_record(profile, source_identity)
        provider_id = canonical_identity(provider)
        case_map = qualification_verifier_case_map(profile, source_identity)
        if result.case_map != case_map:
            raise QualificationCaptureError(
                "qualification.capture.parity-authority-mismatch"
            )
        same(ContentIdentity.parse_uri(profile.identity), profile.to_dict())
        same(provider_id, provider)
        same(case_map.identity, case_map.to_dict())
        cases = {case.case_id: case for case in profile.cases}
        for case in profile.cases:
            same(canonical_identity(case.to_dict()), case.to_dict())
        for parity in result.parity_evidence:
            same(parity.identity, parity.to_dict())
            for evidence in parity.cases:
                same(evidence.identity, evidence.to_dict())
                case = cases[evidence.case_id]
                argument = json.dumps(
                    case.arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                observations = []
                for identity, command in (
                    (evidence.baseline_observation_identity, profile.source_command),
                    (
                        evidence.generated_observation_identity,
                        profile.generated_command,
                    ),
                ):
                    document = reader.read_json(identity)
                    stdout = ContentIdentity.parse_uri(document["stdout"])
                    stderr = ContentIdentity.parse_uri(document["stderr"])
                    same(
                        identity,
                        {
                            "schema": "literate-ai/qualification-process-observation@1",
                            "command": [*command, argument],
                            "exit_status": 0,
                            "stdout": stdout.uri,
                            "stderr": stderr.uri,
                            "json_output_valid": True,
                        },
                    )
                    output = reader.read_bytes(stdout)
                    diagnostic = reader.read_bytes(stderr)
                    if max(len(output), len(diagnostic)) > profile.maximum_output_bytes:
                        raise QualificationCaptureError(
                            "qualification.capture.parity-output-limit"
                        )
                    observations.append(parse_qualification_json_result(output))
                if canonical_digest(observations[0]) != canonical_digest(
                    observations[1]
                ):
                    raise QualificationCaptureError(
                        "qualification.capture.parity-result-mismatch"
                    )
    except QualificationCaptureError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.parity-record-invalid"
        ) from exc


def verify_qualification_packaged_library_processes(
    reader: QualificationEvidenceReader,
    *,
    root: StandardRootIntegrationEvidence,
    tests: StandardGeneratedTestExecutionEvidence,
) -> None:
    """Require packaged library tests and smoke execution from every clean run."""

    from literate_ai.adapters.native_sdk_qualification import (
        packaged_sdk_process_fields,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(root, StandardRootIntegrationEvidence)
        or not isinstance(tests, StandardGeneratedTestExecutionEvidence)
        or root.package_plan.entrypoints
        or tests.component_revision != root.package_plan.root_component_revision
    ):
        raise QualificationCaptureError("qualification.capture.root-process-invalid")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate packaged test result field")
            result[key] = value
        return result

    try:
        for identity, phase in (
            (
                root.root_generated_integration_test_identity,
                "packaged-root-generated-test",
            ),
            (root.packaged_execution_identity, "packaged-project-execution"),
        ):
            document = reader.read_json(identity)
            stdout_id = ContentIdentity.parse_uri(document["stdout_identity"])
            stderr_id = ContentIdentity.parse_uri(document["stderr_identity"])
            expected = {
                "schema": "literate-ai/local-process-observation@1",
                "phase": phase,
                "plan_identity": root.package_plan.identity.uri,
                "returncode": 0,
                "stdout_identity": stdout_id.uri,
                "stderr_identity": stderr_id.uri,
                **packaged_sdk_process_fields(
                    reader,
                    package_plan=root.package_plan,
                    package_result=root.package_result,
                    project_build_plan_identity=root.project_build_plan_identity,
                    process=document,
                    phase="execute"
                    if phase == "packaged-project-execution"
                    else "test",
                ),
            }
            if canonical_json_bytes(document) != canonical_json_bytes(expected):
                raise QualificationCaptureError(
                    "qualification.capture.root-process-mismatch"
                )
            output = reader.read_json(stdout_id)
            if not isinstance(output, str) or not isinstance(
                reader.read_json(stderr_id), str
            ):
                raise QualificationCaptureError(
                    "qualification.capture.root-output-invalid"
                )
            if phase == "packaged-project-execution":
                if not output.strip():
                    raise QualificationCaptureError(
                        "qualification.capture.root-output-invalid"
                    )
                continue
            observed = json.loads(output, object_pairs_hook=unique)
            if (
                not isinstance(observed, dict)
                or set(observed) != {"schema", "cases"}
                or observed["schema"] != "literate-ai/generated-test-results@1"
                or not isinstance(observed["cases"], list)
                or not tests.cases
            ):
                raise QualificationCaptureError(
                    "qualification.capture.root-test-mismatch"
                )
            case_ids = []
            for case in observed["cases"]:
                if (
                    not isinstance(case, dict)
                    or set(case) != {"case_id", "outcome"}
                    or not isinstance(case["case_id"], str)
                    or case["outcome"] != "passed"
                ):
                    raise QualificationCaptureError(
                        "qualification.capture.root-test-mismatch"
                    )
                case_ids.append(case["case_id"])
            if sorted(case_ids) != sorted(case.case_id for case in tests.cases):
                raise QualificationCaptureError(
                    "qualification.capture.root-test-mismatch"
                )
    except QualificationCaptureError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.root-process-invalid"
        ) from exc


def verify_qualification_command_authority(
    reader: QualificationEvidenceReader,
    *,
    contract: object,
    authorization_identity: ContentIdentity,
    plan: object,
) -> None:
    """Reopen caller-projected current command authority before product reads."""

    from literate_ai.application.standard_project_lifecycle import (
        StandardComponentBuildPlan,
    )
    from literate_ai.contracts.executable_components import ComponentCommandContract
    from literate_ai.contracts.native_sdks import native_sdk_consumer_build_identity
    from literate_ai.security.policy import BuildAuthorization, BuildRequest

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(contract, ComponentCommandContract)
        or not isinstance(plan, StandardComponentBuildPlan)
    ):
        raise QualificationCaptureError(
            "qualification.capture.command-authority-invalid"
        )
    try:
        grant = BuildAuthorization.from_dict(reader.read_json(authorization_identity))
        request = BuildRequest.from_dict(
            reader.read_json(ContentIdentity.parse_uri(grant.request_digest))
        )
        if (
            contract.component_revision != plan.component_revision
            or request.builder_id
            != native_sdk_consumer_build_identity(
                contract.locked_build_authority_identity,
                plan.materialization.native_sdk_input_identities,
            ).uri
            or request.sandbox_profile != "local-explicit-host-process"
            or contract.build_system_resolver_identity
            != plan.request.build_system_resolver_identity
            or contract.build_system_toolchain_identity
            != plan.request.build_system_toolchain_identity
            or contract.language_compiler_identity
            != plan.request.language_compiler_identity
            or contract.language_runtime_identity
            != plan.request.language_runtime_identity
        ):
            raise QualificationCaptureError(
                "qualification.capture.command-authority-mismatch"
            )
        shapes = {shape.export_id: shape for shape in contract.artifact_export_shapes()}
        declarations = {
            item.export_id: item for item in plan.manifest.export_declarations
        }
        if set(shapes) != set(declarations) or any(
            getattr(shape, field) != getattr(declarations[export_id], field)
            for export_id, shape in shapes.items()
            for field in (
                "role",
                "abi_identity",
                "target_identity",
                "media_type",
                "producer_identity",
            )
        ):
            raise QualificationCaptureError(
                "qualification.capture.command-authority-mismatch"
            )
        from literate_ai.contracts.executable_components import BuildSubActionKind

        kinds = tuple(action.kind for action in plan.request.sub_actions)
        if kinds not in (
            (BuildSubActionKind.COMPILE,),
            (BuildSubActionKind.RESOLVE_DEPENDENCIES, BuildSubActionKind.COMPILE),
        ) or any(
            action.action.action_id
            != ("build-" if action.kind == BuildSubActionKind.COMPILE else "resolve-")
            + contract.identity.digest[:24]
            for action in plan.request.sub_actions
        ):
            raise QualificationCaptureError(
                "qualification.capture.command-authority-mismatch"
            )
        records = [contract, *contract.commands]
        if not contract.is_library:
            for entrypoint in contract.entrypoint_command_contracts():
                records.extend((entrypoint, *entrypoint.commands))
        for record in records:
            if canonical_json_bytes(
                reader.read_json(record.identity)
            ) != canonical_json_bytes(record.to_dict()):
                raise QualificationCaptureError(
                    "qualification.capture.command-authority-mismatch"
                )
        # Matching pinned commands is not proof of the launched argv, measured
        # executable bytes or historical execution-time authorization.
    except QualificationCaptureError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.command-authority-invalid"
        ) from exc


def verify_qualification_build_authorization(
    reader: QualificationEvidenceReader,
    *,
    candidate: object,
    index_identity: ContentIdentity,
    authorization_identity: ContentIdentity,
    plan: object,
) -> None:
    """Bind historical build authority records; grant no execution permission."""

    from literate_ai.application.standard_project_lifecycle import (
        StandardBuildAuthorization,
        StandardComponentBuildIntent,
    )
    from literate_ai.security.policy import BuildAuthorization, BuildRequest

    try:
        index = reader.read_json(index_identity)
        # The local index counts auxiliary files too; the generated-source
        # closure excludes .codegraph. Its count is not a source bundle size.
        disabled_index = {
            "schema": "literate-ai/disabled-source-index@1",
            "component_revision": plan.component_revision.uri,
            "source": candidate.tree_identity.uri,
        }
        local_index = (
            isinstance(index, dict)
            and set(index) == {"indexer", "component", "tree", "path_count"}
            and index["indexer"] == "local-tree@1"
            and index["component"] == plan.component_revision.uri
            and index["tree"] == candidate.tree_identity.uri
            and type(index["path_count"]) is int
            and index["path_count"] >= 0
        )
        if index != disabled_index and not local_index:
            raise QualificationCaptureError("qualification.capture.index-mismatch")
        grant_document = reader.read_json(authorization_identity)
        grant = BuildAuthorization.from_dict(grant_document)
        request_identity = ContentIdentity.parse_uri(grant.request_digest)
        request_document = reader.read_json(request_identity)
        request = BuildRequest.from_dict(request_document)
        from literate_ai.adapters.qualification_mix import is_mix_request

        mix_request = is_mix_request(plan, request)
        expected_toolchain = (
            plan.request.build_system_toolchain_identity
            if mix_request
            else plan.request.language_compiler_identity
        )
        intent = StandardComponentBuildIntent(
            plan.component_revision,
            candidate.tree_identity,
            candidate.source_bundle_identity,
            request,
            plan.provider_artifact_identities,
            plan.package_artifact_identities,
            plan.materialization.native_sdk_input_identities,
        )
        authorization = StandardBuildAuthorization(
            intent.identity, request_identity, index_identity, grant
        )
        if (
            grant.revoked
            or grant.classification_digest != index_identity.uri
            or grant.effective_revision_digest != plan.component_revision.uri
            or set(grant.privileges) != set(request.requested_privileges)
            or plan.request.authorization_identity != authorization_identity
            or request.toolchain_digest != expected_toolchain.uri
            or (
                mix_request
                and plan.manifest.build_system_driver_identity != expected_toolchain
            )
            or set(request.requested_privileges)
            != {item.value for item in plan.request.requested_privileges}
            or set(request.allowed_outputs)
            != {item.export_id for item in plan.manifest.export_declarations}
        ):
            raise QualificationCaptureError(
                "qualification.capture.build-authorization-mismatch"
            )
        for actual, expected in (
            (grant_document, grant.to_dict()),
            (request_document, request.to_dict()),
            (reader.read_json(intent.identity), intent.to_dict()),
            (reader.read_json(authorization.identity), authorization.to_dict()),
        ):
            if canonical_json_bytes(actual) != canonical_json_bytes(expected):
                raise QualificationCaptureError(
                    "qualification.capture.build-authorization-mismatch"
                )
        # Expiry is evaluated at execution, not against the importer's clock.
        # Historical timing, current policy and importer trust remain required.
    except QualificationCaptureError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.build-authorization-invalid"
        ) from exc


def verify_qualification_build(
    reader: QualificationEvidenceReader,
    *,
    plan: object,
    build: StandardBuildEvidence,
    provider_exports: Mapping | None = None,
    current_commands: Mapping | None = None,
) -> None:
    """Bind build processes and the artifact tree's metadata-only transition."""

    from literate_ai.adapters.lifecycle.standard_npm import (
        StandardNpmDependencyEvidence,
        StandardNpmLifecycleError,
        StandardNpmTarget,
        normalized_npm_inventory,
        parse_npm_source_authority,
    )
    from literate_ai.adapters.native_sdk_qualification import sdk_process_fields
    from literate_ai.application.standard_project_lifecycle import (
        StandardComponentBuildPlan,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(plan, StandardComponentBuildPlan)
        or not isinstance(build, StandardBuildEvidence)
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")

    def same(identity, expected):
        if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
            expected
        ):
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )

    def process(identity, phase):
        value = reader.read_json(identity)
        stdout = ContentIdentity.parse_uri(value["stdout_identity"])
        stderr = ContentIdentity.parse_uri(value["stderr_identity"])
        same(
            identity,
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": phase,
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": stdout.uri,
                "stderr_identity": stderr.uri,
                **sdk_process_fields(reader, plan=plan, process=value, phase=phase),
            },
        )
        if not isinstance(reader.read_json(stdout), str) or not isinstance(
            reader.read_json(stderr), str
        ):
            raise QualificationCaptureError(
                "qualification.capture.build-output-invalid"
            )

    try:
        if (
            build.build_plan_identity != plan.identity
            or build.component_revision != plan.component_revision
            or build.source_tree_identity != plan.request.source_tree_identity
        ):
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )
        value = reader.read_json(build.build_observation_identity)
        process_id = ContentIdentity.parse_uri(value["process_observation_identity"])
        tree = ContentIdentity.parse_uri(value["artifact_tree_identity"])
        native = reader.read_json(process_id)
        observed_plan = plan.identity
        if native.get("schema") == "literate-ai/mix-build-evidence@1":
            # Cache reuse retains the issued build's observation. The Mix
            # decoder reopens that origin and binds it to the current plan.
            observed_plan = ContentIdentity.parse_uri(
                native["standard_authority"]["build_plan_identity"]
            )
        same(
            build.build_observation_identity,
            {
                "schema": "literate-ai/local-build-observation@1",
                "build_plan_identity": observed_plan.uri,
                "process_observation_identity": process_id.uri,
                "artifact_tree_identity": tree.uri,
                "resolved_sbom_identity": build.resolved_sbom.bom_identity.uri,
            },
        )

        def artifact_tree():
            def tree_files(identity):
                document = reader.read_json(identity)
                files = document["files"]
                same(
                    identity,
                    {"schema": "literate-ai/local-source-tree@1", "files": files},
                )
                if not isinstance(files, list) or not files:
                    raise QualificationCaptureError(
                        "qualification.capture.artifact-tree-invalid"
                    )
                paths = set()
                for entry in files:
                    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                        raise QualificationCaptureError(
                            "qualification.capture.artifact-tree-invalid"
                        )
                    path = entry["path"]
                    if (
                        not isinstance(path, str)
                        or "\\" in path
                        or any(part in {"", ".", ".."} for part in path.split("/"))
                        or path in paths
                    ):
                        raise QualificationCaptureError(
                            "qualification.capture.artifact-tree-invalid"
                        )
                    ContentIdentity.parse_uri("sha256:" + entry["sha256"])
                    paths.add(path)
                for path in paths:
                    parts = path.split("/")
                    if any(
                        "/".join(parts[:index]) in paths
                        for index in range(1, len(parts))
                    ):
                        raise QualificationCaptureError(
                            "qualification.capture.artifact-tree-invalid"
                        )
                return files

            before = tree_files(tree)
            custody = reader.read_json(build.artifact_custody_identity)
            final_tree = ContentIdentity.parse_uri(custody["artifact_tree_identity"])
            same(
                build.artifact_custody_identity,
                {
                    "schema": "literate-ai/local-artifact-custody@1",
                    "build_plan_identity": plan.identity.uri,
                    "artifact_tree_identity": final_tree.uri,
                    "export_identities": [item.identity.uri for item in build.exports],
                },
            )
            after = tree_files(final_tree)
            manifest_entries = [
                item for item in after if item["path"] == "artifact-manifest.json"
            ]
            if (
                len(manifest_entries) != 1
                or before
                != [item for item in after if item["path"] != "artifact-manifest.json"]
                or {item["path"]: item["sha256"] for item in before}.get(
                    ".literate/resolved-sbom.cdx.json"
                )
                != build.resolved_sbom.bom_identity.digest
            ):
                raise QualificationCaptureError(
                    "qualification.capture.artifact-tree-mismatch"
                )
            # This file is raw JSON bytes, unlike the canonical record documents.
            manifest_bytes = reader.read_bytes(
                ContentIdentity.parse_uri("sha256:" + manifest_entries[0]["sha256"])
            )
            manifest = json.loads(manifest_bytes)
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"tree", "provider_materials", "build_observation"}
                or manifest["tree"] != tree.uri
                or manifest["build_observation"] != build.build_observation_identity.uri
                or not isinstance(manifest["provider_materials"], list)
                or json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
                != manifest_bytes
            ):
                raise QualificationCaptureError(
                    "qualification.capture.artifact-tree-mismatch"
                )

            return before

        value = reader.read_json(process_id)
        if current_commands is not None:
            from literate_ai.adapters.qualification_mix import mix_target_document

            contract = current_commands.get(plan.component_revision)
            if (
                contract is not None
                and contract.locked_build_authority_identity
                == canonical_identity(mix_target_document(plan))
                and value.get("schema") != "literate-ai/mix-build-evidence@1"
            ):
                raise QualificationCaptureError(
                    "qualification.capture.mix-build-mismatch"
                )
        if value.get("schema") == "literate-ai/local-process-observation@1":
            process(process_id, "build")
            artifact_tree()
            return
        if value.get("schema") == "literate-ai/local-cargo-build-observation@1":
            from literate_ai.adapters.qualification_cargo import (
                verify_qualification_cargo_build,
            )

            verify_qualification_cargo_build(
                reader, plan=plan, observation=value, files=artifact_tree()
            )
            return
        if value.get("schema") == "literate-ai/mix-build-evidence@1":
            from literate_ai.adapters.qualification_mix import (
                verify_qualification_mix_build,
            )

            verify_qualification_mix_build(
                reader,
                plan=plan,
                observation=value,
                files=artifact_tree(),
                exports=build.exports,
                provider_exports=provider_exports or {},
                current_commands=current_commands or {},
            )
            return
        target_id = ContentIdentity.parse_uri(value["npm_target_identity"])
        dependency_id = ContentIdentity.parse_uri(value["dependency_evidence_manifest"])
        checks = value["syntax_checks"]
        if not isinstance(checks, list) or not checks:
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )
        same(
            process_id,
            {
                "schema": "literate-ai/local-npm-build-observation@1",
                "npm_target_identity": target_id.uri,
                "dependency_evidence_manifest": dependency_id.uri,
                "syntax_checks": checks,
            },
        )
        raw = reader.read_json(target_id)
        target = StandardNpmTarget(
            **{
                field.name: (
                    tuple(raw[field.name])
                    if field.name == "npm_command"
                    else raw[field.name]
                    if field.name in ("manifest", "lockfile")
                    else ContentIdentity.parse_uri(raw[field.name])
                )
                for field in fields(StandardNpmTarget)
            }
        )
        same(target_id, target.identity_document())
        if (
            target.component_revision != plan.component_revision
            or target.build_system_toolchain_identity
            != plan.manifest.build_system_driver_identity
            or target.build_system_resolver_identity
            != plan.request.build_system_resolver_identity
            or target.node_toolchain_identity != plan.request.language_compiler_identity
            or target.node_toolchain_identity != plan.request.language_runtime_identity
        ):
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )
        raw = reader.read_json(dependency_id)
        evidence = StandardNpmDependencyEvidence(
            **{
                field.name: (
                    tuple(
                        (item["path"], ContentIdentity.parse_uri(item["identity"]))
                        for item in raw["files"]
                    )
                    if field.name == "files"
                    else ContentIdentity.parse_uri(raw[field.name])
                )
                for field in fields(StandardNpmDependencyEvidence)
            }
        )
        same(dependency_id, evidence.to_dict())
        authority = parse_npm_source_authority(
            reader.read_bytes(evidence.manifest_identity),
            reader.read_bytes(evidence.lockfile_identity),
            target,
        )
        same(evidence.source_authority_identity, authority.identity_document())
        inventory = normalized_npm_inventory(authority)
        if reader.read_bytes(evidence.inventory_identity) != inventory:
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )
        expected = replace(
            evidence,
            authorization_identity=plan.request.authorization_identity,
            component_revision=plan.component_revision,
            build_plan_identity=plan.identity,
            source_tree_identity=plan.request.source_tree_identity,
            packaging_flavor_revision_identity=target.packaging_flavor_revision_identity,
            packaging_profile_identity=target.packaging_profile_identity,
            npm_target_identity=target.identity,
            npm_toolchain_identity=target.build_system_toolchain_identity,
            node_toolchain_identity=target.node_toolchain_identity,
            source_authority_identity=authority.identity,
            manifest_identity=authority.manifest_identity,
            lockfile_identity=authority.lockfile_identity,
            files=tuple(
                sorted(
                    (
                        (".literate/npm/package.json", authority.manifest_identity),
                        (
                            ".literate/npm/package-lock.json",
                            authority.lockfile_identity,
                        ),
                        (".literate/npm/inventory.json", evidence.inventory_identity),
                    )
                )
            ),
        )
        if evidence != expected:
            raise QualificationCaptureError(
                "qualification.capture.build-process-mismatch"
            )
        for identity, phase, arguments in (
            (
                evidence.install_process_identity,
                "npm-ci",
                ["ci", "--ignore-scripts", "--no-audit", "--no-fund", "--no-bin-links"],
            ),
            (evidence.inventory_process_identity, "npm-ls", ["ls", "--all", "--json"]),
        ):
            same(
                identity,
                {
                    "schema": "literate-ai/local-npm-process-observation@1",
                    "phase": phase,
                    "plan_identity": plan.identity.uri,
                    "returncode": 0,
                    "arguments": arguments,
                },
            )
        for identity in set(ContentIdentity.parse_uri(item) for item in checks):
            process(identity, "node-check")
        artifact_tree()
    except QualificationCaptureError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        StandardNpmLifecycleError,
        RecursionError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.build-process-invalid"
        ) from exc


def verify_qualification_boms(
    reader: QualificationEvidenceReader,
    *,
    component_lock: ComponentLock,
    build: StandardBuildEvidence,
) -> None:
    """Revalidate both retained CycloneDX documents against current locked closure."""

    from literate_ai.adapters.dependencies import (
        CycloneDxBomError,
        validate_cyclonedx_bom,
    )
    from literate_ai.contracts.sbom import (
        CycloneDxLifecycle,
        project_component_lock_managed_graph,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(component_lock, ComponentLock)
        or not isinstance(build, StandardBuildEvidence)
    ):
        raise QualificationCaptureError("qualification.capture.bom-authority-invalid")
    try:
        graph = project_component_lock_managed_graph(
            component_lock, build.component_revision
        )
        if canonical_json_bytes(
            reader.read_json(graph.identity)
        ) != canonical_json_bytes(graph.to_dict()):
            raise QualificationCaptureError("qualification.capture.bom-graph-mismatch")
        source = reader.read_bytes(build.source_sbom.bom_identity)
        source_binding = validate_cyclonedx_bom(
            source, lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=graph
        )
        resolved_binding = validate_cyclonedx_bom(
            reader.read_bytes(build.resolved_sbom.bom_identity),
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=graph,
            source_content=source,
            source_managed_graph=graph,
        )
        if (
            source_binding != build.source_sbom
            or resolved_binding != build.resolved_sbom
        ):
            raise QualificationCaptureError(
                "qualification.capture.bom-binding-mismatch"
            )
    except QualificationCaptureError:
        raise
    except (CycloneDxBomError, TypeError, ValueError, RecursionError) as exc:
        raise QualificationCaptureError("qualification.capture.bom-invalid") from exc


def verify_qualification_generated_suite(
    reader: QualificationEvidenceReader,
    *,
    recipe: object,
    component_lock_identity: ContentIdentity,
    tests: StandardGeneratedTestExecutionEvidence,
) -> None:
    """Validate retained suite bytes against a caller-projected current recipe."""

    from literate_ai.adapters.models.coding_cli import (
        CodingCliError,
        GenerationRecipe,
        _acceptance_argument_vectors,
        _acceptance_result_shape,
    )
    from literate_ai.generated_tests import (
        GeneratedTestSuiteError,
        validate_generated_test_suite,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(recipe, GenerationRecipe)
        or not isinstance(component_lock_identity, ContentIdentity)
        or not isinstance(tests, StandardGeneratedTestExecutionEvidence)
        or recipe.component_lock_identity != component_lock_identity
    ):
        raise QualificationCaptureError("qualification.capture.suite-authority-invalid")
    try:
        suite = validate_generated_test_suite(
            reader.read_bytes(tests.generated_test_suite_identity),
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        expected = {}
        for case in suite.cases:
            definition = {
                "schema": "literate-ai/generated-test-case@1",
                "case_id": case.case_id,
                "category": case.category,
                "specification_refs": list(case.specification_refs),
                "arguments": list(case.arguments),
                "expected_result": case.expected_result,
            }
            identity = canonical_identity(definition)
            if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
                definition
            ):
                raise QualificationCaptureError(
                    "qualification.capture.suite-membership-mismatch"
                )
            expected[case.case_id] = identity
        groups = (
            (tests,) if tests.entrypoint_evidence is None else tests.entrypoint_evidence
        )
        for group in groups:
            actual = {case.case_id: case.case_identity for case in group.cases}
            if actual != expected or len(group.cases) != len(expected):
                raise QualificationCaptureError(
                    "qualification.capture.suite-membership-mismatch"
                )
    except QualificationCaptureError:
        raise
    except (
        GeneratedTestSuiteError,
        CodingCliError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise QualificationCaptureError("qualification.capture.suite-invalid") from exc


def verify_qualification_generated_tests(
    reader: QualificationEvidenceReader,
    *,
    build_plan_identity: ContentIdentity,
    build: StandardBuildEvidence,
    tests: StandardGeneratedTestExecutionEvidence,
) -> None:
    """Bind retained case observations to their complete successful process output.

    Current suite recipe/specification and source custody validation remain separate.
    """
    from literate_ai.adapters.native_sdk_qualification import (
        read_sdk_build_plan,
        sdk_process_fields,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(build_plan_identity, ContentIdentity)
        or not isinstance(build, StandardBuildEvidence)
        or not isinstance(tests, StandardGeneratedTestExecutionEvidence)
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")

    def same(identity, expected):
        if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
            expected
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate test result field")
            result[key] = value
        return result

    def cases_for(cases, phase, entrypoint=None, process_identity=None):
        processes = set()
        for case in cases:
            value = reader.read_json(case.observation_identity)
            identity = ContentIdentity.parse_uri(
                value["suite_process_observation_identity"]
            )
            expected = {
                "schema": "literate-ai/generated-test-case-observation@1",
                "observation_kind": "attributed-suite-case"
                if entrypoint is None
                else "attributed-entrypoint-suite-case",
                "suite_process_observation_identity": identity.uri,
                "case_identity": case.case_identity.uri,
                "case_result": {"case_id": case.case_id, "outcome": "passed"},
            }
            if entrypoint is not None:
                expected["entrypoint_identity"] = entrypoint.uri
            same(case.observation_identity, expected)
            # Reopen the original case record. Suite/recipe membership belongs
            # to separate source-custody validation, not this self-description.
            definition = reader.read_json(case.case_identity)
            if (
                definition["schema"] != "literate-ai/generated-test-case@1"
                or definition["case_id"] != case.case_id
            ):
                raise QualificationCaptureError(
                    "qualification.capture.test-process-mismatch"
                )
            if set(definition) != {
                "schema",
                "case_id",
                "category",
                "specification_refs",
                "arguments",
                "expected_result",
            }:
                raise QualificationCaptureError(
                    "qualification.capture.test-process-mismatch"
                )
            processes.add(identity)
        if len(processes) != 1 or (
            process_identity is not None and processes != {process_identity}
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )
        identity = next(iter(processes))
        value = reader.read_json(identity)
        stdout = ContentIdentity.parse_uri(value["stdout_identity"])
        stderr = ContentIdentity.parse_uri(value["stderr_identity"])
        same(
            identity,
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": phase,
                "plan_identity": build_plan_identity.uri,
                "returncode": 0,
                "stdout_identity": stdout.uri,
                "stderr_identity": stderr.uri,
                **sdk_process_fields(
                    reader,
                    plan=plan,
                    process=value,
                    phase="test",
                    entrypoint_identity=entrypoint,
                ),
            },
        )
        output = reader.read_json(stdout)
        if not isinstance(output, str) or not isinstance(reader.read_json(stderr), str):
            raise QualificationCaptureError("qualification.capture.test-output-invalid")
        observed = json.loads(output, object_pairs_hook=unique)
        if (
            set(observed) != {"schema", "cases"}
            or observed["schema"] != "literate-ai/generated-test-results@1"
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )
        raw_cases = observed["cases"]
        if not isinstance(raw_cases, list) or len(raw_cases) != len(cases):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )
        expected = [{"case_id": case.case_id, "outcome": "passed"} for case in cases]
        if sorted(canonical_json_bytes(case) for case in raw_cases) != sorted(
            canonical_json_bytes(case) for case in expected
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )

    try:
        plan = read_sdk_build_plan(reader, build_plan_identity)
        if (
            build.build_plan_identity != build_plan_identity
            or tests.component_revision != build.component_revision
            or tests.build_evidence_identity != build.identity
            or tests.export_identities != build.export_identities
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )
        if tests.entrypoint_evidence is None:
            cases_for(tests.cases, "generated-test")
            return
        units = tests.entrypoint_evidence
        aggregate = []
        for unit in units:
            same(unit.identity, unit.to_dict())
            cases_for(
                unit.cases,
                f"generated-test:{unit.deployment_unit}",
                unit.entrypoint_identity,
                unit.process_observation_identity,
            )
            for case in unit.cases:
                definition = {
                    "schema": "literate-ai/entrypoint-generated-test-case@1",
                    "entrypoint_identity": unit.entrypoint_identity.uri,
                    "case_identity": case.case_identity.uri,
                }
                identity = canonical_identity(definition)
                same(identity, definition)
                aggregate.append(
                    StandardGeneratedTestCaseEvidence(
                        f"{unit.entrypoint_identity.digest[:16]}:{case.case_id[:220]}",
                        identity,
                        case.observation_identity,
                    )
                )
        if tests.cases != tuple(
            sorted(aggregate, key=lambda case: case.case_identity.uri)
        ):
            raise QualificationCaptureError(
                "qualification.capture.test-process-mismatch"
            )
        same(
            tests.runner_identity,
            {
                "schema": "literate-ai/multi-entrypoint-test-runners@1",
                "entrypoint_evidence": [unit.identity.uri for unit in units],
            },
        )
        same(
            tests.test_custody_identity,
            {
                "schema": "literate-ai/multi-entrypoint-test-custody@1",
                "entrypoint_evidence": [unit.identity.uri for unit in units],
            },
        )
    except QualificationCaptureError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.test-process-invalid"
        ) from exc


def verify_qualification_execution(
    reader: QualificationEvidenceReader,
    *,
    build_plan_identity: ContentIdentity,
    execution: StandardExecutionEvidence,
) -> None:
    """Reopen existing single/multiple-entrypoint execution process records."""
    from literate_ai.adapters.native_sdk_qualification import (
        read_sdk_build_plan,
        sdk_process_fields,
    )

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(build_plan_identity, ContentIdentity)
        or not isinstance(execution, StandardExecutionEvidence)
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")

    plan = read_sdk_build_plan(reader, build_plan_identity)

    def process(evidence, phase):
        value = reader.read_json(evidence.observation_identity)
        expected = {
            "schema": "literate-ai/local-process-observation@1",
            "phase": phase,
            "plan_identity": build_plan_identity.uri,
            "returncode": 0,
            "stdout_identity": evidence.stdout_identity.uri,
            "stderr_identity": evidence.stderr_identity.uri,
            **sdk_process_fields(
                reader,
                plan=plan,
                process=value,
                phase="execute",
                entrypoint_identity=getattr(evidence, "entrypoint_identity", None),
                command_identity=evidence.execution_contract_identity,
            ),
        }
        if canonical_json_bytes(
            reader.read_json(evidence.observation_identity)
        ) != canonical_json_bytes(expected):
            raise QualificationCaptureError(
                "qualification.capture.execution-process-mismatch"
            )
        stdout = reader.read_json(evidence.stdout_identity)
        stderr = reader.read_json(evidence.stderr_identity)
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise QualificationCaptureError(
                "qualification.capture.execution-output-invalid"
            )
        return stdout, stderr

    if execution.entrypoint_evidence is None:
        process(execution, "execute")
        return
    units = execution.entrypoint_evidence
    stdout, stderr = {}, {}
    for unit in units:
        if reader.read_json(unit.identity) != unit.to_dict():
            raise QualificationCaptureError(
                "qualification.capture.execution-process-mismatch"
            )
        stdout[unit.deployment_unit], stderr[unit.deployment_unit] = process(
            unit, f"execute:{unit.deployment_unit}"
        )
    expected_records = (
        (
            execution.observation_identity,
            {
                "schema": "literate-ai/multi-entrypoint-execution-observation@1",
                "entrypoint_evidence": [unit.identity.uri for unit in units],
            },
        ),
        (
            execution.execution_contract_identity,
            {
                "schema": "literate-ai/multi-entrypoint-execution-contract@1",
                "entrypoint_evidence": [unit.identity.uri for unit in units],
            },
        ),
        (
            execution.runtime_identity,
            {
                "schema": "literate-ai/multi-entrypoint-runtime@1",
                "runtimes": sorted({unit.runtime_identity.uri for unit in units}),
            },
        ),
        (execution.stdout_identity, stdout),
        (execution.stderr_identity, stderr),
    )
    for identity, expected in expected_records:
        if canonical_json_bytes(reader.read_json(identity)) != canonical_json_bytes(
            expected
        ):
            raise QualificationCaptureError(
                "qualification.capture.execution-process-mismatch"
            )


def reopen_qualification_run(
    reader: QualificationEvidenceReader,
    run: QualificationLifecycleRunEvidence,
    *,
    current_commands: Mapping | None = None,
) -> QualificationLifecycleExecution:
    """Reopen the complete typed lifecycle membership claimed by one run.

    Reuse the producer's qualification derivation and accepted-node custody
    validator. This checks the retained chain; current provider authority,
    independent stage observations and importer trust remain required.
    """

    from literate_ai.application.standard_project_lifecycle import (
        StandardAcceptedSourcePublication,
        StandardProjectLifecycleError,
    )

    if not isinstance(run, QualificationLifecycleRunEvidence):
        raise QualificationCaptureError("qualification.capture.records-invalid")
    lifecycle = reopen_qualification_lifecycle(reader, run.lifecycle_result_identity)
    root = reopen_qualification_root(reader, run)
    receipt = ProjectTestReceipt.from_dict(
        reader.read_json(run.project_receipt_identity)
    )
    execution = QualificationLifecycleExecution(
        lifecycle,
        receipt,
        run.lifecycle_request_identity,
        run.lifecycle_invocation_identity,
        run.driver_identity,
        run.lifecycle_policy_identity,
        run.framework_distribution_identity,
    )
    try:
        derived = QualificationLifecycleRunner._derive_standard(execution)
        for node in lifecycle.node_results:
            StandardAcceptedSourcePublication(
                node.component_revision,
                node.build_plan_identity,
                node.source_output,
                node.exports,
                node.index_identity,
                node.authorization_identity,
                node.source_cache_membership,
                node.build_evidence,
                node.generated_test_evidence,
                node.execution_evidence,
                node.acceptance_evidence,
            )
            candidate = node.source_output.candidate
            provenance = node.source_output.provenance
            expected_custody = {
                "schema": "literate-ai/local-generated-source-custody@1",
                "candidate_identity": candidate.identity.uri,
                "source_generation_identity": node.source_output.identity.uri,
                "source_tree_identity": candidate.tree_identity.uri,
                "source_bom_identity": candidate.source_bom_identity.uri,
                "managed_graph_identity": (
                    node.build_evidence.source_sbom.managed_graph_identity.uri
                ),
                "generated_test_suite_identity": (
                    candidate.generated_test_suite_identity.uri
                ),
            }
            if canonical_json_bytes(
                reader.read_json(candidate.identity)
            ) != canonical_json_bytes(candidate.to_dict()) or canonical_json_bytes(
                reader.read_json(node.build_evidence.source_custody_identity)
            ) != canonical_json_bytes(expected_custody):
                raise QualificationCaptureError(
                    "qualification.capture.source-custody-mismatch"
                )
            if (
                node.build_evidence.source_tree_identity != candidate.tree_identity
                or node.build_evidence.source_sbom.bom_identity
                != candidate.source_bom_identity
                or node.generated_test_evidence.generated_test_suite_identity
                != candidate.generated_test_suite_identity
                or provenance.component_lock_identity != run.component_lock_identity
                or provenance.application_root_revision_identity
                != root.package_plan.root_component_revision
            ):
                raise QualificationCaptureError(
                    "qualification.capture.source-chain-mismatch"
                )
    except QualificationCaptureError:
        raise
    except (TypeError, ValueError, StandardProjectLifecycleError) as exc:
        raise QualificationCaptureError("qualification.capture.run-incomplete") from exc
    plans = {plan.identity: plan for plan in lifecycle.project_build_plan.components}
    for node in lifecycle.node_results:
        verify_qualification_build_authorization(
            reader,
            candidate=node.source_output.candidate,
            index_identity=node.index_identity,
            authorization_identity=node.authorization_identity,
            plan=plans[node.build_plan_identity],
        )
        verify_qualification_build(
            reader,
            plan=plans[node.build_plan_identity],
            build=node.build_evidence,
            provider_exports={
                export.identity: export
                for member in lifecycle.node_results
                for export in member.exports
            },
            current_commands=current_commands,
        )
        verify_qualification_generated_tests(
            reader,
            build_plan_identity=node.build_plan_identity,
            build=node.build_evidence,
            tests=node.generated_test_evidence,
        )
        verify_qualification_execution(
            reader,
            build_plan_identity=node.build_plan_identity,
            execution=node.execution_evidence,
        )
    fields = (
        "source_tree_identities",
        "source_index_identities",
        "build_evidence_identities",
        "resolved_sbom_identities",
        "generated_test_suite_identities",
        "generated_test_evidence_identities",
        "generated_test_case_identities",
        "acceptance_evidence_identities",
        "cache_decision_identities",
        "node_workspace_identities",
    )
    if derived != tuple(getattr(run, name) for name in fields):
        raise QualificationCaptureError("qualification.capture.run-mismatch")
    return execution


def reopen_qualification_root(
    reader: QualificationEvidenceReader,
    run: QualificationLifecycleRunEvidence,
) -> StandardRootIntegrationEvidence:
    """Bind a reopened root package and project receipt to the exact run.

    This verifies cross-record identity relationships, not the truth of stage
    observations or current provider/importer authority. Those checks must precede
    publication or consumption; this function issues no admission token.
    """

    if not isinstance(reader, QualificationEvidenceReader) or not isinstance(
        run, QualificationLifecycleRunEvidence
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")
    lifecycle = reader.read_json(run.lifecycle_result_identity)
    if not isinstance(lifecycle, dict) or lifecycle.get("schema") != (
        "literate-ai/standard-project-lifecycle-result@4"
    ):
        raise QualificationCaptureError("qualification.capture.lifecycle-invalid")

    def identity(name: str) -> ContentIdentity:
        try:
            return ContentIdentity.parse_uri(lifecycle[name])
        except (KeyError, ValueError, TypeError) as exc:
            raise QualificationCaptureError(
                "qualification.capture.lifecycle-incomplete"
            ) from exc

    root = StandardRootIntegrationEvidence.from_dict(
        reader.read_json(identity("root_integration_evidence_identity"))
    )
    aggregate = StandardAggregateReceipt.from_dict(
        reader.read_json(identity("aggregate_receipt_identity"))
    )
    receipt = ProjectTestReceipt.from_dict(
        reader.read_json(run.project_receipt_identity)
    )
    if (
        root.component_lock_identity != run.component_lock_identity
        or root.package_plan.target_identity != run.target_profile_identity
        or root.execution_plan_identity != identity("execution_plan_identity")
        or root.project_build_plan_identity != identity("project_build_plan_identity")
        or aggregate.execution_plan_identity != root.execution_plan_identity
        or aggregate.root_integration_evidence_identity != root.identity
        or aggregate.lifecycle_membership_identity
        != identity("lifecycle_membership_identity")
        or aggregate.admission_identity != identity("admission_identity")
        or aggregate.context_cache_report_identity
        != identity("context_cache_report_identity")
        or [item.uri for item in aggregate.lifecycle_result_identities]
        != lifecycle.get("node_results")
        or [item.uri for item in aggregate.context_prompt_journal_identities]
        != lifecycle.get("context_prompt_journal_identities")
        or [item.uri for item in aggregate.context_benchmark_record_identities]
        != lifecycle.get("context_benchmark_record_identities")
        or receipt.result_identity != run.lifecycle_result_identity
        or receipt.subject_identity != aggregate.identity
        or receipt.suite.content_identity != run.lifecycle_policy_identity
        or receipt.summary.total != run.generated_test_total
    ):
        raise QualificationCaptureError("qualification.capture.root-mismatch")
    # The producer projects these exact existing evidence-set identities. They
    # are embedded commitments, so recompute them rather than requiring a made-up
    # standalone record for every digest in a receipt.
    evidence = {item.kind: item.identity for item in receipt.evidence}
    expected = {
        "lifecycle-command": run.lifecycle_invocation_identity,
        "lifecycle-request": run.lifecycle_request_identity,
        "lifecycle-plan": root.execution_plan_identity,
        "source-cache-lifecycle": aggregate.lifecycle_membership_identity,
        "workspace-admission": aggregate.admission_identity,
    }
    for kind, members in (
        ("acceptance-result", run.acceptance_evidence_identities),
        ("build-result", run.build_evidence_identities),
        ("source-intelligence", run.source_index_identities),
        ("resolved-sbom", run.resolved_sbom_identities),
        ("test-report", run.generated_test_evidence_identities),
    ):
        expected[kind] = canonical_identity(
            {
                "schema": "literate-ai/standard-project-evidence-set@1",
                "kind": kind,
                "members": [item.uri for item in members],
            }
        )
    if set(evidence) != set(STANDARD_FULL_REBUILD_EVIDENCE_KINDS) or any(
        evidence.get(kind) != value for kind, value in expected.items()
    ):
        raise QualificationCaptureError("qualification.capture.receipt-mismatch")
    return root


def verify_qualification_library_acceptance(
    reader: QualificationEvidenceReader,
    *,
    root: StandardRootIntegrationEvidence,
    product: LibraryArtifactProduct,
    component_lock: ComponentLock,
    oracle: LibraryAcceptance,
) -> None:
    """Reopen exact library oracle cases and matching retained observations.

    The caller must reopen the current lock and verifier-owned oracle from trusted
    provider authority. This verifies content and bindings, never authenticates
    a producer or authorizes an importer to execute the package.
    """

    if (
        not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(root, StandardRootIntegrationEvidence)
        or not isinstance(product, LibraryArtifactProduct)
        or not isinstance(component_lock, ComponentLock)
        or not isinstance(oracle, LibraryAcceptance)
    ):
        raise QualificationCaptureError("qualification.capture.records-invalid")
    export, surface = product.artifact_export, product.import_surface
    node = next(
        item
        for item in component_lock.nodes
        if item.revision.identity == component_lock.root_revision
    )
    interfaces = tuple(
        sorted(
            (item.identity for item in node.revision.public_interfaces),
            key=lambda item: item.uri,
        )
    )
    if (
        root.component_lock_identity != component_lock.identity
        or root.package_plan.root_component_revision != component_lock.root_revision
        or export.component_revision != component_lock.root_revision
        or root.package_plan.root_artifact_identity != export.identity
        or root.package_plan.target_identity != export.target_identity
        or export
        not in tuple(
            item
            for manifest in root.artifact_graph.manifests
            for item in manifest.exports
        )
        or oracle.component != node.revision.coordinate.name
        or oracle.specification_set_identity != node.revision.specification_set_identity
        or oracle.public_interface_identities != interfaces
        or oracle.import_surface_identity != surface.identity
        or oracle.language != surface.language
    ):
        raise QualificationCaptureError(
            "qualification.capture.oracle-authority-mismatch"
        )
    oracle_bytes = product_json_bytes(oracle.identity_document())
    oracle_identity = ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(oracle_bytes).hexdigest()
    )
    document = json.loads(oracle_bytes)
    if reader.read_bytes(oracle_identity) != oracle_bytes or (
        reader.read_bytes(oracle.harness_identity) != oracle.harness_content
    ):
        raise QualificationCaptureError("qualification.capture.oracle-record-mismatch")
    if reader.read_json(surface.identity) != surface.to_dict():
        raise QualificationCaptureError("qualification.capture.oracle-record-mismatch")
    cases = document["cases"]
    case_ids = tuple(case["case_id"] for case in cases)
    capabilities = {item.capability for item in surface.capabilities}
    if (
        not cases
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or case_ids != tuple(sorted(set(case_ids)))
        or any(case["capability"] not in capabilities for case in cases)
    ):
        raise QualificationCaptureError("qualification.capture.oracle-cases-invalid")
    actual = reader.read_json(root.independent_acceptance_identity)
    observed = actual.get("observations") if isinstance(actual, dict) else None
    if not isinstance(observed, list) or len(observed) != len(cases):
        raise QualificationCaptureError(
            "qualification.capture.oracle-observation-mismatch"
        )
    observations = []
    for case, observation in zip(cases, observed, strict=True):
        payload = {
            "arguments": case["arguments"],
            "expected_result": case["expected_result"],
        }
        case_identity = product_json_identity(payload)
        try:
            if not isinstance(observation, dict):
                raise ValueError("observation must be an object")
            result_identity = ContentIdentity.parse_uri(
                observation.get("result_identity")
            )
            result_bytes = reader.read_bytes(result_identity)
            result_value = json.loads(result_bytes)
            if result_bytes != product_json_bytes(
                result_value
            ) or not product_json_values_equal(result_value, case["expected_result"]):
                raise ValueError("observed result differs")
        except QualificationCaptureError:
            raise
        except (TypeError, ValueError) as exc:
            raise QualificationCaptureError(
                "qualification.capture.oracle-observation-mismatch"
            ) from exc
        # These payloads are required independent records; the identity-bound
        # reader checks exact identities; product encoding preserves finite values
        # without relaxing the contract reader's integer-only JSON boundary.
        if reader.read_bytes(case_identity) != product_json_bytes(payload):
            raise QualificationCaptureError(
                "qualification.capture.oracle-record-mismatch"
            )
        observations.append(
            {
                "case_id": case["case_id"],
                "capability": case["capability"],
                "case_identity": case_identity.uri,
                "result_identity": result_identity.uri,
            }
        )
    expected = {
        "schema": "literate-ai/local-independent-library-acceptance@1",
        "package_plan_identity": root.package_plan.identity.uri,
        "package_result_identity": root.package_result.identity.uri,
        "root_integration_test_identity": (
            root.root_generated_integration_test_identity.uri
        ),
        "packaged_execution_identity": root.packaged_execution_identity.uri,
        "oracle_identity": oracle_identity.uri,
        "harness_identity": oracle.harness_identity.uri,
        "artifact_identity": export.identity.uri,
        "import_surface_identity": surface.identity.uri,
        "observations": observations,
    }
    from literate_ai.adapters.native_sdk_qualification import (
        packaged_sdk_process_fields,
    )

    sdk_fields = packaged_sdk_process_fields(
        reader,
        package_plan=root.package_plan,
        package_result=root.package_result,
        project_build_plan_identity=root.project_build_plan_identity,
        process=actual,
        phase="library-acceptance",
        library_oracle=oracle,
    )
    if sdk_fields:
        expected.update(
            schema="literate-ai/local-independent-library-acceptance@2", **sdk_fields
        )
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise QualificationCaptureError(
            "qualification.capture.oracle-observation-mismatch"
        )
