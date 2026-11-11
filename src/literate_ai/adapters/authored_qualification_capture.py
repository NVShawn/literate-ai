"""Capture authored library bytes under their original execution owners.

Membership reopening is bounded, model-free integrity checking. It does not
reopen native stage authority, an independent verifier, or importer trust and
cannot publish a positive authored qualification reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.adapters.authored_qualification_execution import (
    CurrentAuthoredQualificationExecution,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    capture_qualification_product_blobs,
)
from literate_ai.contracts import ComponentAuthoring, ComponentLock
from literate_ai.contracts.authored_qualification_products import (
    AuthoredQualificationProducts,
    AuthoredQualificationRunProducts,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationResult,
    AuthoredQualificationRun,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet


@dataclass(frozen=True, slots=True)
class AuthoredQualificationCapture:
    products: AuthoredQualificationProducts
    records: tuple[tuple[ContentIdentity, bytes], ...]


@dataclass(frozen=True, slots=True)
class ReopenedAuthoredProductMembership:
    products: AuthoredQualificationProducts
    result: AuthoredQualificationResult
    exports: tuple[tuple[ContentIdentity, RetainedLibraryExportSet], ...]


def _join_result_lock(result, lock):
    if (
        lock.identity != result.plan.origin.component_lock_identity
        or lock.root_revision != result.plan.execution_plan.root_revision
        or {n.revision.identity for n in lock.nodes}
        != {p.component_revision for p in result.plan.execution_plan.generation_plans}
    ):
        raise QualificationCaptureError("qualification.authored.capture_lock_mismatch")


def _acceptances(run, exports, target):
    if (
        exports.graph != run.root.artifact_graph
        or exports.link_plan_identity != run.root.link_plan.identity
        or run.root.package_plan.target_identity != target
        or {p.artifact_export.component_revision for p in exports.libraries}
        != {n.component_revision for n in run.nodes}
    ):
        raise QualificationCaptureError(
            "qualification.authored.capture_product_mismatch"
        )
    return tuple(
        sorted((n.acceptance for n in run.nodes), key=lambda a: a.identity.uri)
    )


def _join_run_selections(reader, result):
    for ordinal, run in enumerate(result.runs):
        selection = reader.read_json(run.request.run_identity)
        if (
            not isinstance(selection, dict)
            or set(selection)
            != {"schema", "plan_identity", "session_identity", "ordinal"}
            or selection["schema"]
            != "literate-ai/authored-qualification-run-selection@1"
            or selection["plan_identity"] != result.plan.identity.uri
            or type(selection["ordinal"]) is not int
            or selection["ordinal"] != ordinal
        ):
            raise QualificationCaptureError(
                "qualification.authored.capture_selection_mismatch"
            )
        try:
            session = ContentIdentity.parse_uri(selection["session_identity"])
        except (TypeError, ValueError) as exc:
            raise QualificationCaptureError(
                "qualification.authored.capture_selection_mismatch"
            ) from exc
        allocation = canonical_identity(
            {
                "schema": "literate-ai/qualification-workspace-allocation@1",
                "session_identity": session.uri,
                "run_identity": run.request.run_identity.uri,
            }
        )
        if allocation != run.request.workspace_allocation_identity:
            raise QualificationCaptureError(
                "qualification.authored.capture_selection_mismatch"
            )


def capture_authored_qualification_products(owner):
    """Copy products before any original namespace or package owner expires."""

    if not isinstance(owner, CurrentAuthoredQualificationExecution):
        raise TypeError("original authored execution/capture owner required")
    owner.require_unchanged()
    result, lifecycle, recorder = owner.result, owner._lifecycle, owner._recorder
    if not isinstance(result, AuthoredQualificationResult):
        raise QualificationCaptureError("qualification.authored.capture_result_invalid")
    current = lifecycle.authored_plan
    lock = current.oracles.authority.locked.authority.lock
    _join_result_lock(result, lock)
    _join_run_selections(
        QualificationEvidenceReader(
            recorder.entries,
            max_bytes=recorder.max_bytes,
            max_records=recorder.max_records,
        ),
        result,
    )
    if current.plan != result.plan or set(lifecycle.product_sources) != {
        r.request.run_identity.uri for r in result.runs
    }:
        raise QualificationCaptureError("qualification.authored.capture_run_mismatch")
    bindings = {b.component_revision_identity: b for b in current.oracles.bindings}
    recorder.remember_json(lock.to_dict())
    for node in lock.nodes:
        if (
            recorder.remember_json(node.revision.definition.to_dict())
            != node.revision.authoring_identity
        ):
            raise QualificationCaptureError(
                "qualification.authored.capture_lock_mismatch"
            )
    recorder.remember_json(result.plan.to_dict())
    recorder.remember_json(result.to_dict())
    pending = []
    references = {}
    for run in result.runs:
        owner.require_unchanged()
        execution, ports = lifecycle.product_sources[run.request.run_identity.uri]
        integration = execution.lifecycle.root_integration
        if (
            execution.lifecycle.identity != run.lifecycle_result_identity
            or execution.receipt != run.receipt
            or integration != run.root
            or {
                n.component_revision: n.identity
                for n in execution.lifecycle.node_results
            }
            != {n.component_revision: n.node_result_identity for n in run.nodes}
        ):
            raise QualificationCaptureError(
                "qualification.authored.capture_run_mismatch"
            )
        ports.project_package_custody(
            integration.package_plan, integration.package_result
        )
        libraries = []
        for manifest in integration.artifact_graph.manifests:
            for export in manifest.exports:
                if export.role != "library":
                    continue
                contract = ports.contracts[export.component_revision.uri]
                product = LibraryArtifactProduct(
                    export,
                    contract.library_import_surface,
                    getattr(contract, "native_layout", None),
                )
                binding = bindings.get(export.component_revision)
                if (
                    binding is None
                    or product.import_surface.identity
                    != binding.oracle.import_surface_identity
                ):
                    raise QualificationCaptureError(
                        "qualification.authored.capture_surface_mismatch"
                    )
                libraries.append(product)
        exports = RetainedLibraryExportSet(
            integration.artifact_graph,
            integration.link_plan.identity,
            tuple(sorted(libraries, key=lambda p: p.artifact_export.identity.uri)),
        )
        accepted = _acceptances(run, exports, lock.target_profile_identity)
        for record in (
            run,
            exports,
            *libraries,
            *(p.import_surface for p in libraries),
            *accepted,
        ):
            recorder.remember_json(record.to_dict())
        for manifest in exports.graph.manifests:
            for export in manifest.exports:
                reference = export.blob
                identity = ContentIdentity.parse_uri(reference.identity)
                prior = references.get(identity)
                if prior is not None and prior.size != reference.size:
                    raise QualificationCaptureError(
                        "qualification.capture.blob-mismatch"
                    )
                references[identity] = reference
        pending.append((run, exports, accepted, ports))

    # Count physical records once before calling any artifact blob reader.
    # Repeated exports still retain their separate per-run execution membership.
    products = AuthoredQualificationProducts(
        result.plan.origin,
        result.identity,
        tuple(
            sorted(
                (
                    AuthoredQualificationRunProducts(
                        run.request.run_identity,
                        run.identity,
                        exports.identity,
                    )
                    for run, exports, _accepted, _ports in pending
                ),
                key=lambda r: r.run_identity.uri,
            )
        ),
    )
    existing = dict(recorder.entries)
    missing = {i: r for i, r in references.items() if i not in existing}
    sizes = {i: r.size for i, r in missing.items()}
    if products.identity not in existing:
        size = len(canonical_json_bytes(products.to_dict()))
        if products.identity in sizes and sizes[products.identity] != size:
            raise QualificationCaptureError("qualification.capture.blob-mismatch")
        sizes[products.identity] = size
    if sum(sizes.values()) > recorder.max_bytes - recorder.retained_bytes:
        raise QualificationCaptureError("qualification.capture.byte-limit")
    if len(existing) + len(sizes) > recorder.max_records:
        raise QualificationCaptureError("qualification.capture.record-limit")
    for identity, reference in references.items():
        if identity in existing and len(existing[identity]) != reference.size:
            raise QualificationCaptureError("qualification.capture.blob-mismatch")
    for _run, exports, accepted, ports in pending:
        owner.require_unchanged()

        def read(reference, source=ports):
            owner.require_unchanged()
            identity = ContentIdentity.parse_uri(reference.identity)
            if identity in existing:
                return existing[identity]
            payload = source.read_artifact_blob(reference)
            owner.require_unchanged()
            return payload

        captured = capture_qualification_product_blobs(
            exports,
            accepted,
            target_profile_identity=lock.target_profile_identity,
            read_blob=read,
            max_bytes=recorder.max_bytes,
        )
        for reference, payload in captured:
            identity = recorder.remember_bytes(payload)
            if identity.uri != reference.identity:
                raise QualificationCaptureError("qualification.capture.blob-mismatch")
            existing[identity] = payload
    owner.require_unchanged()
    recorder.remember_json(products.to_dict())
    owner.require_unchanged()
    return AuthoredQualificationCapture(products, recorder.entries)


def reopen_authored_product_membership(reader, products_identity):
    """Check exact bytes and membership, without qualification or importer trust."""

    reopened = _reopen_authored_product_records(reader, products_identity)
    for run_identity, exports in reopened.exports:
        run = next(
            r for r in reopened.result.runs if r.request.run_identity == run_identity
        )
        capture_qualification_product_blobs(
            exports,
            _acceptances(run, exports, run.root.package_plan.target_identity),
            target_profile_identity=run.root.package_plan.target_identity,
            read_blob=lambda ref: reader.read_bytes(
                ContentIdentity.parse_uri(ref.identity)
            ),
            max_bytes=reader.max_bytes,
        )
    return reopened


def _reopen_authored_product_records(reader, products_identity):
    """Decode metadata first so semantic callers can refuse before product reads.

    No current owner, native observations, product bytes or admission is supplied.
    """

    if not isinstance(reader, QualificationEvidenceReader) or not isinstance(
        products_identity, ContentIdentity
    ):
        raise TypeError("bounded evidence reader and products identity required")
    products = AuthoredQualificationProducts.from_dict(
        reader.read_json(products_identity)
    )
    result = AuthoredQualificationResult.from_dict(
        reader.read_json(products.qualification_identity)
    )
    raw_lock = reader.read_json(products.origin.component_lock_identity)
    try:
        nodes = raw_lock["nodes"]
        if not isinstance(nodes, list) or len(nodes) > reader.max_records:
            raise ValueError("invalid lock node membership")
        identities = {
            ContentIdentity.from_dict(n["revision"]["authoring_identity"])
            for n in nodes
        }
        definitions = []
        for identity in sorted(identities, key=lambda i: i.uri):
            definition = ComponentAuthoring.from_dict(reader.read_json(identity))
            if definition.identity != identity:
                raise ValueError("authoring identity mismatch")
            definitions.append(definition)
        lock = ComponentLock.from_dict(raw_lock, authorings=tuple(definitions))
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationCaptureError(
            "qualification.authored.capture_lock_mismatch"
        ) from exc
    if (
        products.identity != products_identity
        or result.identity != products.qualification_identity
        or products.origin != result.plan.origin
    ):
        raise QualificationCaptureError(
            "qualification.authored.capture_result_mismatch"
        )
    _join_result_lock(result, lock)
    _join_run_selections(reader, result)
    expected = {r.request.run_identity: r for r in result.runs}
    if {r.run_identity for r in products.runs} != set(expected):
        raise QualificationCaptureError("qualification.authored.capture_run_mismatch")
    exports_by_run = []
    for member in products.runs:
        run = AuthoredQualificationRun.from_dict(
            reader.read_json(member.run_record_identity)
        )
        if (
            run.identity != member.run_record_identity
            or run != expected[member.run_identity]
        ):
            raise QualificationCaptureError(
                "qualification.authored.capture_run_mismatch"
            )
        exports = RetainedLibraryExportSet.from_dict(
            reader.read_json(member.export_set_identity)
        )
        if exports.identity != member.export_set_identity:
            raise QualificationCaptureError(
                "qualification.authored.capture_product_mismatch"
            )
        _acceptances(run, exports, lock.target_profile_identity)
        exports_by_run.append((member.run_identity, exports))
    return ReopenedAuthoredProductMembership(products, result, tuple(exports_by_run))
