"""Synthesize the reviewed full script with the approved Magpie cast, once per turn."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
SPEECH_PATH = HERE.parent / "animated-onboarding" / "speech.py"
SPEC = importlib.util.spec_from_file_location("onboarding_speech", SPEECH_PATH)
assert SPEC and SPEC.loader
SPEECH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SPEECH
_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(SPEECH)
finally:
    sys.dont_write_bytecode = _DONT_WRITE_BYTECODE


def _validate_retained(
    story_bytes: bytes, turn: dict, audio: Path, receipt: Path
) -> None:
    data = audio.read_bytes()
    record = json.loads(receipt.read_text(encoding="utf-8"))
    text = turn["text"]
    expected = {
        "story_sha256": SPEECH.digest(story_bytes),
        "turn_id": turn["id"],
        "speaker": turn["speaker"],
        "text_sha256": SPEECH.digest(text.encode()),
        "audio_sha256": SPEECH.digest(data),
        "voice": turn["voice"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError(f"retained recording provenance drifted: {turn['id']}")
    SPEECH.validate_wav(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument(
        "--minimum-request-interval-seconds",
        type=float,
        default=7.0,
        help="pace new provider requests without retrying a rejected request",
    )
    args = parser.parse_args()
    if args.minimum_request_interval_seconds < 0:
        parser.error("minimum request interval must be non-negative")
    story_bytes = args.story.read_bytes()
    story = json.loads(story_bytes)
    catalog_bytes = args.catalog.read_bytes()
    catalog = json.loads(catalog_bytes)
    available = SPEECH.voice_ids(catalog)
    key = SPEECH.read_key(args.api_key_file)
    args.output.mkdir(parents=True, exist_ok=True)
    retained = generated = 0
    last_request_at: float | None = None
    for turn in story["turns"]:
        voice = turn["voice"]
        if voice not in available:
            parser.error(f"approved voice is absent from observed catalog: {voice}")
        audio_path = args.output / f"{turn['id']}.wav"
        receipt_path = audio_path.with_suffix(".voice.json")
        if audio_path.exists() or receipt_path.exists():
            if not audio_path.exists() or not receipt_path.exists():
                parser.error(f"partial retained recording: {turn['id']}")
            _validate_retained(story_bytes, turn, audio_path, receipt_path)
            retained += 1
            continue
        if last_request_at is not None:
            remaining = args.minimum_request_interval_seconds - (
                time.monotonic() - last_request_at
            )
            if remaining > 0:
                time.sleep(remaining)
        last_request_at = time.monotonic()
        audio = SPEECH.request(
            "/synthesize",
            key,
            {
                "text": turn["text"],
                "language": "en-US",
                "voice": voice,
                "encoding": "LINEAR_PCM",
                "sample_rate_hz": "44100",
            },
        )
        measured = SPEECH.validate_wav(audio)
        receipt = {
            "provider": "NVIDIA NIM",
            "model": "magpie-tts-multilingual",
            "voice": voice,
            "voice_selection": "approved cast selected from observed live catalog",
            "resolved_model_revision": None,
            "language": "en-US",
            "source": SPEECH.SOURCE,
            "story_sha256": SPEECH.digest(story_bytes),
            "turn_id": turn["id"],
            "speaker": turn["speaker"],
            "text_sha256": SPEECH.digest(turn["text"].encode()),
            "catalog_sha256": SPEECH.digest(catalog_bytes),
            "audio_sha256": SPEECH.digest(audio),
            **measured,
            "review": "Full-script listening approval pending",
            "rights": (
                "Stock synthetic voice; no real-person cloning. "
                "NVIDIA API Trial Service Terms apply."
            ),
        }
        audio_path.write_bytes(audio)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        generated += 1
        print(json.dumps({"turn": turn["id"], "seconds": measured["seconds"]}))
    print(
        json.dumps(
            {"generated": generated, "retained": retained, "total": len(story["turns"])}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
