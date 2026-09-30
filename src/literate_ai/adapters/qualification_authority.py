"""Current parity authority shared by execution and retained-evidence reopening."""

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.projects import PinnedInputClosure
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationCaseSurfaceBinding,
    QualificationVerifierCaseMap,
)


def qualification_verifier_record(
    profile: LocalQualificationProfile, source_snapshot_identity: ContentIdentity
) -> dict[str, str]:
    if not isinstance(profile, LocalQualificationProfile) or not isinstance(
        source_snapshot_identity, ContentIdentity
    ):
        raise TypeError(
            "qualification verifier requires typed profile and source identity"
        )
    return {
        "provider": "filesystem-standard-independent-parity@1",
        "source_snapshot_identity": source_snapshot_identity.uri,
        "profile_identity": profile.identity,
    }


def qualification_verifier_case_map(
    profile: LocalQualificationProfile, source_snapshot_identity: ContentIdentity
) -> QualificationVerifierCaseMap:
    return QualificationVerifierCaseMap(
        canonical_identity(
            qualification_verifier_record(profile, source_snapshot_identity)
        ),
        tuple(
            sorted(
                (
                    QualificationCaseSurfaceBinding(
                        case.case_id,
                        canonical_identity(case.to_dict()),
                        profile.covered_surface_ids,
                    )
                    for case in profile.cases
                ),
                key=lambda item: (item.case_id, item.case_identity.uri),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class CurrentQualificationAuthority:
    profile: LocalQualificationProfile
    case_map: QualificationVerifierCaseMap
    _closure: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self) -> None:
        self._closure.require_unchanged()


def read_current_qualification_authority(
    project_root: Path,
    profile_path: str,
    *,
    source_snapshot_identity: ContentIdentity,
    maximum_bytes: int = 16 * 1024 * 1024,
) -> CurrentQualificationAuthority:
    """Read current profile bytes; never copy the candidate's verifier identity.

    The selected path and source identity must come from current project/promotion
    authority. No baseline source tree or model executable is needed to compute
    this expectation. This does not execute cases or establish qualification.
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("qualification.authority.limit-invalid")
    canonical_relative_posix_path(profile_path, label="qualification profile")
    if len(profile_path) > 128:
        raise ValueError("qualification.authority.path-too-long")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("qualification.authority.root-unsafe")
    require_safe_directory(root)
    closure = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=maximum_bytes,
        maximum_total_bytes=maximum_bytes,
    )
    content = closure.pin(
        root.joinpath(*PurePosixPath(profile_path).parts),
        boundary=root,
        label=profile_path,
    )

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("qualification.authority.duplicate-field")
            value[key] = item
        return value

    profile = LocalQualificationProfile.from_dict(
        json.loads(content, object_pairs_hook=unique_object)
    )
    result = CurrentQualificationAuthority(
        profile,
        qualification_verifier_case_map(profile, source_snapshot_identity),
        closure,
    )
    result.require_unchanged()
    return result
