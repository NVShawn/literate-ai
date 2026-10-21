"""Typed retained plan and overlapping Component locks; not runtime acceptance."""

from dataclasses import replace

from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
    RetainedProjectProfile,
)
from tests.support.fixtures_test_component_lock_contracts import component_lock


def retained_project_sbom_fixture():
    lock = component_lock()
    dependency = next(
        n for n in lock.nodes if n.revision.identity != lock.root_revision
    )
    second = replace(
        lock,
        root_revision=dependency.revision.identity,
        nodes=(dependency,),
        edges=(),
        _authorings=(dependency.revision.definition,),
    )
    locks = (lock, second)
    profile = RetainedProjectProfile(
        "host",
        *(canonical_identity(n) for n in range(5)),
        (("tool", "sdk/tool", canonical_identity("tool")),),
        (
            RetainedProjectAction(
                "build", "build", "tool", (), (), ("app",), ("app",), 10
            ),
            RetainedProjectAction(
                "test", "test", "tool", (), ("build",), ("case",), (), 10
            ),
            RetainedProjectAction(
                "docs", "docs", "tool", (), ("build",), ("manual",), (), 10
            ),
            RetainedProjectAction(
                "accept",
                "accept",
                "tool",
                (),
                ("test", "docs"),
                ("verifier",),
                (),
                10,
            ),
        ),
        ("app",),
    )
    plan = RetainedProjectPlan(
        "original-project",
        canonical_identity("project authority"),
        tuple(item.identity for item in locks),
        canonical_identity("original conversion"),
        canonical_identity("standard binding"),
        canonical_identity("original binary manifest"),
        profile,
    )
    return plan, locks
