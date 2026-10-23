"""Package tree bytes reuse existing no-follow custody without granting admission."""

from __future__ import annotations

from pathlib import Path

from literate_ai.adapters.native_sdk_tree_custody import (
    capture_native_sdk_tree,
    materialize_native_sdk_tree,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.storage.cas import FileSystemCAS


def _require_input(value, expected_identity):
    if not isinstance(value, PackageTreeInput) or value.identity != expected_identity:
        raise ValueError("Package tree input differs from the expected exact identity")


def materialize_package_tree_input(
    value: PackageTreeInput,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    parent: Path,
) -> Path:
    """Create staging; the provider still owns publication and admission."""
    _require_input(value, expected_identity)
    return materialize_native_sdk_tree(
        value.tree,
        expected_identity=value.tree.identity,
        store=store,
        limits=limits,
        parent=parent,
    )


def verify_package_tree_input(
    root: Path,
    value: PackageTreeInput,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
) -> None:
    """Reobserve complete current bytes and topology, never just decoded claims."""
    _require_input(value, expected_identity)
    current = capture_native_sdk_tree(
        root,
        store=store,
        limits=limits,
        case_sensitive=value.tree.case_sensitive,
    )
    if current != value.tree:
        raise ValueError("Package tree input bytes or topology changed")
