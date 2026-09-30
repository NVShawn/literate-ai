"""Render this repository's continuous video play from reviewed factual excerpts.

Local neural speech; no demo execution, remote configuration or publication.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import textwrap
from importlib.metadata import version
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "review_art", BASE.parent / "preview/prepare.py"
)
art = importlib.util.module_from_spec(spec)
spec.loader.exec_module(art)
run, text, panel, arrow = art.run, art.text, art.panel, art.arrow


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def caption(value):
    return value.replace("Literate A I", "Literate AI").replace("A I slop", "AI slop")


TITLES = {
    "workers": "The machine you already own",
    "worker-config": "Private execution, portable intent",
    "updates": "Change without losing your decisions",
    "lineage": "Inherit practice, not this cast",
    "orchestration": "Coordinate independent repositories",
    "adoption": "Earn the authority transition",
}


def custom_art(shot, active):
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#10192d"/>',
        text(40, 45, "IT BUILDS. CAN WE SHIP IT?", 18, "#37d8b7"),
        text(40, 115, TITLES[shot], 37),
        '<rect y="600" width="1280" height="120" fill="#080e1c"/>',
    ]
    labels = {
        "workers": [
            ("LOCAL", "Start here"),
            ("SSH", "Your machines"),
            ("PROOF", "Bring it back"),
        ],
        "worker-config": [
            ("PROJECT", "What to build"),
            ("PRIVATE", "Where to run"),
            ("PROBE", "Ready now?"),
        ],
        "updates": [
            ("BASELINE", "Recorded"),
            ("LOCAL", "Your decisions"),
            ("UPSTREAM", "New authority"),
        ],
        "lineage": [
            ("FRAMEWORK", "Shared practice"),
            ("TEAM", "Conventions"),
            ("PROJECTS", "Product intent"),
        ],
        "orchestration": [
            ("LIBRARY", "Exact child pin"),
            ("APPLICATION", "Depends on it"),
            ("PRODUCT", "Own evidence"),
        ],
        "adoption": [
            ("WRAPPED", "Preserve source"),
            ("RETAINED", "Prove baseline"),
            ("QUALIFIED", "Earn spec authority"),
        ],
    }[shot]
    for index, (label, detail) in enumerate(labels):
        x = 275 + index * 255
        parts += [
            panel(x, 260, 235, 145),
            text(x + 15, 315, label, 27, "#37d8b7"),
            text(x + 15, 365, detail, 21),
        ]
        if index < 2:
            parts.append(arrow(x + 237, 333, 15))
    note = {
        "workers": "REMOTE STEPS: WALKTHROUGH, NOT SSH EXECUTION",
        "worker-config": "litai config paths  →  private configuration",
        "updates": "PLAN → REVIEW → APPLY → REBUILD → VERIFY",
        "lineage": "Exact parent revisions • Explicit overrides",
        "orchestration": "Dependency order ≠ inheritance order",
        "adoption": "Draft + review + independent parity before qualification",
    }[shot]
    parts += [
        text(280, 475, note, 22, "#ffbc6b"),
        text(50, 575, "LitAI", 22, "#37d8b7"),
        text(1110, 575, "Sam", 22, "#ffbc6b"),
        f'<rect x="{28 if active == "litai" else 1025}" y="540" '
        'width="225" height="5" fill="#37d8b7"/>',
        "</svg>",
    ]
    return "\n".join(parts)


def terminal_art(entry, active, page):
    lines = entry["lines"]
    chunks = [lines[n : n + 14] for n in range(0, len(lines), 14)] or [[]]
    selected = chunks[min(page, len(chunks) - 1)]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#10192d"/>',
        text(
            35,
            35,
            "RECORDED EXECUTION • EDITED TERMINAL REPLAY • WAITING COMPRESSED",
            18,
            "#ffbc6b",
        ),
        text(35, 85, entry["title"], 33),
        panel(25, 108, 1230, 475, "#080e1c"),
    ]
    for i, line in enumerate(selected):
        # Keep indentation at fixed cells; SVG whitespace is not terminal whitespace.
        pad = len(line) - len(line.lstrip(" "))
        parts.append(
            '<g font-family="monospace">'
            + text(
                45 + pad * 12,
                146 + i * 29,
                line.lstrip(" "),
                21,
                "#37d8b7" if line.startswith("$") else "#e9edf5",
            )
            + "</g>"
        )
    parts += [
        text(
            45,
            573,
            f"{active.upper()}  •  excerpt {min(page + 1, len(chunks))}/{len(chunks)}",
            16,
            "#9eacc0",
        ),
        '<rect y="600" width="1280" height="120" fill="#080e1c"/>',
        "</svg>",
    ]
    return "\n".join(parts)


def burn_caption(image, value, speaker, font):
    lines = textwrap.wrap(
        f"{'LitAI' if speaker == 'litai' else 'Sam'}: {caption(value)}", width=100
    )
    if len(lines) > 4:
        raise ValueError("Shorten this turn: captions exceed four lines")
    overlay = '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">'
    overlay += (
        "".join(
            text(40, 626 + i * 25, line, 22, "#ffffff") for i, line in enumerate(lines)
        )
        + "</svg>"
    )
    run(
        "magick",
        "-background",
        "none",
        "-font",
        str(font),
        "svg:-",
        str(image.parent / "caption.png"),
        source=overlay,
    )
    run(
        "magick",
        str(image),
        str(image.parent / "caption.png"),
        "-composite",
        str(image),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--repository-image", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--audio-only", action="store_true")
    args = parser.parse_args()
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    story = json.loads((BASE / "story.json").read_text())
    evidence = {} if args.audio_only else json.loads(args.evidence.read_text())
    root = args.output.resolve()
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if not args.audio_only:
        shutil.copy2(args.evidence, root / "sessions.json")
    shutil.copy2(args.repository_image, assets / "repository.png")
    model = load_model(args.model.resolve(), model_type="kokoro")
    scenes, chapters = [], []
    elapsed = 0.0
    for i, turn in enumerate(story["turns"], 1):
        print(f"Preparing {i}/{len(story['turns'])} {turn['shot']}", flush=True)
        if turn.get("chapter"):
            chapters.append(
                {
                    "title": turn["chapter"],
                    "scene": i,
                    "approx_seconds": round(elapsed, 2),
                }
            )
        stem = f"shot-{i:02}"
        audio = assets / f"{stem}.wav"
        voice = (
            args.model / "voices" / f"{story['voices'][turn['speaker']]}.safetensors"
        )
        key = hashlib.sha256(
            (
                turn["text"]
                + digest(voice)
                + args.model.name
                + version("mlx-audio")
                + "speed=1.0;kokoro;norm=-16"
            ).encode()
        ).hexdigest()
        cache = assets / f"{stem}.audio-key"
        if not audio.exists() or not cache.exists() or cache.read_text() != key:
            results = list(
                model.generate(
                    text=turn["text"],
                    voice=str(voice.absolute()),
                    speed=1.0,
                    lang_code="a",
                )
            )
            raw = root / "raw.wav"
            sf.write(
                raw,
                np.concatenate([np.asarray(p.audio) for p in results]),
                results[0].sample_rate,
            )
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
                str(audio),
            )
            cache.write_text(key)
        seconds = sf.info(audio).duration + 0.15
        elapsed += seconds
        if args.audio_only:
            continue
        scene = {
            "title": f"{i:02} {turn.get('chapter', turn['shot'])}"[:48],
            "dialogue": [
                {
                    "speaker": turn["speaker"],
                    "text": caption(turn["text"]),
                    "audio": f"assets/{audio.name}",
                }
            ],
        }
        if turn["shot"] == "terminal":
            entry = evidence["excerpts"][turn["capture"]]
            pages = max(1, (len(entry["lines"]) + 13) // 14)
            clips = []
            for page in range(pages):
                frame = root / f"{stem}-page-{page}.png"
                run(
                    "magick",
                    "-font",
                    str(args.font),
                    "svg:-",
                    str(frame),
                    source=terminal_art(entry, turn["speaker"], page),
                )
                burn_caption(frame, turn["text"], turn["speaker"], args.font)
                clips.append(frame)
            concat = root / "frames.txt"
            concat.write_text(
                "".join(
                    f"file '{p.name}'\nduration {max(seconds / pages, 3):.3f}\n"
                    for p in clips
                )
                + f"file '{clips[-1].name}'\n"
            )
            recording = assets / f"{stem}.mp4"
            run(
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
                "-vf",
                "fps=24,format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "24",
                str(recording),
            )
            scene["recording"] = f"assets/{recording.name}"
        else:
            source = (
                custom_art(turn["shot"], turn["speaker"])
                if turn["shot"] in TITLES
                else art.artwork(turn["shot"], turn["speaker"])
            )
            source = source.replace(
                "LITERATE AI   /   GREENFIELD REVIEW CUT", "IT BUILDS. CAN WE SHIP IT?"
            ).replace(
                "Next: your first real coding session.",
                "Keep the skepticism. Make it repeatable.",
            )
            slide = assets / f"{stem}.png"
            run("magick", "-font", str(args.font), "svg:-", str(slide), source=source)
            if turn["shot"] in {"repo", "where"}:
                browser = root / "browser.png"
                run(
                    "magick",
                    str(args.repository_image),
                    "-resize",
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
                portrait = root / f"{speaker}.png"
                run(
                    "magick",
                    str(BASE.parent / "assets" / filename),
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
            burn_caption(slide, turn["text"], turn["speaker"], args.font)
            scene["visual"] = f"assets/{slide.name}"
        scenes.append(scene)
    if args.audio_only:
        return
    production = {
        "model": "mlx-community/Kokoro-82M-bf16",
        "snapshot": args.model.name,
        "voices": story["voices"],
        "mlx_audio": version("mlx-audio"),
        "synthetic": True,
        "cloud_speech": False,
        "story_sha256": digest(BASE / "story.json"),
        "producer_sha256": digest(Path(__file__)),
        "chapters": chapters,
        "edition": "public feedback; not framework release qualification",
        "review": (
            "94-second voice/style audition accepted by user; "
            "full-cut audience review pending"
        ),
    }
    (root / "production.json").write_text(json.dumps(production, indent=2) + "\n")
    (root / "course.json").write_text(
        json.dumps(
            {
                "schema": "literate-ai/video-course@1",
                "id": story["id"],
                "title": story["title"],
                "embed_subtitles": False,
                "audio_bitrate_kbps": 80,
                "speakers": {"litai": {"name": "LitAI"}, "sam": {"name": "Sam"}},
                "scenes": scenes,
                "evidence": ["production.json", "sessions.json"],
            },
            indent=2,
        )
        + "\n"
    )
    print(root / "course.json")


if __name__ == "__main__":
    main()
