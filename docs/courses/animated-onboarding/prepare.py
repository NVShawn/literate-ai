"""Validate the storyboard and prepare measured audio/timing for offline rendering."""

from __future__ import annotations

import argparse
import array
import base64
import hashlib
import io
import json
import math
import re
import sys
import wave
from pathlib import Path

from speech import validate_wav


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(story):
    if set(story.get("speakers", {})) != {"sam", "litai"}:
        raise ValueError(
            "this project-owned pilot requires its two declared characters"
        )
    turns = story.get("turns", [])
    if not 1 <= len(turns) <= 40:
        raise ValueError("pilot must have 1..40 turns")
    seen = set()
    for t in turns:
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", t.get("id", ""))
            or t["id"] in seen
        ):
            raise ValueError("turn IDs must be unique safe local filenames")
        seen.add(t["id"])
        if t.get("speaker") not in story["speakers"] or t.get("shot") not in {
            "existing",
            "doctor",
            "plan",
            "evidence",
            "next",
        }:
            raise ValueError("unknown speaker or shot")
        if t.get("gesture") not in {"question", "explain", "agree", "invite"}:
            raise ValueError("unknown finite gesture")
        if not isinstance(t.get("text"), str) or not 1 <= len(t["text"]) <= 220:
            raise ValueError("dialogue must be a short readable turn")
        seconds = t.get("seconds")
        if (
            type(seconds) not in {int, float}
            or not math.isfinite(seconds)
            or not 0.5 <= seconds <= 30
        ):
            raise ValueError("authored animatic duration must be finite and bounded")
    if sum(t["seconds"] for t in turns) > 180:
        raise ValueError("pilot exceeds the three-minute authoring budget")
    return turns


def prepare(story_path, output, audio_dir=None, animatic=False):
    if output.exists():
        raise ValueError("preserve previous outputs; choose a new output directory")
    raw = story_path.read_bytes()
    story = json.loads(raw)
    turns = validate(story)
    if bool(audio_dir) == bool(animatic):
        raise ValueError("select recorded --audio-dir or explicit --animatic")
    blocks, receipts, result, recordings = [], [], [], []
    rate = None
    clock = 0
    for t in turns:
        if animatic:
            rate = 24000
            samples = bytes(round(t["seconds"] * rate) * 2)
            measured = len(samples) / (2 * rate)
        else:
            audio = (audio_dir / (t["id"] + ".wav")).read_bytes()
            r = json.loads((audio_dir / (t["id"] + ".voice.json")).read_text())
            if (
                r.get("story_sha256") != digest(raw)
                or r.get("text_sha256")
                != digest(t.get("spoken_text", t["text"]).encode())
                or r.get("audio_sha256") != digest(audio)
                or r.get("turn_id") != t["id"]
                or r.get("speaker") != t["speaker"]
            ):
                raise ValueError("stale or mismatched recorded dialogue provenance")
            meta = validate_wav(audio)
            if rate is not None and rate != meta["sample_rate"]:
                raise ValueError("recorded turns must share the measured sample rate")
            rate = meta["sample_rate"]
            with wave.open(io.BytesIO(audio)) as w:
                samples = w.readframes(w.getnframes())
            measured = meta["seconds"]
            receipts.append(r)
        start = clock
        if clock + measured + 0.15 > 180:
            raise ValueError("recorded pilot exceeds the three-minute authoring budget")
        blocks.append(samples)
        wav_turn = io.BytesIO()
        with wave.open(wav_turn, "wb") as w:
            w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            w.writeframes(samples)
        recordings.append((t["id"], wav_turn.getvalue()))
        clock += measured
        result.append({**t, "start": start, "end": clock})
        # Match the existing litai video per-turn 150 ms tail. Rendering itself
        # remains a separate authoring step; the core receipt owns media checks.
        blocks.append(bytes(round(0.15 * rate) * 2))
        clock += round(0.15 * rate) / rate
    pcm = b"".join(blocks)
    envelope = []
    values = array.array("h", pcm)
    if sys.byteorder != "little":
        values.byteswap()
    window = rate // 50
    for i in range(0, len(values), window):
        chunk = values[i : i + window]
        rms = math.sqrt(sum(v * v for v in chunk) / max(1, len(chunk))) / 32768
        envelope.append(round(min(1, rms * 5), 5))
    wav_bytes = io.BytesIO()
    with wave.open(wav_bytes, "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(pcm)
    audio = wav_bytes.getvalue()
    data = {
        "id": story["id"],
        "title": story["title"],
        "speakers": story["speakers"],
        "turns": result,
        "duration": len(pcm) / (rate * 2),
        "audio": None
        if animatic
        else "data:audio/wav;base64," + base64.b64encode(audio).decode(),
        "envelope": envelope,
        "animatic": animatic,
    }
    output.mkdir(parents=True)
    scene_bytes = (json.dumps(data, separators=(",", ":")) + "\n").encode()
    (output / "scene.json").write_bytes(scene_bytes)
    (output / "audio.wav").write_bytes(audio)
    (output / "assets").mkdir()
    for name, recording in recordings:
        (output / "assets" / (name + ".wav")).write_bytes(recording)
    (output / "inputs.json").write_text(
        json.dumps(
            {
                "story_sha256": digest(raw),
                "audio_sha256": digest(audio),
                "scene_sha256": digest(scene_bytes),
                "recording_assets": {
                    "assets/" + name + ".wav": digest(recording)
                    for name, recording in recordings
                },
                "duration": data["duration"],
                "voices": receipts,
                "animatic": animatic,
                "human_review": "pending",
                "scope": "Illustrated walkthrough, no demo execution or publication",
            },
            indent=2,
        )
        + "\n"
    )
    # One recording per turn resets the core renderer's scene clock. AAC and
    # frame-grid rounding therefore cannot accumulate into mouth/caption drift.
    course = {
        "schema": "literate-ai/video-course@1",
        "id": story["id"],
        "title": story["title"]
        + (" — silent animatic" if animatic else " — voice audition"),
        "embed_subtitles": False,
        "speakers": {k: {"name": v} for k, v in story["speakers"].items()},
        "evidence": [
            "inputs.json",
            "authoring.json",
            "qa.json",
            "source-evidence.json",
            "PRODUCTION.md",
        ],
        "scenes": [
            {
                "title": "Onboarding: " + t["id"],
                "points": [],
                "terminal": [],
                "recording": "segments/" + t["id"] + ".mp4",
                "dialogue": [
                    {
                        "speaker": t["speaker"],
                        "text": t["text"],
                        "audio": "assets/" + t["id"] + ".wav",
                    }
                ],
            }
            for t in turns
        ],
    }
    (output / "course.json").write_text(json.dumps(course, indent=2) + "\n")
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--story", type=Path, default=Path(__file__).with_name("story.json"))
    p.add_argument("--output", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--audio-dir", type=Path)
    g.add_argument("--animatic", action="store_true")
    a = p.parse_args()
    d = prepare(a.story, a.output, a.audio_dir, a.animatic)
    print(
        json.dumps(
            {
                "duration": d["duration"],
                "animatic": d["animatic"],
                "turns": len(d["turns"]),
            }
        )
    )


if __name__ == "__main__":
    main()
