"""Canonical Flavor Markdown authoring and compatibility-exit tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.adapters.flavor_markdown import (
    FLAVOR_MARKDOWN_SCHEMA,
    parse_flavor_markdown,
)
from literate_ai.contracts import FlavorDefinition
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
)

REPOSITORY = Path(__file__).resolve().parents[2]


def resolved(path: Path) -> FlavorDefinition:
    return parse_flavor_markdown(path.read_bytes(), source=path.as_posix()).resolve(
        lambda uri: path.parent.joinpath(*Path(uri).parts).read_bytes()
    )


class FlavorMarkdownTests(unittest.TestCase):
    def test_repository_flavors_are_canonical_resolvable_markdown(self) -> None:
        paths = tuple(sorted((REPOSITORY / "flavors").glob("*/flavor.md")))
        self.assertTrue(paths, "repository must declare at least one Flavor")
        coordinates: set[tuple[str, str]] = set()
        for path in paths:
            with self.subTest(path=path):
                metadata, body = parse_authoring_markdown(
                    path.read_bytes(), source=path.as_posix()
                )
                self.assertEqual(metadata["schema"], FLAVOR_MARKDOWN_SCHEMA)
                self.assertIn("# ", body)
                definition = resolved(path)
                self.assertEqual(definition.supported_targets, (metadata["target"],))
                self.assertTrue(definition.specification_fragments)
                coordinate = (str(metadata["namespace"]), str(metadata["name"]))
                self.assertNotIn(coordinate, coordinates)
                coordinates.add(coordinate)


if __name__ == "__main__":
    unittest.main()
