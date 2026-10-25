from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "docs" / "courses" / "full-onboarding"
SPEC = importlib.util.spec_from_file_location(
    "full_onboarding_prepare", SOURCE / "prepare.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(MODULE)
finally:
    sys.dont_write_bytecode = _DONT_WRITE_BYTECODE


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def silent_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(bytes(round(seconds * rate) * 2))
    return target.getvalue()


class FullOnboardingPrepareTests(unittest.TestCase):
    def test_measured_audio_and_receipts_produce_chapter_and_course_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            story = {
                "id": "test-film",
                "title": "Test film",
                "speakers": {"sam": "Sam", "litai": "Literate AI"},
                "chapters": [
                    {
                        "id": "01",
                        "title": "Test chapter",
                        "shot": "pipeline",
                        "turns": [
                            {
                                "id": "01-01",
                                "speaker": "sam",
                                "voice": "Magpie-Multilingual.EN-US.Jason.Neutral",
                                "text": "One bounded line.",
                                "performance": {
                                    "play": "Point.",
                                    "listen": "Look.",
                                    "focus": "Card.",
                                },
                            }
                        ],
                    }
                ],
            }
            story_path = root / "story.json"
            story_bytes = (json.dumps(story, indent=2) + "\n").encode()
            story_path.write_bytes(story_bytes)
            audio_dir = root / "voices"
            audio_dir.mkdir()
            audio = silent_wav()
            (audio_dir / "01-01.wav").write_bytes(audio)
            receipt = {
                "story_sha256": digest(story_bytes),
                "turn_id": "01-01",
                "speaker": "sam",
                "text_sha256": digest(b"One bounded line."),
                "audio_sha256": digest(audio),
                "voice": "Magpie-Multilingual.EN-US.Jason.Neutral",
            }
            (audio_dir / "01-01.voice.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            output = root / "package"
            result = MODULE.prepare(story_path, audio_dir, output)
            self.assertEqual(len(result["chapters"]), 1)
            self.assertEqual(len(result["recording_assets"]), 1)
            self.assertTrue((output / "chapters" / "01" / "scene.json").is_file())
            self.assertTrue((output / "course.json").is_file())


if __name__ == "__main__":
    unittest.main()
