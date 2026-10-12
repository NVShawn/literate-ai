"""2D compositions for the film: cards, close-ups, recorded-output frames, captions.

ImageMagick's SVG reader takes one font per pass, so each typeface is its own
transparent layer, composited in order. Every frame states what it is: a recorded
terminal replay, a repository file excerpt, recorded output, or an illustration.
"""

from __future__ import annotations

import html
import re
import subprocess
import textwrap
from pathlib import Path

W, H = 1280, 720
TEAL, AMBER, INK, MUTED, NAVY = "#37d8b7", "#ffbc6b", "#e9edf5", "#9eacc0", "#10192d"
# Face crops of the original portraits in ../../assets (see PROMPTS.md).
FACES = {
    "litai": ("litai-guide.png", "560x560+430+0", TEAL),
    "sam": ("sam-engineer.png", "520x520+330+0", AMBER),
}
NUMBERS = "one two three four five six seven".split()


def magick(*argv: str, source: str | None = None) -> None:
    subprocess.run(
        ["magick", *argv], input=source, text=True, capture_output=True, check=True
    )


def text(x, y, value, size, color=INK, anchor="start"):
    # The SVG reader can swallow a leading or final quote; sentinels keep them.
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
        f'text-anchor="{anchor}">​{html.escape(value, quote=False)}​</text>'
    )


def layers(out: Path, stack, background: str = "none") -> Path:
    """Render [(font, [svg items])] as successive layers on one canvas."""
    head = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">'
    base = out.with_suffix(".layer.png")
    for index, (font, items) in enumerate(stack):
        target = out if index == 0 else base
        magick(
            "-background",
            background if index == 0 else "none",
            "-font",
            str(font),
            "svg:-",
            str(target),
            source=head + "".join(items) + "</svg>",
        )
        if index:
            magick(str(out), str(base), "-composite", str(out))
    base.unlink(missing_ok=True)
    return out


def closeup(speaker: str, assets: Path, out: Path, size: int = 132) -> Path:
    """A circular reaction close-up of the active speaker, ringed in their color."""
    name, crop, color = FACES[speaker]
    face = out.with_suffix(".face.png")
    magick(
        str(assets / name),
        "-crop",
        crop,
        "+repage",
        "-resize",
        f"{size}x{size}",
        str(face),
    )
    magick(
        "-size",
        f"{size + 8}x{size + 8}",
        "xc:none",
        "-fill",
        "#0b1324",
        "-stroke",
        color,
        "-strokewidth",
        "4",
        "-draw",
        f"circle {(size + 8) / 2},{(size + 8) / 2} {(size + 8) / 2},4",
        "(",
        str(face),
        "(",
        "-size",
        f"{size}x{size}",
        "xc:none",
        "-fill",
        "white",
        "-draw",
        f"circle {size / 2},{size / 2} {size / 2},1",
        ")",
        "-compose",
        "DstIn",
        "-composite",
        ")",
        "-geometry",
        "+4+4",
        "-compose",
        "Over",
        "-composite",
        str(out),
    )
    face.unlink(missing_ok=True)
    return out


def chapter_label(chapter: str) -> tuple[str, str]:
    """'Belief two: it grades its own homework' -> ('BELIEF 2 OF 7', ...)."""
    match = re.match(r"Belief (\w+): (.+)", chapter)
    if match and match.group(1).lower() in NUMBERS:
        number = NUMBERS.index(match.group(1).lower()) + 1
        return f"BELIEF {number} OF 7", match.group(2)
    return "SETTLE THE ARGUMENT. BEFORE CI.", chapter


def chapter_card(chapter, speaker, assets, fonts, out: Path) -> Path:
    """Large speaker portrait with the chapter title set beside it."""
    kicker, title = chapter_label(chapter)
    lines = textwrap.wrap(title[0].upper() + title[1:], 18)[:3]
    name, _, color = FACES[speaker]
    # Kicker, title lines and rule centered as one block on the portrait axis.
    top = 330 - (40 + 76 * len(lines) + 30) // 2
    rule = top + 40 + 76 * len(lines) + 4
    layers(
        out,
        [
            (
                fonts["body"],
                [
                    f'<rect width="{W}" height="{H}" fill="{NAVY}"/>',
                    '<circle cx="330" cy="330" r="300" fill="#142942"/>',
                    f'<rect x="660" y="{rule}" width="120" height="6" fill="{color}"/>',
                ],
            ),
            (fonts["label"], [text(660, top + 22, kicker, 22, AMBER)]),
            (
                fonts["display"],
                [
                    text(660, top + 40 + 76 * (i + 1) - 18, line, 64)
                    for i, line in enumerate(lines)
                ],
            ),
        ],
        NAVY,
    )
    portrait = out.with_suffix(".portrait.png")
    magick(str(assets / name), "-resize", "x560", str(portrait))
    magick(str(out), str(portrait), "-geometry", "+70+40", "-composite", str(out))
    portrait.unlink(missing_ok=True)
    return out


def tag(kind: str, detail: str, fonts, out: Path) -> Path:
    """Top-left provenance strip: what the viewer is looking at."""
    return layers(
        out,
        [
            (
                fonts["body"],
                ['<rect width="1280" height="64" fill="#0a1222" opacity="0.72"/>'],
            ),
            (
                fonts["label"],
                [text(40, 40, kind + ("  •  " + detail if detail else ""), 17, AMBER)],
            ),
        ],
    )


def browser(screenshot: Path, address: str, fonts, out: Path, crop=None) -> Path:
    """Recorded HTML output in a neutral window frame (no product chrome)."""
    layers(
        out,
        [
            (
                fonts["body"],
                [
                    f'<rect width="{W}" height="{H}" fill="{NAVY}"/>',
                    '<rect x="60" y="84" width="1160" height="508" rx="14" '
                    'fill="#1b2740"/>',
                    *[
                        f'<circle cx="{86 + 20 * i}" cy="106" r="6" fill="#4a5a78"/>'
                        for i in range(3)
                    ],
                    '<rect x="160" y="94" width="620" height="24" rx="12" '
                    'fill="#0e1628"/>',
                ],
            ),
            (fonts["label"], [text(176, 112, address, 14, MUTED)]),
        ],
        NAVY,
    )
    shot = out.with_suffix(".shot.png")
    argv = [str(screenshot)]
    if crop:
        argv += ["-crop", crop, "+repage"]
    magick(
        *argv,
        "-resize",
        "1140x460^",
        "-gravity",
        "north",
        "-extent",
        "1140x460",
        str(shot),
    )
    magick(str(out), str(shot), "-geometry", "+70+128", "-composite", str(out))
    shot.unlink(missing_ok=True)
    return out


def caption(value: str, speaker: str, fonts, out: Path) -> Path:
    """Transparent caption band overlay; stays still while the picture moves."""
    lines = textwrap.wrap(f"{'LitAI' if speaker == 'litai' else 'Sam'}: {value}", 104)
    if len(lines) > 4:
        raise ValueError("Shorten this turn: captions exceed four lines")
    return layers(
        out,
        [
            (
                fonts["body"],
                [
                    '<rect y="600" width="1280" height="120" '
                    'fill="#080e1c" opacity="0.94"/>'
                ],
            ),
            (
                fonts["caption"],
                [
                    text(40, 630 + i * 25, line, 21, "#ffffff")
                    for i, line in enumerate(lines)
                ],
            ),
        ],
    )
