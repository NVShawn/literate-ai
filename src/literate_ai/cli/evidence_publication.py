"""Retain a verified graph and atomically publish its compact current map."""

import time
from pathlib import Path

from literate_ai.adapters.current_evidence import publish_current_evidence
from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.adapters.lifecycle_lock import ProjectLifecycleLockError
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.application.current_evidence import (
    prepare_current_evidence,
    retain_prepared_current_evidence,
)
from literate_ai.application.evidence_resolution import ConfiguredEvidenceResolver
from literate_ai.projects import discover_project
from literate_ai.security.evidence.graph import (
    EvidenceGraphLimits,
    EvidenceVerificationState,
)
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.trust import EvidenceRevocations, EvidenceTrustPolicy

from .errors import CliFailure
from .evidence_verification import _document, _retention, _stores


def publish_evidence_from_args(args):
    try:
        selected = Path(args.project)
        plan = EvidenceVerificationPlan.from_dict(_document(args.plan))
        policy = EvidenceTrustPolicy.from_dict(_document(args.policy))
        project = discover_project(selected)
        if project is None:
            raise ValueError("project not found")

        def current_policy():
            return EvidenceTrustPolicy.from_dict(_document(args.policy))

        def authority():
            if (
                EvidenceVerificationPlan.from_dict(_document(args.plan)) != plan
                or current_policy() != policy
            ):
                raise ValueError("evidence.current.authority-changed")
            return validated_project_authority_identity(
                selected, synchronize_source_intelligence=False
            )

        def state():
            return EvidenceVerificationState(
                int(time.time()),
                EvidenceRevocations.from_dict(_document(args.revocations)),
            )

        limits = EvidenceGraphLimits()
        resolver = ConfiguredEvidenceResolver(_stores(args, plan.root))
        prepared = prepare_current_evidence(
            project,
            plan=plan,
            policy=policy,
            resolver=resolver,
            retention=_retention(args.retention, limits),
            current_state=state,
            project_authority=authority,
            limits=limits,
        )
        # Publication owns only this explicit immutable bundle and the configured
        # receipt pointer. Failed later admission can leave unreferenced CAS bytes.
        bundle = FileSystemEvidenceStore(Path(args.bundle_store), writable=True)
        retain_prepared_current_evidence(prepared, bundle, limits=limits.reads)
        result = publish_current_evidence(
            project,
            prepared.current,
            plan=plan,
            policy=policy,
            resolver=resolver,
            bundle_store=bundle,
            current_state=state,
            project_authority=authority,
            current_policy=current_policy,
            limits=limits,
        )
        return {"schema": "literate-ai/evidence-publication-result@1", **result}
    except CliFailure:
        raise
    except (ValueError, OSError, RecursionError, ProjectLifecycleLockError) as error:
        raise CliFailure(
            getattr(error, "code", "evidence.publication.refused"),
            "current evidence publication refused",
        ) from None
