"""Actual adopted Make/Git scope with fixture-only Cargo review declarations."""

import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace

from literate_ai.adapters.retained_cargo_review_v2 import read_retained_cargo_review_v2
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.retained_cargo_binding_v2 import RetainedCargoBindingV2
from literate_ai.contracts.retained_cargo_custody import (
    RetainedCargoCustodyPolicy,
    RetainedCargoInputBudget,
    RetainedCargoOwnerBudget,
)
from literate_ai.contracts.retained_cargo_patches import CargoWorkspaceExpectationV2
from literate_ai.contracts.retained_cargo_policy import RetainedLibraryGatePolicyV2
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoLibtestProtocol,
)
from literate_ai.contracts.retained_cargo_tests import retained_cargo_test_targets
from literate_ai.contracts.retained_cargo_tests_v2 import (
    RetainedCargoTestInventoryV2,
    RetainedCargoTestTargetV2,
)
from literate_ai.contracts.retained_cargo_workspace_v2 import (
    RetainedCargoScratchRoot,
    RetainedCargoWorkspacePlanV2,
)
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from literate_ai.projects import load_project
from tests.support.fixtures_test_retained_cargo_import import (
    RetainedCargoImportTests,
    blob,
)
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _legacy_project,
    _selectors,
)


@contextmanager
def scratch_review_fixture(base, *, with_provider=False, fixture=None):
    project = base / "converted"
    _legacy_project(project)
    (project / ".gitignore").write_text("pkgs/\ncargo-output/\n")
    subprocess.run(
        ["git", "init", "-q", str(project)],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
    )
    _adapter().initialize(
        project,
        flavor_selectors=_selectors(project),
        source_intelligence_provider="none",
        convert=True,
    )
    loaded = load_project(project)
    fixture = RetainedCargoImportTests() if fixture is None else fixture
    fixture.setUp()
    try:
        implementation = json.loads(
            (project / ".literate/legacy-lift-shift.json").read_bytes()
        )["implementation_directory"]
        common = replace(fixture.plan, workspace_root=implementation)
        budget = RetainedCargoInputBudget(1000, 4000, 64 * 1024**2, 128 * 1024**2)
        custody = RetainedCargoCustodyPolicy(
            tuple(
                RetainedCargoOwnerBudget(s, budget) for s in ("cargo-home", "workspace")
            ),
            RetainedCargoInputBudget(2000, 8000, 64 * 1024**2, 256 * 1024**2),
        )
        graph = CargoWorkspaceExpectationV2(
            common.graph, getattr(fixture, "registry_patches", ())
        )
        plan = RetainedCargoWorkspacePlanV2(
            common,
            graph,
            (
                RetainedCargoScratchRoot("cargo-output", "cargo-output"),
                RetainedCargoScratchRoot("pkgs", "member-packages"),
            ),
            custody,
        )
        policy = RetainedLibraryGatePolicyV2(
            RetainedLibraryGatePolicy(
                loaded.definition.project_id,
                common.gates,
                tuple(
                    sorted(
                        (common.cargo_identity, common.rustc_identity),
                        key=lambda x: x.uri,
                    )
                ),
            ),
            custody,
        )
        targets = tuple(
            sorted(
                (
                    RetainedCargoTestTargetV2(
                        p.root,
                        t.name,
                        tuple(sorted(t.kinds)),
                        getattr(fixture, "harnesses", {}).get(
                            (p.root, t.name, tuple(sorted(t.kinds))), True
                        ),
                        getattr(fixture, "protocols", {}).get(
                            (p.root, t.name, tuple(sorted(t.kinds))),
                            RetainedCargoLibtestProtocol(("reviewed_case",), ()),
                        ),
                    )
                    for p, t in retained_cargo_test_targets(common.graph)
                ),
                key=lambda x: x.key,
            )
        )
        inventory = RetainedCargoTestInventoryV2(
            loaded.definition.project_id, plan.identity, policy.identity, targets
        )
        plan_bytes = canonical_json_bytes(plan.to_dict())
        policy_bytes = canonical_json_bytes(policy.to_dict())
        tests_bytes = canonical_json_bytes(inventory.to_dict())
        binding = RetainedCargoBindingV2(
            replace(
                fixture.binding,
                importer_project_id=loaded.definition.project_id,
                workspace_plan=blob(plan_bytes),
                destinations=tuple(
                    (identity, implementation + "/" + path)
                    for identity, path in fixture.binding.destinations
                ),
            ),
            blob(policy_bytes),
            blob(tests_bytes),
        )
        folder = project / "review"
        folder.mkdir()
        for name, content in [
            ("binding.json", canonical_json_bytes(binding.to_dict())),
            ("plan.json", plan_bytes),
            ("policy.json", policy_bytes),
            ("tests.json", tests_bytes),
        ]:
            (folder / name).write_bytes(content)
        reviewed = read_retained_cargo_review_v2(
            project,
            binding_path="review/binding.json",
            reviewed_binding=blob(canonical_json_bytes(binding.to_dict())),
            plan_path="review/plan.json",
            gate_policy_path="review/policy.json",
            test_inventory_path="review/tests.json",
        )
        yield (reviewed, fixture) if with_provider else reviewed
    finally:
        fixture.doCleanups()
