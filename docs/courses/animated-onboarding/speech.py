"""Explicit NVIDIA Magpie authoring step, separate from offline video rendering.

No automatic retries, voice cloning, implicit fallback, or credentials in receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

BASE = "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio"
SOURCE = "https://build.nvidia.com/nvidia/magpie-tts-multilingual/api"
MAX_AUDIO = 32 * 1024 * 1024


def read_key(path=None):
    raw = (
        path.read_text().strip()
        if path
        else os.environ.get("NVIDIA_API_KEY", "").strip()
    )
    tokens = set(re.findall(r"nvapi-[A-Za-z0-9_-]+", raw))
    if len(tokens) == 1:
        return tokens.pop()
    if tokens or not raw or any(c.isspace() for c in raw):
        raise ValueError("provide exactly one NVIDIA Build API key")
    return raw


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def voice_ids(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith("Magpie-") and len(value) <= 160 else set()
    if isinstance(value, list):
        return set().union(*(voice_ids(v) for v in value))
    if isinstance(value, dict):
        return set().union(*(voice_ids(v) for v in value.values()))
    return set()


def request(route: str, key: str, fields=None, *, opener=urllib.request.urlopen):
    headers = {"Authorization": "Bearer " + key}
    data = None
    if fields is not None:
        boundary = "litai-" + uuid.uuid4().hex
        pieces = []
        for name, value in fields.items():
            pieces.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
            )
        data = b"".join(pieces) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    try:
        with opener(
            urllib.request.Request(BASE + route, data=data, headers=headers), timeout=60
        ) as response:
            body = response.read(MAX_AUDIO + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"NVIDIA speech HTTP {exc.code}; no fallback or retry performed"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError(
            "NVIDIA speech transport unavailable; no fallback or retry performed"
        ) from None
    if len(body) > MAX_AUDIO:
        raise ValueError("speech response exceeds the authoring size limit")
    return body


def validate_wav(data: bytes) -> dict:
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if (
                wav.getcomptype() != "NONE"
                or wav.getsampwidth() != 2
                or wav.getnchannels() != 1
            ):
                raise ValueError("expected mono 16-bit PCM WAV")
            rate, frames = wav.getframerate(), wav.getnframes()
            if not 8000 <= rate <= 96000 or not 0 < frames / rate <= 120:
                raise ValueError("speech duration/sample rate outside authoring bounds")
            samples = wav.readframes(frames)
            if len(samples) != frames * 2:
                raise ValueError("truncated speech WAV")
            return {"sample_rate": rate, "frames": frames, "seconds": frames / rate}
    except (wave.Error, EOFError):
        raise ValueError("speech endpoint did not return a valid WAV") from None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("voices", "synthesize"))
    p.add_argument("--api-key-file", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--catalog", type=Path)
    p.add_argument("--story", type=Path)
    p.add_argument("--turn")
    p.add_argument("--voice")
    a = p.parse_args()
    if a.output.exists() or (
        a.operation == "synthesize" and a.output.with_suffix(".voice.json").exists()
    ):
        p.error("output already exists; preserve previous auditions")
    key = read_key(a.api_key_file)
    if a.operation == "voices":
        raw = request("/list_voices", key)
        doc = json.loads(raw)
        if not voice_ids(doc):
            p.error("live response did not identify usable Magpie voices")
        a.output.parent.mkdir(parents=True, exist_ok=True)
        with a.output.open("xb") as f:
            f.write(raw)
        print(
            json.dumps(
                {"voices": sorted(voice_ids(doc)), "catalog_sha256": digest(raw)}
            )
        )
        return
    if not all((a.catalog, a.story, a.turn, a.voice)):
        p.error("synthesize requires --catalog, --story, --turn and --voice")
    catalog = a.catalog.read_bytes()
    if a.voice not in voice_ids(json.loads(catalog)):
        p.error("voice is absent from the observed catalog")
    story_bytes = a.story.read_bytes()
    turn = next(
        (t for t in json.loads(story_bytes)["turns"] if t["id"] == a.turn), None
    )
    if turn is None:
        p.error("unknown dialogue turn")
    text = turn.get("spoken_text", turn["text"])
    if not isinstance(text, str) or not 1 <= len(text) <= 1200:
        p.error("speech text must contain 1..1200 characters")
    audio = request(
        "/synthesize",
        key,
        {
            "text": text,
            "language": "en-US",
            "voice": a.voice,
            "encoding": "LINEAR_PCM",
            "sample_rate_hz": "44100",
        },
    )
    measured = validate_wav(audio)
    receipt = {
        "provider": "NVIDIA NIM",
        "model": "magpie-tts-multilingual",
        "voice": a.voice,
        "voice_selection": "requested from observed catalog",
        "resolved_model_revision": None,
        "language": "en-US",
        "source": SOURCE,
        "story_sha256": digest(story_bytes),
        "turn_id": a.turn,
        "speaker": turn["speaker"],
        "text_sha256": digest(text.encode()),
        "catalog_sha256": digest(catalog),
        "audio_sha256": digest(audio),
        **measured,
        "review": (
            "Listening approval pending; integrity is not voice-quality acceptance"
        ),
        "rights": (
            "Stock synthetic voice; no real-person cloning. "
            "NVIDIA API Trial Service Terms apply."
        ),
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("xb") as f:
        f.write(audio)
    with a.output.with_suffix(".voice.json").open("x") as f:
        f.write(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"turn": a.turn, "voice": a.voice, **measured}))


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
