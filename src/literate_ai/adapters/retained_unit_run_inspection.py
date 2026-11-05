"""Reopen immutable accepted-run assertions using existing completion checks.

The resulting completed-run value contains historical facts, not a live SDK,
retained admission, LINK handoff or permission to execute anything.
"""

import json

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.native_sdk_dependencies import NativeSdkDependencyEvidence
from literate_ai.adapters.retained_project_acceptance import (
    RetainedProjectAcceptanceObservation,
    read_retained_acceptance_policy,
)
from literate_ai.adapters.retained_project_boms import (
    RetainedProjectResolvedBom,
    RetainedProjectSourceBom,
)
from literate_ai.adapters.retained_project_completion import (
    RetainedProjectCompletedRun,
    _stage_manifest,
)
from literate_ai.adapters.retained_project_execution import (
    RetainedProjectCommandObservation,
)
from literate_ai.adapters.retained_project_filesystem import (
    RetainedFilesystemObservation,
)
from literate_ai.adapters.retained_project_testing import (
    RetainedProjectVerifiedTestReport,
    read_retained_project_test_policy,
)
from literate_ai.adapters.retained_sdk_metadata import (
    SDK_METADATA_MEDIA_TYPES,
    retained_sdk_metadata_closure,
)
from literate_ai.adapters.retained_sdk_run_inspection import (
    verify_retained_sdk_run_assertions,
)
from literate_ai.adapters.retained_unit_run_evidence import RUN_EVIDENCE_SCHEMA
from literate_ai.contracts import (
    ContentIdentity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_filesystem import RetainedFilesystemState
from literate_ai.contracts.retained_project_tests import RetainedProjectTestReport
from literate_ai.contracts.retained_secondary_evidence import (
    RetainedSecondaryAccessEvidence,
)
from literate_ai.contracts.sbom import CycloneDxRepositorySourceResolution


class _Reader:
    def __init__(self, inputs, policy, guard):
        self.store, self.policy, self.guard = inputs.store, policy, guard
        if 512 * len(inputs.receipt.records) > policy.max_provider_memory_bytes:
            raise ValueError("retained.unit.run-record-memory-policy-exceeded")
        self.records = {ref.identity: ref for ref in inputs.receipt.records}
        # Current source/output/payload manifests and the receipt already remain
        # live while this reader reconstructs additional facts. Reserve all roots
        # conservatively before any new raw/decoded/typed caches are populated.
        self.seen = {ref.identity: ref.size for _, ref in inputs.receipt.roots}
        self.seen[inputs.receipt.identity.uri] = len(
            canonical_json_bytes(inputs.receipt.to_dict())
        )
        self.total = sum(self.seen.values())
        self._require_budget(self.total)
        self.raw_cache, self.documents, self.manifests = {}, {}, {}

    def _require_budget(self, total):
        if (
            total > self.policy.max_provider_metadata_bytes
            or 16 * total + 512 * len(self.records)
            > self.policy.max_provider_memory_bytes
        ):
            raise ValueError("retained.unit.run-metadata-policy-exceeded")

    def raw(self, reference):
        self.guard()
        if not isinstance(reference, BlobRef) or reference.size > min(
            64 * 1024**2,
            getattr(self.policy, "maximum_document_bytes", 64 * 1024**2),
            self.policy.max_provider_metadata_bytes,
        ):
            raise ValueError("retained.unit.run-document-policy-exceeded")
        known_size = self.seen.get(reference.identity)
        if known_size is not None and known_size != reference.size:
            raise ValueError("retained.unit.run-record-size-differs")
        if reference.identity not in self.seen:
            self._require_budget(self.total + reference.size)
            self.seen[reference.identity] = reference.size
            self.total += reference.size
        if reference.identity not in self.raw_cache:
            self.raw_cache[reference.identity] = self.store.get_bytes(reference)
        else:
            # Caching bounds allocation; it never suppresses a fresh custody
            # check or revives bytes after the caller's live guard is revoked.
            self.store.verify(reference)
        self.guard()
        return self.raw_cache[reference.identity]

    def document(self, reference, *, historical_record=False):
        if (
            not isinstance(reference, BlobRef)
            or reference.media_type
            not in (
                {*SDK_METADATA_MEDIA_TYPES, "application/octet-stream"}
                if historical_record
                else {"application/json"}
            )
            or reference.size == 0
        ):
            raise ValueError("retained.unit.canonical-run-document-required")
        raw = self.raw(reference)
        if reference.identity not in self.documents:
            value = json.loads(raw)
            if type(value) is not dict or canonical_json_bytes(value) != raw:
                raise ValueError("retained.unit.canonical-run-document-required")
            self.documents[reference.identity] = value
        self.guard()
        return self.documents[reference.identity]

    def manifest(self, reference):
        value = self.document(reference)
        if reference.identity not in self.manifests:
            self.manifests[reference.identity] = RetainedProjectManifest.from_dict(
                value
            )
        return self.manifests[reference.identity]

    def reference(self, identity):
        identity = ContentIdentity.parse_uri(identity).uri
        try:
            return self.records[identity]
        except KeyError:
            raise ValueError("retained.unit.run-record-missing") from None

    def record(self, identity):
        # Frozen receipts can preserve the original octet-stream label when
        # canonical policy JSON was retained first as source bytes. The caller
        # explicitly selects this known record's JSON role; retain its label and
        # still verify byte custody, canonical JSON and the contextual schema.
        return self.document(self.reference(identity), historical_record=True)

    def identities(self, document, name, count):
        values = document.get(name)
        if type(values) is not list or len(values) != count:
            raise ValueError("retained.unit.complete-run-observations-required")
        selected = tuple(ContentIdentity.parse_uri(value).uri for value in values)
        if len(set(selected)) != len(selected):
            raise ValueError("retained.unit.unique-run-observations-required")
        return selected


def reopen_retained_unit_run(
    inputs,
    planning,
    evidence_reference,
    *,
    policy,
    test_receipt_policy,
    admission_guard,
    expected_sdk_platform=None,
    expected_sdk_architecture=None,
    current_sdk_snapshots=(),
    workspace_root_observer=None,
):
    if workspace_root_observer is not None and not callable(workspace_root_observer):
        raise TypeError("retained.unit.callable-workspace-observer-required")
    reader = _Reader(inputs, policy, admission_guard)
    plan, receipt = inputs.plan, inputs.receipt
    if planning.plan != plan:
        raise ValueError("retained.unit.run-current-plan-differs")
    evidence = reader.document(evidence_reference)
    if (
        type(evidence) is not dict
        or set(evidence)
        != {
            "schema",
            "retained_receipt_identity",
            "tool_observation",
            "export_observation",
            "sdk_dependencies",
            "sdk_metadata_records",
            "command_output_manifests",
            "admitted",
            "release_authorized",
        }
        or evidence["schema"] != RUN_EVIDENCE_SCHEMA
        or evidence["retained_receipt_identity"] != receipt.identity.uri
        or evidence["admitted"] is not False
        or evidence["release_authorized"] is not False
        or type(evidence["sdk_dependencies"]) is not list
        or type(evidence["sdk_metadata_records"]) is not list
        or 512 * len(evidence["sdk_metadata_records"])
        > policy.max_provider_memory_bytes
        or type(evidence["command_output_manifests"]) is not list
        or len(evidence["command_output_manifests"]) != len(plan.profile.actions)
        or 512 * len(evidence["sdk_dependencies"]) > policy.max_provider_memory_bytes
    ):
        raise ValueError("retained.unit.closed-run-evidence-required")
    completed = reader.document(receipt.root("completed-run"))
    test_policy = read_retained_project_test_policy(
        inputs.source, inputs.source_manifest, plan.profile, test_receipt_policy
    )
    acceptance = read_retained_acceptance_policy(
        inputs.source, plan.profile, plan.component_locks, inputs.source_manifest
    )
    if acceptance is None:
        raise ValueError("retained.unit.current-independent-acceptance-required")
    commands = reader.identities(
        completed, "command_observations", len(plan.profile.actions)
    )
    tests = reader.identities(completed, "test_observations", len(test_policy.reports))
    expected_cases = tuple(
        case
        for action in plan.profile.actions
        if action.stage == "accept"
        for case in acceptance.cases
        if case.action_id == action.action_id
    )
    cases = reader.identities(completed, "acceptance_observations", len(expected_cases))
    sdk_references = tuple(
        BlobRef.from_dict(value) for value in evidence["sdk_dependencies"]
    )
    if len({ref.identity for ref in sdk_references}) != len(sdk_references):
        raise ValueError("retained.unit.unique-sdk-dependency-assertions-required")
    sdk_metadata = tuple(
        BlobRef.from_dict(value) for value in evidence["sdk_metadata_records"]
    )
    if (
        512 * (len(reader.records) + len(sdk_metadata))
        > policy.max_provider_memory_bytes
    ):
        raise ValueError("retained.unit.run-record-memory-policy-exceeded")
    if len({value.identity for value in sdk_metadata}) != len(sdk_metadata):
        raise ValueError("retained.unit.unique-sdk-metadata-required")
    for reference in sdk_metadata:
        if (
            reference.media_type not in SDK_METADATA_MEDIA_TYPES
            or reference.identity in reader.records
            and reader.records[reference.identity] != reference
        ):
            raise ValueError("retained.unit.sdk-metadata-reference-differs")
        reader.records[reference.identity] = reference
    reader._require_budget(reader.total)
    source = RetainedProjectSourceBom(
        plan,
        inputs.source_manifest,
        planning.managed_sbom_graph,
        inputs.component_locks,
    )
    source.require_content(reader.raw(receipt.root("source-bom")))
    dependencies = []
    for reference in sdk_references:
        value = reader.document(reference)
        dependency = NativeSdkDependencyEvidence(
            ContentIdentity.from_dict(value["input_identity"]),
            CycloneDxRepositorySourceResolution.from_dict(
                value["repository_resolution"]
            ),
            canonical_json_bytes(value["components"]),
            tuple(tuple(edge) for edge in value["edges"]),
        )
        if dependency.to_dict() != value:
            raise ValueError("retained.unit.sdk-dependency-assertion-differs")
        dependencies.append(dependency)
    roots, build_roots = [], ()
    for action in plan.profile.actions:
        roots.extend(action.outputs)
        if action.stage == "build":
            build_roots = tuple(roots)
    resolved = RetainedProjectResolvedBom(
        source,
        _stage_manifest(inputs.output_manifest, build_roots),
        reader.raw(BlobRef.from_dict(evidence["tool_observation"])),
        reader.raw(BlobRef.from_dict(evidence["export_observation"])),
        tuple(dependencies),
    )
    resolved.require_content(reader.raw(receipt.root("resolved-bom")))
    for name, value in (
        ("test_policy_identity", test_policy),
        ("project_test_policy_identity", test_receipt_policy),
        ("acceptance_policy_identity", acceptance),
    ):
        if (
            value is None
            or completed[name] != value.identity.uri
            or reader.record(completed[name]) != value.to_dict()
        ):
            raise ValueError("retained.unit.current-verification-policy-differs")

    def stream(identity):
        return reader.raw(reader.reference(ContentIdentity.parse_uri(identity).uri))

    observations = []
    for action, identity, manifest_ref in zip(
        plan.profile.actions,
        commands,
        evidence["command_output_manifests"],
        strict=True,
    ):
        value = reader.record(identity)
        observed = RetainedProjectCommandObservation(
            ContentIdentity.parse_uri(value["plan_identity"]),
            action,
            ContentIdentity.parse_uri(value["tool_identity"]),
            ContentIdentity.parse_uri(value["environment_identity"]),
            ContentIdentity.parse_uri(value["dependency_identity"]),
            BoundedProcessResult(
                value["exit_code"],
                stream("sha256:" + value["stdout"]),
                stream("sha256:" + value["stderr"]),
            ),
            reader.manifest(BlobRef.from_dict(manifest_ref))
            if manifest_ref is not None
            else None,
            ContentIdentity.parse_uri(value["native_sdk_inputs_identity"])
            if "native_sdk_inputs_identity" in value
            else None,
            tuple(
                ContentIdentity.parse_uri(v)
                for v in value.get("native_sdk_executions", [])
            ),
        )
        if observed.to_dict() != value or observed.identity.uri != identity:
            raise ValueError("retained.unit.command-assertion-differs")
        observations.append(observed)
    reports = []
    for identity in tests:
        value = reader.record(identity)
        observed = RetainedProjectVerifiedTestReport(
            *(
                ContentIdentity.parse_uri(value[name])
                for name in (
                    "plan_identity",
                    "policy_identity",
                    "observation_identity",
                    "blob_identity",
                )
            ),
            RetainedProjectTestReport.from_dict(value["report"]),
        )
        if observed.to_dict() != value or observed.identity.uri != identity:
            raise ValueError("retained.unit.test-assertion-differs")
        report_path = dict(test_policy.reports).get(observed.report.action_id)
        member = next(
            (
                member
                for member in inputs.output_manifest.members
                if member.path == report_path
            ),
            None,
        )
        if (
            member is None
            or member.symlink_target is not None
            or observed.blob_identity.uri != "sha256:" + member.sha256
        ):
            raise ValueError("retained.unit.test-report-output-member-differs")
        reference = BlobRef(member.sha256, member.size)
        if reference.size > min(
            16 * 1024**2, getattr(policy, "maximum_document_bytes", 64 * 1024**2)
        ):
            raise ValueError("retained.unit.test-report-document-policy-exceeded")

        def unique_object(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("retained.unit.test-report-duplicate-key")
                result[key] = item
            return result

        # Original reports follow their declared JSON protocol; whitespace and
        # object key order need not be canonical. Read the SAME named CAS blob
        # under the shared reader's byte/memory budget before parsing it.
        actual_report = RetainedProjectTestReport.from_dict(
            json.loads(reader.raw(reference), object_pairs_hook=unique_object)
        )
        if actual_report != observed.report:
            raise ValueError("retained.unit.test-report-content-differs")
        reports.append(observed)
    accepted = []
    for identity, case in zip(cases, expected_cases, strict=True):
        value = reader.record(identity)
        if (
            value["case_identity"] != case.identity.uri
            or value["exit_code"] != case.expected_exit
            or reader.reference(value["stdout_identity"]).size
            != len(case.expected_stdout)
            or reader.reference(value["stderr_identity"]).size
            != len(case.expected_stderr)
        ):
            raise ValueError("retained.completion.acceptance-observation-mismatch")
        file_observation = value.get("filesystem_observation")
        files = None
        if file_observation is not None:
            files = RetainedFilesystemObservation(
                ContentIdentity.parse_uri(file_observation["case_identity"]),
                tuple(
                    RetainedFilesystemState.from_dict(v)
                    for v in file_observation["entries"]
                ),
            )
            if files.to_dict() != file_observation:
                raise ValueError("retained.unit.filesystem-assertion-differs")
        observed = RetainedProjectAcceptanceObservation(
            *(
                ContentIdentity.parse_uri(value[name])
                for name in (
                    "plan_identity",
                    "policy_identity",
                    "case_identity",
                    "command_identity",
                    "dependency_identity",
                    "output_manifest_identity",
                    "stdout_identity",
                    "stderr_identity",
                )
            ),
            value["exit_code"],
            BoundedProcessResult(
                value["exit_code"],
                stream(value["stdout_identity"]),
                stream(value["stderr_identity"]),
            ),
            tuple(
                ContentIdentity.parse_uri(v)
                for v in value.get("native_sdk_executions", [])
            ),
            files,
            *(
                ContentIdentity.parse_uri(value[name]) if name in value else None
                for name in (
                    "filesystem_dependency_identity",
                    "filesystem_environment_identity",
                )
            ),
        )
        if observed.to_dict() != value or observed.identity.uri != identity:
            raise ValueError("retained.unit.acceptance-assertion-differs")
        accepted.append(observed)
    sdk_final = completed.get("native_sdk_final_check_identity")
    secondary = completed.get("secondary_access_identity")
    for identity in (
        *[v.uri for o in observations for v in o.native_sdk_executions],
        *[v.uri for o in accepted for v in o.native_sdk_executions],
        *((sdk_final,) if sdk_final is not None else ()),
        *((secondary,) if secondary is not None else ()),
    ):
        reader.raw(reader.reference(identity))
    reopened = RetainedProjectCompletedRun(
        source,
        resolved,
        tuple(observations),
        test_policy,
        test_receipt_policy,
        tuple(reports),
        acceptance,
        tuple(accepted),
        inputs.output_manifest,
        ContentIdentity.parse_uri(sdk_final) if sdk_final is not None else None,
        ContentIdentity.parse_uri(secondary) if secondary is not None else None,
    )
    if (
        reopened.to_dict() != completed
        or reopened.identity.uri != receipt.root("completed-run").identity
    ):
        raise ValueError("retained.unit.completed-run-assertion-differs")
    if (
        receipt.passed != sum(v.report.passed for v in reports)
        or receipt.skipped != sum(v.report.skipped for v in reports)
        or source.identity.uri != completed["source_observation_identity"]
        or resolved.identity.uri != completed["resolved_observation_identity"]
    ):
        raise ValueError("retained.unit.completed-run-summary-differs")
    if plan.profile.secondary_access is not None:
        if secondary is None:
            raise ValueError("retained.unit.secondary-cleanup-required")
        historical = RetainedSecondaryAccessEvidence.from_dict(reader.record(secondary))
        historical.require_context(
            plan,
            export_manifest_identity=resolved.output_manifest.identity,
            command_observations=tuple(value.identity for value in observations),
        )

    def read_sdk_document(identity, *, reference=None):
        expected = reader.reference(identity.uri)
        if reference is not None and reference != expected:
            raise ValueError("retained.unit.sdk-metadata-reference-differs")
        return reader.record(identity.uri)

    sdk_documents = retained_sdk_metadata_closure(
        tuple(
            sorted(
                {
                    identity
                    for observation in (*observations, *accepted)
                    for identity in observation.native_sdk_executions
                },
                key=lambda value: value.uri,
            )
        ),
        read_document=read_sdk_document,
        guard=admission_guard,
        maximum_records=min(
            1_000_000,
            policy.max_provider_metadata_bytes // 512,
            policy.max_provider_memory_bytes // 512,
        ),
    )
    workspace_roots = verify_retained_sdk_run_assertions(
        reader,
        inputs,
        reopened,
        sdk_documents,
        maximum_steps=policy.max_closure_steps,
        expected_platform=expected_sdk_platform,
        expected_architecture=expected_sdk_architecture,
        current_sdk_snapshots=current_sdk_snapshots,
    )
    if {identity.uri for identity in sdk_documents} != {
        value.identity for value in sdk_metadata
    }:
        raise ValueError("retained.unit.exact-sdk-metadata-closure-required")
    admission_guard()
    if workspace_root_observer is not None:
        workspace_root_observer(workspace_roots)
    return reopened
