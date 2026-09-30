"""Publish a freshly verified map at the project's configured receipt boundary."""

import os
import tempfile

from literate_ai.application.current_evidence import verify_current_evidence
from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import ProjectError
from literate_ai.security.evidence.current import CurrentEvidenceMap
from literate_ai.security.evidence.graph import EvidenceGraphLimits
from literate_ai.test_receipts import (
    MAXIMUM_PROJECT_TEST_RECEIPT_BYTES,
    _canonical_tracked_bytes,
    _parse_current_map,
    _parse_tracked,
    _read_bounded,
    _target,
)

from .lifecycle_lock import project_lifecycle_lock

_DEFAULT_LIMITS = EvidenceGraphLimits()


def _existing(target):
    if not target.exists() and not target.is_symlink():
        return None
    content = _read_bounded(
        target, code="evidence.current.pointer-invalid", label="current receipt"
    )
    current = _parse_current_map(content)
    if current is None:
        receipt, finalized = _parse_tracked(
            content, code="evidence.current.pointer-invalid", label="current receipt"
        )
        if content != _canonical_tracked_bytes(receipt, finalized):
            raise ValueError("evidence.current.pointer-not-canonical")
    return content


def publish_current_evidence(
    project,
    current: CurrentEvidenceMap,
    *,
    plan,
    policy,
    resolver,
    bundle_store,
    current_state,
    project_authority,
    current_policy,
    limits=_DEFAULT_LIMITS,
):
    """Atomically install an index after fresh verification under the lifecycle lock.

    All stores and authority providers are trusted, independent caller inputs.
    The bundle must already be retained. This operation never grants execution;
    readers must reverify the map against their own current authority on each use.
    """

    if not isinstance(current, CurrentEvidenceMap):
        raise ValueError("evidence.current.invalid")
    candidate = canonical_json_bytes(current.to_dict()) + b"\n"
    if len(candidate) > MAXIMUM_PROJECT_TEST_RECEIPT_BYTES:
        raise ValueError("evidence.current.map-limit")
    with project_lifecycle_lock(
        project.root, operation="test-receipt.publish-evidence"
    ):
        if current_policy() != policy:
            raise ValueError("evidence.current.policy-changed")
        target = _target(project, create_parent=True)
        if target is None:
            raise ProjectError(
                "project.test_receipt_unconfigured", "project has no receipt path"
            )
        before = _existing(target)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = target.parent / os.path.basename(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            # Even an identical map must pass fresh signature, expiry, revocation,
            # receipt finalization and original signed-store availability checks.
            checked = verify_current_evidence(
                project,
                current,
                plan=plan,
                policy=policy,
                resolver=resolver,
                bundle_store=bundle_store,
                current_state=current_state,
                project_authority=project_authority,
                limits=limits,
            )
            if (
                current_policy() != policy
                or project_authority() != plan.project_authority
            ):
                raise ValueError("evidence.current.authority-changed")
            if (
                _read_bounded(
                    temporary,
                    code="evidence.current.staging-invalid",
                    label="staged current map",
                )
                != candidate
            ):
                raise ValueError("evidence.current.staging-changed")
            if (
                _target(project, create_parent=False) != target
                or _existing(target) != before
            ):
                raise ValueError("evidence.current.pointer-changed")
            if before != candidate:
                os.replace(temporary, target)
            return {
                "path": target.relative_to(project.root).as_posix(),
                "current_map_identity": current.identity.uri,
                "verification_identity": checked.verification.identity.uri,
                "updated": before != candidate,
                "authenticated": True,
                "execution_authorized": False,
            }
        finally:
            temporary.unlink(missing_ok=True)
