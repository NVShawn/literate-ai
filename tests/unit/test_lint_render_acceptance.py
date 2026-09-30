"""Lint-and-render snapshot oracle for hand-authored library/UI Components."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_acceptance import (
    LINT_RENDER_SCHEMA,
    ComponentAcceptanceError,
    LintRenderAcceptance,
    load_lint_render_acceptance,
    oracle_path,
    resolve_component_acceptance_oracle,
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
SNAPSHOT_IDENTITY = (
    "sha256:af429266fe206334118d5504a92832dbb421ebcecbdab7a527a4ab1f7a49eafa"
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
    def test_hand_authored_library_passes_when_snapshot_matches(self) -> None:
        lock = _library_lock()
        oracle = load_lint_render_acceptance(
            oracle_path(FIXTURE_ROOT, COMPONENT_NAME),
            COMPONENT_NAME,
            FIXTURE_ROOT,
        )
        self.assertIsInstance(oracle, LintRenderAcceptance)
        self.assertEqual(oracle.snapshot_identity, SNAPSHOT_IDENTITY)
        first = oracle.accept(lock)
        self.assertEqual(first, oracle.accept(lock))

    def test_hand_authored_library_fails_closed_on_render_drift(self) -> None:
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
        with self.assertRaises(ComponentAcceptanceError) as raised:
            oracle.accept(_library_lock())
        self.assertEqual(raised.exception.code, "component_acceptance.render_drift")
        self.assertIn(SNAPSHOT_IDENTITY, raised.exception.message)

    def test_drifted_render_passes_after_snapshot_rebind(self) -> None:
        root = _copy_fixture()
        self.addCleanup(shutil.rmtree, root.parent, ignore_errors=True)
        surface = root / "lib" / "surface.py"
        surface.write_text(
            surface.read_text(encoding="utf-8").replace(
                "Exact greeting", "Rebound greeting"
            ),
            encoding="utf-8",
        )
        rendered = subprocess.run(
            [sys.executable, "render.py"],
            cwd=root / "lib",
            check=True,
            capture_output=True,
        )
        rebound = f"sha256:{hashlib.sha256(rendered.stdout).hexdigest()}"
        path = oracle_path(root, COMPONENT_NAME)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["render"]["snapshot_identity"] = rebound
        path.write_text(json.dumps(document), encoding="utf-8")
        oracle = load_lint_render_acceptance(path, COMPONENT_NAME, root)
        self.assertTrue(oracle.accept(_library_lock()).uri.startswith("sha256:"))

    def test_unknown_fields_and_absolute_argv_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = oracle_path(root, COMPONENT_NAME)
            path.parent.mkdir(parents=True)
            (root / "lib").mkdir()
            path.write_text(
                json.dumps(
                    {
                        "schema": LINT_RENDER_SCHEMA,
                        "specification_set_identity": SPEC_IDENTITY,
                        "source_root": "lib",
                        "lint": {
                            "arguments": ["{python}", "lint.py"],
                            "surprise": True,
                        },
                        "render": {
                            "arguments": ["/bin/sh", "-c", "echo hi"],
                            "snapshot_identity": SNAPSHOT_IDENTITY,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ComponentAcceptanceError) as raised:
                load_lint_render_acceptance(path, COMPONENT_NAME, root)
            self.assertEqual(
                raised.exception.code, "component_acceptance.contract_invalid"
            )

    def test_oracle_dispatch_loads_lint_render_for_library_entrypoints(self) -> None:
        lock = _library_lock()
        oracle = resolve_component_acceptance_oracle(
            FIXTURE_ROOT, "components/greeting-surface", lock
        )
        self.assertIsInstance(oracle, LintRenderAcceptance)
        self.assertEqual(oracle.component_name, COMPONENT_NAME)

    def test_library_without_oracle_document_stays_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(
                resolve_component_acceptance_oracle(
                    Path(temporary), "components/greeting-surface", _library_lock()
                )
            )

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
