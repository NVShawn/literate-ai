from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "full_onboarding_compile", SOURCE / "compile_story.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FullOnboardingCompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.story = MODULE.compile_narrative(
            (SOURCE / "NARRATIVE.md").read_text(encoding="utf-8")
        )

    def test_reviewed_script_has_complete_bounded_structure(self) -> None:
        self.assertEqual(len(self.story["chapters"]), 12)
        self.assertEqual(len(self.story["turns"]), 86)
        self.assertTrue(
            all(
                chapter["animatic_seconds"] <= 180 for chapter in self.story["chapters"]
            )
        )

    def test_every_turn_has_exact_cast_and_complete_performance_direction(self) -> None:
        for turn in self.story["turns"]:
            self.assertIn(turn["speaker"], {"sam", "litai"})
            self.assertEqual(turn["voice"], MODULE.VOICE[turn["speaker"]])
            self.assertEqual(set(turn["performance"]), {"play", "listen", "focus"})
            self.assertTrue(all(turn["performance"].values()))

    def test_spoken_text_excludes_stage_direction_markup(self) -> None:
        for turn in self.story["turns"]:
            self.assertNotIn("*Play:*", turn["text"])
            self.assertNotIn("*Listen:*", turn["text"])
            self.assertNotIn("*Focus/hold:*", turn["text"])


if __name__ == "__main__":
    unittest.main()
