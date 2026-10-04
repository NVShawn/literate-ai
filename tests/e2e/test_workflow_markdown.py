"""Human-authored workflow Markdown and normalized wire projection tests."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts import ProjectSourceGenerationCustody
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
)
from tests.support.fixtures_test_project_cli import copy_generation_catalogs, invoke

REPOSITORY = Path(__file__).resolve().parents[2]


class NamedWorkflowCatalogTests(unittest.TestCase):
    """Nested workflow directories are independent catalog entries.

    Components select one by its ordinary catalog-relative path. Production is
    the outer workflow; staging and dev nest underneath so the filesystem is
    the scoping mechanism. Each pairs with ``routing.json`` in the matching
    directory, the same sentinel pattern as ``workflow.md`` and ``SKILL.md``.
    """

    def _prepare_model_routing_sample(self, project: Path) -> Path:
        status, result = invoke(
            "init", str(project), "--empty", "--flavor", "python", "--flavor", "macos"
        )
        self.assertEqual(status, 0, result)
        copy_generation_catalogs(project)
        for relative in (
            "workflows/production/staging/dev/workflow.md",
            "routing/production/staging/dev/routing.json",
        ):
            shutil.copy2(REPOSITORY / relative, project / relative)
        shutil.copytree(REPOSITORY / "flavors", project / "flavors", dirs_exist_ok=True)
        component = project / "samples" / "model-routing"
        shutil.copytree(REPOSITORY / "samples" / "model-routing", component)
        metadata, _body = parse_authoring_markdown(
            (component / "component.md").read_bytes(), source="model-routing"
        )
        self.assertEqual(
            metadata["workflow_definition"]["uri"],
            "workflows/production/staging/dev/workflow.md",
        )
        self.assertEqual(
            metadata["routing_policy"]["uri"],
            "routing/production/staging/dev/routing.json",
        )
        status, result = invoke(
            "project", "documentation-review", str(project), "--record"
        )
        self.assertEqual(status, 0, result)
        status, result = invoke("lock", str(component), "--target=host")
        self.assertEqual(status, 0, result)
        return component

    def _assert_nested_plan(self, component: Path) -> dict:
        status, envelope = invoke("plan", str(component), "--target=host")
        self.assertEqual(status, 0, envelope)
        plan = envelope["result"]
        self.assertEqual(
            plan["workflow"]["reference"]["uri"],
            "workflows/production/staging/dev/workflow.md",
        )
        self.assertEqual(
            plan["routing"]["reference"]["uri"],
            "routing/production/staging/dev/routing.json",
        )
        self.assertEqual(
            [stage["stage_id"] for stage in plan["workflow"]["model_stages"]],
            ["plan", "generate"],
        )
        return plan

    def test_real_sample_locks_and_plans_with_nested_catalog(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            project = Path(directory) / "project"
            component = self._prepare_model_routing_sample(project)
            self._assert_nested_plan(component)
            workflow = project / "workflows/production/staging/dev/workflow.md"
            workflow.write_bytes(
                workflow.read_bytes() + b"\nChanged workflow authority.\n"
            )
            status, result = invoke("plan", str(component), "--target=host")
            self.assertEqual(status, 2, result)
            self.assertEqual(result["error"]["code"], "component_lock.stale")

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_real_sample_locks_plans_and_generates_with_nested_catalog(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            project = Path(directory) / "project"
            component = self._prepare_model_routing_sample(project)
            plan = self._assert_nested_plan(component)
            output = project / "generated" / "model-routing"
            status, envelope = invoke(
                "generate", str(component), "--target=host", "--output", str(output)
            )
            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            custody = ProjectSourceGenerationCustody.from_dict(
                result["standard_source_generation"]
            )
            self.assertEqual(
                result["component_lock_identity"],
                plan["resolution"]["component_lock_identity"],
            )
            self.assertTrue(custody.successful)
            self.assertEqual(
                custody.root_revision.uri, plan["resolution"]["root_revision_identity"]
            )
            self.assertEqual(len(custody.components), 1)
            self.assertTrue(tuple(output.rglob("main.py")))


if __name__ == "__main__":
    unittest.main()
