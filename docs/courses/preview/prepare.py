"""Prepare this repository's review cut, not an inherited narrative template.

Requires a local MLX-Audio environment, a downloaded Kokoro snapshot, ImageMagick,
ffmpeg, and an explicit font. Produces inputs for the portable litai video builder.
No cloud speech, voice cloning, demo execution, installation, or publication.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import subprocess
from pathlib import Path


def run(*argv: str, source: str | None = None) -> str:
    return subprocess.run(
        argv, input=source, text=True, capture_output=True, check=True, timeout=300
    ).stdout


def text(x: int, y: int, value: str, size: int = 28, color: str = "#e9edf5") -> str:
    # The SVG reader can swallow a final quote; an invisible sentinel preserves
    # literal shell-command punctuation in the rendered artwork.
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}">'
        f"{html.escape(value, quote=False)}\u200b</text>"
    )


def panel(x: int, y: int, w: int, h: int, fill: str = "#21304b") -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" '
        f'fill="{fill}" stroke="#405471" stroke-width="2"/>'
    )


def laptop(x: int, y: int, label: str, screen: str, color: str) -> str:
    return (
        panel(x, y, 210, 140, "#0b1221")
        + f'<path d="M{x - 15} {y + 148} h240 l-20 16 h-200z" fill="#8394ac"/>'
        + text(x + 24, y + 88, screen, 35, color)
        + text(x + 15, y + 200, label, 24)
    )


def arrow(x: int, y: int, width: int, color: str = "#37d8b7") -> str:
    return (
        f'<path d="M{x} {y} h{width}" stroke="{color}" stroke-width="5"/>'
        f'<path d="M{x + width - 12} {y - 12} l16 12 -16 12" fill="{color}"/>'
    )


HEADINGS = {
    "zip": "It builds. But can you ship it?",
    "proof": "Which version did you actually test?",
    "shrug": "Reassuring is not reproducible.",
    "bridge": "Close the gap between built and shipped.",
    "where": "Start here. No secret handshake.",
    "repo": "Get Literate AI from the public repository",
    "tools": "A small starting kit",
    "clone": "Clone. Enter. Install.",
    "consent": "Read the prerequisite prompt.",
    "path": "Installed is not always on your PATH.",
    "doctor": "Prove your command is available.",
    "login": "The harness and the agent are separate.",
    "next": "Now give the agent a destination.",
    "pipeline": "A candidate must earn its way to shipping.",
    "end": "Next: your first real coding session.",
}


def artwork(shot: str, active: str) -> str:
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#10192d"/>',
        '<circle cx="1100" cy="0" r="600" fill="#142942"/>',
        text(40, 45, "LITERATE AI   /   GREENFIELD REVIEW CUT", 18, "#37d8b7"),
        text(40, 115, HEADINGS[shot], 37),
        '<rect y="600" width="1280" height="120" fill="#080e1c"/>',
        text(50, 575, "LitAI", 22, "#37d8b7"),
        text(1110, 575, "Sam", 22, "#ffbc6b"),
    ]
    if shot in {"zip", "proof", "shrug", "bridge"}:
        parts.extend(
            [
                laptop(295, 260, "SAM'S MACHINE", "build: OK", "#37d8b7"),
                laptop(775, 260, "YOUR USER", "...?", "#ffbc6b"),
                arrow(535, 330, 195, "#ffbc6b" if shot != "bridge" else "#37d8b7"),
            ]
        )
        if shot == "zip":
            parts.extend(
                [
                    panel(435, 175, 435, 58, "#594128"),
                    text(455, 212, "final-final-really-final.zip", 27, "#ffcf8f"),
                ]
            )
        elif shot in {"proof", "shrug"}:
            parts.extend(
                [panel(535, 375, 200, 70), text(566, 421, "PROOF ?", 30, "#ffbc6b")]
            )
        else:
            parts.extend(
                [
                    panel(530, 375, 210, 70),
                    text(550, 419, "SPEC + TESTS", 25, "#37d8b7"),
                ]
            )
    elif shot in {"where", "repo"}:
        parts.extend(
            [
                panel(275, 145, 755, 415, "#e7ecf2"),
                text(295, 185, "github.com/jordanhubbard/literate-ai", 26, "#10192d"),
            ]
        )
    elif shot in {"clone", "doctor", "path"}:
        parts.append(panel(275, 195, 755, 330, "#080e1c"))
        lines = {
            "clone": [
                "git clone https://github.com/jordanhubbard/literate-ai.git",
                "cd literate-ai",
                "make install",
            ],
            "doctor": [
                'export PATH="$HOME/.local/bin:$PATH"',
                "litai --version",
                "litai doctor",
            ],
            "path": [
                "INSTALLER → PREFIX → PATH",
                "Then open a new shell or update PATH.",
            ],
        }[shot]
        for index, line in enumerate(lines):
            parts.append(
                text(
                    300,
                    250 + index * 62,
                    line,
                    21 if shot == "clone" else 25,
                    "#9ce9d2",
                )
            )
        parts.append(text(300, 555, "SETUP WALKTHROUGH • macOS / Linux", 19, "#9eacc0"))
    elif shot == "tools":
        for index, (symbol, label) in enumerate(
            (("Py", "Python 3.11+"), ("Git", "Git"), ("{ }", "Make"))
        ):
            x = 300 + index * 235
            parts.extend(
                [
                    panel(x, 235, 205, 195),
                    text(x + 52, 322, symbol, 48, "#37d8b7"),
                    text(x + 15, 399, label, 24),
                ]
            )
    elif shot == "consent":
        parts.extend(
            [
                panel(340, 220, 615, 285),
                text(380, 280, "Missing prerequisites?", 31),
                text(380, 338, "Review the exact packages.", 27),
                panel(400, 390, 190, 62, "#205345"),
                text(445, 431, "Approve", 27),
                panel(660, 390, 190, 62, "#54412c"),
                text(710, 431, "Decline", 27),
            ]
        )
    elif shot == "login":
        parts.extend(
            [
                panel(290, 225, 300, 250),
                panel(720, 225, 300, 250),
                text(330, 300, "Literate AI", 36, "#37d8b7"),
                text(330, 375, "SDLC harness", 26),
                text(760, 300, "Coding agent", 32, "#ffbc6b"),
                text(760, 375, "Separate login", 26),
                arrow(610, 350, 80),
                text(395, 535, "Credentials stay off camera.", 29),
            ]
        )
    else:
        for index, label in enumerate(("SPEC", "CODE", "PROOF", "PACKAGE")):
            x = 285 + index * 190
            parts.extend(
                [panel(x, 290, 165, 110), text(x + 17, 354, label, 27, "#37d8b7")]
            )
            if index < 3:
                parts.append(arrow(x + 168, 345, 16))
        parts.append(text(365, 475, "Shipping is a separate, explicit decision.", 26))
    x = 28 if active == "litai" else 1025
    parts.append(
        f'<rect x="{x}" y="540" width="225" height="5" rx="2" fill="#37d8b7"/>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument(
        "--model", required=True, type=Path, help="downloaded Kokoro snapshot"
    )
    parser.add_argument("--repository-image", required=True, type=Path)
    args = parser.parse_args()
    from importlib.metadata import version

    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    story_path = Path(__file__).with_name("storyboard.json")
    story = json.loads(story_path.read_text())
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copy2(args.repository_image, assets / "repository.png")
    model = load_model(args.model.resolve(), model_type="kokoro")
    scenes = []
    for number, turn in enumerate(story["turns"], 1):
        print(
            f"Preparing shot {number}/{len(story['turns'])}: {turn['shot']}", flush=True
        )
        stem = f"shot-{number:02}"
        voice = (
            args.model / "voices" / f"{story['voices'][turn['speaker']]}.safetensors"
        )
        result = list(
            model.generate(
                text=turn["text"], voice=str(voice.absolute()), speed=1.0, lang_code="a"
            )
        )
        audio = np.concatenate([np.asarray(part.audio) for part in result])
        raw = assets / f"{stem}-raw.wav"
        sf.write(raw, audio, result[0].sample_rate)
        narration = assets / f"{stem}.wav"
        run(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(raw),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=9",
            "-ar",
            "24000",
            str(narration),
        )
        slide = assets / f"{stem}.png"
        run(
            "magick",
            "-font",
            str(args.font),
            "svg:-",
            str(slide),
            source=artwork(turn["shot"], turn["speaker"]),
        )
        if turn["shot"] in {"where", "repo"}:
            browser = assets / "browser-thumb.png"
            run(
                "magick",
                str(assets / "repository.png"),
                "-resize",
                "720x355",
                "-background",
                "white",
                "-gravity",
                "north",
                "-extent",
                "720x355",
                str(browser),
            )
            run(
                "magick",
                str(slide),
                str(browser),
                "-geometry",
                "+290+195",
                "-composite",
                str(slide),
            )
        for speaker, filename, x in (
            ("litai", "litai-guide.png", 15),
            ("sam", "sam-engineer.png", 1020),
        ):
            portrait = assets / f"portrait-{speaker}.png"
            run(
                "magick",
                str(story_path.parent.parent / "assets" / filename),
                "-resize",
                "245x315",
                "-channel",
                "A",
                "-evaluate",
                "multiply",
                "1" if speaker == turn["speaker"] else "0.55",
                str(portrait),
            )
            run(
                "magick",
                str(slide),
                str(portrait),
                "-geometry",
                f"+{x}+215",
                "-composite",
                str(slide),
            )
        scenes.append(
            {
                "title": HEADINGS[turn["shot"]],
                "visual": f"assets/{slide.name}",
                "dialogue": [
                    {
                        "speaker": turn["speaker"],
                        "text": turn["text"]
                        .replace("Literate A I", "Literate AI")
                        .replace("lit eye", "litai"),
                        "audio": f"assets/{narration.name}",
                    }
                ],
            }
        )
    provenance = {
        "model": "mlx-community/Kokoro-82M-bf16",
        "snapshot": args.model.name,
        "mlx_audio": version("mlx-audio"),
        "voices": story["voices"],
        "storyboard_sha256": hashlib.sha256(story_path.read_bytes()).hexdigest(),
        "synthetic_voices": True,
        "cloud_speech": False,
        "repository_screenshot": "https://github.com/jordanhubbard/literate-ai",
        "status": "creative-review-only; setup walkthrough, not a recorded installation",
    }
    (root / "production.json").write_text(json.dumps(provenance, indent=2) + "\n")
    manifest = {
        "schema": "literate-ai/video-course@1",
        "id": story["id"],
        "title": story["title"],
        "speakers": {"litai": {"name": "LitAI"}, "sam": {"name": "Sam"}},
        "scenes": scenes,
        "evidence": ["production.json"],
    }
    (root / "course.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(root / "course.json")


if __name__ == "__main__":
    main()
