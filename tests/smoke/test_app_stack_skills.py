"""App-stack catalog skills and flavors stay byte-identical to the init template."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPO / "src" / "literate_ai" / "project_template"
_STACK_SKILLS = (
    "mcp-application/SKILL.md",
    "mcp-application/webmcp/SKILL.md",
    "frontend-application/SKILL.md",
    "frontend-application/react-application/SKILL.md",
    "frontend-application/react-dashboard-application/SKILL.md",
    "backend-application/SKILL.md",
    "backend-application/python-service-application/SKILL.md",
    "backend-application/rust-service-application/SKILL.md",
    "backend-application/scheduler-lease-worker/SKILL.md",
    "backend-application/durable-split-service/SKILL.md",
    "backend-application/rest-application/SKILL.md",
    "backend-application/grpc-application/SKILL.md",
)
_STACK_FLAVORS = ("ui-react", "lang-javascript", "package-npm")


class AppStackSkillCatalogTests(unittest.TestCase):
    def test_catalog_and_template_copies_are_byte_identical(self) -> None:
        relatives = [
            Path("skills", "specification-to-source", *skill.split("/"))
            for skill in _STACK_SKILLS
        ] + [Path("flavors", flavor, "flavor.md") for flavor in _STACK_FLAVORS]
        for relative in relatives:
            with self.subTest(path=relative.as_posix()):
                self.assertEqual(
                    (REPO / relative).read_bytes(),
                    (TEMPLATE_ROOT / relative).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
