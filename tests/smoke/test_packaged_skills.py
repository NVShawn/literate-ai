from __future__ import annotations

import io
import json
import unittest

from literate_ai.cli import main
from literate_ai.source_to_specification import (
    builtin_skill_set,
    load_builtin_skill_catalog,
    resolve_skill_set,
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
    def test_installed_catalog_is_complete_and_resolvable(self):
        catalog = load_builtin_skill_catalog()
        selected = builtin_skill_set(catalog)
        resolved = resolve_skill_set(selected, catalog)
        self.assertEqual(tuple(skill.skill_id for skill in resolved), EXPECTED_SKILLS)

    def test_cli_lists_packaged_skills_without_a_checkout_path(self):
        status, output, errors = invoke("spec", "skills")
        self.assertEqual((status, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual(
            tuple(skill["skill_id"] for skill in payload["result"]["skills"]),
            EXPECTED_SKILLS,
        )


if __name__ == "__main__":
    unittest.main()
