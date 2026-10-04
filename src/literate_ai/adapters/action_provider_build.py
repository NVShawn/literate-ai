"""Historical provider build/artifact transfer under current consumer authority."""

from literate_ai.adapters.action_build_result import (
    MAX_BUILD_ARCHIVE_BYTES,
    MAX_BUILD_ARCHIVE_FILES,
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
    _capture_files,
    _verify_files,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_provider_record import (
    ProviderBuildTransfer,
    validate_provider_transfers,
)
from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.directory_artifacts import (
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    verify_qualification_build,
    verify_qualification_execution,
    verify_qualification_generated_tests,
    verify_qualification_suite_membership,
)
from literate_ai.contracts import (
    ContentIdentity,
    CycloneDxLifecycle,
    GeneratedSourceCandidate,
    StandardComponentAcceptanceEvidence,
    canonical_json_bytes,
)
from literate_ai.storage.cas import BlobNotFoundError


def _invalid():
    raise ActionWireError(
        "action_build.provider_invalid", "provider build transfer differs"
    )


class _ProviderEvidenceReader(QualificationEvidenceReader):
    """Track the exact verified proof closure, excluding unrelated retained records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opened = set()

    def read_bytes(self, identity):
        content = super().read_bytes(identity)
        self.opened.add(identity)
        return content


def verify_accepted_component_records(records, receipt, validation, generation_plan):
    """Reopen source, BUILD, TEST, EXECUTE and fixed-policy acceptance proof."""
    if not isinstance(receipt, StandardComponentAcceptanceEvidence):
        _invalid()
    from literate_ai.application.standard_project_lifecycle import (
        StandardComponentBuildPlan,
    )

    reader = _ProviderEvidenceReader(
        records,
        max_bytes=MAX_BUILD_EVIDENCE_BYTES,
        max_records=MAX_BUILD_EVIDENCE_RECORDS,
    )
    for evidence in (
        receipt,
        receipt.build,
        receipt.generated_tests,
        receipt.execution,
    ):
        if reader.read_bytes(evidence.identity) != canonical_json_bytes(
            evidence.to_dict()
        ):
            _invalid()
    if reader.read_json(receipt.acceptance_policy_identity) != {
        "schema": "literate-ai/local-standard-acceptance-policy@1",
        "requires": ["build", "generated-tests", "execution"],
    }:
        _invalid()
    custody = reader.read_json(receipt.build.source_custody_identity)
    if (
        not isinstance(custody, dict)
        or custody.get("schema") != "literate-ai/local-generated-source-custody@1"
        or custody.get("source_generation_identity")
        != receipt.source_generation_identity.uri
        or custody.get("source_tree_identity") != receipt.build.source_tree_identity.uri
        or custody.get("generated_test_suite_identity")
        != receipt.generated_test_suite_identity.uri
    ):
        _invalid()
    candidate = GeneratedSourceCandidate.from_dict(
        reader.read_json(ContentIdentity.parse_uri(custody["candidate_identity"]))
    )
    if (
        candidate.identity.uri != custody["candidate_identity"]
        or candidate.source_bom_identity != receipt.build.source_sbom.bom_identity
        or candidate.generated_test_suite_identity
        != receipt.generated_test_suite_identity
        or candidate.component_revision != receipt.component_revision
        or candidate.tree_identity != receipt.build.source_tree_identity
        or candidate.component_generation_plan_identity != generation_plan.identity
        or candidate.generation_key_identity != generation_plan.generation_key.identity
        or generation_plan.component_revision != receipt.component_revision
        or custody.get("managed_graph_identity")
        != validation.managed_graph.identity.uri
    ):
        _invalid()
    source_content = reader.read_bytes(receipt.build.source_sbom.bom_identity)
    bom, suite = validation.validate(
        source_content, reader.read_bytes(receipt.generated_test_suite_identity)
    )
    resolved = validate_cyclonedx_bom(
        reader.read_bytes(receipt.build.resolved_sbom.bom_identity),
        lifecycle=CycloneDxLifecycle.RESOLVED,
        managed_graph=validation.managed_graph,
        source_content=source_content,
        source_managed_graph=validation.managed_graph,
    )
    if bom != receipt.build.source_sbom or resolved != receipt.build.resolved_sbom:
        _invalid()
    verify_qualification_suite_membership(
        reader, suite=suite, tests=receipt.generated_tests
    )
    plan = StandardComponentBuildPlan.from_dict(
        reader.read_json(receipt.build.build_plan_identity)
    )
    verify_qualification_build(reader, plan=plan, build=receipt.build)
    verify_qualification_generated_tests(
        reader,
        build_plan_identity=plan.identity,
        build=receipt.build,
        tests=receipt.generated_tests,
    )
    verify_qualification_execution(
        reader,
        build_plan_identity=plan.identity,
        execution=receipt.execution,
    )
    return reader


def capture_provider_build(
    *,
    receipt,
    artifact_root,
    records,
    cas,
    deadline,
    require_current,
    source_validation,
    generation_plan,
):
    """Capture historical build data; the caller supplies trusted accepted custody."""

    def guard():
        deadline.remaining()
        require_current()
        deadline.remaining()

    guard()
    reader = verify_accepted_component_records(
        records, receipt, source_validation, generation_plan
    )
    files, custody = _capture_files(artifact_root, guard)
    _verify_files(files, receipt.build, reader)
    archive = encode_directory_export(
        files,
        max_bytes=MAX_BUILD_ARCHIVE_BYTES,
        max_entries=MAX_BUILD_ARCHIVE_FILES,
    )
    guard()
    archive_ref = cas.put_bytes(archive)
    refs = []
    for identity, content in records:
        if identity not in reader.opened:
            continue
        guard()
        if len(content) > MAX_ACTION_RECORD_BYTES:
            _invalid()
        reference = cas.put_bytes(content)
        if reference.identity != identity.uri:
            _invalid()
        refs.append(reference)
    custody.require_unchanged()
    guard()
    result = ProviderBuildTransfer(
        receipt.identity, archive_ref, tuple(refs), source_validation
    )
    validate_provider_transfers((receipt,), (result,))
    return result


def read_provider_build(
    *,
    transfer,
    receipt,
    cas,
    deadline,
    require_current,
    generation_plan,
    blob_source=None,
):
    """Reopen exact proof and artifact bytes without executing provider commands."""

    def guard():
        deadline.remaining()
        require_current()
        deadline.remaining()

    guard()
    validate_provider_transfers((receipt,), (transfer,))

    def fetch(reference):
        guard()
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            content = blob_source(reference)
            guard()
            if (
                not isinstance(content, bytes)
                or len(content) != reference.size
                or record_identity(content).uri != reference.identity
            ):
                _invalid()
            if cas.put_bytes(content, media_type=reference.media_type) != reference:
                _invalid()
        content = cas.get_bytes(reference)
        guard()
        return content

    records = tuple(
        (ContentIdentity.parse_uri(ref.identity), fetch(ref))
        for ref in transfer.evidence_records
    )
    reader = verify_accepted_component_records(
        records, receipt, transfer.source_validation, generation_plan
    )
    files = read_directory_export(
        fetch(transfer.artifact_archive),
        transfer.artifact_archive,
        max_bytes=MAX_BUILD_ARCHIVE_BYTES,
        max_entries=MAX_BUILD_ARCHIVE_FILES,
    )
    _verify_files(files, receipt.build, reader)
    guard()
    return files, reader
