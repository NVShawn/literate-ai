"""Select complete explicit Cargo input roots without granting exclusions.

The execution transaction still owns reviewed workspace output exclusions,
ancestor configuration custody, measured tools and current receiving authority.
This reader never installs dependencies, creates Cargo directories or executes
commands. In particular it does not narrow CARGO_HOME to registry/git subtrees.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.retained_cargo_input_owners_v2 import (
    RetainedCargoInputOwnersV2,
    RetainedCargoInputRootV2,
    capture_retained_cargo_input_owners_v2,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_cargo_policy import RetainedLibraryGatePolicyV2


@dataclass(frozen=True, slots=True)
class RetainedCargoInputScopeV2:
    policy: RetainedLibraryGatePolicyV2
    environment_identity: ContentIdentity
    owners: RetainedCargoInputOwnersV2
    roots: tuple[tuple[str, Path], ...]

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-input-scope@2",
                "policy": self.policy.identity.uri,
                "environment": self.environment_identity.uri,
                "owners": self.owners.identity.uri,
                "roots": [
                    {"scope": scope, "path": str(root)} for scope, root in self.roots
                ],
            }
        )

    def require_unchanged(self, *, environment: Mapping[str, str]):
        if _environment_identity(environment) != self.environment_identity:
            raise ValueError("retained.cargo.v2-input-environment-changed")
        self.owners.require_unchanged()
        if _environment_identity(environment) != self.environment_identity:
            raise ValueError("retained.cargo.v2-input-environment-changed")


def _environment_identity(environment):
    if not isinstance(environment, Mapping) or any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or not name
        or "=" in name
        or "\x00" in name + value
        for name, value in environment.items()
    ):
        raise ValueError("retained.cargo.v2-input-environment-invalid")
    return canonical_identity(dict(environment))


def capture_retained_cargo_input_scope_v2(
    workspace: Path,
    *,
    policy: RetainedLibraryGatePolicyV2,
    environment: Mapping[str, str],
    maximum_metadata_bytes: int = 1024 * 1024 * 1024,
) -> RetainedCargoInputScopeV2:
    """Capture entire declared roots, including previously unknown home entries.

    No exclusions are inferred from Git ignore status, Cargo conventions or a
    serialized workspace plan. Output exclusions require a separate live owner;
    this complete-scope reader does not establish that materialization authority.
    The caller must separately capture configuration outside these roots.
    """
    if type(policy) is not RetainedLibraryGatePolicyV2:
        raise ValueError("retained.cargo.v2-input-gate-policy-required")
    live_environment = environment
    environment = dict(environment) if isinstance(environment, Mapping) else environment
    environment_identity = _environment_identity(environment)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("retained.cargo.v2-input-workspace-invalid")
    roots = {"workspace": workspace}
    for scope, variable in (
        ("cargo-home", "CARGO_HOME"),
        *(("external:" + name, name) for name in policy.external_input_variables),
    ):
        value = environment.get(variable)
        if not value or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("retained.cargo.v2-input-explicit-root-required")
        roots[scope] = Path(value)
    owners = capture_retained_cargo_input_owners_v2(
        tuple(
            RetainedCargoInputRootV2(owner.scope, roots[owner.scope])
            for owner in policy.input_custody.owners
        ),
        policy=policy.input_custody,
        maximum_metadata_bytes=maximum_metadata_bytes,
    )
    result = RetainedCargoInputScopeV2(
        policy,
        environment_identity,
        owners,
        tuple((scope, tree.root) for scope, tree in owners.trees),
    )
    result.require_unchanged(environment=live_environment)
    return result
