"""Local retained admission and streamed CAS custody for package consumers.

Only the live execution owner can mint this local capability. A decoded receipt is
an inspectable assertion, never an authenticated remote admission or release grant.
"""

from __future__ import annotations

import stat
import tempfile
from dataclasses import replace
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.exclusive_directory import (
    directory_node,
    publish_directory_exclusive,
)
from literate_ai.adapters.retained_project_acceptance import (
    ACCEPTANCE_POLICY_PATH,
    read_retained_acceptance_policy,
)
from literate_ai.adapters.retained_project_execution import (
    RetainedProjectExecutionWorkspace,
)
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    _stream,
    verify_retained_project_inputs,
)
from literate_ai.contracts import BlobRef, canonical_json_bytes
from literate_ai.contracts.retained_project import (
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
from literate_ai.storage.cas import FileSystemCAS

_DOCUMENT_BYTES = 64 * 1024 * 1024
_METADATA_BYTES = 512 * 1024 * 1024
_LOCAL_ADMISSION = object()


def _under(path, roots):
    while path:
        if path in roots:
            return True
        path = path.rpartition("/")[0]
    return False


def _payload_manifest(run):
    selected = set(run.source_bom.plan.profile.package_inventory)
    members = tuple(m for m in run.output_manifest.members if _under(m.path, selected))
    directories = {d for d in run.output_manifest.directories if _under(d, selected)}
    for path in (*[m.path for m in members], *tuple(directories)):
        parent = path.rpartition("/")[0]
        while parent:
            directories.add(parent)
            parent = parent.rpartition("/")[0]
    roots = tuple(
        sorted({p.split("/", 1)[0] for p in (*[m.path for m in members], *directories)})
    )
    return RetainedProjectManifest(
        roots, members, run.output_manifest.limits, tuple(sorted(directories))
    )


def _file_reference(member):
    return BlobRef(member.sha256, member.size)


def _retained_member(store, root, member):
    if _stream(root, member.path, member.size) != (
        member.size,
        member.mode,
        member.sha256,
    ):
        raise ValueError("retained.receipt.member-changed")
    reference = (
        store.put_bytes(member.symlink_target.encode("utf-8"))
        if member.symlink_target is not None
        else store.put_file(root / member.path)
    )
    if reference != _file_reference(member):
        raise ValueError("retained.receipt.member-retention-mismatch")


def _metadata_reference(store, identity):
    path = store.path_for(BlobRef(identity.digest, 0))
    node = path.lstat()
    if not stat.S_ISREG(node.st_mode) or not 0 < node.st_size <= _DOCUMENT_BYTES:
        raise ValueError("retained.receipt.metadata-size-or-type")
    reference = BlobRef(identity.digest, node.st_size, media_type="application/json")
    document = store.get_manifest(reference)
    if canonical_json_bytes(document) != store.get_bytes(reference):
        raise ValueError("retained.receipt.metadata-noncanonical")
    return reference


class AdmittedRetainedProject:
    """In-process local admission with original authority and exact retained bytes.

    The capability cannot be recovered merely by parsing a receipt. Transported
    receipts need independent authentication/admission, which this owner does not
    implement. No package format, installation or release is authorized here.
    """

    def __init__(self, receipt, run, store, source, current_plan, *, _seal=None):
        if _seal is not _LOCAL_ADMISSION:
            raise ValueError("retained.receipt.live-admission-required")
        self.receipt, self.run, self.store = receipt, run, store
        self.source, self.current_plan = source, current_plan

    def require_current(self):
        plan = self.run.source_bom.plan
        if self.current_plan() != plan:
            raise ValueError("retained.receipt.plan-changed")
        verify_retained_project_inputs(self.source, self.run.source_bom.manifest)
        if (
            read_retained_acceptance_policy(
                self.source,
                plan.profile,
                plan.component_locks,
                self.run.source_bom.manifest,
            )
            != self.run.acceptance_policy
        ):
            raise ValueError("retained.receipt.verifier-policy-changed")
        reference = BlobRef(
            self.receipt.identity.digest,
            len(canonical_json_bytes(self.receipt.to_dict())),
            media_type="application/json",
        )
        if self.store.get_manifest(reference) != self.receipt.to_dict():
            raise ValueError("retained.receipt.receipt-changed")
        for item in self.receipt.records:
            self.store.verify(item)
        for manifest in (self.run.source_bom.manifest, self.run.output_manifest):
            for member in manifest.members:
                self.store.verify(_file_reference(member))
        if self.current_plan() != plan:
            raise ValueError("retained.receipt.plan-changed")

    def relocated(self, store):
        if not isinstance(store, FileSystemCAS):
            raise TypeError("retained.receipt.filesystem-store-required")
        result = AdmittedRetainedProject(
            self.receipt,
            self.run,
            store,
            self.source,
            self.current_plan,
            _seal=_LOCAL_ADMISSION,
        )
        result.require_current()
        return result

    @property
    def payload_manifest(self):
        reference = self.receipt.root("payload-manifest")
        manifest = RetainedProjectManifest.from_dict(self.store.get_manifest(reference))
        if manifest != _payload_manifest(self.run):
            raise ValueError("retained.receipt.payload-selection-changed")
        return manifest

    def materialize_payload(self, destination):
        """Publish a new exact payload tree from retained CAS, with bounded reads."""
        self.require_current()
        destination = Path(destination).absolute()
        parent = directory_node(destination.parent)
        manifest = self.payload_manifest
        stage = Path(
            tempfile.mkdtemp(prefix=".retained-payload-", dir=destination.parent)
        )
        owned = directory_node(stage)
        try:
            for directory in manifest.directories:
                (stage / directory).mkdir(parents=True, exist_ok=True)
            for member in manifest.members:
                if member.symlink_target is not None:
                    raise ValueError("retained.receipt.payload-links-not-qualified")
                target = stage / member.path
                target.parent.mkdir(parents=True, exist_ok=True)
                self.store.copy_to(_file_reference(member), target)
                target.chmod(member.mode)
            self._require_payload(stage, manifest)
            self.require_current()
            publish_directory_exclusive(
                stage, destination, expected_source=owned, expected_parent=parent
            )
            self._require_payload(destination, manifest)
            self.require_current()
            return manifest
        finally:
            if stage.exists() and directory_node(stage) == owned:
                _remove_tree(stage)

    @staticmethod
    def _require_payload(root, manifest):
        require_safe_directory(root)
        if tuple(sorted(p.name for p in root.iterdir())) != manifest.roots:
            raise ValueError("retained.receipt.extra-payload-root")
        verify_retained_project_inputs(root, manifest)


def admit_retained_project(workspace, store, *, worker_evidence=None):
    """Mint only after all live gates, retaining bytes before returning admission."""
    if not isinstance(workspace, RetainedProjectExecutionWorkspace) or not isinstance(
        store, FileSystemCAS
    ):
        raise TypeError("retained.receipt.live-workspace-and-store-required")
    run = workspace.completed_run()
    plan = run.source_bom.plan
    records = {}
    retained_bytes = 0

    def remember(content, *, media_type="application/json", expected=None):
        nonlocal retained_bytes
        if len(content) > _DOCUMENT_BYTES:
            raise ValueError("retained.receipt.document-budget")
        reference = store.put_bytes(content, media_type=media_type)
        if expected is not None and reference.identity != expected.uri:
            raise ValueError("retained.receipt.record-identity-mismatch")
        if reference.identity not in records:
            retained_bytes += reference.size
            if retained_bytes > _METADATA_BYTES:
                raise ValueError("retained.receipt.metadata-budget")
            records[reference.identity] = reference
        return reference

    def document(value, expected=None):
        return remember(canonical_json_bytes(value), expected=expected)

    if worker_evidence is not None:
        from literate_ai.adapters.retained_project_platform import (
            require_retained_worker_evidence,
        )
        from literate_ai.contracts.retained_project_worker import (
            RetainedLocalWorkerEvidence,
        )

        if not isinstance(worker_evidence, RetainedLocalWorkerEvidence):
            raise TypeError("retained.receipt.typed-worker-evidence-required")
        require_retained_worker_evidence(
            worker_evidence, plan, run.source_bom.component_locks
        )
        document(worker_evidence.to_dict(), worker_evidence.identity)

    roots = {
        "plan": document(plan.to_dict(), plan.identity),
        "completed-run": document(run.to_dict(), run.identity),
        "input-manifest": document(
            run.source_bom.manifest.to_dict(), run.source_bom.manifest.identity
        ),
        "output-manifest": document(
            run.output_manifest.to_dict(), run.output_manifest.identity
        ),
        "payload-manifest": document(_payload_manifest(run).to_dict()),
        "source-bom": remember(
            run.source_bom.content,
            media_type="application/vnd.cyclonedx+json",
            expected=run.source_bom.binding.bom_identity,
        ),
        "resolved-bom": remember(
            run.resolved_bom.content,
            media_type="application/vnd.cyclonedx+json",
            expected=run.resolved_bom.binding.bom_identity,
        ),
    }
    # Input capture already streamed and sealed these original bytes, including
    # relative link text. Never follow source links or copy executable hardlinks.
    for member in run.source_bom.manifest.members:
        reference = store.put_file(workspace.inputs.parent / "blobs" / member.sha256)
        if reference != _file_reference(member):
            raise ValueError("retained.receipt.input-retention-mismatch")
    for member in run.output_manifest.members:
        _retained_member(store, workspace.outputs, member)
    policy_path = workspace.source / ACCEPTANCE_POLICY_PATH
    policy = RetainedProjectMember(
        ACCEPTANCE_POLICY_PATH,
        policy_path.lstat().st_size,
        0o644,
        plan.profile.acceptance_policy_identity.digest,
        "verifier-policy",
    )
    _retained_member(store, workspace.source, policy)
    roots["acceptance-policy-bytes"] = BlobRef(policy.sha256, policy.size)
    records["sha256:" + policy.sha256] = roots["acceptance-policy-bytes"]
    document(run.test_policy.to_dict(), run.test_policy.identity)
    document(run.configured_test_policy.to_dict(), run.configured_test_policy.identity)
    document(run.acceptance_policy.to_dict(), run.acceptance_policy.identity)
    for observation in (*run.observations, *run.acceptance_observations):
        document(observation.to_dict(), observation.identity)
        remember(observation.result.stdout, media_type="application/octet-stream")
        remember(observation.result.stderr, media_type="application/octet-stream")
    for report in run.test_reports:
        document(report.to_dict(), report.identity)
    current = workspace.completed_run()
    if (
        replace(
            current, native_sdk_final_check_identity=run.native_sdk_final_check_identity
        )
        != run
    ):
        raise ValueError("retained.receipt.run-changed-during-retention")
    run = current
    previous_run = roots["completed-run"]
    roots["completed-run"] = document(run.to_dict(), run.identity)
    if previous_run != roots["completed-run"]:
        records.pop(previous_run.identity)
    sdk_ids = {
        identity
        for observation in (*run.observations, *run.acceptance_observations)
        for identity in observation.native_sdk_executions
    }
    if run.native_sdk_final_check_identity is not None:
        sdk_ids.add(run.native_sdk_final_check_identity)
    for identity in sorted(sdk_ids, key=lambda v: v.uri):
        reference = _metadata_reference(store, identity)
        remember(store.get_bytes(reference), expected=identity)
    receipt = RetainedProjectReceipt(
        plan.project_id,
        tuple(sorted(roots.items())),
        tuple(records[k] for k in sorted(records)),
        sum(report.report.passed for report in run.test_reports),
        sum(report.report.skipped for report in run.test_reports),
    )
    workspace._require_current()
    document(receipt.to_dict(), receipt.identity)
    admission = AdmittedRetainedProject(
        receipt,
        run,
        store,
        workspace.source,
        workspace.current_plan,
        _seal=_LOCAL_ADMISSION,
    )
    admission.require_current()
    return admission
