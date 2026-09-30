"""Portable instructional course packages: authored scenes, narration and evidence.

Demo commands are inert display text. Rendering never executes them. External
tools are optional host capabilities; importing the CLI needs none of them.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

SCHEMA = "literate-ai/video-course@1"
RECEIPT = "literate-ai/video-course-result@1"
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class VideoError(ValueError):
    """A course cannot be rendered or its evidence cannot be verified."""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VideoError("expected a JSON object")
    return value


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _asset(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise VideoError("asset must be a portable relative file path")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise VideoError("asset escapes the course package")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise VideoError(f"missing or escaping course asset: {name}")
    return path


def _text(value: Any, label: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise VideoError(f"{label} must contain 1..{maximum} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise VideoError(f"{label} contains control characters")
    return value


def plan_course(manifest: Path) -> dict[str, Any]:
    """Validate authored intent and bind every referenced local asset."""
    document = _json(manifest)
    if document.get("schema") != SCHEMA:
        raise VideoError(f"schema must be {SCHEMA}")
    if not _ID.fullmatch(_text(document.get("id"), "id", 70)):
        raise VideoError("course id must be lowercase words separated by hyphens")
    _text(document.get("title"), "title", 70)
    speakers = document.get("speakers")
    if not isinstance(speakers, dict) or not 1 <= len(speakers) <= 2:
        raise VideoError("declare one or two speakers")
    assets: dict[str, str] = {}

    def bind(name: str) -> None:
        assets[name] = _digest(_asset(manifest.parent, name))

    for key, speaker in speakers.items():
        if not _ID.fullmatch(key) or not isinstance(speaker, dict):
            raise VideoError("invalid speaker")
        _text(speaker.get("name"), "speaker name", 24)
        _text(speaker.get("voice"), "speaker voice", 100)
        if speaker.get("portrait"):
            bind(speaker["portrait"])
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 100:
        raise VideoError("declare 1..100 scenes")
    for scene in scenes:
        if not isinstance(scene, dict):
            raise VideoError("scene must be an object")
        _text(scene.get("title"), "scene title", 48)
        for field, count, width in (("points", 3, 66), ("terminal", 4, 76)):
            lines = scene.get(field, [])
            if not isinstance(lines, list) or len(lines) > count:
                raise VideoError(f"{field} permits at most {count} lines")
            for line in lines:
                _text(line, field, width)
        if scene.get("recording"):
            bind(scene["recording"])
        turns = scene.get("dialogue")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 20:
            raise VideoError("each scene needs 1..20 dialogue turns")
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("speaker") not in speakers:
                raise VideoError("dialogue references an undeclared speaker")
            _text(turn.get("text"), "dialogue")
            if turn.get("audio"):
                bind(turn["audio"])
    evidence = document.get("evidence", [])
    if not isinstance(evidence, list):
        raise VideoError("evidence must be a list of local files")
    for name in evidence:
        bind(name)
    identity = hashlib.sha256(
        json.dumps({"document": document, "assets": assets}, sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema": "literate-ai/video-course-plan@1",
        "course_id": document["id"],
        "source_identity": identity,
        "scene_count": len(scenes),
        "assets": assets,
        "document": document,
        "executes_demo_commands": False,
    }


def init_course(path: Path) -> dict[str, Any]:
    value = {
        "schema": SCHEMA,
        "id": "first-project",
        "title": "Your first project",
        "speakers": {
            "guide": {"name": "Guide", "voice": "Samantha"},
            "engineer": {"name": "Engineer", "voice": "Daniel"},
        },
        "evidence": [],
        "scenes": [
            {
                "title": "Inspect before applying",
                "points": ["Review the proposed changes"],
                "terminal": ["litai onboard create my-project"],
                "dialogue": [
                    {"speaker": "engineer", "text": "Will this change my project?"},
                    {
                        "speaker": "guide",
                        "text": "Review this plan before applying it.",
                    },
                ],
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    return {"manifest": str(path), "schema": SCHEMA}


def _run(argv: list[str], *, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            argv,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoError(f"{argv[0]} unavailable or timed out") from exc
    if result.returncode:
        raise VideoError(f"{Path(argv[0]).name} failed: {result.stderr[-2000:]}")
    return result.stdout


def _probe(path: Path) -> dict[str, Any]:
    return json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _duration(path: Path) -> float:
    value = float(_probe(path)["format"]["duration"])
    if not math.isfinite(value) or not 0 < value <= 7200:
        raise VideoError("media duration must be finite and between zero and two hours")
    return value


def _stamp(seconds: float, *, vtt: bool = False) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds_int, milliseconds = divmod(milliseconds, 1000)
    separator = "." if vtt else ","
    return f"{hours:02}:{minutes:02}:{seconds_int:02}{separator}{milliseconds:03}"


def _slide(scene: dict, title: str, position: int, total: int) -> str:
    def text(x: int, y: int, size: int, value: str, color: str = "#e5edf7") -> str:
        return (
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            'xml:space="preserve">'
            f"{html.escape(value)}</text>"
        )

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#101827"/>',
        '<rect width="1280" height="12" fill="#21c8b7"/>',
        text(65, 64, 23, title.upper(), "#21c8b7"),
        text(65, 158, 38, scene["title"]),
    ]
    for index, line in enumerate(scene.get("points", [])):
        parts.append(text(65, 254 + 58 * index, 27, line))
    if scene.get("terminal"):
        parts.append(
            '<rect x="55" y="432" width="1160" height="216" rx="12" fill="#060c16"/>'
        )
        for index, line in enumerate(scene["terminal"]):
            parts.append(text(78, 476 + 43 * index, 24, line, "#9fe6d8"))
    parts.append(text(65, 687, 18, f"Instructional demo  |  {position} / {total}"))
    parts.append("</svg>")
    return "\n".join(parts)


def build_course(
    manifest: Path, output: Path, *, font: Path, backend: str
) -> dict[str, Any]:
    """Build into new custody; failed attempts never replace a prior course."""
    plan = plan_course(manifest)
    document = plan["document"]
    if not font.is_file():
        raise VideoError("provide an installed font file with --font")
    font_digest = _digest(font)
    if backend not in {"recorded", "say", "espeak"}:
        raise VideoError("unsupported narration backend")
    tools = ["ffmpeg", "ffprobe", "magick"]
    if backend != "recorded":
        tools.append({"say": "say", "espeak": "espeak-ng"}[backend])
    for tool in tools:
        if shutil.which(tool) is None:
            raise VideoError(f"missing prerequisite: {tool}; no tool was installed")
    if backend == "recorded" and any(
        not turn.get("audio")
        for scene in document["scenes"]
        for turn in scene["dialogue"]
    ):
        raise VideoError("recorded narration requires audio on every dialogue turn")
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=document["id"] + "-", dir=output))
    clips: list[Path] = []
    cues: list[tuple[float, float, str]] = []
    elapsed = 0.0
    for number, scene in enumerate(document["scenes"], 1):
        scene_elapsed = 0.0
        slide = stage / f"scene-{number:03}.png"
        _run(
            ["magick", "-font", str(font), "svg:-", str(slide)],
            input_text=_slide(
                scene, document["title"], number, len(document["scenes"])
            ),
        )
        for speaker_index, speaker in enumerate(document["speakers"].values()):
            if speaker.get("portrait"):
                portrait = stage / f"portrait-{speaker_index}.png"
                _run(
                    [
                        "magick",
                        str(_asset(manifest.parent, speaker["portrait"])),
                        "-thumbnail",
                        "100x110",
                        str(portrait),
                    ]
                )
                _run(
                    [
                        "magick",
                        str(slide),
                        str(portrait),
                        "-geometry",
                        f"+{1000 + speaker_index * 110}+85",
                        "-composite",
                        str(slide),
                    ]
                )
        for turn_index, turn in enumerate(scene["dialogue"]):
            speaker = document["speakers"][turn["speaker"]]
            # Recorded narration is timed as a whole turn. Synthesized narration
            # is timed per sentence: caption timing never estimates word ratios.
            chunks = (
                [turn["text"]]
                if turn.get("audio")
                else [
                    chunk
                    for sentence in re.split(r"(?<=[.!?])\s+", turn["text"])
                    for chunk in textwrap.wrap(sentence, 180)
                ]
            )
            for chunk_index, chunk in enumerate(chunks):
                stem = f"s{number:03}-t{turn_index:02}-c{chunk_index:02}"
                narration = stage / (stem + ".wav")
                if turn.get("audio"):
                    narration = _asset(manifest.parent, turn["audio"])
                elif backend == "say":
                    narration = narration.with_suffix(".aiff")
                    # Narration goes through stdin, never command-line switches.
                    _run(
                        [
                            "say",
                            "-v",
                            speaker["voice"],
                            "-r",
                            "168",
                            "-o",
                            str(narration),
                        ],
                        input_text=chunk,
                    )
                else:
                    _run(
                        [
                            "espeak-ng",
                            "-v",
                            speaker["voice"],
                            "-s",
                            "168",
                            "-w",
                            str(narration),
                            "--stdin",
                        ],
                        input_text=chunk,
                    )
                seconds = _duration(narration)
                clip = stage / (stem + ".mp4")
                if scene.get("recording"):
                    video_input = [
                        "-stream_loop",
                        "-1",
                        "-i",
                        str(_asset(manifest.parent, scene["recording"])),
                    ]
                    video_filter = f"trim=start={scene_elapsed},setpts=PTS-STARTPTS,"
                else:
                    video_input = ["-loop", "1", "-framerate", "24", "-i", str(slide)]
                    video_filter = ""
                _run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        *video_input,
                        "-i",
                        str(narration),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-af",
                        "apad",
                        "-t",
                        str(seconds + 0.15),
                        "-vf",
                        video_filter
                        + "scale=1280:720:force_original_aspect_ratio=decrease,"
                        "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                        "-r",
                        "24",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "23",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-ar",
                        "48000",
                        "-b:a",
                        "160k",
                        str(clip),
                    ]
                )
                cues.append((elapsed, elapsed + seconds, f"{speaker['name']}: {chunk}"))
                clip_duration = _duration(clip)
                elapsed += clip_duration
                scene_elapsed += clip_duration
                clips.append(clip)
    concat = stage / "clips.txt"
    concat.write_text(
        "".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8"
    )
    raw = stage / "uncaptioned.mp4"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            str(raw),
        ]
    )
    for suffix in ("srt", "vtt"):
        lines = ["WEBVTT", ""] if suffix == "vtt" else []
        for index, (start, end, caption) in enumerate(cues, 1):
            if suffix == "srt":
                lines.append(str(index))
            lines.extend(
                [
                    f"{_stamp(start, vtt=suffix == 'vtt')} --> "
                    f"{_stamp(end, vtt=suffix == 'vtt')}",
                    "\n".join(textwrap.wrap(caption, 70)),
                    "",
                ]
            )
        (stage / f"{document['id']}.{suffix}").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    video = stage / f"{document['id']}.mp4"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(raw),
            "-i",
            str(stage / f"{document['id']}.srt"),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "1:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
            "-disposition:s:0",
            "default",
            "-movflags",
            "+faststart",
            str(video),
        ]
    )
    if (
        plan_course(manifest)["source_identity"] != plan["source_identity"]
        or _digest(font) != font_digest
    ):
        raise VideoError(
            "course inputs changed during rendering; output is not accepted"
        )
    result = {
        "schema": RECEIPT,
        "course_id": document["id"],
        "source_identity": plan["source_identity"],
        "renderer_sha256": _digest(Path(__file__)),
        "font_sha256": font_digest,
        "narration_backend": backend,
        "duration_seconds": _duration(video),
        "caption_count": len(cues),
        "publication_authorized": False,
        "artifacts": {
            f"{document['id']}.{suffix}": _digest(stage / f"{document['id']}.{suffix}")
            for suffix in ("mp4", "srt", "vtt")
        },
        "tools": {
            name: _run([name, "-version"]).splitlines()[0]
            for name in ("ffmpeg", "ffprobe", "magick")
        },
    }
    receipt = stage / "video-result.json"
    receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    verify_course(receipt, manifest=manifest)
    return {**result, "directory": str(stage), "receipt": str(receipt)}


def verify_course(receipt: Path, *, manifest: Path | None = None) -> dict[str, Any]:
    result = _json(receipt)
    if result.get("schema") != RECEIPT:
        raise VideoError("unsupported video receipt")
    if manifest and plan_course(manifest)["source_identity"] != result.get(
        "source_identity"
    ):
        raise VideoError("course source or evidence changed; rebuild the video")
    artifacts = result.get("artifacts", {})
    course_id = result.get("course_id", "")
    if not _ID.fullmatch(course_id) or set(artifacts) != {
        f"{course_id}.{suffix}" for suffix in ("mp4", "srt", "vtt")
    }:
        raise VideoError("receipt must name a video and both caption formats")
    for name, expected in artifacts.items():
        if _digest(_asset(receipt.parent, name)) != expected:
            raise VideoError(f"artifact digest mismatch: {name}")
    video = _asset(receipt.parent, f"{course_id}.mp4")
    media = _probe(video)
    if {stream["codec_type"] for stream in media["streams"]} != {
        "video",
        "audio",
        "subtitle",
    }:
        raise VideoError(
            "video must contain picture, narration, and embedded subtitles"
        )
    seconds = _duration(video)
    if abs(seconds - result.get("duration_seconds", -1)) > 0.1:
        raise VideoError("receipt duration differs from media")
    captions = (receipt.parent / f"{course_id}.srt").read_text(encoding="utf-8")
    times = re.findall(
        r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", captions
    )
    if not times or len(times) != result.get("caption_count"):
        raise VideoError("caption count differs from receipt")
    previous = 0.0
    for values in times:
        numbers = [int(value) for value in values]
        start = numbers[0] * 3600 + numbers[1] * 60 + numbers[2] + numbers[3] / 1000
        end = numbers[4] * 3600 + numbers[5] * 60 + numbers[6] + numbers[7] / 1000
        if start < previous or end <= start or end > seconds + 0.1:
            raise VideoError("captions overlap or escape media duration")
        previous = end
    _run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"])
    return {
        "schema": "literate-ai/video-course-verification@1",
        "accepted": True,
        "course_id": course_id,
        "duration_seconds": seconds,
        "caption_count": len(times),
    }
