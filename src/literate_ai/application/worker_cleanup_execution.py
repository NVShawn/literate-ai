"""Exact cleanup proposals and authorization-gated supported-tool execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from literate_ai.application.worker_cleanup import (
    CleanupCandidate,
    CleanupRoot,
    CleanupScanPolicy,
    _candidate_id,
    investigate_cleanup_candidates,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class CleanupTarget:
    candidate: CleanupCandidate
    path: Path | str
    root: CleanupRoot


@dataclass(frozen=True, slots=True)
class CleanupProposal:
    worker_id: str
    policy_identity: ContentIdentity
    created_at_ms: int
    targets: tuple[CleanupTarget, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/worker-cleanup-proposal@1",
                "worker_id": self.worker_id,
                "policy_identity": self.policy_identity.uri,
                "created_at_ms": self.created_at_ms,
                "candidates": [item.candidate.to_dict() for item in self.targets],
            }
        )


@dataclass(frozen=True, slots=True)
class CleanupAuthorization:
    worker_id: str
    policy_identity: ContentIdentity
    proposal_identity: ContentIdentity
    proposal_created_at_ms: int
    target_ids: tuple[str, ...]
    operation: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    candidate_id: str
    root: str
    estimated_bytes: int
    available_before: int
    available_after: int
    recovered_bytes: int
    recovery: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "root": self.root,
            "estimated_bytes": self.estimated_bytes,
            "available_before": self.available_before,
            "available_after": self.available_after,
            "recovered_bytes": self.recovered_bytes,
            "recovery": self.recovery,
        }


def create_cleanup_proposal(
    policy: CleanupScanPolicy,
    *,
    worker_id: str,
    policy_identity: ContentIdentity,
    created_at_ms: int,
) -> CleanupProposal:
    """Retain private exact targets only for complete, proved-inactive candidates."""

    investigation = investigate_cleanup_candidates(policy)
    if investigation.status != "complete":
        raise ValueError("worker.cleanup_investigation_incomplete")
    roots = {root.alias: root for root in policy.roots}
    targets: list[CleanupTarget] = []
    for candidate in investigation.candidates:
        if candidate.active_use != "inactive":
            continue
        root = roots[candidate.root]
        root_path = Path(root.path)
        matches = [
            child
            for child in root_path.iterdir()
            if _candidate_id(root, child.relative_to(root_path).as_posix())
            == candidate.candidate_id
        ]
        if len(matches) != 1:
            raise ValueError("worker.cleanup_candidate_changed")
        targets.append(CleanupTarget(candidate, matches[0], root))
    return CleanupProposal(
        worker_id,
        policy_identity,
        created_at_ms,
        tuple(sorted(targets, key=lambda item: item.candidate.candidate_id)),
    )


def create_remote_cleanup_proposal(
    policy: CleanupScanPolicy,
    investigation,
    *,
    worker_id: str,
    policy_identity: ContentIdentity,
    created_at_ms: int,
) -> CleanupProposal:
    """Create a non-executable proposal from bound remote investigation evidence."""

    if investigation.status != "complete":
        raise ValueError("worker.cleanup_investigation_incomplete")
    roots = {root.alias: root for root in policy.roots}
    targets = []
    seen = set()
    for candidate in investigation.candidates:
        root = roots.get(candidate.root)
        if (
            root is None
            or candidate.candidate_id in seen
            or candidate.ownership != root.ownership
        ):
            raise ValueError("worker.cleanup_candidate_invalid")
        seen.add(candidate.candidate_id)
        if candidate.active_use == "inactive":
            targets.append(CleanupTarget(candidate, root.path, root))
    return CleanupProposal(
        worker_id,
        policy_identity,
        created_at_ms,
        tuple(sorted(targets, key=lambda item: item.candidate.candidate_id)),
    )


def execute_authorized_cleanup(
    proposal: CleanupProposal,
    authorization: CleanupAuthorization,
    *,
    now_ms: int,
    measure_available,
    runner=subprocess.run,
    timeout_seconds: int = 60,
) -> tuple[CleanupReceipt, ...]:
    """Run only configured exact-target tools, then report measured capacity change."""

    expected_ids = tuple(item.candidate.candidate_id for item in proposal.targets)
    if (
        authorization.worker_id != proposal.worker_id
        or authorization.policy_identity != proposal.policy_identity
        or authorization.proposal_identity != proposal.identity
        or authorization.proposal_created_at_ms != proposal.created_at_ms
        or authorization.target_ids != expected_ids
        or authorization.operation != "configured-cleanup-tool"
        or now_ms >= authorization.expires_at_ms
    ):
        raise ValueError("worker.cleanup_authorization_invalid")
    receipts = []
    for target in proposal.targets:
        current = create_cleanup_proposal(
            CleanupScanPolicy((target.root,), 60000, 100000, 16, 0),
            worker_id=proposal.worker_id,
            policy_identity=proposal.policy_identity,
            created_at_ms=proposal.created_at_ms,
        )
        current_target = next(
            (
                item
                for item in current.targets
                if item.candidate.candidate_id == target.candidate.candidate_id
            ),
            None,
        )
        if (
            current_target is None
            or current_target.candidate.bytes != target.candidate.bytes
            or current_target.candidate.entries != target.candidate.entries
            or current_target.path != target.path
        ):
            raise ValueError("worker.cleanup_candidate_changed")
        before = measure_available(target.root.alias)
        command = tuple(
            str(target.path) if argument == "{target}" else argument
            for argument in target.root.cleanup_command
        )
        completed = runner(
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise ValueError("worker.cleanup_tool_failed")
        after = measure_available(target.root.alias)
        receipts.append(
            CleanupReceipt(
                target.candidate.candidate_id,
                target.root.alias,
                target.candidate.bytes,
                before,
                after,
                max(0, after - before),
                target.root.recovery,
            )
        )
    return tuple(receipts)


__all__ = [
    "CleanupAuthorization",
    "CleanupProposal",
    "CleanupReceipt",
    "CleanupTarget",
    "create_cleanup_proposal",
    "create_remote_cleanup_proposal",
    "execute_authorized_cleanup",
]
