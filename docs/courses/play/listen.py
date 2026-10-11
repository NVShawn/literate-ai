"""Local ASR listen-proxy: transcribe each narration file and compare with the script.

python listen.py ASR_MODEL_DIR PACKAGE_DIR [--turns 1,3,4] [--report FILE]
Runs in the isolated mlx-audio environment with a downloaded speech-recognition
snapshot. Prefer mlx-community/whisper-large-v3-turbo plus the tokenizer and
preprocessor files from openai/whisper-large-v3-turbo; Parakeet drops "plus plus"
and mishears "agentic" even when the narration is right. Nothing leaves the host.
A low score flags a turn for a human listen; it is not a substitute for one.
"""

import argparse
import difflib
import json
import re
from pathlib import Path

from mlx_audio.stt.utils import load_model

BASE = Path(__file__).resolve().parent
SPOKEN = {"++": " plus plus", "%": " percent"}


def words(value):
    value = value.lower()
    for written, spoken in SPOKEN.items():
        value = value.replace(written, spoken)
    return re.findall(r"[a-z0-9]+", value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("package", type=Path)
    parser.add_argument("--turns", help="comma-separated 1-based turn numbers")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()
    turns = json.loads((BASE / "story.json").read_text())["turns"]
    chosen = (
        [int(n) for n in args.turns.split(",")]
        if args.turns
        else range(1, len(turns) + 1)
    )
    model = load_model(str(args.model))
    rows = []
    for number in chosen:
        audio = args.package / "assets" / f"shot-{number:02}.wav"
        if not audio.exists():
            continue
        heard = model.generate(str(audio)).text.strip()
        score = difflib.SequenceMatcher(
            None, words(turns[number - 1]["text"]), words(heard)
        ).ratio()
        rows.append({"turn": number, "score": round(score, 3), "heard": heard})
        flag = "  " if score >= args.threshold else "!!"
        print(f"{flag} {number:02} {score:.2f} | {heard}", flush=True)
    if args.report:
        args.report.write_text(json.dumps(rows, indent=2) + "\n")
    low = [row["turn"] for row in rows if row["score"] < args.threshold]
    print(f"{len(rows)} turns checked; below {args.threshold}: {low or 'none'}")


if __name__ == "__main__":
    main()
