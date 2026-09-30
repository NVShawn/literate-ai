"""ADR 0010 Flavor names migrate transactionally and preserve legacy inputs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.project_initialization import (
    canonical_flavor_catalog_collisions,
    canonical_flavor_coordinate,
    canonical_flavor_selector,
    initialize_project,
)
from literate_ai.cli.catalog import migrate_flavor_names
from literate_ai.contracts import RepositoryParentSelection


class FlavorNameMigrationTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "flavors" / "pip").mkdir(parents=True)
        (root / "flavors" / "pip" / "flavor.md").write_text(
            '---\nname: "package.pip"\ntarget: "pip"\n---\n# pip\n',
            encoding="utf-8",
        )
        (root / "components" / "app").mkdir(parents=True)
        (root / "components" / "app" / "component.lock.json").write_text(
            json.dumps({"flavor": "flavor://literate-ai/package.pip"}) + "\n",
            encoding="utf-8",
        )
        (root / "literate.project.json").write_text(
            json.dumps(
                {
                    "flavor_roots": ["flavors"],
                    "component_roots": ["components"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _project_with_workflow_and_routing(self, root: Path) -> None:
        self._project(root)
        (root / "workflows").mkdir(parents=True)
        (root / "workflows" / "build.md").write_text(
            '---\nflavor: "flavor://literate-ai/package.pip"\n---\n# build\n',
            encoding="utf-8",
        )
        (root / "routing").mkdir(parents=True)
        (root / "routing" / "policy.json").write_text(
            json.dumps({"flavor": "flavor://literate-ai/package.pip"}) + "\n",
            encoding="utf-8",
        )
        manifest_path = root / "literate.project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workflow_roots"] = ["workflows"]
        manifest["routing_roots"] = ["routing"]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    def test_record_rewrites_stale_workflow_and_routing_taxonomy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project_with_workflow_and_routing(root)

            recorded = migrate_flavor_names(root, record=True)

            self.assertEqual(recorded["state"], "recorded")
            self.assertIn(
                {"path": "workflows/build.md"},
                recorded["rewrites"],
            )
            self.assertIn(
                {"path": "routing/policy.json"},
                recorded["rewrites"],
            )
            self.assertIn(
                "flavor://literate-ai/package-pip",
                (root / "workflows" / "build.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "flavor://literate-ai/package-pip",
                (root / "routing" / "policy.json").read_text(encoding="utf-8"),
            )

    def test_dry_run_is_non_mutating_and_record_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            planned = migrate_flavor_names(root, record=False)

            self.assertEqual(planned["state"], "planned")
            self.assertIn(
                {"from": "flavors/pip", "to": "flavors/package-pip"},
                planned["moves"],
            )
            self.assertEqual(planned["removals"], [])
            self.assertEqual(
                before,
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )

            recorded = migrate_flavor_names(root, record=True)

            self.assertEqual(recorded["state"], "recorded")
            self.assertFalse((root / "flavors" / "pip").exists())
            self.assertTrue((root / "flavors" / "package-pip" / "flavor.md").is_file())
            self.assertIn(
                'name: "package-pip"',
                (root / "flavors" / "package-pip" / "flavor.md").read_text(),
            )
            self.assertIn(
                "flavor://literate-ai/package-pip",
                (root / "components" / "app" / "component.lock.json").read_text(),
            )

    def test_record_removes_alias_when_canonical_directory_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            package_pip = root / "flavors" / "package-pip"
            package_pip.mkdir()
            (package_pip / "flavor.md").write_text(
                '---\nname: "package-pip"\ntarget: "pip"\n---\n# pip\n',
                encoding="utf-8",
            )

            planned = migrate_flavor_names(root, record=False)

            self.assertEqual(planned["moves"], [])
            self.assertIn(
                {
                    "path": "flavors/pip",
                    "canonical": "flavors/package-pip",
                },
                planned["removals"],
            )
            self.assertTrue((root / "flavors" / "pip" / "flavor.md").is_file())

            recorded = migrate_flavor_names(root, record=True)

            self.assertEqual(recorded["state"], "recorded")
            self.assertFalse((root / "flavors" / "pip").exists())
            self.assertTrue((package_pip / "flavor.md").is_file())
            self.assertIn(
                "flavor://literate-ai/package-pip",
                (root / "components" / "app" / "component.lock.json").read_text(),
            )

    def test_legacy_selectors_resolve_to_canonical_coordinates(self) -> None:
        self.assertEqual(
            canonical_flavor_selector("+pip"),
            "+flavor://literate-ai/package-pip",
        )
        self.assertEqual(
            canonical_flavor_selector("+python"),
            "+flavor://literate-ai/lang-python",
        )
        self.assertEqual(
            canonical_flavor_selector("+cpp"),
            canonical_flavor_selector("+lang-cpp"),
        )
        self.assertEqual(
            canonical_flavor_selector("+lang-cpp"),
            "+flavor://literate-ai/lang-cpp",
        )

    def test_dotted_selectors_fail_closed(self) -> None:
        for legacy in ("+package.pip", "+flavor://literate-ai/package.pip"):
            with self.subTest(selector=legacy):
                with self.assertRaises(ValueError):
                    canonical_flavor_selector(legacy)

    def test_bare_and_qualified_cpp_selectors_stamp_the_same_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bare = Path(directory) / "bare"
            qualified = Path(directory) / "qualified"
            initialize_project(
                bare,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+cpp", "+macos"),
            )
            initialize_project(
                qualified,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+lang-cpp", "+os-macos"),
            )
            self.assertTrue((bare / "flavors" / "lang-cpp" / "flavor.md").is_file())
            self.assertFalse((bare / "flavors" / "cpp").exists())
            self.assertEqual(
                (bare / "flavors" / "lang-cpp" / "flavor.md").read_bytes(),
                (qualified / "flavors" / "lang-cpp" / "flavor.md").read_bytes(),
            )

    def test_canonical_flavor_coordinate_maps_packaging_and_document_aliases(
        self,
    ) -> None:
        self.assertEqual(
            canonical_flavor_coordinate("pip"),
            "flavor://literate-ai/package-pip",
        )
        self.assertEqual(
            canonical_flavor_coordinate("package.pip"),
            "flavor://literate-ai/package-pip",
        )
        self.assertEqual(
            canonical_flavor_coordinate("google-workspace"),
            "flavor://literate-ai/doc-google-workspace",
        )
        self.assertEqual(
            canonical_flavor_coordinate("flavor://literate-ai/microsoft-365"),
            "flavor://literate-ai/doc-microsoft-365",
        )

    def test_duplicate_alias_directory_fails_closed(self) -> None:
        collisions = canonical_flavor_catalog_collisions(
            (
                (
                    "flavors/pip",
                    "pip",
                    "flavor://literate-ai/package.pip",
                ),
                (
                    "flavors/package-pip",
                    "package-pip",
                    "flavor://literate-ai/package-pip",
                ),
                (
                    "flavors/google-workspace",
                    "google-workspace",
                    "flavor://literate-ai/google-workspace",
                ),
                (
                    "flavors/doc-google-workspace",
                    "doc-google-workspace",
                    "flavor://literate-ai/doc-google-workspace",
                ),
            )
        )

        self.assertEqual(
            collisions,
            (
                (
                    "flavor://literate-ai/doc-google-workspace",
                    (
                        "flavors/doc-google-workspace",
                        "flavors/google-workspace",
                    ),
                ),
                (
                    "flavor://literate-ai/package-pip",
                    ("flavors/package-pip", "flavors/pip"),
                ),
            ),
        )
        self.assertEqual(
            canonical_flavor_catalog_collisions(
                (
                    (
                        "flavors/lang-python",
                        "lang-python",
                        "flavor://literate-ai/lang-python",
                    ),
                    (
                        "flavors/package-pip",
                        "package-pip",
                        "flavor://literate-ai/package-pip",
                    ),
                )
            ),
            (),
        )

    def test_init_from_deprecated_selectors_stamps_canonical_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "canonical-packaging"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+pip", "+python", "+macos"),
            )

            self.assertTrue(
                (target / "flavors" / "package-pip" / "flavor.md").is_file()
            )
            self.assertFalse((target / "flavors" / "pip").exists())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "canonical-docs"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+google-workspace", "+python", "+macos"),
            )

            self.assertTrue(
                (target / "flavors" / "doc-google-workspace" / "flavor.md").is_file()
            )
            self.assertFalse((target / "flavors" / "google-workspace").exists())


if __name__ == "__main__":
    unittest.main()
