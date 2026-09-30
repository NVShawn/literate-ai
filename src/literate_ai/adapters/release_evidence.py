"""Fresh release receipt authentication from independently pinned policy inputs."""

import json
import time
from pathlib import Path

from literate_ai.application.current_evidence import verify_current_evidence
from literate_ai.application.evidence_resolution import ConfiguredEvidenceResolver
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.projects import discover_project
from literate_ai.security.evidence.graph import EvidenceVerificationState
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.records import _name
from literate_ai.security.evidence.trust import EvidenceRevocations, EvidenceTrustPolicy
from literate_ai.test_receipts import _parse_current_map, _read_bounded, _target

from .evidence_storage import (
    FileSystemEvidenceStore,
    HttpsEvidenceStore,
    MonorepoEvidenceStore,
)
from .project_validation import validated_project_authority_identity


def validate_release_evidence_policy(value):
    required = {
        "plan_path",
        "plan_identity",
        "policy_path",
        "policy_identity",
        "revocations_path",
        "bundle_store",
        "stores",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("release evidence policy fields invalid")
    for key in ("plan_path", "policy_path", "revocations_path", "bundle_store"):
        canonical_relative_posix_path(value[key], label=key)
    for key in ("plan_identity", "policy_identity"):
        ContentIdentity.parse_uri(value[key])
    stores = value["stores"]
    if not isinstance(stores, list) or not 1 <= len(stores) <= 1024:
        raise ValueError("release evidence stores invalid")
    names = set()
    for store in stores:
        if not isinstance(store, dict) or set(store) != {"id", "kind", "location"}:
            raise ValueError("release evidence store fields invalid")
        _name(store["id"])
        if store["id"] in names:
            raise ValueError("release evidence duplicate store")
        names.add(store["id"])
        if store["kind"] == "https":
            HttpsEvidenceStore(store["location"])
        elif store["kind"] in {"filesystem", "monorepo"}:
            canonical_relative_posix_path(store["location"], label="store location")
        else:
            raise ValueError("release evidence store kind invalid")
    return json.loads(json.dumps(value))


def _path(root, relative):
    parts = canonical_relative_posix_path(relative, label="release evidence path").parts
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or (
            path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("release evidence path traverses a symbolic link")
    return path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate release evidence input field")
        result[key] = value
    return result


def _document(path):
    content = _read_bounded(
        path,
        code="release.evidence_input_invalid",
        label="release evidence input",
        maximum_bytes=4 * 1024 * 1024,
    )
    return json.loads(content, object_pairs_hook=_pairs)


def verify_release_current_evidence(root: Path, binding):
    """Reauthenticate the configured map; no stored success flag is consumed."""

    binding = validate_release_evidence_policy(binding)
    project = discover_project(root)
    if project is None:
        raise ValueError("release evidence project missing")
    target = _target(project, create_parent=False)
    if target is None:
        raise ValueError("release evidence receipt unconfigured")
    current = _parse_current_map(
        _read_bounded(
            target,
            code="release.evidence_current_invalid",
            label="current release receipt",
        )
    )
    if current is None:
        raise ValueError("release evidence receipt is unauthenticated")

    def inputs():
        plan = EvidenceVerificationPlan.from_dict(
            _document(_path(root, binding["plan_path"]))
        )
        policy = EvidenceTrustPolicy.from_dict(
            _document(_path(root, binding["policy_path"]))
        )
        if (
            plan.identity.uri != binding["plan_identity"]
            or policy.identity.uri != binding["policy_identity"]
        ):
            raise ValueError("release evidence pinned input changed")
        return plan, policy

    plan, policy = inputs()

    def authority():
        inputs()
        return validated_project_authority_identity(
            root, synchronize_source_intelligence=False
        )

    def state():
        return EvidenceVerificationState(
            int(time.time()),
            EvidenceRevocations.from_dict(
                _document(_path(root, binding["revocations_path"]))
            ),
        )

    stores = {}
    for store in binding["stores"]:
        kind, location = store["kind"], store["location"]
        stores[store["id"]] = (
            HttpsEvidenceStore(location)
            if kind == "https"
            else (
                FileSystemEvidenceStore
                if kind == "filesystem"
                else MonorepoEvidenceStore
            )(_path(root, location))
        )
    checked = verify_current_evidence(
        project,
        current,
        plan=plan,
        policy=policy,
        resolver=ConfiguredEvidenceResolver(stores),
        bundle_store=FileSystemEvidenceStore(_path(root, binding["bundle_store"])),
        current_state=state,
        project_authority=authority,
    )
    fresh = discover_project(root)
    if (
        fresh is None
        or _target(fresh, create_parent=False) != target
        or authority() != plan.project_authority
    ):
        raise ValueError("release evidence project changed")
    if (
        _parse_current_map(
            _read_bounded(
                target,
                code="release.evidence_current_invalid",
                label="current release receipt",
            )
        )
        != current
    ):
        raise ValueError("release evidence current map changed")
    return checked.current.identity.uri
