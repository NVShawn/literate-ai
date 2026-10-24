"""Authoring-only guards; no network calls, speech billing or framework changes."""

import copy
import io
import json
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path

from prepare import prepare, validate
from speech import digest, read_key, request, validate_wav, voice_ids

BASE = Path(__file__).parent


def wav_data(seconds=0.1):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        w.writeframes(bytes(round(seconds * 24000) * 2))
    return out.getvalue()


class AuthoringTests(unittest.TestCase):
    def setUp(self):
        self.story = json.loads((BASE / "story.json").read_text())

    def test_story_sources_resolve_to_real_project_files(self):
        root = BASE.parents[2]
        for name in self.story["sources"]:
            self.assertTrue((root / name).is_file(), name)

    def test_rejects_unbounded_time_and_unsafe_ids(self):
        long = copy.deepcopy(self.story)
        for turn in long["turns"]:
            turn["seconds"] = 30
        with self.assertRaisesRegex(ValueError, "three-minute"):
            validate(long)
        for value in [float("nan"), float("inf"), -1, True, 500]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.story)
                bad["turns"][0]["seconds"] = value
                validate(bad)
        for value in ["../secret", "/absolute", "duplicate"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.story)
                bad["turns"][0]["id"] = value
                if value == "duplicate":
                    bad["turns"][1]["id"] = value
                validate(bad)

    def test_animatic_is_explicit_silent_and_preserves_prior_output(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "cut"
            data = prepare(BASE / "story.json", out, animatic=True)
            self.assertTrue(data["animatic"])
            self.assertIsNone(data["audio"])
            self.assertTrue(all(v == 0 for v in data["envelope"]))
            self.assertAlmostEqual(
                validate_wav((out / "audio.wav").read_bytes())["seconds"],
                data["duration"],
            )
            self.assertEqual(
                len(list((out / "assets").glob("*.wav"))), len(self.story["turns"])
            )
            with self.assertRaises(ValueError):
                prepare(BASE / "story.json", out, animatic=True)

    def test_recorded_audio_uses_measured_timing_and_refuses_stale_story(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            recordings = root / "recordings"
            recordings.mkdir()
            raw = (BASE / "story.json").read_bytes()
            audio = wav_data(0.1)
            for turn in self.story["turns"]:
                (recordings / (turn["id"] + ".wav")).write_bytes(audio)
                receipt = {
                    "story_sha256": digest(raw),
                    "text_sha256": digest(turn["text"].encode()),
                    "audio_sha256": digest(audio),
                    "turn_id": turn["id"],
                    "speaker": turn["speaker"],
                }
                (recordings / (turn["id"] + ".voice.json")).write_text(
                    json.dumps(receipt)
                )
            result = prepare(BASE / "story.json", root / "valid", recordings)
            self.assertAlmostEqual(result["turns"][0]["end"], 0.1)
            changed = copy.deepcopy(self.story)
            changed["turns"][0]["text"] = "Changed words."
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "stale"):
                prepare(changed_path, root / "stale", recordings)
            self.assertFalse((root / "stale").exists())

    def test_corrupt_or_truncated_wav_is_refused(self):
        for data in [b"not audio", wav_data()[:-10]]:
            with self.assertRaises(ValueError):
                validate_wav(data)

    def test_missing_recording_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            with self.assertRaises(FileNotFoundError):
                prepare(BASE / "story.json", root / "missing", root)
            self.assertFalse((root / "missing").exists())

    def test_catalog_handles_nested_response_without_guessing_ids(self):
        self.assertEqual(
            voice_ids(
                {
                    "voices": [
                        {"name": "Magpie-Multilingual.EN-US.Aria"},
                        {"description": "not a voice"},
                    ]
                }
            ),
            {"Magpie-Multilingual.EN-US.Aria"},
        )
        self.assertEqual(voice_ids({"voices": []}), set())

    def test_auth_failure_does_not_leak_key_or_retry(self):
        calls = []

        def denied(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(
                req.full_url, 401, "private service body", {}, None
            )

        with self.assertRaises(ValueError) as error:
            request("/list_voices", "test-secret", opener=denied)
        self.assertEqual(len(calls), 1)
        self.assertIn("401", str(error.exception))
        self.assertNotIn("test-secret", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    def test_key_file_supports_a_label_but_refuses_ambiguity(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "key"
            p.write_text("NVIDIA_BUILD_API_KEY=nvapi-test-key")
            self.assertEqual(read_key(p), "nvapi-test-key")
            p.write_text("nvapi-one nvapi-two")
            with self.assertRaises(ValueError):
                read_key(p)


if __name__ == "__main__":
    unittest.main()
