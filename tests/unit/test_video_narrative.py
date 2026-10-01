"""Repository-owned narrative and pronunciation checks, not a generic cast policy."""

import json
import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class VideoNarrativeTests(unittest.TestCase):
    def test_pronunciation_preserves_display_text(self):
        producer = runpy.run_path(str(ROOT / "docs/courses/play/produce.py"))
        original = "Litai uses AI, C++, CLI, SSH and TinyXML2."
        spoken = producer["spoken_text"](original)
        self.assertEqual(original, "Litai uses AI, C++, CLI, SSH and TinyXML2.")
        self.assertIn("Lit A. I.", spoken)
        self.assertIn("uses A. I.", spoken)
        self.assertIn("C plus plus", spoken)
        self.assertIn("C. L. I.", spoken)
        self.assertIn("S. S. H.", spoken)
        self.assertEqual(producer["caption"](original), original)

    def test_replayed_captures_exist_and_presenter_leads(self):
        story = json.loads((ROOT / "docs/courses/play/story.json").read_text())
        sessions = json.loads(
            (
                ROOT / "media/courses/sam-meets-literate-ai/package/sessions.json"
            ).read_text()
        )
        word_counts = {speaker: 0 for speaker in story["voices"]}
        for turn in story["turns"]:
            word_counts[turn["speaker"]] += len(turn["text"].split())
            if turn["shot"] == "terminal":
                self.assertIn(turn["capture"], sessions["excerpts"])
        self.assertGreater(word_counts["litai"], 2 * word_counts["sam"])
        # Planning ceiling only: rendered media still requires timing/listening review.
        self.assertLessEqual(sum(word_counts.values()), 1300)
        self.assertEqual(story["target_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
