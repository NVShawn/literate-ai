"""The Docker containerization Flavor is a pinned, composable deployment input."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.project_initialization import (
    FLAVOR_SELECTOR_CANONICAL_NAMES,
    KNOWN_FLAVOR_SELECTORS,
)
from literate_ai.cli.generation import load_flavor_catalog

REPOSITORY = Path(__file__).resolve().parents[2]


class DockerContainerFlavorCatalogTests(unittest.TestCase):
    def test_container_flavor_coordinates_and_capability(self) -> None:
        (docker,) = load_flavor_catalog((REPOSITORY / "flavors" / "deploy-docker",))

        self.assertEqual(docker.coordinate_uri, "flavor://literate-ai/deploy-docker")
        self.assertEqual(docker.axis, "deployment")
        self.assertEqual(docker.value, "docker")
        self.assertEqual(
            tuple(skill.skill_id for skill in docker.skills),
            ("docker-container-application",),
        )

        authoring = parse_flavor_markdown(
            (REPOSITORY / "flavors" / "deploy-docker" / "flavor.md").read_bytes(),
            source="flavors/deploy-docker/flavor.md",
        )
        provided = {capability["name"] for capability in authoring.provides}
        self.assertIn("deploy.container-image", provided)
        self.assertEqual(
            authoring.co_requisites,
            ("flavor://literate-ai/container-python",),
        )

    def test_python_container_base_is_digest_pinned(self) -> None:
        (base,) = load_flavor_catalog((REPOSITORY / "flavors" / "container-python",))
        self.assertEqual(base.coordinate_uri, "flavor://literate-ai/container-python")
        self.assertEqual(base.axis, "deployment")
        self.assertEqual(base.value, "python-container-base")

        definition = (
            REPOSITORY / "flavors" / "container-python" / "container-base.json"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "docker.io/library/python@sha256:cd04730b8511def3fbf14204d66a0c1536f290b8e896ed5a94cd64cb15ac1356",
            definition,
        )
        self.assertNotIn('"image": "python:', definition)

    def test_inheritance_contract_declared_in_specification(self) -> None:
        spec = (
            REPOSITORY / "flavors" / "deploy-docker" / "openspec" / "spec.md"
        ).read_text(encoding="utf-8")
        self.assertIn("deploy.container-base", spec)
        self.assertIn("DAG order", spec)
        # Every requirement carries at least one scenario.
        requirements = spec.count("### Requirement:")
        scenarios = spec.count("#### Scenario:")
        self.assertGreaterEqual(requirements, 4)
        self.assertGreaterEqual(scenarios, requirements)

    def test_selectors_resolve_to_the_deployment_axis(self) -> None:
        self.assertEqual(
            FLAVOR_SELECTOR_CANONICAL_NAMES.get("docker"),
            "flavor://literate-ai/deploy-docker",
        )
        self.assertEqual(KNOWN_FLAVOR_SELECTORS.get("deploy-docker"), "deployment")
        self.assertEqual(KNOWN_FLAVOR_SELECTORS.get("docker"), "deployment")

    def test_template_copy_is_byte_equal(self) -> None:
        for name in ("container-python", "deploy-docker"):
            source = REPOSITORY / "flavors" / name
            template = (
                REPOSITORY
                / "src"
                / "literate_ai"
                / "project_template"
                / "flavors"
                / name
            )
            source_files = sorted(
                path.relative_to(source) for path in source.rglob("*") if path.is_file()
            )
            template_files = sorted(
                path.relative_to(template)
                for path in template.rglob("*")
                if path.is_file()
            )
            self.assertEqual(source_files, template_files)
            for relative in source_files:
                self.assertEqual(
                    (source / relative).read_bytes(),
                    (template / relative).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
