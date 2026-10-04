"""CLI generation admission tests for canonical Component-lock authority."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.cli.generation as generation_cli
from literate_ai.adapters.component_lock_planning import (
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from tests.support.fixtures_test_component_lock_planning import _fixture

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SELECTORS = ("+macos", "+python")
_TARGET = "macos-host"


def _prepare_locked(arguments):
    return FilesystemLockedGenerationApplicationAdapter().prepare(
        GenerationPreparationRequest(
            component_root=Path(arguments.specification),
            target_name=arguments.target,
            flavor_selectors=tuple(arguments.flavor),
            flavor_roots=tuple(Path(item) for item in arguments.flavor_root),
            recipe_id=arguments.recipe_id,
        )
    )


def _generation_fixture(root: Path) -> tuple[Path, Path]:
    component, flavors = _fixture(root)
    component_manifest = component / "component.md"
    component_manifest.write_text(
        component_manifest.read_text(encoding="utf-8").replace(
            "workflows/host.json", "workflows/host.md"
        ),
        encoding="utf-8",
    )
    (root / "skills" / "implement.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "locked-generation-test",
                "version": "1.0.0",
                "title": "Locked generation test",
                "stages": ["plan", "generate"],
                "dependencies": [],
                "instructions": "Generate the exact portable test application.",
                "limitations": ["Use only locked inputs."],
                "trust": "test-reviewed",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(
        _REPOSITORY_ROOT / "workflows" / "sample-host.md",
        root / "workflows" / "host.md",
    )
    shutil.copy2(
        _REPOSITORY_ROOT / "routing" / "sample-host.json",
        root / "routing" / "default.json",
    )
    python_manifest = flavors / "lang-python" / "flavor.md"
    python_flavor, body = parse_authoring_markdown(
        python_manifest.read_bytes(), source=python_manifest.as_posix()
    )
    python_flavor["authoring_inputs"] = []
    python_manifest.write_bytes(render_authoring_markdown(python_flavor, body))
    return component, flavors


def _write_lock(component: Path, flavors: Path):
    plan = FilesystemComponentLockPlanner().plan(
        component,
        target_name=_TARGET,
        flavor_selectors=_SELECTORS,
        flavor_roots=(flavors,),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    ComponentResolutionAuditStore(component, _TARGET).update(result.catalog_audit)
    ComponentLockStore(component).update(result.lock)
    return result.lock


def _arguments(component: Path, flavors: Path, output: Path, **overrides):
    values = {
        "specification": str(component),
        "target": _TARGET,
        "flavor": list(_SELECTORS),
        "flavor_root": [str(flavors)],
        "recipe_id": None,
        "output": str(output),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LockedGenerationCliTests(unittest.TestCase):
    def test_prepare_uses_only_locked_authority_and_not_legacy_resolvers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _generation_fixture(root)
            lock = _write_lock(component, flavors)
            coding_cli = root / "bin" / "codex"
            coding_cli.parent.mkdir()
            coding_cli.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
            coding_cli.chmod(0o755)
            with (
                mock.patch(
                    "literate_ai.application.component_lock_resolution.ComponentLockResolver"
                ) as lock_resolver,
                mock.patch.dict(
                    os.environ,
                    {
                        "CODING_CLI": "codex",
                        "PATH": os.pathsep.join(
                            (str(coding_cli.parent), os.environ["PATH"])
                        ),
                    },
                ),
            ):
                prepared = _prepare_locked(
                    _arguments(component, flavors, root.parent / "generated")
                )
                report = generation_cli.plan_from_args(
                    _arguments(component, flavors, root.parent / "generated")
                )

            lock_resolver.assert_not_called()
            self.assertFalse(hasattr(generation_cli, "ComponentComposer"))
            self.assertFalse(hasattr(generation_cli, "FlavorResolver"))
            self.assertEqual(prepared.recipe.component_lock_identity, lock.identity)
            locked_root = next(
                node.revision
                for node in lock.nodes
                if node.revision.identity == lock.root_revision
            )
            self.assertEqual(
                prepared.definition.workflow_definition,
                locked_root.workflow_definition,
            )
            self.assertEqual(
                prepared.definition.routing_policy,
                locked_root.routing_policy,
            )
            self.assertEqual(
                prepared.recipe.managed_sbom_graph.resolved_graph_identity,
                lock.identity,
            )
            self.assertEqual(
                report["resolution"]["component_lock_identity"], lock.identity.uri
            )
            self.assertIn(
                "acceptance/execution.json",
                {item.path for item in prepared.recipe.documents},
            )
            prompt = prepared.recipe.prompt()
            self.assertNotIn('{"stdout":"hello\\n"}', prompt)
            self.assertNotIn("Generation-safe invocation contract", prompt)

    def test_missing_stale_and_mismatched_locks_fail_before_agent_invocation(
        self,
    ) -> None:
        cases = ("missing", "stale", "target", "selector")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                component, flavors = _generation_fixture(root)
                if case != "missing":
                    _write_lock(component, flavors)
                overrides = {}
                if case == "stale":
                    specification = component / "specs" / "spec.md"
                    specification.write_text(
                        specification.read_text(encoding="utf-8") + "\nDrift.\n",
                        encoding="utf-8",
                    )
                elif case == "target":
                    overrides["target"] = "linux-host"
                elif case == "selector":
                    overrides["flavor"] = ["+macos", "-python"]
                with mock.patch.object(
                    generation_cli.FilesystemStandardSourceGenerationAdapter,
                    "from_environment",
                ) as generator:
                    with self.assertRaises(generation_cli.CliFailure):
                        generation_cli.generate_from_args(
                            _arguments(
                                component,
                                flavors,
                                root.parent / "generated",
                                **overrides,
                            )
                        )
                generator.assert_not_called()

    def test_live_closure_is_rechecked_immediately_before_agent_egress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _generation_fixture(root)
            _write_lock(component, flavors)
            specification = component / "specs" / "spec.md"
            fake_adapter = mock.Mock()

            def construct_after_drift(**_kwargs):
                specification.write_text(
                    specification.read_text(encoding="utf-8") + "\nLate drift.\n",
                    encoding="utf-8",
                )
                return fake_adapter

            with (
                mock.patch.object(
                    generation_cli,
                    "_require_reviewed_component_authority",
                    return_value=None,
                ),
                mock.patch.object(
                    generation_cli.FilesystemStandardSourceGenerationAdapter,
                    "from_environment",
                    side_effect=construct_after_drift,
                ),
                self.assertRaises(generation_cli.CliFailure) as stale,
            ):
                generation_cli.generate_from_args(
                    _arguments(component, flavors, root.parent / "generated")
                )

            self.assertEqual(stale.exception.code, "component_lock.stale")
            fake_adapter.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
