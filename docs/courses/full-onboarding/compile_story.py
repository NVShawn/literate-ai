"""Compile the reviewed Markdown narrative into bounded chapter speech manifests."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

CHAPTER = re.compile(r"^## (?P<id>[0-9]{2}) — (?P<title>.+)$", re.MULTILINE)
TURN = re.compile(
    r"^\*\*(?P<id>[0-9]{2}\.[0-9]{2}) · (?P<speaker>Sam|LitAI):\*\* "
    r"“(?P<text>.+?)”\n\n(?P<direction>.*?)(?=\n\*\*[0-9]{2}\.[0-9]{2} ·|\n## |\Z)",
    re.MULTILINE | re.DOTALL,
)
ANNOTATION = re.compile(
    r"\*Play:\*\s+(?P<play>.*?)\s+\*Listen:\*\s+(?P<listen>.*?)\s+"
    r"\*Focus/hold:\*\s+(?P<focus>.*)",
    re.DOTALL,
)
VOICE = {
    "sam": "Magpie-Multilingual.EN-US.Jason.Neutral",
    "litai": "Magpie-Multilingual.EN-US.Aria",
}
SHOT = {
    "01": "pipeline",
    "02": "authority",
    "03": "adoption",
    "04": "targets",
    "05": "workflow",
    "06": "bookscan",
    "07": "aviation",
    "08": "elixirssi",
    "09": "lertx",
    "10": "daemonarchy",
    "11": "ci",
    "12": "next-step",
}


@dataclass(frozen=True)
class Chapter:
    identifier: str
    title: str
    start: int
    end: int


def _plain(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())


def _chapters(source: str) -> list[Chapter]:
    matches = list(CHAPTER.finditer(source))
    return [
        Chapter(
            match.group("id"),
            match.group("title"),
            match.start(),
            matches[index + 1].start() if index + 1 < len(matches) else len(source),
        )
        for index, match in enumerate(matches)
    ]


def compile_narrative(source: str) -> dict[str, object]:
    chapters = _chapters(source)
    if [item.identifier for item in chapters] != [
        f"{value:02d}" for value in range(1, 13)
    ]:
        raise ValueError("narrative must contain chapters 01 through 12 exactly once")
    result: list[dict[str, object]] = []
    all_turns: list[dict[str, object]] = []
    for chapter in chapters:
        body = source[chapter.start : chapter.end]
        turns: list[dict[str, object]] = []
        for match in TURN.finditer(body):
            if not match.group("id").startswith(chapter.identifier + "."):
                raise ValueError("turn identifier does not match its chapter")
            annotation = ANNOTATION.fullmatch(match.group("direction").strip())
            if annotation is None:
                raise ValueError(
                    f"turn {match.group('id')} lacks complete performance direction"
                )
            text = _plain(match.group("text"))
            words = len(re.findall(r"[A-Za-z0-9]+(?:[-'’][A-Za-z0-9]+)*", text))
            # A bounded animatic estimate only. Recorded speech replaces it before
            # release.
            seconds = round(min(30.0, max(2.5, words / 2.65 + 1.0)), 3)
            speaker = "sam" if match.group("speaker") == "Sam" else "litai"
            turn = {
                "id": match.group("id").replace(".", "-"),
                "chapter": chapter.identifier,
                "speaker": speaker,
                "voice": VOICE[speaker],
                "text": text,
                "seconds": seconds,
                "shot": SHOT[chapter.identifier],
                "performance": {
                    "play": _plain(annotation.group("play")),
                    "listen": _plain(annotation.group("listen")),
                    "focus": _plain(annotation.group("focus")),
                },
            }
            turns.append(turn)
            all_turns.append(turn)
        if not turns:
            raise ValueError(f"chapter {chapter.identifier} has no dialogue")
        duration = sum(float(item["seconds"]) + 0.15 for item in turns)
        if not math.isfinite(duration) or duration > 180:
            raise ValueError(f"chapter {chapter.identifier} exceeds the scene budget")
        result.append(
            {
                "id": chapter.identifier,
                "title": chapter.title,
                "shot": SHOT[chapter.identifier],
                "turns": turns,
                "animatic_seconds": round(duration, 3),
            }
        )
    identifiers = [str(item["id"]) for item in all_turns]
    if len(all_turns) != 86 or len(set(identifiers)) != len(identifiers):
        raise ValueError("reviewed narrative must compile to 86 unique turns")
    return {
        "schema": "literate-ai/full-onboarding-story@1",
        "id": "eight-hours-to-make-coffee",
        "title": "Eight Hours to Make Coffee",
        "speakers": {"sam": "Sam", "litai": "Literate AI"},
        "voices": VOICE,
        "chapters": result,
        "turns": all_turns,
        "boundary": (
            "Authored timing is animatic-only; release rendering requires measured "
            "recordings, fresh evidence, and complete playback approval."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--narrative", type=Path, default=Path(__file__).with_name("NARRATIVE.md")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve prior production inputs")
    document = compile_narrative(args.narrative.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "chapters": len(document["chapters"]),
                "turns": len(document["turns"]),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
