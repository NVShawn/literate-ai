"""Bounded exit policy for the welded ``component.json`` authoring format."""

from __future__ import annotations

LEGACY_COMPONENT_AUTHORING_DEPRECATED_IN = "0.2.0"
LEGACY_COMPONENT_AUTHORING_LAST_SUPPORTED_SERIES = "0.2.x"
LEGACY_COMPONENT_AUTHORING_REMOVED_IN = "0.3.0"
LEGACY_COMPONENT_AUTHORING_MIGRATION_COMMAND = "litai component migrate COMPONENT"


def legacy_component_migration_message() -> str:
    """Return the stable actionable diagnostic for ordinary project commands."""

    return (
        "legacy-only component.json authoring is deprecated in "
        f"{LEGACY_COMPONENT_AUTHORING_DEPRECATED_IN} and is accepted only by the "
        "explicit migration command during the "
        f"{LEGACY_COMPONENT_AUTHORING_LAST_SUPPORTED_SERIES} compatibility series; "
        f"run `{LEGACY_COMPONENT_AUTHORING_MIGRATION_COMMAND}` before ordinary project "
        f"operations. Legacy authoring support is removed in "
        f"{LEGACY_COMPONENT_AUTHORING_REMOVED_IN}."
    )


def legacy_component_compatibility_exit() -> dict[str, str]:
    """Project the versioned policy into migration reports and API responses."""

    return {
        "deprecated_in": LEGACY_COMPONENT_AUTHORING_DEPRECATED_IN,
        "last_supported_series": LEGACY_COMPONENT_AUTHORING_LAST_SUPPORTED_SERIES,
        "removed_in": LEGACY_COMPONENT_AUTHORING_REMOVED_IN,
        "migration_command": LEGACY_COMPONENT_AUTHORING_MIGRATION_COMMAND,
    }


__all__ = [
    "LEGACY_COMPONENT_AUTHORING_DEPRECATED_IN",
    "LEGACY_COMPONENT_AUTHORING_LAST_SUPPORTED_SERIES",
    "LEGACY_COMPONENT_AUTHORING_MIGRATION_COMMAND",
    "LEGACY_COMPONENT_AUTHORING_REMOVED_IN",
    "legacy_component_compatibility_exit",
    "legacy_component_migration_message",
]
