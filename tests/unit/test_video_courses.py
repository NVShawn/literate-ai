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
    plan_course,
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

    def test_init_never_overwrites(self):
        before = self.manifest.read_bytes()
        with self.assertRaises(FileExistsError):
            init_course(self.manifest)
        self.assertEqual(before, self.manifest.read_bytes())

    def test_evidence_and_narration_drift_change_identity(self):
        document = json.loads(self.manifest.read_text())
        evidence = self.root / "evidence.txt"
        evidence.write_text("one test passed")
        document["evidence"] = [evidence.name]
        self.save(document)
        first = plan_course(self.manifest)["source_identity"]
        evidence.write_text("two tests passed")
        second = plan_course(self.manifest)["source_identity"]
        self.assertNotEqual(first, second)
        document["scenes"][0]["dialogue"][0]["text"] = "A different question."
        self.save(document)
        self.assertNotEqual(second, plan_course(self.manifest)["source_identity"])

    def test_paths_and_dialogue_fail_closed(self):
        document = json.loads(self.manifest.read_text())
        for path in ("../outside.txt", "/absolute.txt", "missing.txt"):
            with self.subTest(path=path):
                document["evidence"] = [path]
                self.save(document)
                with self.assertRaises(VideoError):
                    plan_course(self.manifest)
        document["evidence"] = []
        document["scenes"][0]["dialogue"][0]["speaker"] = "unknown"
        self.save(document)
        with self.assertRaises(VideoError):
            plan_course(self.manifest)

    def test_symlink_cannot_escape_package(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.txt"
            outside.write_text("not a course asset")
            try:
                (self.root / "link.txt").symlink_to(outside)
            except OSError:
                self.skipTest("host cannot create symlinks")
            document = json.loads(self.manifest.read_text())
            document["evidence"] = ["link.txt"]
            self.save(document)
            with self.assertRaises(VideoError):
                plan_course(self.manifest)

    def test_missing_tools_preserve_previous_outputs(self):
        font = self.root / "font.ttf"
        font.write_bytes(b"font")
        output = self.root / "output"
        output.mkdir()
        previous = output / "previous.mp4"
        previous.write_bytes(b"accepted prior output")
        with patch("shutil.which", return_value=None), self.assertRaises(VideoError):
            build_course(self.manifest, output, font=font, backend="recorded")
        self.assertEqual(list(output.iterdir()), [previous])

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
        self.save(document)
        result = build_course(
            self.manifest, self.root / "renders", font=font, backend="recorded"
        )
        receipt = Path(result["receipt"])
        self.assertTrue(verify_course(receipt, manifest=self.manifest)["accepted"])
        self.assertEqual(result["caption_count"], 2)
        document["title"] = "Changed tutorial"
        self.save(document)
        with self.assertRaisesRegex(VideoError, "source or evidence changed"):
            verify_course(receipt, manifest=self.manifest)
        (receipt.parent / "first-project.srt").write_text("corrupt")
        with self.assertRaisesRegex(VideoError, "digest mismatch"):
            verify_course(receipt)
