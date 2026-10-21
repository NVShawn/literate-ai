"""Private verifier case selection for real retained executable fixtures."""

import hashlib
import json
from dataclasses import replace

from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_filesystem import (
    RetainedFilesystemCase,
    RetainedFilesystemInput,
    RetainedFilesystemState,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)


def filesystem_case():
    return RetainedFilesystemCase(
        (RetainedFilesystemInput("data", b"abcdef"),),
        (
            RetainedFilesystemState(
                "data", "file", 3, hashlib.sha256(b"abc").hexdigest()
            ),
        ),
        RetainedProjectLimits(8, 16, 64),
    )


def select_filesystem_case(completed):
    fixture = completed.fixture
    path = fixture.source / ACCEPTANCE_POLICY_PATH
    policy = RetainedProjectAcceptancePolicy.from_dict(json.loads(path.read_bytes()))
    (case,) = policy.cases
    case = replace(
        case,
        argv=("report", "{case_root}/data", "abc"),
        stdin=b"",
        expected_stdout=b"",
        filesystem=filesystem_case(),
    )
    policy = replace(policy, cases=(case,))
    content = canonical_json_bytes(policy.to_dict())
    path.write_bytes(content)
    fixture.plan = replace(
        fixture.plan,
        profile=replace(
            fixture.plan.profile,
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        ),
    )
    fixture.current = fixture.plan
    completed.graph = project_retained_project_managed_graph(
        fixture.plan, completed.locks, version="1.0.0"
    )
    return policy
