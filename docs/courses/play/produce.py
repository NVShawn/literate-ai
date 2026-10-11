"""Render this repository's continuous video play from reviewed factual excerpts.

Local neural speech; no demo execution, remote configuration or publication.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
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
sys.path.insert(0, str(BASE))
from scenes import cards, motion, sound  # noqa: E402

ASSETS = BASE.parent / "assets"
# Illustrative 3D scenes (scenes/blender_scenes.py) for these shots.
BLENDER_SHOTS = {
    "big-picture": "overnight",
    "cicd": "queue",
    "pre-ci": "machines",
    "workers": "machines",
}
# Recorded litai render html views (shoot.py), shown for these shots.
HTML_SCREENS = ["graph-overview.png", "graph-selected.png", "verification-health.png"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def caption(value):
    return value.replace("Literate A I", "Literate AI").replace("A I slop", "AI slop")


def spoken_text(value):
    """Pronunciation is a speech concern, never a mutation of commands/captions."""
    # Spaced initials ("A. I.") read as two sentences: "eh. I slop" and
    # "literally, AI" in local ASR checks. Unspaced initials read as one word.
    value = re.sub(r"\bLitai\b", "Lit A.I.", value, flags=re.IGNORECASE)
    value = re.sub(r"\bAI\b", "A.I.", value)
    value = re.sub(r"\bhtml\b", "H.T.M.L.", value, flags=re.IGNORECASE)
    for written, spoken in (
        ("C++", "C plus plus"),
        ("CLI", "C.L.I."),
        ("SSH", "S.S.H."),
        ("YAML", "yaml"),
        ("CTest", "C test"),
        ("CMake", "C make"),
        ("TinyXML2", "Tiny X.M.L. two"),
        ("Q3", "Q three"),
        ("SemVer", "sem-ver"),
    ):
        value = value.replace(written, spoken)
    return value


FILM = "SETTLE THE ARGUMENT. BEFORE CI."
TITLES = {
    "big-picture": "Agentic development, done properly",
    "slop": "\u201cThe usual AI slop pitch\u201d",
    "repair": "Rejected candidates come back with the error",
    "cicd": "CI is where most teams first learn the truth",
    "pre-ci": "The whole gate, before CI",
    "html": "See what the agent did",
    "learning": "Lessons become reviewed authority",
    "challenge": "Thursday: settle the argument",
    "contract": "Component: the promise, not the implementation",
    "flavors": "Flavors: choose how that promise is built",
    "skills": "A catalog is not a prompt",
    "workers": "The machine you already own",
    "worker-config": "Private execution, portable intent",
    "updates": "Change without losing your decisions",
    "lineage": "Upstream changes arrive as plans",
    "orchestration": "Coordinate independent repositories",
    "adoption": "Earn the authority transition",
}


NOTES = {
    "big-picture": "Developers decide • Code is a derived artifact",
    "slop": "Seven beliefs • One terminal",
    "repair": "Bounded • Diagnostics redacted • Then it stops",
    "cicd": "The pipeline file is untested code with production keys",
    "pre-ci": "make release-check  •  release evidence starts at gate one",
    "html": "litai render html  •  one file, no server",
    "learning": "No model retraining • Git is the memory",
    "challenge": "Keep the tools. Challenge the claims.",
    "contract": "Same observable behavior • Independent acceptance",
    "flavors": "C++ + Make is another choice, not a free automatic port",
    "skills": "Nested SKILL.md: parent guidance + focused child delta",
    "workers": "REMOTE STEPS: WALKTHROUGH, NOT SSH EXECUTION",
    "worker-config": "litai config paths  →  private configuration",
    "updates": "PLAN → REVIEW → APPLY → REBUILD → VERIFY",
    "lineage": "Exact parent revisions • Explicit overrides",
    "orchestration": "Dependency order ≠ inheritance order",
    "adoption": "Draft + review + independent parity before qualification",
}


def custom_art(shot, active):
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#10192d"/>',
        text(40, 45, FILM, 18, "#37d8b7"),
        text(40, 115, TITLES[shot], 37),
        '<rect y="600" width="1280" height="120" fill="#080e1c"/>',
    ]
    labels = {
        "big-picture": [
            ("PROMISE", "Reviewed spec"),
            ("PROOF", "3 platforms, pre-CI"),
            ("RECEIPT", "Exact commit"),
        ],
        "slop": [
            ("CTO", "Agent-first by Q3"),
            ("THURSDAY", "Architecture review"),
            ("SAM", "Leads the holdouts"),
        ],
        "repair": [
            ("REJECTED", "Build or test fails"),
            ("FEEDBACK", "Redacted error"),
            ("RETRY", "At most twice more"),
        ],
        "cicd": [
            ("PUSH", "And pray"),
            ("QUEUE", "An hour later"),
            ("RED", "Fix you never ran"),
        ],
        "pre-ci": [
            ("CHECK", "Lint • specs • docs"),
            ("TEST", "Suite • samples"),
            ("SHIP", "Wheel • install • e2e"),
        ],
        "html": [
            ("GRAPH", "Authority + sources"),
            ("HEALTH", "Versions • locks"),
            ("STALE", "Blocks qualification"),
        ],
        "learning": [
            ("OBSERVE", "Typed evidence"),
            ("PROPOSE", "litai learn"),
            ("ADMIT", "Your review"),
        ],
        "challenge": [
            ("LITAI", "Show the proof"),
            ("EVIDENCE", "Settles it"),
            ("SAM", "C++ • Make • Tests"),
        ],
        "contract": [
            ("INPUT", "Name + messages"),
            ("CONTRACT", "Normalization rules"),
            ("OUTPUT", "Greeting + counts"),
        ],
        "flavors": [
            ("BEHAVIOR", "Greeting Component"),
            ("TARGET", "Python + Make"),
            ("TECHNIQUE", "Pinned skill recipe"),
        ],
        "skills": [
            ("AGENT TASK", "Adopt / release"),
            ("RECIPE", "Selected skills"),
            ("CATALOG", "Not all loaded"),
        ],
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
        if index < 2 and shot not in {"skills", "challenge", "slop", "html"}:
            parts.append(arrow(x + 237, 333, 15))
    note = NOTES[shot]
    parts += [
        text(280, 475, note, 22, "#ffbc6b"),
        text(50, 575, "LitAI", 22, "#37d8b7"),
        text(1110, 575, "Sam", 22, "#ffbc6b"),
        f'<rect x="{28 if active == "litai" else 1025}" y="540" '
        'width="225" height="5" fill="#37d8b7"/>',
        "</svg>",
    ]
    return "\n".join(parts)


def provenance(entry):
    """What a terminal-style page actually is; never call a file a recording."""
    if "recorded_revision" in entry:
        return f"RECORDED GATE LOG EXCERPT • REVISION {entry['recorded_revision']}"
    if "file_sha256" in entry:
        return "REPOSITORY FILE EXCERPT • EXACT LINES • HASH RECORDED"
    if "evidence_sha256" in entry:
        return "RECORDED HARNESS EVIDENCE • EXCERPT"
    if entry.get("observed"):
        return "RECORDED DIRECTORY LISTING • EXCERPT"
    return "RECORDED EXECUTION • EDITED TERMINAL REPLAY • WAITING COMPRESSED"


def terminal_art(entry, active, page):
    lines = entry["lines"]
    chunks = [lines[n : n + 14] for n in range(0, len(lines), 14)] or [[]]
    selected = chunks[min(page, len(chunks) - 1)]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">',
        '<rect width="1280" height="720" fill="#10192d"/>',
        text(35, 35, provenance(entry), 18, "#ffbc6b"),
        text(35, 85, entry["title"], 33),
        panel(25, 108, 1230, 475, "#080e1c"),
    ]
    for i, line in enumerate(selected):
        # Keep indentation at fixed cells; SVG whitespace is not terminal whitespace.
        pad = len(line) - len(line.lstrip(" "))
        body = line.lstrip(" ")
        parts.append(
            '<g font-family="monospace">'
            + text(
                45 + pad * 12,
                146 + i * 29,
                # The SVG reader swallows a leading quote too (JSON strings).
                ("\u200b" + body) if body.startswith('"') else body,
                # 0.6em monospace advance = the 12px cell used for indentation.
                20,
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


def blender_clip(name, scenes, blender):
    """Render (once) and encode an illustrative 3D scene; cached by its inputs."""
    script = BASE / "scenes/blender_scenes.py"
    key = hashlib.sha256((digest(script) + name + str(blender)).encode()).hexdigest()
    clip = scenes / "blender" / f"{name}-{key[:12]}.mp4"
    if not clip.exists():
        frames = clip.with_suffix("")
        run(
            str(blender),
            "-b",
            "--factory-startup",
            "-P",
            str(script),
            "--",
            name,
            str(frames),
            "192",
        )
        motion.sequence(frames, clip)
        motion.validate(clip, 192 / motion.FPS)
    return clip


def staged_art(turn, fonts, out):
    """The diagram cards, re-set in the film's typefaces with both portraits."""
    source = (
        custom_art(turn["shot"], turn["speaker"])
        if turn["shot"] in TITLES
        else art.artwork(turn["shot"], turn["speaker"])
    ).replace("LITERATE AI   /   GREENFIELD REVIEW CUT", FILM)
    run("magick", "-font", str(fonts["label"]), "svg:-", str(out), source=source)
    for speaker, filename, x in (
        ("litai", "litai-guide.png", 15),
        ("sam", "sam-engineer.png", 1020),
    ):
        active = speaker == turn["speaker"]
        portrait = out.with_name(f"{out.stem}-{speaker}.png")
        run(
            "magick",
            str(ASSETS / filename),
            "-resize",
            "265x340" if active else "235x300",
            "-channel",
            "A",
            "-evaluate",
            "multiply",
            "1" if active else "0.45",
            str(portrait),
        )
        y = 200 if active else 235
        run(
            "magick",
            str(out),
            str(portrait),
            "-geometry",
            f"+{x}+{y}",
            "-composite",
            str(out),
        )
        portrait.unlink()
    return out


