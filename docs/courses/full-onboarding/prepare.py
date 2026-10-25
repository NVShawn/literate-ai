"""Prepare measured chapter timelines and release-video inputs from retained speech."""

from __future__ import annotations

import argparse
import array
import base64
import hashlib
import io
import json
import math
import shutil
import sys
import wave
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wav(data: bytes) -> tuple[int, bytes, float]:
    try:
        with wave.open(io.BytesIO(data)) as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
            ):
                raise ValueError("recordings must be mono 16-bit PCM WAV")
            rate = source.getframerate()
            frames = source.getnframes()
            payload = source.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError("recording is not a valid WAV") from exc
    if (
        not 8000 <= rate <= 96000
        or not 0 < frames / rate <= 120
        or len(payload) != frames * 2
    ):
        raise ValueError("recording duration, sample rate, or payload is invalid")
    return rate, payload, frames / rate


def _envelope(pcm: bytes, rate: int) -> list[float]:
    values = array.array("h", pcm)
    if sys.byteorder != "little":
        values.byteswap()
    window = max(1, rate // 50)
    result = []
    for index in range(0, len(values), window):
        chunk = values[index : index + window]
        rms = (
            math.sqrt(sum(value * value for value in chunk) / max(1, len(chunk)))
            / 32768
        )
        result.append(round(min(1, rms * 5), 5))
    return result


def prepare(story_path: Path, audio_dir: Path, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("output exists; preserve previous production packages")
    story_bytes = story_path.read_bytes()
    story = json.loads(story_bytes)
    output.mkdir(parents=True)
    (output / "assets").mkdir()
    (output / "chapters").mkdir()
    (output / "segments").mkdir()
    recording_hashes: dict[str, str] = {}
    receipt_hashes: dict[str, str] = {}
    chapter_records = []
    course_scenes = []
    for chapter in story["chapters"]:
        rate = None
        clock = 0.0
        blocks: list[bytes] = []
        turns = []
        receipts = []
        for authored in chapter["turns"]:
            name = authored["id"]
            audio_path = audio_dir / f"{name}.wav"
            receipt_path = audio_dir / f"{name}.voice.json"
            audio = audio_path.read_bytes()
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            expected = {
                "story_sha256": digest(story_bytes),
                "turn_id": name,
                "speaker": authored["speaker"],
                "text_sha256": digest(authored["text"].encode()),
                "audio_sha256": digest(audio),
                "voice": authored["voice"],
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError(f"stale or mismatched recording provenance: {name}")
            measured_rate, pcm, seconds = _wav(audio)
            if rate is not None and measured_rate != rate:
                raise ValueError("all full-film recordings must share one sample rate")
            rate = measured_rate
            start = clock
            blocks.append(pcm)
            clock += seconds
            end = clock
            tail = bytes(round(0.15 * rate) * 2)
            blocks.append(tail)
            clock += len(tail) / (rate * 2)
            turn = {**authored, "start": start, "end": end}
            turns.append(turn)
            receipts.append(receipt)
            shutil.copyfile(audio_path, output / "assets" / f"{name}.wav")
            recording_hashes[f"assets/{name}.wav"] = digest(audio)
            receipt_hashes[f"{name}.voice.json"] = digest(receipt_bytes)
            course_scenes.append(
                {
                    "title": f"{chapter['id']} · {chapter['title']} · {name}",
                    "points": [],
                    "terminal": [],
                    "recording": f"segments/{name}.mp4",
                    "dialogue": [
                        {
                            "speaker": authored["speaker"],
                            "text": authored["text"],
                            "audio": f"assets/{name}.wav",
                        }
                    ],
                }
            )
        if rate is None or clock > 180:
            raise ValueError(
                f"measured chapter {chapter['id']} exceeds the scene budget"
            )
        pcm = b"".join(blocks)
        rendered = io.BytesIO()
        with wave.open(rendered, "wb") as target:
            target.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            target.writeframes(pcm)
        chapter_audio = rendered.getvalue()
        chapter_dir = output / "chapters" / chapter["id"]
        chapter_dir.mkdir()
        (chapter_dir / "audio.wav").write_bytes(chapter_audio)
        scene = {
            "schema": "literate-ai/full-onboarding-scene@1",
            "id": chapter["id"],
            "title": chapter["title"],
            "shot": chapter["shot"],
            "speakers": story["speakers"],
            "turns": turns,
            "duration": len(pcm) / (rate * 2),
            "audio": "data:audio/wav;base64,"
            + base64.b64encode(chapter_audio).decode(),
            "envelope": _envelope(pcm, rate),
            "review": "pending complete playback",
        }
        scene_bytes = (json.dumps(scene, separators=(",", ":")) + "\n").encode()
        (chapter_dir / "scene.json").write_bytes(scene_bytes)
        chapter_records.append(
            {
                "id": chapter["id"],
                "scene": f"chapters/{chapter['id']}/scene.json",
                "scene_sha256": digest(scene_bytes),
                "audio_sha256": digest(chapter_audio),
                "seconds": scene["duration"],
                "turns": len(turns),
                "voices": receipts,
            }
        )
    course = {
        "schema": "literate-ai/video-course@1",
        "id": story["id"],
        "title": story["title"],
        "embed_subtitles": False,
        "speakers": {key: {"name": value} for key, value in story["speakers"].items()},
        "evidence": [
            "inputs.json",
            "qa.json",
            "review.json",
            "NARRATIVE.md",
            "PRODUCTION.md",
            "RESEARCH.md",
        ],
        "scenes": course_scenes,
    }
    (output / "course.json").write_text(
        json.dumps(course, indent=2) + "\n", encoding="utf-8"
    )
    inputs = {
        "schema": "literate-ai/full-onboarding-inputs@1",
        "story_sha256": digest(story_bytes),
        "recording_assets": recording_hashes,
        "recording_receipts": receipt_hashes,
        "chapters": chapter_records,
        "complete_listening_review": "pending",
        "complete_viewing_review": "pending",
        "publication": "not authorized by preparation",
    }
    (output / "inputs.json").write_text(
        json.dumps(inputs, indent=2) + "\n", encoding="utf-8"
    )
    return inputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.story, args.audio_dir, args.output)
    print(
        json.dumps(
            {
                "chapters": len(result["chapters"]),
                "turns": len(result["recording_assets"]),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
