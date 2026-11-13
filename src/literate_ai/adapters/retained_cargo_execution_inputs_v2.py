"""Join live package/source owners with Cargo's ancestor configuration inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai.adapters.locked_generation_authority import (
    read_only_component_catalog_observations,
)
from literate_ai.adapters.retained_cargo_execution_inputs import _state
from literate_ai.adapters.retained_cargo_input_scope_v2 import (
    RetainedCargoInputScopeV2,
    capture_retained_cargo_input_scope_v2,
)
from literate_ai.adapters.retained_cargo_materialization_v2 import (
    RetainedCargoMaterializationV2,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import (
    PinnedInputClosure,
    read_only_project_configuration_parses,
)

_CREATION_KEY = object()


@dataclass(init=False, frozen=True, slots=True)
class RetainedCargoExecutionInputsV2:
    materialized: RetainedCargoMaterializationV2
    scope: RetainedCargoInputScopeV2
    _states: tuple = field(repr=False)
    _configuration: PinnedInputClosure = field(repr=False)

    def __init__(self, *, key, materialized, scope, states, configuration):
        if key is not _CREATION_KEY:
            raise TypeError("retained.inputs.v2-live-capture-required")
        for name, value in (
            ("materialized", materialized),
            ("scope", scope),
            ("_states", states),
            ("_configuration", configuration),
        ):
            object.__setattr__(self, name, value)

    def current_identity(self, *, environment: Mapping[str, str]):
        with (
            read_only_project_configuration_parses(isolated=True),
            read_only_component_catalog_observations(isolated=True),
        ):
            self.materialized.require_unchanged()
            self.scope.require_unchanged(environment=environment)
            for path, before in self._states:
                if _state(path) != before:
                    raise ValueError(
                        "retained.inputs.v2-ancestor-configuration-changed"
                    )
            self._configuration.require_unchanged()
            self.scope.require_unchanged(environment=environment)
            self.materialized.require_unchanged()
            return canonical_identity(
                {
                    "schema": "literate-ai/retained-cargo-execution-inputs@2",
                    "binding": self.materialized.files.reviewed.binding.identity.uri,
                    "scope": self.scope.identity.uri,
                    "configuration": self._configuration.identity,
                    "present": [str(path) for path, state in self._states if state[2]],
                    "absent": [
                        str(path) for path, state in self._states if state[2] is None
                    ],
                }
            )

    def require_metadata_paths(self, metadata, *, environment):
        """Require every source/manifest to resolve within captured input owners."""
        self.current_identity(environment=environment)
        files = {
            (tree.root / relative).resolve(strict=True)
            for _, tree in self.scope.owners.trees
            for relative, _, _ in tree.files
        }
        for package in self.materialized.packages:
            files.update((package.root / item.path) for item in package.files)
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("packages"), list
        ):
            raise ValueError("retained.inputs.v2-metadata-invalid")
        for package in metadata["packages"]:
            if not isinstance(package, dict) or not isinstance(
                package.get("targets"), list
            ):
                raise ValueError("retained.inputs.v2-metadata-invalid")
            paths = [package.get("manifest_path")]
            for target in package["targets"]:
                if not isinstance(target, dict):
                    raise ValueError("retained.inputs.v2-metadata-invalid")
                paths.append(target.get("src_path"))
            for value in paths:
                if not isinstance(value, str):
                    raise ValueError("retained.inputs.v2-metadata-invalid")
                path = Path(value)
                if (
                    not path.is_absolute()
                    or ".." in path.parts
                    or path.resolve(strict=True) not in files
                ):
                    raise ValueError("retained.inputs.v2-uncaptured-package-path")
        self.current_identity(environment=environment)


def read_retained_cargo_execution_inputs_v2(
    materialized: RetainedCargoMaterializationV2,
    *,
    environment: Mapping[str, str],
    maximum_metadata_bytes: int = 1024 * 1024 * 1024,
) -> RetainedCargoExecutionInputsV2:
    """Capture the entire provisioned Cargo home, not registry/git shortcuts.

    Dependency provisioning and exact-command cache warming precede this reader.
    No cache mutation is permitted during its lifetime. Workspace outputs are
    excluded only through the materializer's still-live original scratch owner.
    """
    if type(materialized) is not RetainedCargoMaterializationV2:
        raise TypeError("retained.inputs.v2-live-materialization-required")
    materialized.require_unchanged()
    reviewed = materialized.files.reviewed
    workspace = materialized.scratch.context.workspace
    scope = capture_retained_cargo_input_scope_v2(
        workspace,
        policy=reviewed.policy,
        environment=environment,
        maximum_metadata_bytes=maximum_metadata_bytes,
        scratch=materialized.scratch,
    )
    budget = reviewed.policy.input_custody.aggregate
    counts = scope.owners.counts
    remaining = budget.maximum_total_bytes - counts["bytes"]
    entries = budget.maximum_entries - counts["entries"]
    files = budget.maximum_files - counts["files"]
    if min(remaining, entries, files) < 1:
        raise ValueError("retained.inputs.v2-configuration-budget-exhausted")
    closure = PinnedInputClosure(
        maximum_files=min(files, entries),
        maximum_file_bytes=min(budget.maximum_file_bytes, remaining),
        maximum_total_bytes=remaining,
    )
    states = []
    for index, parent in enumerate(workspace.parents):
        directory = parent / ".cargo"
        states.append((directory, _state(directory)))
        for name in ("config", "config.toml"):
            path = directory / name
            before = _state(path)
            states.append((path, before))
            if before[2] is not None:
                closure.pin(path, boundary=directory, label=f"ancestor-{index}/{name}")
        if len(states) > entries:
            raise ValueError("retained.inputs.v2-configuration-entry-limit")
    result = RetainedCargoExecutionInputsV2(
        key=_CREATION_KEY,
        materialized=materialized,
        scope=scope,
        states=tuple(states),
        configuration=closure,
    )
    result.current_identity(environment=environment)
    return result
