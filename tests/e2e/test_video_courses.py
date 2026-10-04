"""Course authoring works without media tools; integrity rejects stale inputs."""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli import main
from literate_ai.video_courses import (
    VideoError,
    build_course,
    init_course,
    verify_course,
)


class VideoCourseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "course.json"
        init_course(self.manifest)

    def save(self, value):
        self.manifest.write_text(json.dumps(value), encoding="utf-8")

    def test_public_cli_plan_has_no_tool_or_execution_requirement(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch(
            "subprocess.run", side_effect=AssertionError("unexpected execution")
        ):
            status = main(
                ["video", "plan", str(self.manifest)], stdout=output, stderr=errors
            )
        self.assertEqual(status, 0, errors.getvalue())
        self.assertFalse(
            json.loads(output.getvalue())["result"]["executes_demo_commands"]
        )
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_real_media_roundtrip_and_tamper_detection(self):
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "magick")):
            self.skipTest("optional media tools unavailable")
        # Host-specific font discovery belongs in tests, never in product defaults.
        fonts = [
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        font = next((path for path in fonts if path.is_file()), None)
        if font is None:
            self.skipTest("no test font available")
        audio = self.root / "tone.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.25",
                str(audio),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        document = json.loads(self.manifest.read_text())
        for turn in document["scenes"][0]["dialogue"]:
            turn["audio"] = audio.name
        document["embed_subtitles"] = False
        document["audio_bitrate_kbps"] = 80
        self.save(document)
        result = build_course(
            self.manifest, self.root / "renders", font=font, backend="recorded"
        )
        receipt = Path(result["receipt"])
        self.assertTrue(verify_course(receipt, manifest=self.manifest)["accepted"])
        self.assertEqual(result["caption_count"], 1)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(receipt.parent / "first-project.mp4"),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotIn(
            "subtitle", {s["codec_type"] for s in json.loads(probe.stdout)["streams"]}
        )
        document["embed_subtitles"] = True
        self.save(document)
        embedded = build_course(
            self.manifest, self.root / "embedded", font=font, backend="recorded"
        )
        self.assertTrue(
            verify_course(Path(embedded["receipt"]), manifest=self.manifest)["accepted"]
        )
        document["title"] = "Changed tutorial"
        self.save(document)
        with self.assertRaisesRegex(VideoError, "source or evidence changed"):
            verify_course(receipt, manifest=self.manifest)
        (receipt.parent / "first-project.srt").write_text("corrupt")
        with self.assertRaisesRegex(VideoError, "digest mismatch"):
            verify_course(receipt)
