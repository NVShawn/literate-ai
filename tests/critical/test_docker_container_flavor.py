"""The Docker containerization Flavor is a pinned, composable deployment input."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.cli.generation import load_flavor_catalog

REPOSITORY = Path(__file__).resolve().parents[2]


class DockerContainerFlavorCatalogTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