def studio_scene(turn, stem, seconds, evidence, args, fonts, work, html_index):
    """One turn as moving picture: chapter card, body, provenance and captions.

    Returns the clip and the sound cues (kind, offset) that belong to it.
    """
    work.mkdir(parents=True, exist_ok=True)
    total = seconds + 0.25
    opening, clips, cues, overlays, remaining = [], [], [], [], total
    if turn.get("chapter"):
        span = min(3.0, total * 0.45)
        card = cards.chapter_card(
            turn["chapter"], turn["speaker"], ASSETS, fonts, work / "chapter.png"
        )
        opening.append(motion.still(card, span, work / "chapter.mp4", zoom=0.03))
        cues.append(("whoosh", 0.0))
        remaining -= span
    start = total - remaining
    shot = turn["shot"]
    if shot == "terminal":
        entry = evidence["excerpts"][turn["capture"]]
        pages = []
        for page in range(max(1, (len(entry["lines"]) + 13) // 14)):
            frame = work / f"page-{page}.png"
            run(
                "magick",
                "-font",
                str(fonts["mono"]),
                "svg:-",
                str(frame),
                source=terminal_art(entry, turn["speaker"], page),
            )
            pages.append(frame)
        if turn["capture"] == "render-html" and args.screens:
            # The rendered pages themselves: recorded output, not illustration.
            for name in ("graph-overview.png", "verification-health.png"):
                frame = work / f"screen-{name}"
                cards.browser(
                    args.screens / name,
                    f"views/{name.split('-')[0]}"
                    f"{'' if name.startswith('graph') else '-health'}.html"
                    "   (recorded litai render html output)",
                    fonts,
                    frame,
                )
                pages.append(frame)
        span = remaining / len(pages)
        for index, frame in enumerate(pages):
            clips.append(
                motion.still(frame, span, work / f"page-{index}.mp4", zoom=0.015)
            )
            cues.append(("tick", start + index * span))
        overlays.append(
            (cards.closeup(turn["speaker"], ASSETS, work / "face.png", 92), 1172, 4)
        )
    elif shot in BLENDER_SHOTS and args.blender:
        clip = blender_clip(BLENDER_SHOTS[shot], args.scenes, args.blender)
        clips.append(motion.fit(clip, remaining, work / "scene.mp4"))
        overlays += [
            (
                cards.tag(
                    "ILLUSTRATION",
                    "NOT A RECORDING  •  " + TITLES[shot].upper(),
                    fonts,
                    work / "tag.png",
                ),
                0,
                0,
            ),
            (cards.closeup(turn["speaker"], ASSETS, work / "face.png", 116), 24, 468),
        ]
    elif shot == "html" and args.screens:
        name = HTML_SCREENS[html_index % len(HTML_SCREENS)]
        frame = cards.browser(
            args.screens / name,
            "views/graph.html   (recorded litai render html output)",
            fonts,
            work / "screen.png",
        )
        clips.append(motion.still(frame, remaining, work / "screen.mp4", zoom=0.03))
        overlays += [
            (
                cards.tag(
                    "RECORDED OUTPUT",
                    "litai render html  •  sams-first-project",
                    fonts,
                    work / "tag.png",
                ),
                0,
                0,
            ),
            (cards.closeup(turn["speaker"], ASSETS, work / "face.png", 92), 1172, 4),
        ]
    else:
        frame = staged_art(turn, fonts, work / "art.png")
        clips.append(motion.still(frame, remaining, work / "art.mp4", zoom=0.035))
    # Provenance tags and close-ups belong to the body, never the chapter card.
    body = motion.dress(
        motion.join(clips, work / "joined.mp4"),
        overlays,
        remaining,
        work / "body.mp4",
        fade=False,
    )
    words = cards.caption(
        caption(turn["text"]), turn["speaker"], fonts, work / "caption.png"
    )
    clip = motion.dress(
        motion.join([*opening, body], work / "turn.mp4"),
        [(words, 0, 0)],
        total,
        args.output / "assets" / f"{stem}.mp4",
        fade=bool(turn.get("chapter")),
    )
    motion.validate(clip, total)
    return clip, cues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    # ImageMagick's -font overrides SVG font-family; terminals need fixed cells.
    parser.add_argument("--mono-font", type=Path, help="terminal replay font")
    parser.add_argument("--display-font", type=Path, help="titles and cards")
    parser.add_argument("--label-font", type=Path, help="labels and diagrams")
    # Studio mode: moving pictures, 3D scenes and sound; heavy assets stay here.
    parser.add_argument("--scenes", type=Path, help="private studio asset directory")
    parser.add_argument("--blender", type=Path, help="Blender executable for 3D")
    parser.add_argument("--screens", type=Path, help="shoot.py screenshot directory")
    parser.add_argument("--sound-bed", action="store_true", help="add the sound bed")
    parser.add_argument("--repository-image", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--audio-only", action="store_true")
    parser.add_argument("--limit-turns", type=int)
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
    engine = story.get("speech", {}).get("engine", "kokoro")
    model = load_model(args.model.resolve(), model_type=engine)
    scenes, chapters = [], []
    elapsed = 0.0
    fonts = {
        "display": args.display_font or args.font,
        "label": args.label_font or args.font,
        "body": args.font,
        "caption": args.font,
        "mono": args.mono_font or args.font,
    }
    if args.scenes:
        args.scenes = args.scenes.resolve()
        args.output = root
        bed = sound.bed(args.scenes / "sound/bed.wav", 1800) if args.sound_bed else None
    html_index = 0
    turns = story["turns"][: args.limit_turns] if args.limit_turns else story["turns"]
    for i, turn in enumerate(turns, 1):
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
        voice_name = story["voices"][turn["speaker"]]
        voice = args.model / "voices" / f"{voice_name}.safetensors"
        speech = spoken_text(turn["text"])
        direction = (
            story.get("speech", {}).get("directions", {}).get(turn["speaker"], "")
        )
        key = hashlib.sha256(
            (
                speech
                + (digest(voice) if engine == "kokoro" else voice_name)
                + direction
                + args.model.name
                + version("mlx-audio")
                + engine
                + ";speed=1.0;seed=42;norm=-16;temperature=0.7"
            ).encode()
        ).hexdigest()
        cache = assets / f"{stem}.audio-key"
        if not audio.exists() or not cache.exists() or cache.read_text() != key:
            import mlx.core as mx

            mx.random.seed(42)
            settings = (
                {"voice": str(voice.absolute()), "speed": 1.0, "lang_code": "a"}
                if engine == "kokoro"
                else {
                    "voice": voice_name,
                    "instruct": direction,
                    "lang_code": "english",
                    "temperature": 0.7,
                    "max_tokens": 1800,
                }
            )
            results = list(
                model.generate(
                    text=speech,
                    **settings,
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
        offset = elapsed
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
        if args.scenes:
            _, cues = studio_scene(
                turn,
                stem,
                seconds,
                evidence,
                args,
                fonts,
                args.scenes / "work" / stem,
                html_index,
            )
            html_index += turn["shot"] == "html"
            scene["recording"] = f"assets/{stem}.mp4"
            if bed:
                # Speech-only WAV stays beside the mix for listen.py checks.
                mixed = assets / f"{stem}.mix.wav"
                sound.mix(
                    audio,
                    mixed,
                    bed,
                    offset,
                    seconds - 0.15,
                    [
                        (sound.cue(kind, args.scenes / f"sound/{kind}.wav"), at)
                        for kind, at in cues
                        if at < seconds - 0.4
                    ],
                )
                scene["dialogue"][0]["audio"] = f"assets/{mixed.name}"
        elif turn["shot"] == "terminal":
            entry = evidence["excerpts"][turn["capture"]]
            pages = max(1, (len(entry["lines"]) + 13) // 14)
            clips = []
            for page in range(pages):
                frame = root / f"{stem}-page-{page}.png"
                run(
                    "magick",
                    "-font",
                    str(args.mono_font or args.font),
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
                "LITERATE AI   /   GREENFIELD REVIEW CUT",
                FILM,
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
    if not args.limit_turns and elapsed > story.get("target_seconds", float("inf")):
        raise ValueError(
            f"Narration alone takes {elapsed:.1f}s; trim the script before rendering "
            f"the {story['target_seconds']}s film. Audio cache is retained."
        )
    production = {
        "model": story.get("speech", {}).get("model", "mlx-community/Kokoro-82M-bf16"),
        "snapshot": args.model.name,
        "voices": story["voices"],
        "mlx_audio": version("mlx-audio"),
        "synthetic": True,
        "cloud_speech": False,
        "story_sha256": digest(BASE / "story.json"),
        "producer_sha256": digest(Path(__file__)),
        "chapters": chapters,
        "edition": "public feedback; not framework release qualification",
        "speech_directions": story.get("speech", {}).get("directions", {}),
        "spoken_text": [spoken_text(turn["text"]) for turn in turns],
        "review": "Revised Qwen voice audition: human listening approval pending",
        "audition": bool(args.limit_turns),
    }
    if args.scenes:
        production["studio"] = {
            "credits": "CREDITS.md",
            "fonts": {
                role: {"file": path.name, "sha256": digest(path)}
                for role, path in fonts.items()
            },
            "blender": run(str(args.blender), "--version").splitlines()[0]
            if args.blender
            else None,
            "imagemagick": run("magick", "-version").splitlines()[0],
            "ffmpeg": run("ffmpeg", "-version").splitlines()[0],
            "scene_scripts": {
                path.name: digest(path)
                for path in sorted((BASE / "scenes").glob("*.py"))
            },
            "sound": "original; synthesized by scenes/sound.py, no third-party audio"
            if args.sound_bed
            else None,
            "screens": "shoot.py captures of recorded litai render html output"
            if args.screens
            else None,
            "labels": "3D scenes tagged ILLUSTRATION; terminal and screen pages "
            "state their recorded source",
        }
    (root / "production.json").write_text(json.dumps(production, indent=2) + "\n")
    (root / "course.json").write_text(
        json.dumps(
            {
                "schema": "literate-ai/video-course@1",
                "id": story["id"] + ("-audition" if args.limit_turns else ""),
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
