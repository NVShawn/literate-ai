"""Read-only retained graph verification from explicit independent operator inputs."""

import json
import time
from copy import copy
from pathlib import Path

from literate_ai.adapters.evidence_storage import (
    FileSystemEvidenceStore,
    HttpsEvidenceStore,
    MonorepoEvidenceStore,
)
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.application.current_evidence import verify_current_evidence
from literate_ai.application.evidence_resolution import ConfiguredEvidenceResolver
from literate_ai.application.evidence_verification import verify_retained_evidence_graph
from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import discover_project
from literate_ai.security.evidence.current import CurrentEvidenceMap
from literate_ai.security.evidence.dsse import MAX_ENVELOPE_BYTES, DsseEnvelope
from literate_ai.security.evidence.graph import (
    EvidenceGraphLimits,
    EvidenceVerificationState,
)
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.records import EvidenceLocator
from literate_ai.security.evidence.retention import SignedEvidenceRetention
from literate_ai.security.evidence.statements import EvidenceStatement
from literate_ai.security.evidence.trust import EvidenceRevocations, EvidenceTrustPolicy
from literate_ai.test_receipts import _read_bounded, _target

from .errors import CliFailure


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("nonfinite JSON")


def _document(path):
    content = _read_bounded(
        Path(path),
        code="evidence.verification.input-invalid",
        label="verification input",
        maximum_bytes=4 * 1024 * 1024,
    )
    return _json_document(content)


