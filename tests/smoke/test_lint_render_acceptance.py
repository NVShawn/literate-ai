"""Lint-and-render snapshot oracle for hand-authored library/UI Components."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_acceptance import (
    load_lint_render_acceptance,
    oracle_path,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.contracts import canonical_identity

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "lint-render-library"
COMPONENT_NAME = "greeting-surface"
SPEC_IDENTITY = (
    "sha256:610593b3ebc644ee102a7103f54c04b3b687d1bebde06da357aaf72053a01d26"
)


def _library_lock(
    specification_set_identity: str = SPEC_IDENTITY,
    name: str = COMPONENT_NAME,
):
    revision_identity = canonical_identity({"root": name})
    return SimpleNamespace(
        root_revision=revision_identity,
        nodes=(
            SimpleNamespace(
                revision=SimpleNamespace(
                    identity=revision_identity,
                    specification_set_identity=SimpleNamespace(
                        uri=specification_set_identity
                    ),
                    definition=SimpleNamespace(
                        coordinate=SimpleNamespace(name=name),
                        entrypoints=(),
                    ),
                )
            ),
        ),
    )


def _copy_fixture() -> Path:
    temporary = Path(tempfile.mkdtemp())
    destination = temporary / "lint-render-library"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


class LintRenderAcceptanceTests(unittest.TestCase):
    def test_project_acceptance_uses_lint_render_instead_of_exempting(self) -> None:
        oracle = load_lint_render_acceptance(
            oracle_path(FIXTURE_ROOT, COMPONENT_NAME),
            COMPONENT_NAME,
            FIXTURE_ROOT,
        )
        observation = oracle.accept(_library_lock())
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
                independent_acceptance_oracle=oracle,
            )
            plan = SimpleNamespace(
                identity=canonical_identity({"library-package-plan": True}),
                entrypoints=(),
            )
            result = SimpleNamespace(
                identity=canonical_identity({"library-package-result": True})
            )
            evidence = ports.accept_project_independently(
                _library_lock(),
                None,
                None,
                plan,
                result,
                canonical_identity({"root-test": True}),
                canonical_identity({"package-execution": True}),
            )
        self.assertEqual(
            evidence,
            canonical_identity(
                {
                    "schema": "literate-ai/local-independent-lint-render-acceptance@1",
                    "package_plan_identity": plan.identity.uri,
                    "package_result_identity": result.identity.uri,
                    "root_integration_test_identity": canonical_identity(
                        {"root-test": True}
                    ).uri,
                    "packaged_execution_identity": canonical_identity(
                        {"package-execution": True}
                    ).uri,
                    "oracle_identity": oracle.identity.uri,
                    "observation_identity": observation.uri,
                }
            ),
        )

    def test_project_acceptance_surfaces_render_drift(self) -> None:
        root = _copy_fixture()
        self.addCleanup(shutil.rmtree, root.parent, ignore_errors=True)
        surface = root / "lib" / "surface.py"
        surface.write_text(
            surface.read_text(encoding="utf-8").replace(
                "Exact greeting", "Drifted greeting"
            ),
            encoding="utf-8",
        )
        oracle = load_lint_render_acceptance(
            oracle_path(root, COMPONENT_NAME), COMPONENT_NAME, root
        )
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
                independent_acceptance_oracle=oracle,
            )
            plan = SimpleNamespace(
                identity=canonical_identity({"library-package-plan": True}),
                entrypoints=(),
            )
            with self.assertRaises(LocalStandardLifecycleError) as raised:
                ports.accept_project_independently(
                    _library_lock(),
                    None,
                    None,
                    plan,
                    SimpleNamespace(
                        identity=canonical_identity({"library-package-result": True})
                    ),
                    canonical_identity({"root-test": True}),
                    canonical_identity({"package-execution": True}),
                )
        self.assertIn("component_acceptance.render_drift", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
