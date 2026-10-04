from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_resolution_audit_store``."""


import subprocess
from pathlib import Path

from literate_ai.contracts.component_locking import ComponentResolutionAudit
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_component_lock_contracts import component_lock


def audit(label: str = "catalog") -> ComponentResolutionAudit:
    lock = component_lock()
    return ComponentResolutionAudit(
        component_lock_identity=lock.identity,
        catalog_identity=canonical_identity({"catalog": label}),
        resolver_identity=lock.resolver_identity,
        candidates=(),
    )


def create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))