def _json_document(content):
    document = json.loads(
        content, object_pairs_hook=_pairs, parse_constant=_invalid_constant
    )
    pending = [(document, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON depth exceeded")
        if isinstance(value, dict):
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)
    return document


def _current_map(path):
    content = _read_bounded(
        Path(path),
        code="evidence.current.map-invalid",
        label="current evidence map",
        maximum_bytes=256 * 1024,
    )
    current = CurrentEvidenceMap.from_dict(_json_document(content))
    if content != canonical_json_bytes(current.to_dict()) + b"\n":
        raise ValueError("evidence.current.map-not-canonical")
    return current


def _stores(args, root):
    result = {}
    for items, factory in (
        (args.store, lambda p: FileSystemEvidenceStore(Path(p))),
        (args.monorepo_store, lambda p: MonorepoEvidenceStore(Path(p))),
        (args.https_store, HttpsEvidenceStore),
    ):
        for item in items:
            name, separator, location = item.partition("=")
            if not separator or not location or name in result:
                raise ValueError("store mapping invalid")
            # Validate the same portable store-id contract used by signed locators.
            EvidenceLocator(name, root, 0)
            result[name] = factory(location)
    if not 1 <= len(result) <= 1024:
        raise ValueError("store mapping invalid")
    return result


def _retention(paths, limits):
    if not 1 <= len(paths) <= limits.reads.maximum_objects:
        raise ValueError("retention count invalid")
    retention = []
    total = 0
    for path in paths:
        content = _read_bounded(
            Path(path),
            code="evidence.verification.input-invalid",
            label="retention envelope",
            maximum_bytes=MAX_ENVELOPE_BYTES,
        )
        total += len(content)
        if total > limits.reads.maximum_total_bytes:
            raise ValueError("retention byte budget exceeded")
        # Parse a routing hint; the graph authenticates it before I/O.
        statement = EvidenceStatement.from_bytes(
            DsseEnvelope.from_bytes(content).payload
        )
        if not isinstance(statement.predicate, EvidenceLocator):
            raise ValueError("retention predicate invalid")
        retention.append(SignedEvidenceRetention(statement.predicate, content))
    return tuple(retention)


def require_current_evidence_from_args(args):
    """Authenticate the configured current receipt; never fall back to unsigned mode."""

    try:
        project = discover_project(Path(args.project))
        if project is None:
            raise ValueError("project not found")
        target = _target(project, create_parent=False)
        if target is None:
            raise ValueError("project has no configured receipt")
        verification = copy(args)
        verification.current_map = str(target)
        verification.retention = None
        result = verify_evidence_from_args(verification)
        # Configuration and path custody may change during graph reads. Reopen
        # the project rather than trusting the initial loaded definition.
        fresh_project = discover_project(Path(args.project))
        if (
            fresh_project is None
            or _target(fresh_project, create_parent=False) != target
            or validated_project_authority_identity(
                Path(args.project), synchronize_source_intelligence=False
            ).uri
            != result["project_authority"]
        ):
            raise ValueError("configured receipt path changed")
        if _current_map(target).identity.uri != result["current_map_identity"]:
            raise ValueError("configured receipt changed")
        return {
            **result,
            "current_receipt_path": target.relative_to(project.root).as_posix(),
        }
    except CliFailure:
        raise
    except (ValueError, OSError, RecursionError) as error:
        raise CliFailure(
            getattr(error, "code", "evidence.current.refused"),
            "current receipt evidence verification refused",
        ) from None


def verify_evidence_from_args(args):
    try:
        project = Path(args.project)
        authority = validated_project_authority_identity(
            project, synchronize_source_intelligence=False
        )
        plan = EvidenceVerificationPlan.from_dict(_document(args.plan))
        policy = EvidenceTrustPolicy.from_dict(_document(args.policy))
        if plan.project_authority != authority:
            raise CliFailure(
                "evidence.verification.project-mismatch",
                "verification plan does not bind current project authority",
            )
        if bool(args.current_map) != bool(args.bundle_store):
            raise ValueError("current map requires exactly one explicit bundle store")
        limits = EvidenceGraphLimits()
        resolver = ConfiguredEvidenceResolver(_stores(args, plan.root))

        def current_state():
            revocations = EvidenceRevocations.from_dict(_document(args.revocations))
            return EvidenceVerificationState(int(time.time()), revocations)

        current = None
        if args.current_map:
            current = _current_map(args.current_map)
            loaded = discover_project(project)
            if loaded is None:
                raise ValueError("project not found")
            prepared = verify_current_evidence(
                loaded,
                current,
                plan=plan,
                policy=policy,
                resolver=resolver,
                bundle_store=FileSystemEvidenceStore(Path(args.bundle_store)),
                current_state=current_state,
                project_authority=lambda: validated_project_authority_identity(
                    project, synchronize_source_intelligence=False
                ),
                limits=limits,
            )
            checked = prepared.verification
        else:
            retention = _retention(args.retention, limits)
            checked = verify_retained_evidence_graph(
                plan.root,
                requirements=plan.requirements,
                resolver=resolver,
                retention=tuple(retention),
                policy=policy,
                current_state=current_state,
                limits=limits,
            )
        # Refuse changed authority or operator inputs, including a policy change
        # during storage I/O. Revocations were independently reloaded by the graph.
        if (
            validated_project_authority_identity(
                project, synchronize_source_intelligence=False
            )
            != authority
            or EvidenceVerificationPlan.from_dict(_document(args.plan)).identity
            != plan.identity
            or EvidenceTrustPolicy.from_dict(_document(args.policy)).identity
            != policy.identity
            or (current is not None and _current_map(args.current_map) != current)
        ):
            raise CliFailure(
                "evidence.verification.authority-changed",
                "verification authority changed during evidence reads",
            )
        return {
            "schema": "literate-ai/evidence-verification-result@1",
            "authenticated": True,
            "retained": True,
            "receipt_updated": False,
            "execution_authorized": False,
            "project_authority": authority.uri,
            "plan_identity": plan.identity.uri,
            "policy_identity": policy.identity.uri,
            "verification_identity": checked.identity.uri,
            "revocations_identity": checked.graph.final_state.revocations.identity.uri,
            "checked_at": checked.graph.final_state.now,
            "root": plan.root.to_dict(),
            "run_count": len(checked.graph.runs),
            "object_count": len(checked.graph.objects),
            "retention_count": len(checked.retention),
            **(
                {"current_map_identity": current.identity.uri}
                if current is not None
                else {}
            ),
        }
    except CliFailure:
        raise
    except (ValueError, OSError, RecursionError) as error:
        code = getattr(error, "code", "evidence.verification.input-invalid")
        raise CliFailure(code, "retained evidence verification refused") from None
