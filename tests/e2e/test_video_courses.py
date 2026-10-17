"""Course authoring works without media tools; integrity rejects stale inputs."""

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai import video_courses
from literate_ai.cli import main
from literate_ai.video_courses import (
    MEDIA_DURATION,
    MEDIA_EMPTY,
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


def _ffmpeg(*argv):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *map(str, argv)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _zero_sample_mp4(path):
    """A real MP4 container whose muxer received no video or audio samples."""
    _ffmpeg(
        *("-f", "lavfi", "-i", "color=c=black:s=64x36:r=24:d=1"),
        *("-t", "0", "-c:v", "libx264", "-pix_fmt", "yuv420p", path),
    )


def _film(path, *, video_seconds, audio_seconds):
    _ffmpeg(
        *("-f", "lavfi", "-i", f"color=c=black:s=64x36:r=24:d={video_seconds}"),
        *("-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_seconds}"),
        *("-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p"),
        *("-c:a", "aac", "-ar", "48000", path),
    )


class MeasuredMediaTests(unittest.TestCase):
    """Hash and manifest agreement never stands in for measured media content."""

    def setUp(self):
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
            self.skipTest("optional media tools unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def receipt(self, film, *, duration, cue_end="00:00:01,850"):
        """Write captions and a receipt whose digests agree with the bytes."""
        cue = f"00:00:00,000 --> {cue_end}\nNarrator: Hello.\n"
        (self.root / "course.srt").write_text("1\n" + cue + "\n")
        (self.root / "course.vtt").write_text("WEBVTT\n\n" + cue.replace(",", "."))
        shutil.copyfile(film, self.root / "course.mp4")
        receipt = self.root / "video-result.json"
        artifacts = {
            name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
            for name in ("course.mp4", "course.srt", "course.vtt")
        }
        receipt.write_text(
            json.dumps(
                {
                    "schema": "literate-ai/video-course-result@1",
                    "course_id": "course",
                    "duration_seconds": duration,
                    "caption_count": 1,
                    "embed_subtitles": False,
                    "artifacts": artifacts,
                }
            )
        )
        return receipt

    def assert_refused(self, receipt, code):
        with self.assertRaises(VideoError) as raised:
            verify_course(receipt)
        self.assertEqual(raised.exception.code, code)
        output, errors = io.StringIO(), io.StringIO()
        status = main(["video", "verify", str(receipt)], stdout=output, stderr=errors)
        self.assertNotEqual(status, 0)
        self.assertIn(code, output.getvalue() + errors.getvalue())

    def test_verify_accepts_measured_film(self):
        film = self.root / "good.mp4"
        _film(film, video_seconds=2, audio_seconds=2)
        result = verify_course(self.receipt(film, duration=2.0))
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["duration_seconds"], 2.0, delta=0.05)

    def test_verify_refuses_zero_sample_film_despite_matching_digests(self):
        film = self.root / "empty.mp4"
        _zero_sample_mp4(film)
        self.assertLess(film.stat().st_size, 1024)
        self.assert_refused(self.receipt(film, duration=97.5), MEDIA_EMPTY)

    def test_verify_refuses_film_missing_narrated_scenes(self):
        # Empty scene clips vanish when concatenated; the captions still narrate
        # them, so the measured film is shorter than its narration.
        film = self.root / "first-scene-only.mp4"
        _film(film, video_seconds=2, audio_seconds=2)
        receipt = self.receipt(film, duration=2.0, cue_end="00:01:37,500")
        self.assert_refused(receipt, MEDIA_DURATION)

    def test_verify_refuses_video_that_stops_before_its_audio(self):
        # The container header still reports the audio length, which is what an
        # unmeasured duration check would have accepted.
        film = self.root / "truncated.mp4"
        _film(film, video_seconds=0.5, audio_seconds=2)
        self.assert_refused(self.receipt(film, duration=2.0), MEDIA_DURATION)


class MeasuredBuildTests(unittest.TestCase):
    """Build measures every scene clip it renders before accepting the film."""

    def setUp(self):
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "magick")):
            self.skipTest("optional media tools unavailable")
        fonts = [
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        self.font = next((path for path in fonts if path.is_file()), None)
        if self.font is None:
            self.skipTest("no test font available")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "course.json"
        init_course(self.manifest)
        _ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", self.root / "a.wav"
        )
        document = json.loads(self.manifest.read_text())
        scene = document["scenes"][0]
        scene["dialogue"][0]["audio"] = "a.wav"
        document["scenes"] = [scene, {**scene, "title": "Second scene"}]
        document["embed_subtitles"] = False
        self.manifest.write_text(json.dumps(document))

    def build_replacing_second_clip(self, replacement):
        real_run = video_courses._run

        def run(argv, **kwargs):
            output = real_run(argv, **kwargs)
            if argv[-1].endswith("s002-t00-c00.mp4"):
                shutil.copyfile(replacement, argv[-1])
            return output

        with patch.object(video_courses, "_run", side_effect=run):
            return build_course(
                self.manifest, self.root / "out", font=self.font, backend="recorded"
            )

    def test_build_reports_measured_film_duration(self):
        result = build_course(
            self.manifest, self.root / "out", font=self.font, backend="recorded"
        )
        # Two one-second narrations, each followed by the rendered clip tail.
        self.assertAlmostEqual(result["duration_seconds"], 2.3, delta=0.15)
        self.assertTrue(verify_course(Path(result["receipt"]))["accepted"])

    def test_build_refuses_zero_sample_scene_clip(self):
        empty = self.root / "empty.mp4"
        _zero_sample_mp4(empty)
        with self.assertRaises(VideoError) as raised:
            self.build_replacing_second_clip(empty)
        self.assertEqual(raised.exception.code, MEDIA_EMPTY)
        self.assertIn("s002-t00-c00.mp4", str(raised.exception))
        self.assertEqual(list((self.root / "out").glob("*/video-result.json")), [])

    def test_build_refuses_scene_clip_shorter_than_narration(self):
        short = self.root / "short.mp4"
        _film(short, video_seconds=0.25, audio_seconds=0.25)
        with self.assertRaises(VideoError) as raised:
            self.build_replacing_second_clip(short)
        self.assertEqual(raised.exception.code, MEDIA_DURATION)
