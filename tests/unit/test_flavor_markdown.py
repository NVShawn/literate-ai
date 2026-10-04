"""Canonical Flavor Markdown authoring and compatibility-exit tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.flavor_markdown import (
    FLAVOR_MARKDOWN_SCHEMA,
    FlavorMarkdownError,
    parse_flavor_markdown,
)
from literate_ai.contracts import FlavorDefinition
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from scripts.migrate_flavor_authoring import migrate
from tests.support.fixtures_test_component_lock_planning import _fixture

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

    def test_legacy_json_migrates_without_semantic_definition_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            root = project / "flavors" / "lang-python"
            shutil.copytree(REPOSITORY / "flavors" / "lang-python", root)
            shutil.copytree(
                REPOSITORY
                / "skills"
                / "specification-to-source"
                / "python-portable-application",
                project
                / "skills"
                / "specification-to-source"
                / "python-portable-application",
            )
            canonical = root / "flavor.md"
            definition = resolved(canonical)
            canonical.unlink()
            legacy = root / "flavor.json"
            legacy.write_text(
                json.dumps(definition.to_dict(), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            migrated = migrate(legacy)
            self.assertEqual(resolved(migrated), definition)
            self.assertFalse(legacy.exists())

    def test_unknown_key_and_dual_authority_fail_closed(self) -> None:
        source = REPOSITORY / "flavors" / "lang-python" / "flavor.md"
        metadata, body = parse_authoring_markdown(
            source.read_bytes(), source=source.as_posix()
        )
        metadata["surprise"] = True
        with self.assertRaises(FlavorMarkdownError):
            parse_flavor_markdown(
                render_authoring_markdown(metadata, body), source="invalid flavor"
            )

        with tempfile.TemporaryDirectory() as directory:
            component, flavors = _fixture(Path(directory))
            path = flavors / "lang-python" / "flavor.md"
            (path.parent / "flavor.json").write_text(
                json.dumps(resolved(path).to_dict()), encoding="utf-8"
            )
            with self.assertRaises(ComponentLockPlanningError) as caught:
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )
            self.assertEqual(
                caught.exception.code, "component_lock.flavor_authority_ambiguous"
            )

    def test_markdown_body_is_part_of_selected_flavor_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            component, flavors = _fixture(Path(directory))
            planner = FilesystemComponentLockPlanner()
            first = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            path = flavors / "lang-python" / "flavor.md"
            path.write_bytes(
                path.read_bytes().replace(
                    b"The referenced specification",
                    b"The exact referenced specification",
                )
            )
            second = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            first_revisions = {
                item.flavor_revision.uri
                for item in first.nodes[0].flavor_candidates
                if item.status.value == "selected"
            }
            second_revisions = {
                item.flavor_revision.uri
                for item in second.nodes[0].flavor_candidates
                if item.status.value == "selected"
            }
            self.assertNotEqual(first_revisions, second_revisions)


if __name__ == "__main__":
    unittest.main()
