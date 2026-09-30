"""Concrete observations for the reusable operator status application surface."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_project_component_lock_identities,
    current_rebuild_project_authority_identity,
)
from literate_ai.adapters.conversion_authority import (
    ConversionAuthorityError,
    FilesystemConversionAuthorityStore,
    require_current_qualified_conversion_authority,
)
from literate_ai.adapters.host_install import (
    HostInstallError,
    detect_current_install_target,
    load_host_install_sbom,
    observe_host_install_dependencies,
)
from literate_ai.adapters.models import (
    CodingCliError,
    inspect_coding_cli_authentication,
)
from literate_ai.adapters.project_initialization import flavor_selector_directory
from literate_ai.adapters.project_tracker import (
    ProjectTrackerError,
    inspect_project_tracker,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_host_paths
from literate_ai.contracts import StandardProjectLifecycleDriver
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project
from literate_ai.test_receipts import inspect_project_test_receipt

OPERATOR_STATUS_SCHEMA = "literate-ai/operator-status@1"
HOST_PREFLIGHT_SCHEMA = "literate-ai/operator-host-preflight@1"


class OperatorStatusError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def inspect_host_preflight(
    *,
    environment: Mapping[str, str] | None = None,
    flavor_selectors: Sequence[str] = (),
) -> dict[str, object]:
    """Observe host paths, composed native tools, and coding auth without writes."""

    configured = dict(os.environ if environment is None else environment)
    try:
        paths = resolve_host_paths(environment=configured)
        path_report: dict[str, object] = {
            "state": "resolved",
            "config_root": str(paths.config_root),
            "state_root": str(paths.state_root),
            "data_root": str(paths.data_root),
            "cache_root": str(paths.cache_root),
            "managed_tool_root": str(paths.managed_tool_root),
            "install_root": str(paths.install_root),
            "temporary_root": str(paths.temporary_root),
        }
    except (UserPathError, OSError, ValueError) as exc:
        path_report = {
            "state": "invalid",
            "error": {
                "code": getattr(exc, "code", "operator.user_paths_invalid"),
                "message": getattr(exc, "message", str(exc)),
            },
        }
    try:
        target = detect_current_install_target(environment=configured)
        flavor_names = tuple(
            dict.fromkeys(
                flavor_selector_directory(selector) for selector in flavor_selectors
            )
        )
        sbom_root = Path(str(files("literate_ai.project_template").joinpath("flavors")))
        sbom_path, sbom = load_host_install_sbom(
            sbom_root, target, flavor_names=flavor_names
        )
        host_tools: dict[str, object] = observe_host_install_dependencies(
            sbom_path=sbom_path,
            sbom=sbom,
            environment=configured,
        ).to_dict()
    except (HostInstallError, OSError, ValueError) as exc:
        host_tools = {
            "schema": "literate-ai/host-install-report@2",
            "ready": False,
            "error": {
                "code": getattr(exc, "code", "operator.host_preflight_failed"),
                "message": getattr(exc, "message", str(exc)),
            },
        }
    try:
        coding_cli = inspect_coding_cli_authentication(configured)
    except CodingCliError as exc:
        coding_cli = {
            "state": "unavailable",
            "error": {"code": exc.code, "message": exc.message},
        }
    return {
        "schema": HOST_PREFLIGHT_SCHEMA,
        "paths": path_report,
        "host_tools": host_tools,
        "coding_cli": coding_cli,
    }


def _standard_binding_distribution_mismatch(lifecycle_driver: object) -> bool:
    """Detect a pinned Standard binding that no longer matches the installed wheel.

    `litai rebuild` requires a resolved Standard lifecycle driver binding before it
    will execute. When the project-pinned framework distribution identity differs
    from the currently installed wheel bytes, rebuild's front door fails closed, so
    recommending it first is a guaranteed dead end. Detect that exact condition here
    so callers can redirect to the reviewed `rebind-standard` transaction instead.
    """

    if not isinstance(lifecycle_driver, StandardProjectLifecycleDriver):
        return False
    try:
        resolve_standard_project_lifecycle_driver(lifecycle_driver)
    except StandardLifecycleBindingError as exc:
        return exc.code == "standard_binding.distribution_mismatch"
    return False


def _next_verb(
    stage: str | None,
    locks: dict[str, object],
    *,
    standard_binding_distribution_mismatch: bool = False,
) -> str:
    if stage == "wrapped":
        return "litai project test-receipt run-retained CANDIDATE --project ."
    if stage == "retained":
        return "litai spec derive CASE"
    if stage == "drafted":
        return (
            "litai spec qualify COMPONENT --source SOURCE --profile PROFILE "
            "--output OUTPUT --allow-host-execution"
        )
    if standard_binding_distribution_mismatch:
        return "litai project lifecycle rebind-standard --project . --output OUTPUT"
    if stage == "qualified":
        return "litai rebuild --allow-host-execution"
    if locks.get("state") != "current":
        return "litai lock"
    return "litai rebuild --allow-host-execution"


def inspect_operator_status(
    selected: Path,
    *,
    view: str = "status",
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Compose existing read-only services into one versioned operator snapshot."""

    try:
        project = discover_project(Path(selected))
    except (OSError, ProjectError) as exc:
        raise OperatorStatusError(
            getattr(exc, "code", "project.not_found"),
            getattr(exc, "message", str(exc)),
        ) from exc
    if project is None:
        raise OperatorStatusError(
            "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
        )
    try:
        tracker = inspect_project_tracker(project.root)
    except ProjectTrackerError as exc:
        if exc.code == "project.tracker_host_ambiguous":
            raise OperatorStatusError(exc.code, exc.message) from exc
        tracker = {
            "state": "unavailable",
            "error": {"code": exc.code, "message": exc.message},
        }

    stage_value: dict[str, object] | None = None
    try:
        conversion = FilesystemConversionAuthorityStore(project.root).load_optional()
        if conversion is not None:
            if conversion.project_id != project.definition.project_id:
                raise OperatorStatusError(
                    "conversion_authority.project_mismatch",
                    "conversion authority identifies another project",
                )
            stage_value = {
                "path": ".literate/conversion-authority.json",
                "identity": conversion.identity.uri,
                **conversion.to_dict(),
            }
            if conversion.stage.value == "qualified":
                try:
                    require_current_qualified_conversion_authority(project, conversion)
                    stage_value["evidence_state"] = "current"
                    stage_value["effective_release_authority"] = "specification"
                except ConversionAuthorityError as exc:
                    stage_value["evidence_state"] = "invalid"
                    stage_value["effective_release_authority"] = "original-source"
                    stage_value["error"] = {"code": exc.code, "message": exc.message}
            else:
                stage_value["evidence_state"] = "recorded"
                stage_value["effective_release_authority"] = "original-source"
    except ConversionAuthorityError as exc:
        raise OperatorStatusError(exc.code, exc.message) from exc

    authority: dict[str, object]
    authority_identity = None
    try:
        authority_identity = validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        authority = {"state": "current", "identity": authority_identity.uri}
    except ProjectValidationError as exc:
        authority = {
            "state": "invalid",
            "error": {"code": exc.code, "message": exc.message},
        }

    try:
        lock_identities = current_project_component_lock_identities(project)
        locks: dict[str, object] = {
            "state": "current",
            "count": len(lock_identities),
            "identities": [item.uri for item in lock_identities],
        }
    except ProjectComponentLockSetError as exc:
        lock_identities = ()
        locks = {
            "state": "invalid",
            "count": 0,
            "identities": [],
            "error": {"code": exc.code, "message": exc.message},
        }

    if authority_identity is None:
        receipt: dict[str, object] = {
            "state": "unavailable-authority-invalid",
            "configured": project.definition.test_receipt is not None,
        }
    else:
        try:
            receipt = inspect_project_test_receipt(
                project, project_revision_identity=authority_identity
            )
            if receipt.get("state") == "stale" and lock_identities:
                locked_authority = current_rebuild_project_authority_identity(
                    project, authority_identity
                )
                locked_receipt = inspect_project_test_receipt(
                    project, project_revision_identity=locked_authority
                )
                if locked_receipt.get("state") == "current":
                    receipt = locked_receipt
        except (ProjectComponentLockSetError, ProjectError) as exc:
            receipt = {
                "state": "invalid",
                "error": {
                    "code": getattr(exc, "code", "project.test_receipt_invalid"),
                    "message": getattr(exc, "message", str(exc)),
                },
            }

    host = inspect_host_preflight(
        environment=environment,
        flavor_selectors=project.definition.default_flavor_selectors,
    )
    stage = None if stage_value is None else str(stage_value["stage"])
    standard_binding_distribution_mismatch = _standard_binding_distribution_mismatch(
        project.definition.lifecycle_driver
    )
    return {
        "schema": OPERATOR_STATUS_SCHEMA,
        "view": view,
        "project": {
            "root": str(project.root),
            "project_id": project.definition.project_id,
            "kind": "adopted" if stage_value is not None else "created",
            "authority": authority,
            "conversion_authority": stage_value,
            "locks": locks,
            "test_receipt": receipt,
            "tracker": tracker,
        },
        "host": host,
        "next_verb": _next_verb(
            stage,
            locks,
            standard_binding_distribution_mismatch=(
                standard_binding_distribution_mismatch
            ),
        ),
    }


__all__ = [
    "HOST_PREFLIGHT_SCHEMA",
    "OPERATOR_STATUS_SCHEMA",
    "OperatorStatusError",
    "inspect_host_preflight",
    "inspect_operator_status",
]
