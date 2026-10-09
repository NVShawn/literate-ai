from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from literate_ai.cli import main
from literate_ai.source_to_specification import (
    builtin_skill_set,
    load_builtin_skill_catalog,
    resolve_skill_set,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_SKILLS = REPO_ROOT / "skills" / "source-to-specification"
PACKAGED_SKILLS = files("literate_ai.source_to_specification.builtin_skills")
STATE_MACHINE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "source_to_specification" / "state-machine"
)
EXPECTED_SKILLS = (
    "architecture",
    "api-surface",
    "behavior-state",
    "tests",
    "security",
    "operations",
)


def invoke(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


class PackagedSkillTests(unittest.TestCase):
    def test_packaged_manifests_stay_in_sync_with_repository_manifests(self):
        repository_ids = {
            path.parent.name for path in REPOSITORY_SKILLS.glob("*/SKILL.md")
        }
        packaged_ids = {
            item.name
            for item in PACKAGED_SKILLS.iterdir()
            if item.is_dir() and item.joinpath("SKILL.md").is_file()
        }
        self.assertEqual(packaged_ids, repository_ids)
        for skill_id in sorted(repository_ids):
            with self.subTest(skill_id=skill_id):
                self.assertEqual(
                    PACKAGED_SKILLS.joinpath(skill_id, "SKILL.md").read_bytes(),
                    (REPOSITORY_SKILLS / skill_id / "SKILL.md").read_bytes(),
                )

    def test_installed_catalog_is_complete_and_resolvable(self):
        catalog = load_builtin_skill_catalog()
        selected = builtin_skill_set(catalog)
        resolved = resolve_skill_set(selected, catalog)
        self.assertEqual(tuple(skill.skill_id for skill in resolved), EXPECTED_SKILLS)

    def test_detected_languages_select_exact_translators_after_common_skills(self):
        catalog = load_builtin_skill_catalog()
        selected = builtin_skill_set(
            catalog,
            languages=("typescript", "python", "rust", "cpp", "elixir"),
        )
        resolved = resolve_skill_set(selected, catalog)
        self.assertEqual(
            tuple(skill.skill_id for skill in resolved),
            (
                *EXPECTED_SKILLS,
                "language-python",
                "language-cpp",
                "language-elixir",
                "language-rust",
                "language-javascript",
            ),
        )
        self.assertIn("python", selected.skill_set_id)
        self.assertIn("javascript", selected.skill_set_id)

    def test_cli_lists_packaged_skills_without_a_checkout_path(self):
        status, output, errors = invoke("spec", "skills")
        self.assertEqual((status, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual(
            tuple(skill["skill_id"] for skill in payload["result"]["skills"]),
            EXPECTED_SKILLS,
        )

    def test_cli_falls_back_to_packaged_skills_for_standalone_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            standalone = Path(temporary) / "standalone"
            shutil.copytree(STATE_MACHINE_FIXTURE, standalone)
            status, output, errors = invoke(
                "spec", "derive", str(standalone / "case.json")
            )
        self.assertEqual((status, errors), (0, ""))
        self.assertTrue(json.loads(output)["ok"])


if __name__ == "__main__":
    unittest.main()
