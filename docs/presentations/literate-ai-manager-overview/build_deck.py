#!/usr/bin/env python3
"""Build the Literate-AI manager/engineering overview deck with python-pptx.

Codex-independent replacement for build_deck.mjs, which depended on the
proprietary @oai/artifact-tool runtime bundled only inside Codex's
"presentations" plugin. See source-notes.md's rebuild entry for why that
path was retired: the plugin's own artifact-tool dependency no longer
resolves at all in this environment, independent of the (still separately
live) organizational Codex spend cap.

Deterministic, no LLM calls. Mirrors build_deck.mjs's primitive shape
vocabulary and per-slide layout so the deck stays byte-for-semantics
equivalent for slides 1-18 and 23-26, while adding the four release-
engineering slides (19-22) that deck-specification.md already calls for
but build_deck.mjs never implemented.

Usage:
    python3 build_deck.py
Environment overrides (matching build_deck.mjs):
    LITERATE_AI_DECK_SOURCE, LITERATE_AI_REPO, LITERATE_AI_DECK_OUTPUT
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("LITERATE_AI_DECK_SOURCE", str(HERE)))
REPO = Path(os.environ.get("LITERATE_AI_REPO", str(SOURCE.parents[2])))
ASSETS = SOURCE / "assets"


def _obj_dir() -> Path:
    return Path(os.environ.get("OBJ_DIR") or (REPO / "_build"))


OUT = Path(
    os.environ.get("LITERATE_AI_DECK_OUTPUT")
    or (
        REPO
        / "docs"
        / "presentations"
        / "literate-ai-manager-overview"
        / "literate-ai-manager-and-engineering-overview.pptx"
    )
)

W, H, TOTAL = 1280, 720, 26
PX = 9525  # EMU per px at 96dpi; matches scripts/verify_document_pair.py's EMU_PER_PX.

C = {
    "ink": "#101317",
    "panel": "#23282F",
    "steel": "#65707C",
    "fog": "#EEF1F3",
    "white": "#FFFFFF",
    "orange": "#FF6B35",
    "orange2": "#FF9B66",
    "blue": "#72B7D6",
    "green": "#76B900",
    "green2": "#7BC6A4",
    "red": "#F47C7C",
    "line": "#D8DEE3",
    "muted": "#AAB3BC",
    "code": "#1A1F26",
    "codeline": "#333B45",
}
FONT = "Helvetica Neue"
MONO = "Courier New"

_GEOMETRY = {
    "rect": MSO_SHAPE.RECTANGLE,
    "roundRect": MSO_SHAPE.ROUNDED_RECTANGLE,
    "ellipse": MSO_SHAPE.OVAL,
    "chevron": MSO_SHAPE.CHEVRON,
}
_RADIUS = {"rounded-xl": 0.14, "rounded-lg": 0.09, "rounded-full": 0.5}
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#"))


def _px(v: float) -> Emu:
    return Emu(round(v * PX))


def _set_alpha(fill, opacity: float) -> None:
    solid_fill = fill.fore_color._xFill
    srgb = solid_fill.find(qn("a:srgbClr"))
    if srgb is None:
        return
    for existing in srgb.findall(qn("a:alpha")):
        srgb.remove(existing)
    alpha = solid_fill.makeelement(qn("a:alpha"), {"val": str(round(opacity * 100000))})
    srgb.append(alpha)


def shape(slide, geometry, x, y, w, h, fill, **opts):
    sp = slide.shapes.add_shape(_GEOMETRY[geometry], _px(x), _px(y), _px(w), _px(h))
    sp.shadow.inherit = False
    if fill == "none":
        sp.fill.background()
    else:
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(fill)
        opacity = opts.get("opacity")
        if opacity is not None:
            _set_alpha(sp.fill, opacity)
    stroke = opts.get("stroke", "none")
    if stroke in (None, "none"):
        sp.line.fill.background()
    else:
        sp.line.color.rgb = _rgb(stroke)
        sp.line.width = Pt(opts.get("strokeWidth") or 1)
    radius_key = opts.get("radius")
    if radius_key and geometry == "roundRect":
        sp.adjustments[0] = _RADIUS.get(radius_key, 0.05)
    return sp


def text(slide, value, x, y, w, h, size=28, color=C["ink"], bold=False, **opts):
    box = slide.shapes.add_textbox(_px(x), _px(y), _px(w), _px(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)
    tf.text = value
    align = _ALIGN.get(opts.get("align"))
    font_name = opts.get("font", FONT)
    italic = opts.get("italic", False)
    for para in tf.paragraphs:
        if align is not None:
            para.alignment = align
        for run in para.runs:
            run.font.name = font_name
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.italic = italic
            run.font.color.rgb = _rgb(color)
    if opts.get("fit"):
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    return box


def mono(slide, value, x, y, w, h, size=12, color=C["fog"], bold=False):
    return text(slide, value, x, y, w, h, size, color, bold, font=MONO)


def line(slide, x, y, w, h=4, color=C["orange"]):
    return shape(slide, "rect", x, y, w, h, color)


def dot(slide, x, y, d, color, stroke="none"):
    return shape(
        slide, "ellipse", x, y, d, d, color,
        stroke=stroke, strokeWidth=0 if stroke == "none" else 2,
    )


def pill(slide, label, x, y, w, fill=C["orange"], color=C["white"], size=13):
    shape(slide, "roundRect", x, y, w, 32, fill, radius="rounded-full")
    text(slide, label, x + 10, y + 6, w - 20, 20, size, color, True, align="center", fit=True)


def chip(slide, label, x, y, w, fill=C["panel"], color=C["fog"], size=13):
    shape(slide, "roundRect", x, y, w, 34, fill, radius="rounded-lg")
    text(slide, label, x + 12, y + 7, w - 24, 20, size, color, True, fit=True)


def labeled_card(slide, x, y, w, h, fill, heading, body, heading_color, body_color, heading_size=20, body_size=15, **opts):
    """A rounded cell whose copy lives in the shape text frame, so Google cannot overflow it."""
    sp = shape(slide, "roundRect", x, y, w, h, fill, radius="rounded-xl", **opts)
    tf = sp.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = _px(18)
    tf.margin_top = tf.margin_bottom = _px(18)
    lines = [heading, *body.split("\n")]
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = PP_ALIGN.CENTER
        para.text = line
        if i == 0:
            para.space_after = Pt(10)
        for run in para.runs:
            run.font.name = FONT
            run.font.size = Pt(heading_size if i == 0 else body_size)
            run.font.bold = True
            run.font.color.rgb = _rgb(heading_color if i == 0 else body_color)
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    return sp


def caption_bar(slide, value, y=648, fill="#FFF0E8", color=C["ink"], dark=False):
    shape(slide, "roundRect", 70, y, 1140, 36, fill, radius="rounded-full")
    text(slide, value, 90, y + 8, 1100, 22, 13, color, True, align="center", fit=True)


def add_image_cover(slide, key, x=0, y=0, w=W, h=H):
    """Embed a cover-fitted JPEG at slide resolution.

    Authoring assets stay high-resolution PNG. The PPTX must stay under the
    project's per-file documentation byte limit (16 MiB), so the builder
    never ships the raw asset bytes.
    """
    path = ASSETS / f"{key}.png"
    target_w, target_h = int(w), int(h)
    with Image.open(path) as im:
        im = im.convert("RGB")
        iw, ih = im.size
        img_ratio, box_ratio = iw / ih, target_w / target_h
        if img_ratio > box_ratio:
            new_w = int(ih * box_ratio)
            left = (iw - new_w) // 2
            im = im.crop((left, 0, left + new_w, ih))
        elif img_ratio < box_ratio:
            new_h = int(iw / box_ratio)
            top = (ih - new_h) // 2
            im = im.crop((0, top, iw, top + new_h))
        im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        buf.seek(0)
    return slide.shapes.add_picture(buf, _px(x), _px(y), _px(w), _px(h))


# NOTE: unlike the retired @oai/artifact-tool build (whose shape `opacity` option
# was silently ignored, rendering an OPAQUE ink panel per build_deck.mjs's own
# comment), python-pptx's a:alpha element gives real translucency here. Kept the
# same call sites and default opacity as the original for visual parity; only the
# rendered result is now actually translucent rather than opaque.
def wash(slide, x=0, y=0, w=W, h=H, opacity=0.35):
    return shape(slide, "rect", x, y, w, h, C["ink"], opacity=opacity)


def title(slide, value, sub, dark, n, tag=None):
    # Reserved header band (y=28–252). Title boxes are taller than the type they
    # hold so Google's Arial substitution cannot paint into the subtitle.
    ink = C["white"] if dark else C["ink"]
    muted = C["muted"] if dark else C["steel"]
    pill(slide, "LITERATE-AI", 70, 28, 132, C["white"] if dark else C["ink"], C["ink"] if dark else C["white"])
    if tag:
        tag_w = max(188, 32 + len(tag) * 9)
        pill(slide, tag, 214, 28, tag_w, C["orange"], C["ink"], 12)
    text(slide, str(n).zfill(2), 1170, 32, 40, 22, 13, ink, True, align="right")
    long = len(value) > 46
    t_size = 32 if long else 38
    text(slide, value, 70, 78, 1140, 84, t_size, ink, True, fit=True)
    if sub:
        text(slide, sub, 72, 168, 1100, 68, 17, muted, False, fit=True)
    line(slide, 70, 248, 88, 6, C["orange"])


def footer(slide, n, dark=False):
    # Footer lives below y=696 so bottom captions ending at y=684 cannot collide.
    text(slide, "LITERATE-AI", 70, 698, 150, 16, 11, C["muted"] if dark else C["steel"], True)
    line(slide, 1080, 704, 120, 3, C["panel"] if dark else C["line"])
    line(slide, 1080, 704, max(8, 120 * n / TOTAL), 3, C["orange"])


def arrow(slide, x1, y, x2, color=C["orange"]):
    line(slide, x1, y, max(4, x2 - x1 - 13), 4, color)
    shape(slide, "chevron", x2 - 19, y - 8, 19, 20, color)


def notes(slide, lines):
    slide.notes_slide.notes_text_frame.text = "\n".join(lines)


def build() -> Path:
    p = Presentation()
    p.slide_width = _px(W)
    p.slide_height = _px(H)
    blank = p.slide_layouts[6]

    def add_slide(bg=C["white"]):
        s = p.slides.add_slide(blank)
        shape(s, "rect", 0, 0, W, H, bg)
        return s

    # 01 - cover
    s = add_slide(C["ink"])
    add_image_cover(s, "cover-hero")
    # Cover copy stays in the left third so it cannot paint into the hero graphic.
    wash(s, 0, 0, 480, H, 0.55)
    pill(s, "LITERATE-AI", 70, 55, 132, C["white"], C["ink"])
    text(s, "Software that can\nrebuild itself\nfrom intent", 70, 130, 380, 210, 36, C["white"], True)
    text(s, "A path from isolated code generation to governed creation of complete applications and software portfolios.", 74, 360, 370, 110, 18, C["fog"])
    line(s, 74, 490, 112, 7, C["orange"])
    text(s, "THE APPLICATION FOUNDRY VISION", 74, 512, 370, 28, 15, C["orange2"], True)
    text(s, "2026", 1160, 668, 50, 18, 12, C["muted"], True, align="right")
    notes(s, [
        "Opening frame. This deck argues for a focused application-layer investment; it is not a product-completion announcement.",
        "Slides 7 through 11 are the technical sequence and slides 19 through 22 are the release-engineering sequence; point engineering reviewers there, a decision audience can move past them.",
        "Every quantified figure in this deck traces to source-notes.md. Nothing here is a modeled ROI or productivity estimate.",
    ])

    # 02 - the problem
    s = add_slide(C["fog"])
    title(s, "We still scale software by copying its implementation.", "Each product, platform, and repository inherits another version of the same knowledge—and another place for it to drift.", False, 2)
    text(s, "ONE PRODUCT", 75, 330, 210, 28, 16, C["steel"], True)
    line(s, 124, 398, 870, 5, C["ink"])
    xs = [95, 305, 515, 725, 935]
    labels2 = ["original", "copy", "fork", "patch", "coordinate"]
    for i, x in enumerate(xs):
        fill = C["orange"] if i == 0 else C["white"]
        stroke = C["orange"] if i == 0 else (C["red"] if i >= 3 else C["line"])
        dot(s, x, 369, 58, fill, stroke)
        text(s, str(i + 1), x + 19, 386, 20, 20, 17, C["white"] if i == 0 else C["ink"], True, align="center")
        if i > 0:
            line(s, x - 150, 398, 150, 3, C["orange"] if i < 3 else C["red"])
        text(s, labels2[i], x - 24, 440, 106, 22, 15, C["steel"], True, align="center")
    text(s, "The implementation multiplies. The intent does not.", 75, 548, 760, 46, 28, C["ink"], True)
    footer(s, 2)
    notes(s, [
        "The problem statement is qualitative and deliberately carries no figures. Do not attach a drift or duplication statistic to it.",
        "The claim is structural: knowledge is re-expressed per copy, so every copy becomes an independent place for it to diverge.",
    ])

    # 03 - the inversion
    s = add_slide(C["ink"])
    title(s, "Make intent the product. Make implementation renewable.", "Literate-AI keeps observable behavior readable and durable, then treats source as a candidate that must earn acceptance.", True, 3)
    card_w, card_h, gap = 340, 188, 40
    xs3 = [70, 70 + card_w + gap, 70 + 2 * (card_w + gap)]
    arrow(s, xs3[0] + card_w, 392, xs3[1])
    arrow(s, xs3[1] + card_w, 392, xs3[2])
    labeled_card(s, xs3[0], 300, card_w, card_h, C["orange"], "INTENT", "Readable behavior\n+ constraints", C["ink"], C["ink"], 22, 16)
    labeled_card(s, xs3[1], 300, card_w, card_h, C["panel"], "REGENERATE", "Exact plan\n+ bounded execution", C["white"], C["muted"], 22, 16, stroke="#3A424B", strokeWidth=1)
    labeled_card(s, xs3[2], 300, card_w, card_h, C["green2"], "PROOF", "Build + test\n+ independent acceptance", C["ink"], C["ink"], 22, 16)
    text(s, "Durable", xs3[0], 504, card_w, 28, 18, C["orange2"], True, align="center")
    text(s, "Replaceable", xs3[1], 504, card_w, 28, 18, C["blue"], True, align="center")
    text(s, "Promotable", xs3[2], 504, card_w, 28, 18, C["green2"], True, align="center")
    footer(s, 3, True)
    notes(s, [
        "This is the thesis slide. The three mechanisms behind it are shown on slides 7, 8 and 9 respectively — do not treat this diagram as the explanation.",
        "\"Candidate\" is precise: generated source holds no build, acceptance, cache-membership, or publication authority until the current gates pass.",
    ])

    # 04 - implementation velocity
    s = add_slide(C["white"])
    title(s, "The implementation is moving unusually fast.", "One authority model now spans language, build, operating-system, and packaging choices while preserving exact target constraints.", False, 4)
    facts = [("6", "languages"), ("4", "build systems"), ("3", "OS families"), ("6", "package providers")]
    for i, (num, label) in enumerate(facts):
        x = 72 + i * 296
        text(s, num, x, 338, 240, 70, 48, C["orange"] if i == 3 else C["ink"], True, align="center")
        line(s, x + 30, 423, 180, 4, C["orange"] if i == 3 else C["line"])
        text(s, label, x, 448, 240, 30, 18, C["steel"], True, align="center")
    stages4 = ["plan", "generate", "build", "test", "run", "accept", "receipt"]
    for i, label in enumerate(stages4):
        x = 70 + i * 168
        shape(s, "chevron", x, 528, 156, 44, C["orange"] if i == 0 else C["panel"])
        text(
            s, label, x + 6, 540, 128, 22, 13,
            C["ink"] if i == 0 else C["white"], True, align="center",
        )
    caption_bar(s, "Surface area, not maturity. Slides 7–11 show the control model.")
    footer(s, 4)
    notes(s, [
        "Current matrix: Python 3.11+, C++17, Rust 2021, JavaScript/Node 20+, Swift, and Go; Bazel, GNU Make, CMake, and Cargo; macOS, Linux, and Windows Flavors.",
        "Packaging Flavors are pip, Conan, apt, Homebrew, WinGet, and Chocolatey. Multi-provider planning is implemented; native application-package construction and independent verification are live for pip wheels and Conan cache archives.",
        "The separate host installer selects one strict CycloneDX prerequisite SBOM by OS, CPU, and accelerator coordinate, then delegates missing native packages to APT, Homebrew, or WinGet only after consent.",
        "The private user-owned inventory currently has six workers spanning macOS ARM64, Windows 11 CPU/GPU, Ubuntu 24.04 CPU/GPU, and Ubuntu 26.04 CPU. The deck does not disclose endpoints or claim the full Cartesian matrix is qualified.",
    ])

    # 05 - real sample applications
    s = add_slide(C["ink"])
    title(s, "The samples now prove complete application boundaries.", "Readable Components generate, build, run, and pass independent acceptance across package, deployment-unit, durable-service, and host-platform contracts.", True, 5)
    apps = [
        ("PACKAGE PROOF", "Verified wheel +\nConan bytes", C["orange"]),
        ("MULTI-ENTRYPOINT", "One build + every\ndeployment unit", C["blue"]),
        ("DURABLE SPLIT", "Frontend · API\ncollector · cache", C["green2"]),
        ("HOST PORTABILITY", "Tuple SBOM +\nconfig migration", C["orange2"]),
    ]
    for i, (label, desc, color) in enumerate(apps):
        x = 70 + i * 286
        shape(s, "roundRect", x, 326, 262, 178, C["panel"], radius="rounded-xl", stroke=color, strokeWidth=2)
        text(s, label, x + 18, 348, 226, 38, 16, color, True, align="center")
        line(s, x + 76, 405, 110, 4, color)
        text(s, desc, x + 24, 430, 214, 52, 15, C["fog"], False, align="center")
    arrow(s, 215, 548, 402)
    arrow(s, 505, 548, 692)
    arrow(s, 795, 548, 982)
    text(s, "SPEC", 88, 535, 120, 28, 17, C["white"], True, align="center")
    text(s, "SOURCE", 405, 535, 120, 28, 17, C["white"], True, align="center")
    text(s, "BINARY", 695, 535, 120, 28, 17, C["white"], True, align="center")
    text(s, "ACCEPTANCE", 985, 535, 170, 28, 17, C["green2"], True, align="center")
    footer(s, 5, True)
    notes(s, [
        "These are repository contracts and samples, not product mockups. The harness catalogs 26 behavior applications, including three explicit OS pins.",
        "The multi-entrypoint lifecycle carries every deployment unit through build, TEST, EXECUTE, package dispatch, independent acceptance, receipts, and SBOM evidence. The durable split portfolio keeps frontend, API, collector, and SQLite cache as separate generated authority boundaries.",
        "The opt-in sample package target constructs and independently verifies pip wheels and Conan cache archives. Host installation is a separate SBOM-driven prerequisite contract and does not claim native application-package construction for APT, Homebrew, or WinGet.",
    ])

    # 06 - the destination
    s = add_slide(C["ink"])
    add_image_cover(s, "application-foundry")
    wash(s, 0, 0, 610, H, 0.36)
    pill(s, "THE NEXT STEP", 70, 50, 150, C["white"], C["ink"])
    text(s, "The goal is not better\ncode generation.", 70, 126, 490, 105, 45, C["white"], True)
    text(s, "It is the governed creation of complete applications: interfaces, services, resources, tests, packages, releases, and runtime projections.", 72, 278, 440, 126, 21, C["fog"])
    text(s, "An application foundry.", 72, 486, 400, 38, 28, C["orange2"], True)
    footer(s, 6, True)
    notes(s, [
        "Aspirational framing, explicitly labeled as the next step rather than current behavior.",
        "This slide closes the argument section. The next five slides answer \"how does it actually work\" before the deck returns to consequences.",
    ])

    # 07 - HOW IT WORKS 1/5 - the durable authority, shown
    s = add_slide(C["white"])
    title(s, "This is the durable authority.", "A Component specification is a readable file. Requirements, scenarios, and the public capability contract are the product; the source tree is what gets regenerated from them.", False, 7, "HOW IT WORKS · 1/5")
    shape(s, "roundRect", 70, 272, 556, 350, C["code"], radius="rounded-lg")
    text(s, "components/money-calculation/component.md", 88, 286, 520, 18, 11, C["orange2"], True)
    mono(s, "### Requirement: Exact integer discount calculation\n\nThe portable run entrypoint SHALL accept a\nnon-negative integer subtotal in cents and a\ndiscount in basis points from 0 through 10000,\ncompute discount as\nfloor((subtotal_cents * discount_basis_points\n      + 5000) / 10000)\n\n#### Scenario: Half-up discount rounding\n- WHEN a 999-cent subtotal receives a\n  1250-basis-point discount\n- THEN the discount is 125 cents and the\n  total is 874 cents", 88, 314, 520, 290, 12, C["fog"])
    shape(s, "roundRect", 654, 272, 556, 350, C["panel"], radius="rounded-lg")
    text(s, "interfaces/money-calculation.md  —  the public capability contract", 672, 286, 520, 18, 11, C["blue"], True)
    mono(s, "This contract is language-neutral. The realized\nprovider artifact SHALL expose its portable `run`\nentrypoint through the environment variable\nLITAI_CAPABILITY_MONEY_CALCULATION.\n\nConsumers SHALL use this public process contract\nand SHALL NOT import, copy, or depend on private\nprovider source layout.", 672, 314, 520, 200, 12, C["fog"])
    line(s, 672, 530, 520, 1, C["codeline"])
    mono(s, "flavor_slots:  build.system · language-ecosystem · platform.os", 672, 546, 520, 20, 11, C["muted"])
    caption_bar(s, "Language, build, OS, and the source tree sit below this line. They are Flavor choices and regeneration targets.", 648)
    footer(s, 7)
    notes(s, [
        "Both excerpts are verbatim from the repository sample at components/money-calculation/. Nothing on this slide is illustrative prose.",
        "This is the slide that earns the thesis. A deck claiming readable intent is the product must show the readable intent.",
        "The right panel is the reuse boundary: consumers bind to a public process contract, never to provider source layout. That is what makes a provider's private implementation replaceable without touching its consumers.",
    ])

    # 08 - HOW IT WORKS 2/5 - the generation key
    s = add_slide(C["ink"])
    add_image_cover(s, "generation-key")
    wash(s, 0, 0, W, 268, 0.72)
    shape(s, "rect", 0, 268, 430, 314, C["ink"], opacity=0.82)
    shape(s, "rect", 960, 268, 320, 200, C["ink"], opacity=0.72)
    title(s, "What a generation key binds — and what it refuses to.", "Cache identity is the narrow boundary the whole model rests on. Bind too much and reuse dies; bind too little and authority leaks across the line.", True, 8, "HOW IT WORKS · 2/5")
    text(s, "INSIDE THE KEY", 70, 278, 360, 22, 13, C["orange"], True)
    binds = [
        "Spec set + document identities",
        "Target and Flavors",
        "Skills, workflow, routing",
        "Exact model + locked assets",
        "Own exported interfaces",
        "Direct public contracts",
    ]
    for i, label in enumerate(binds):
        chip(s, label, 70, 306 + i * 38, 340, C["panel"], C["fog"], 12)
    text(s, "OUTSIDE", 980, 278, 220, 22, 13, C["muted"], True)
    refuses = [
        "Authoring revisions",
        "Private dep specs",
        "Dep source / tests",
        "Dep routing / skills",
    ]
    for i, label in enumerate(refuses):
        chip(s, label, 980, 306 + i * 38, 220, C["code"], C["muted"], 12)
    shape(s, "rect", 0, 582, W, 138, C["ink"], opacity=0.78)
    text(s, "BLAST RADIUS", 70, 592, 220, 18, 12, C["orange2"], True)
    br = [
        ("leaf", "invalidates itself"),
        ("exported interface", "leaf + consumers"),
        ("diamond", "once per layer"),
        ("deeper private", "consumer reusable"),
    ]
    for i, (a, b) in enumerate(br):
        x = 70 + i * 288
        # Mini graph: one highlighted node, optional consumers.
        cy = 628
        if i == 0:
            dot(s, x + 110, cy, 18, C["orange"])
        elif i == 1:
            dot(s, x + 110, cy, 16, C["orange"])
            dot(s, x + 70, cy + 22, 12, C["blue"])
            dot(s, x + 150, cy + 22, 12, C["blue"])
            line(s, x + 116, cy + 16, 2, 12, C["steel"])
            line(s, x + 124, cy + 16, 2, 12, C["steel"])
        elif i == 2:
            dot(s, x + 110, cy + 22, 16, C["orange"])
            dot(s, x + 70, cy, 12, C["green2"])
            dot(s, x + 150, cy, 12, C["green2"])
        else:
            dot(s, x + 70, cy, 12, C["muted"])
            dot(s, x + 150, cy + 18, 16, C["panel"], C["steel"])
        text(s, a, x, 652, 250, 18, 13, C["white"], True)
        text(s, b, x, 670, 250, 18, 12, C["muted"])
    footer(s, 8, True)
    notes(s, [
        "Authority: docs/architecture/component-execution-plans.md, \"Generation-key boundary\". Every line on this slide is a stated rule, not a summary.",
        "The refusal column is the substantive engineering claim. Binding aggregate revisions would be the easy implementation and would destroy both reuse and the acceptance-oracle separation.",
        "ComponentGenerationPlan still retains the exact direct edges for audit; cache membership uses generation_key.identity. The audit plan may legitimately change when a provider revision changes even while the consumer source key remains reusable.",
    ])

    # 09 - HOW IT WORKS 3/5 - layers, concurrency, budgets
    s = add_slide(C["white"])
    title(s, "Layered execution. Bounded concurrency. Exact budgets.", "Ordering and cost are decided before any model runs. A plan that cannot be scheduled is a stable planning error, not a failed generation.", False, 9, "HOW IT WORKS · 3/5")
    shape(s, "roundRect", 70, 300, 556, 330, C["fog"], radius="rounded-lg")
    layers = [("LAYER 0", ["A", "B", "C"], C["orange"]), ("LAYER 1", ["D", "E"], C["blue"]), ("LAYER 2", ["F"], C["green2"])]
    for i, (label, nodes, color) in enumerate(layers):
        y = 326 + i * 100
        text(s, label, 92, y + 16, 86, 20, 12, C["steel"], True)
        for j, n in enumerate(nodes):
            x = 190 + j * 118
            shape(s, "roundRect", x, y, 100, 52, C["white"], radius="rounded-lg", stroke=color, strokeWidth=2)
            text(s, n, x, y + 14, 100, 24, 19, C["ink"], True, align="center")
        if i < 2:
            shape(s, "rect", 238, y + 52, 4, 48, C["line"])
    shape(s, "rect", 186, 318, 4, 68, C["orange"])
    text(s, "concurrent, up to an explicit bound", 190, 392, 240, 32, 11, C["orange"], True)
    text(s, "Every applicable edge requires the provider to occupy an earlier layer than its consumer.", 92, 592, 510, 32, 13, C["steel"])
    facts9 = [
        ("Emission is deterministic", "Results emit in canonical Component-revision order, not completion order."),
        ("Budgets are exact", "A reported measurement that exceeds the node's budget fails that node and retains the observed values for diagnosis."),
        ("Absent is not zero", "Attempts, wall time, tokens, and cost each stay null when unmeasured. The framework never invents a zero."),
        ("Reuse is a strict predicate", "Key, context manifest, budget and decision, prompt, recipe, workspace allocation, and complete typed output must all match."),
    ]
    for i, (a, b) in enumerate(facts9):
        y = 302 + i * 84
        line(s, 656, y, 7, 58, C["orange"] if i == 3 else C["ink"])
        text(s, a, 678, y - 4, 520, 24, 17, C["ink"], True)
        text(s, b, 678, y + 22, 520, 52, 13, C["steel"])
    footer(s, 9)
    notes(s, [
        "Authority: docs/architecture/component-execution-plans.md, sections \"Stable action layers\" and \"Bounded incremental generation\".",
        "This is the cost-control answer. If asked \"what does a run cost\": concurrency is explicitly bounded, per-node budgets are exact and enforced after the fact, and a cache hit reports no model execution at all.",
        "ComponentExecutionPlan carries one action plan for every canonical phase: generate, validate, build, run, package, deploy.",
        "The planner rechecks closure, cycles, and exact public-interface bindings before a coding CLI can run, and never repairs an interface failure by exposing a dependency's private material.",
    ])

    # 10 - HOW IT WORKS 4/5 - the negative path
    s = add_slide(C["ink"])
    add_image_cover(s, "gate-failure")
    wash(s, 0, 0, W, 268, 0.72)
    shape(s, "rect", 0, 268, 560, 370, C["ink"], opacity=0.82)
    title(s, "What happens when a gate fails.", "The negative path is the design, not an afterthought. Failure stays bounded, attributable, and cheap to retry.", True, 10, "HOW IT WORKS · 4/5")
    rows10 = [
        ("FAILED PROVIDER", "cancel dependents", "independents continue", C["red"]),
        ("KEY MISMATCH", "fail closed", "explicit regen only", C["orange"]),
        ("CACHE HIT", "re-run current gates", "not republished", C["blue"]),
        ("BUDGET OVERRUN", "fail, keep values", "not charged to the hit", C["green2"]),
    ]
    for i, (a, b, c, color) in enumerate(rows10):
        y = 278 + i * 78
        shape(s, "roundRect", 70, y, 470, 68, C["panel"], radius="rounded-lg")
        line(s, 70, y, 6, 68, color)
        text(s, a, 92, y + 8, 430, 20, 13, color, True)
        chip(s, b, 92, y + 32, 186, C["code"], C["fog"], 11)
        chip(s, c, 288, y + 32, 232, C["code"], C["muted"], 11)
    caption_bar(s, "Reuse authorizes the exact source bytes only. It grants no build, acceptance, or publication authority.", 648, C["code"], C["muted"], True)
    footer(s, 10, True)
    notes(s, [
        "Authority: docs/architecture/component-execution-plans.md, \"Bounded incremental generation\" and the accepted-source membership path.",
        "The cache-hit row is the one most audiences do not expect. Matching identities authorize reuse of the immutable source artifacts only; the service creates a new candidate, output, and provenance projection bound to the current prepared node and reruns every current gate.",
        "Any prepared recipe lock must equal the current execution-plan lock. A mismatch fails before work starts.",
    ])

    # 11 - HOW IT WORKS 5/5 - the trust boundary
    s = add_slide(C["ink"])
    add_image_cover(s, "isolation-containment")
    wash(s, 0, 0, W, 268, 0.72)
    shape(s, "rect", 0, 268, 575, 370, C["ink"], opacity=0.88)
    title(s, "What never crosses the model boundary.", "Generated bytes are untrusted input. The build contract has to exist before they do.", True, 11, "HOW IT WORKS · 5/5")
    text(s, "DECLARATION FIRST", 70, 278, 360, 20, 13, C["orange2"], True)
    shape(s, "roundRect", 70, 304, 470, 70, C["panel"], radius="rounded-lg", stroke=C["orange"], strokeWidth=2)
    text(s, "BuildRequestDeclaration", 90, 314, 430, 20, 15, C["white"], True)
    text(s, "builder · toolchain · sandbox · privileges · outputs", 90, 340, 430, 22, 12, C["muted"])
    text(s, "cannot name a source bundle", 90, 358, 430, 18, 13, C["orange"], True)
    arrow(s, 220, 386, 340, C["orange"])
    shape(s, "roundRect", 70, 404, 470, 58, C["code"], radius="rounded-lg")
    text(s, "Generated tree + tests + SBOM admit a source-bound BuildRequest", 90, 420, 430, 30, 13, C["fog"], False, fit=True)
    text(s, "ISOLATION LADDER", 70, 478, 360, 20, 13, C["steel"], True)
    lv = [
        ("host-yolo", "no containment", C["red"]),
        ("process-limited", "limits, not a security boundary", C["orange"]),
        ("os-sandboxed", "OS-enforced controls", C["green2"]),
    ]
    for i, (a, b, color) in enumerate(lv):
        x = 70 + i * 158
        shape(s, "roundRect", x, 504, 148, 70, C["panel"], radius="rounded-lg")
        line(s, x, 504, 148, 4, color)
        text(s, a, x + 8, 516, 132, 22, 11, C["white"], True, font=MONO, align="center", fit=True)
        text(s, b, x + 8, 540, 132, 28, 11, C["muted"], False, align="center", fit=True)
    caption_bar(s, "Live samples run with explicit YOLO as the host user. Production containment is an architecture contract — not a shipped backend.", 648, "#3A2A22", C["orange2"], True)
    footer(s, 11, True)
    notes(s, [
        "Authority: docs/architecture/production-containment-threat-model.md and the typed build boundary in component-execution-plans.md.",
        "Do not soften the orange band. The threat model document itself opens by saying it is a contract, not a claim that production containment backends exist.",
        "The declaration-cannot-name-a-source-bundle rule is the concrete expression of \"generated source is untrusted\": the build's authority is fixed before the model produces anything.",
        "A future trusted launcher authorization must precede execution, and authenticated OPS-300 evidence must qualify the resulting report.",
    ])

    # 12 - evidence
    s = add_slide(C["ink"])
    add_image_cover(s, "evidence-artifact")
    wash(s, 0, 0, 620, H, 0.58)
    title(s, "Correctness ships with the artifact.", "Source plus the evidence to trust, review, release, and reproduce it. Failed steps retain diagnostics; successful steps clean up.", True, 12)
    pillars = [
        ("BEHAVIOR", "Tests + bounded execution", C["orange"]),
        ("INTENT", "Independent acceptance", C["blue"]),
        ("PROVENANCE", "SBOMs + retained pointers", C["green2"]),
    ]
    for i, (a, b, color) in enumerate(pillars):
        y = 278 + i * 92
        line(s, 70, y, 8, 70, color)
        text(s, a, 96, y, 400, 28, 22, C["white"], True)
        text(s, b, 96, y + 32, 400, 28, 16, C["fog"])
    caption_bar(s, "Acceptance is a property of the release — not a memory of the process.", 648, C["orange"], C["ink"], True)
    footer(s, 12, True)
    notes(s, [
        "Independent acceptance is a separate oracle from the generating model. That separation is exactly why the generation key refuses to bind aggregate authoring revisions (slide 8).",
        "Pre-build and post-build CycloneDX evidence are supported today; authority is docs/architecture/sbom-and-dependency-graph.md.",
        "Each per-node result binds the current context and budget identities and is the common journal, benchmark, and receipt-facing evidence surface.",
        "The release evidence ledger retains failed sample scratch, installed workspaces, transcripts, and diagnostics while successful steps still clean up.",
    ])

    # 13 - component DAG
    s = add_slide(C["ink"])
    title(s, "Applications are exact Component DAGs—not flattened prompts.", "The 0.9 durable split portfolio keeps upstream credentials, durable writes, read-only service, and browser presentation behind separate public contracts.", True, 13)
    text(s, "ROOT 1 · COLLECTION", 70, 286, 490, 22, 13, C["orange2"], True)
    text(s, "ROOT 2 · SERVING", 650, 286, 530, 22, 13, C["blue"], True)
    ns = [
        (70, 350, 210, "COLLECTOR", "only upstream caller", C["orange"]),
        (360, 350, 220, "SNAPSHOT CACHE", "SQLite schema owner", C["green2"]),
        (660, 350, 200, "READ-ONLY API", "no credentials", C["blue"]),
        (940, 350, 240, "BROWSER FRONTEND", "same-origin API only", C["orange2"]),
    ]
    for x1, x2, color in ((280, 360, C["orange"]), (580, 660, C["steel"]), (860, 940, C["steel"])):
        arrow(s, x1, 402, x2, color)
    for x, y, w, label, sub, color in ns:
        shape(s, "roundRect", x, y, w, 106, C["panel"], radius="rounded-xl", stroke=color, strokeWidth=2)
        text(s, label, x + 12, y + 22, w - 24, 28, 15, C["white"], True, align="center", fit=True)
        text(s, sub, x + 12, y + 60, w - 24, 24, 12, C["muted"], False, align="center", fit=True)
    shape(s, "roundRect", 70, 584, 1120, 54, C["code"], radius="rounded-lg")
    text(s, "Two Standard roots share public capability contracts; private specifications, source, tests, and credentials never flatten across an edge.", 92, 600, 1076, 26, 15, C["orange2"], True, align="center", fit=True)
    footer(s, 13, True)
    notes(s, [
        "This is the implemented durable-split-service portfolio from samples/_harness/durable-split-service/portfolio.json, not an illustrative architecture. Collection and serving are independently executable Standard roots that share the snapshot-cache capability.",
        "The cache owns exact schema and migration; the collector alone invokes the upstream fixture; the API is read-only; browser acceptance exercises the frontend. Failure, retry, lease, restart, and idempotence are verified as cross-process flows.",
        "The general planner still supports arbitrary acyclic graphs, rejects cycles, validates exact interface bindings, emits provider-before-consumer layers, and never gives a consumer private transitive Component detail.",
    ])

    # 14 - one intent, many targets
    s = add_slide(C["ink"])
    add_image_cover(s, "one-many")
    wash(s, 0, 0, 585, H, 0.30)
    pill(s, "COMPOUNDING REUSE", 70, 48, 202, C["white"], C["ink"])
    text(s, "One application intent\ncan serve many products\nand runtimes.", 70, 122, 455, 158, 44, C["white"], True)
    text(s, "Flavors make language, OS, build, packaging, deployment, and hardware variation explicit—without forking behavior.", 72, 336, 430, 112, 20, C["fog"])
    shape(s, "roundRect", 72, 462, 440, 58, C["panel"], radius="rounded-lg")
    text(s, "Packaging composes too: pip + Conan can serve different\ncommunities from the same exact Component authority.", 92, 474, 410, 36, 13, C["fog"])
    text(s, "Variation becomes a first-class product decision.", 72, 542, 440, 50, 24, C["orange2"], True)
    footer(s, 14, True)
    notes(s, [
        "Flavor axes include build.system, implementation.language-ecosystem, platform.os, packaging, toolchain, and documentation.ecosystem.",
        "Packaging is multi-value: compatible providers form independent plans. OS constraints still reject apt, Homebrew, WinGet, or Chocolatey on incompatible targets before generation.",
        "The documentation.ecosystem axis governs format and collaboration surface, never product claims, approval state, build execution, or release authority.",
    ])

    # 15 - cross-platform fan-out
    s = add_slide(C["ink"])
    title(s, "One command fans proof across real operating systems.", "The user supplies private runner destinations. Literate-AI selects each platform Flavor, checks out the exact revision, and executes targets in parallel.", True, 15)
    shape(s, "roundRect", 70, 326, 250, 224, C["orange"], radius="rounded-xl")
    text(s, "FAN-OUT DRIVER", 96, 354, 198, 28, 20, C["ink"], True, align="center")
    mono(s, "sample: *\nrevision: exact Git SHA\nmode: fail-fast\nresume: checkpoint", 96, 406, 198, 100, 14, C["ink"], True)
    runners = [
        (420, 310, "macOS ARM64", "one worker", C["blue"]),
        (730, 310, "Windows 11", "CPU + NVIDIA workers", C["orange2"]),
        (420, 470, "Ubuntu 24.04", "CPU + NVIDIA workers", C["green2"]),
        (730, 470, "Ubuntu 26.04", "one CPU worker", C["green2"]),
    ]
    for i, (x, y, label, sub, color) in enumerate(runners):
        arrow(s, 320, 390 if i < 2 else 486, x, color)
        shape(s, "roundRect", x, y, 250, 106, C["panel"], radius="rounded-xl", stroke=color, strokeWidth=2)
        text(s, label, x + 20, y + 20, 210, 28, 20, C["white"], True, align="center")
        text(s, sub, x + 20, y + 58, 210, 30, 13, C["muted"], False, align="center")
    shape(s, "roundRect", 1010, 348, 200, 178, C["code"], radius="rounded-xl", stroke=C["green2"], strokeWidth=2)
    text(s, "COMPACT\nEVIDENCE", 1032, 376, 156, 54, 19, C["green2"], True, align="center")
    text(s, "passed targets\nfail-fast repair\nfull rerun resets", 1032, 444, 156, 60, 13, C["fog"], False, align="center")
    text(s, "Runner identities never enter the repository. The matrix is operator-owned configuration.", 215, 606, 850, 28, 18, C["orange2"], True, align="center")
    footer(s, 15, True)
    notes(s, [
        "Implemented by scripts/fanout_samples.py and documented in docs/user/samples.md. Six configured worker entries execute with bounded parallelism and platform Flavors selected from the user-owned inventory.",
        "The catalog retains every sample for readers. Executable discovery admits a pinned sample only when its platform matches the worker, before a coding agent is invoked.",
        "Inventory is not passing evidence. Successful target IDs are checkpointed under OBJ_DIR, and a completed repair cycle still requires one clean from-zero rerun for release evidence.",
    ])

    # 16 - one operating model
    s = add_slide(C["ink"])
    add_image_cover(s, "manager-section")
    wash(s, 0, 0, 620, H, 0.40)
    pill(s, "ONE OPERATING MODEL", 70, 50, 205, C["white"], C["ink"])
    text(s, "One authority connects\nroadmap to operations.", 70, 136, 500, 96, 39, C["white"], True)
    text(s, "Product intent, engineering execution, program dependencies, and operational evidence become different views of the same content-identified system.", 72, 286, 460, 118, 19, C["fog"])
    for i, label in enumerate(["ROADMAP", "ENGINEERING", "RELEASE", "OPERATIONS"]):
        pill(s, label, 620 + i * 150, 620, 132, C["green2"] if i == 3 else C["panel"], C["white"])
    footer(s, 16, True)
    notes(s, [
        "\"Content-identified\" is literal: identities are content addresses, which is what lets these four views reference the same objects without a synchronization process.",
        "This is a consequence slide. Its mechanism was established on slides 8 and 9.",
    ])

    # 17 - modernization
    s = add_slide(C["ink"])
    add_image_cover(s, "legacy-transform")
    shape(s, "rect", 0, 0, 660, H, C["ink"])
    pill(s, "MODERNIZATION", 70, 50, 155, C["white"], C["ink"])
    text(s, "Existing systems become\nstarting knowledge.", 70, 124, 540, 96, 40, C["white"], True)
    steps = [
        ("INDEX", "inert mirror", "01"),
        ("DRAFT", "reviewable specs", "02"),
        ("QUALIFY", "human + evidence", "03"),
    ]
    for i, (a, b, num) in enumerate(steps):
        y = 278 + i * 88
        shape(s, "roundRect", 70, y, 520, 76, C["panel"], radius="rounded-lg")
        text(s, num, 88, y + 26, 44, 24, 15, C["orange"], True)
        text(s, a, 138, y + 16, 170, 24, 18, C["white"], True)
        text(s, b, 330, y + 20, 240, 36, 16, C["muted"], True)
    caption_bar(
        s,
        "Draft extraction is available. Authority stays source-baseline until current qualification evidence.",
        648,
        "#3A2A22",
        C["orange2"],
        True,
    )
    footer(s, 17, True)
    notes(s, [
        "Authority: docs/architecture/source-promotion.md and the source-qualification schema.",
        "The current qualification model carries a v2-required boundary. Draft generation is explicitly not automatic authority transfer.",
        "Do not claim live repository acquisition as an ordinary generation behavior. That has not become implemented and verified.",
    ])

    # 18 - litai CLI
    s = add_slide(C["fog"])
    title(s, "litai exposes the lifecycle without replacing native tools.", "The CLI owns specification, deterministic connector, target, package-plan, and release authority; native ecosystems perform their own work.", False, 18)
    stages = [
        ("AUTHOR", "init · design · spec · lock", C["orange"]),
        ("RESOLVE", "verify · plan · catalog · skills", C["blue"]),
        ("REALIZE", "generate · build · test · run", C["green2"]),
        ("OPERATE", "config · work · document\npackage · release", C["orange2"]),
    ]
    for i, (a, b, color) in enumerate(stages):
        x = 70 + i * 286
        shape(s, "roundRect", x, 328, 262, 164, C["white"], radius="rounded-xl", stroke=color, strokeWidth=3)
        text(s, a, x + 20, 350, 222, 28, 18, color, True, align="center")
        line(s, x + 72, 394, 118, 4, color)
        mono(s, b, x + 22, 422, 218, 48, 13, C["ink"], True)
    shape(s, "roundRect", 144, 536, 992, 82, C["ink"], radius="rounded-xl")
    mono(s, "litai package plan  →  build --allow-host-execution  →  verify  →  release", 174, 554, 932, 26, 16, C["orange2"], True)
    text(s, "One accepted closure  •  multiple compatible native formats  •  publication remains separately authorized", 174, 586, 932, 22, 14, C["fog"], False, align="center")
    footer(s, 18)
    notes(s, [
        "Current litai help includes deterministic design, config, work, document, skills, and prompt surfaces alongside release, package, lifecycle, worker, graph, and source-to-specification commands. Diagnostic and connector verbs do not authorize a release.",
        "Package plan is read-only. Package build reruns the accepted Standard lifecycle before constructing selected pip/Conan bytes beneath OBJ_DIR; package verify independently reopens them. None of these verbs publishes.",
        "Host configuration paths and 0.8.x migration are centralized behind litai config. make install reads a tuple-specific native prerequisite SBOM; make uninstall removes only manifest-owned Literate AI paths.",
        "Native build and package managers remain delegated execution engines, never specification authority.",
    ])

    # 19 - RELEASE ENGINEERING 1/4 - checkpointed resume
    s = add_slide(C["white"])
    title(s, "Release checks resume from where they stopped, not from zero.", "A checkpoint is keyed by content, not by intent to skip work — so a resumed pass is always safe to trust, and is never silently promoted to a full pass.", False, 19, "RELEASE ENGINEERING · 1/4")
    gates = ["layout", "python", "lint", "format", "openspec", "doc-check", "driver", "skills", "e2e", "wheel", "install", "proj-e2e", "samples", "receipt"]
    gate_w = 76
    start_x = 70
    y0 = 316
    for i, g in enumerate(gates):
        x = start_x + i * (gate_w + 5)
        state = "done" if i < 6 else ("failed" if i == 6 else "pending")
        fill = C["green2"] if state == "done" else (C["red"] if state == "failed" else C["fog"])
        stroke = "none" if state != "pending" else C["line"]
        shape(s, "roundRect", x, y0, gate_w, 44, fill, radius="rounded-lg", stroke=stroke, strokeWidth=1)
        text(s, g, x, y0 + 15, gate_w, 20, 8, C["ink"] if state != "pending" else C["steel"], True, align="center")
    text(s, "FIRST RUN — stops at driver-review, exit status names an interrupted run", 70, 368, 900, 20, 13, C["red"], True)
    arrow(s, 200, 412, 300, C["orange"])
    text(s, "resume", 210, 392, 100, 18, 12, C["orange"], True)
    for i, g in enumerate(gates):
        x = start_x + i * (gate_w + 5)
        state = "skipped" if i < 6 else "done"
        fill = C["panel"] if state == "skipped" else C["green2"]
        shape(s, "roundRect", x, 432, gate_w, 44, fill, radius="rounded-lg")
        text(s, g, x, 432 + 15, gate_w, 20, 8, C["muted"] if state == "skipped" else C["ink"], True, align="center")
    text(s, "SECOND RUN — the first six gates are not rerun; only driver-review onward executes", 70, 484, 900, 20, 13, C["green"], True)
    shape(s, "roundRect", 70, 528, 1140, 96, C["code"], radius="rounded-lg")
    mono(s, "python-check: content-hash per test module — always safe to trust a resume\nrelease-check: gate-name list only, no content fingerprint — reset explicitly\n                before re-validating a checkout against a different commit", 92, 546, 1096, 64, 13, C["fog"])
    footer(s, 19)
    notes(s, [
        "Authority: scripts/run_checkpointed_unittests.py, src/literate_ai/test_checkpointing.py, and docs/user/troubleshooting.md's checkpoint sections.",
        "Gate names abbreviated for slide width; the real 14-gate list is repository-layout-check, python-check, lint, format-check, openspec-check, documentation-check, driver-review, skills-check, installed-e2e, wheel-check, install-check, installed-project-e2e, samples, test-receipt-current (see the Makefile's RELEASE_GATES). There is no codegraph-ready gate.",
        "A checkpointed run that resumed prior state deliberately reports a distinct exit status from a from-scratch clean pass and asks for one more full run before it counts as release evidence — a resumed pass is never silently promoted to the same evidentiary weight as a full one.",
    ])

    # 20 - RELEASE ENGINEERING 2/4 - Release Policy
    s = add_slide(C["ink"])
    title(s, "Release Policy", "Writable trunk. Exact-main release candidates. Locked release lines with named authority.", True, 20, "RELEASE ENGINEERING · 2/4")
    labeled_card(s, 70, 300, 330, 168, C["panel"], "FREE / PRE-RELEASE", "main stays writable\nPre-release targets major.minor\ngreen RC tags: exact main only", C["orange2"], C["fog"], 18, 13, stroke=C["orange"], strokeWidth=2)
    arrow(s, 400, 382, 470, C["orange"])
    labeled_card(s, 470, 300, 330, 168, C["panel"], "CUT + LOCKDOWN", "release/<major>.<minor>.x\nREADME Release Engineers merge\nand create/publish major.minor", C["blue"], C["fog"], 18, 13, stroke=C["blue"], strokeWidth=2)
    arrow(s, 800, 382, 870, C["orange"])
    labeled_card(s, 870, 300, 340, 168, C["panel"], "PATCH AUTHORITY", "strict: Release Engineers only\nloose: listed writer + reason\ntrunk-first break-glass", C["green2"], C["fog"], 18, 13, stroke=C["green2"], strokeWidth=2)
    shape(s, "roundRect", 70, 500, 1140, 82, C["code"], radius="rounded-lg")
    mono(s, "Literate-AI-Release: major.minor     or     Literate-AI-Release: none", 98, 522, 1084, 24, 15, C["orange2"], True)
    text(s, "LitAI enforces release commands. Forge protection should mirror policy; it is not implied or claimed live.", 98, 552, 1084, 20, 12, C["muted"], fit=True)
    caption_bar(
        s,
        "Contribution sweep + document-pair preflight bind every minor cut; markers protect branch cleanup.",
        648,
        C["code"],
        C["orange2"],
        True,
    )
    footer(s, 20, True)
    notes(s, [
        "Release Policy authority: docs/architecture/project-releases.md, repository_policy in literate.project.json, and README.md's exact Release Engineers section.",
        "Free and Pre-release both keep main writable for ordinary work. Pre-release binds one major.minor target; release rc accepts only exact green main. Cutting release/<major>.<minor>.x starts line lockdown.",
        "Only README Release Engineers merge release-line PRs or create/publish major or minor releases. Loose mode permits only configured writers to use reason-bearing break-glass for a commit already landed on main; strict mode does not.",
        "Every PR body has exactly one Literate-AI-Release: major.minor or Literate-AI-Release: none line. Missing, duplicate, malformed, stale, and other-line values remain unknown.",
        "LitAI enforces command authorization and state. Forge branch protection is separate and should mirror this policy; no live forge-protection claim is made here.",
        "Peer-work collection records exact merged or reason-bearing dead markers, inspects open PRs and every worktree, rejects stale/unmerged/protected/dirty state, plans by default, and requires --apply --authorize-delete.",
        "ADR 0034 adds a Python-owned contribution sweep and authenticated document-pair preflight for every major or minor cut. This slide does not claim v1.2.0 is already published.",
        "Gate target selection remains local worker fleet or GitHub Actions with the same declared gate; GitLab is recognized but fail-closed, and a single legacy-local VM remains complete when no fleet is configured.",
    ])

    # 21 - RELEASE ENGINEERING 3/4 - every step times itself
    s = add_slide(C["white"])
    title(s, "Every step times itself.", "Generation, build, test, and release each record one structured span — diagnostic, never authority.", False, 21, "RELEASE ENGINEERING · 3/4")
    stages21 = ["resolve", "generate", "build", "test", "run", "accept"]
    widths = [70, 190, 130, 150, 90, 160]
    x = 70
    for label, w in zip(stages21, widths, strict=True):
        shape(s, "roundRect", x, 316, w, 56, C["panel"], radius="rounded-lg")
        text(s, label, x, 336, w, 20, 12, C["white"], True, align="center")
        bar_w = max(10, w - 20)
        shape(s, "rect", x + 10, 388, bar_w, 10, C["orange"])
        x += w + 8
    text(s, "relative recorded duration per stage, one real run", 70, 410, 500, 20, 12, C["steel"])
    shape(s, "roundRect", 70, 440, 1140, 188, C["code"], radius="rounded-lg")
    text(s, "one structured JSON-Lines span per command", 92, 454, 500, 18, 11, C["orange2"], True)
    mono(s, '{"stage": "build", "target": {"kind": "local", "id": "worker-a"},\n "coding_cli": "claude", "model": "claude-sonnet-5",\n "start": "2026-08-19T18:41:02Z", "end": "2026-08-19T18:44:11Z",\n "duration_seconds": 189.4, "outcome": "pass"}', 92, 480, 1096, 132, 13, C["fog"])
    caption_bar(s, "Diagnostic, never authority: a read-only or missing build root never fails the command it observes.", 648)
    footer(s, 21)
    notes(s, [
        "Authority: src/literate_ai/perf.py and src/literate_ai/cli/perf.py.",
        "Every top-level litai command, and every worker dispatched during a --target local fan-out, records this span shape: stage, target kind and id, coding CLI, model, start/end timestamps, duration, and pass/fail outcome.",
        "litai perf show / litai perf chart --dir PATH can report on an archived copy of that telemetry directly, so it survives past the disposable build directory being cleaned.",
        "The field values shown are a representative shape, not a claim about any specific historical run's exact numbers.",
    ])

    # 22 - RELEASE ENGINEERING 4/4 - provenance pin
    s = add_slide(C["ink"])
    title(s, "Every release is pinned—and leaves an evidence trail.", "The exact source reviewed is the exact source shipped. The ledger indexes pointers; it does not authorize or accept the release.", True, 22, "RELEASE ENGINEERING · 4/4")
    shape(s, "roundRect", 70, 272, 552, 250, C["panel"], radius="rounded-lg", stroke=C["orange"], strokeWidth=2)
    text(s, "WHAT THE PIN PROTECTS", 92, 288, 500, 22, 15, C["orange"], True)
    text(s, "One content-addressed digest over the declared source-file closure. Any change invalidates it. Re-recording is an explicit one-line diff.", 92, 318, 508, 56, 14, C["fog"], fit=True)
    chip(s, "pinned   sha256:c36c33630e23…", 92, 384, 508, C["code"], C["green2"], 13)
    chip(s, "computed sha256:c36c33630e23…", 92, 426, 508, C["code"], C["green2"], 13)
    text(s, "match — current lifecycle driver TCB", 92, 472, 508, 22, 13, C["orange2"], True)
    shape(s, "roundRect", 658, 272, 552, 250, C["code"], radius="rounded-lg")
    text(s, "A REAL PIN CHECK", 680, 288, 500, 22, 13, C["orange2"], True)
    mono(s, "lifecycle driver : sample-host-conformance\nstate            : current\ndeclared TCB:\n    scripts/run_samples.py\n    src/literate_ai\n    tests/conformance/support/runtime_oracles.py\n    …", 680, 318, 508, 180, 12, C["fog"])
    # Evidence chain as chevrons, not a paragraph.
    stages22 = ["release", "gate", "host", "sample", "variant", "phase"]
    for i, label in enumerate(stages22):
        x = 70 + i * 190
        shape(s, "chevron", x, 540, 170, 44, C["orange"] if i == 0 else C["panel"])
        text(s, label, x + 12, 552, 140, 22, 14, C["ink"] if i == 0 else C["white"], True, align="center")
    caption_bar(s, "The documentation-authority marker applies the same fail-closed pattern: a stale marker blocks validation too.", 648, "#3A2A22", C["muted"], True)
    footer(s, 22, True)
    notes(s, [
        "Authority: src/literate_ai/adapters/project_lifecycle_driver.py (lifecycle_driver_implementation_identity, the TCB pin), scripts/review_lifecycle_driver.py, and docs/user/troubleshooting.md.",
        "The right-panel excerpt is a shortened rendering of the current scripts/review_lifecycle_driver.py pin-check output and declared implementation paths, generated from this repository rather than a hypothetical.",
        "The documentation-authority marker (docs/architecture/design-traceability.md) is the same fail-closed pattern applied to the documentation surface instead of the source closure.",
        "Evidence authority: src/literate_ai/evidence_ledger.py, src/literate_ai/project_releases.py, and src/literate_ai/step_harness.py. The ledger indexes and points; it does not authorize a build or accept a release.",
        "Failure custody retains failed sample scratch, installed workspaces, and diagnostics; successful steps still clean up. The sample scratch remains external to the checkout.",
    ])

    # 23 - the investment horizon
    s = add_slide(C["ink"])
    add_image_cover(s, "engineering-section")
    wash(s, 0, 0, 700, H, 0.58)
    title(s, "1.2 overlaps exact work and reuses only verified bytes.", "Bounded DAG scheduling, cache authority, Debian packages, native acceptance, and worktree placement advance together.", True, 23)
    work = [
        ("SCHEDULE", "Ready DAG nodes on bounded slots"),
        ("REUSE", "Rehash shared cache objects"),
        ("PACKAGE", "Construct + inspect real .deb"),
        ("ACCEPT", "Native CLI + gRPC evidence"),
    ]
    for i, (a, b) in enumerate(work):
        y = 272 + i * 80
        line(s, 70, y, 8, 64, C["green2"] if i == 0 else C["orange"])
        text(s, a, 96, y, 480, 26, 18, C["white"], True)
        text(s, b, 96, y + 28, 480, 28, 15, C["fog"])
    caption_bar(s, "1.2 candidate slices; production dispatch, LAN proof, and remote package custody remain open.", 648, C["code"], C["orange2"], True)
    footer(s, 23, True)
    notes(s, [
        "The implemented 1.1 native-custody and 1.0 operator/release boundaries remain. 1.2 adds dependency-aware bounded scheduling, cache authority, real local Debian construction, native CLI and gRPC acceptance, and canonical worktree placement.",
        "These are bounded implemented slices. Production command and SSH composition, measured real LAN warm-cache hits, remote native-package custody, complete native-CLI lifecycle proof, and audited legacy-worktree retirement remain follow-on work.",
    ])

    # 24 - end state
    s = add_slide(C["ink"])
    add_image_cover(s, "living-portfolio")
    wash(s, 0, 0, 575, H, 0.35)
    pill(s, "THE END STATE", 70, 50, 145, C["white"], C["ink"])
    text(s, "A living portfolio\nof intent.", 70, 136, 430, 104, 48, C["white"], True)
    text(s, "Shared capabilities evolve once. Products compose them independently. Every change carries its dependency path and proof forward.", 72, 294, 420, 118, 21, C["fog"])
    text(s, "Software becomes a regeneratable knowledge system.", 72, 506, 440, 64, 25, C["orange2"], True)
    footer(s, 24, True)
    notes(s, [
        "Explicitly the long-term destination, not near-term investment. Keep that distinction audible.",
        "\"Carries its dependency path forward\" is the blast-radius rule from slide 8 stated as an outcome.",
    ])

    # 25 - the ask
    s = add_slide(C["fog"])
    title(s, "Use the exact parallel path. Harden for the portfolio.", "Take one demanding downstream application through installed 1.2 scheduling, cache, package, and native acceptance, then invest in production operations.", False, 25)
    asks = [
        ("TWO WEEKS", "Containment + operational hardening", C["orange"]),
        ("ONE APPLICATION", "Installed adoption through retained", C["blue"]),
        ("CURRENT GATES", "No shortcuts around build, test, acceptance, or evidence", C["green2"]),
    ]
    for i, (a, b, color) in enumerate(asks):
        x = 72 + i * 390
        line(s, x, 300, 330, 8, color)
        text(s, a, x, 328, 330, 34, 22, C["ink"], True)
        text(s, b, x, 372, 330, 72, 17, C["steel"])
    shape(s, "roundRect", 240, 560, 800, 46, C["ink"], radius="rounded-full")
    text(s, "Decision: fund hardening and select one 1.2 production adopter.", 268, 574, 744, 22, 16, C["white"], True, align="center", fit=True)
    footer(s, 25)
    notes(s, [
        "This is the decision slide. The requested outcome is production-hardening investment plus selection of the first downstream 1.2 production adoption.",
        "The third column is not boilerplate: proving the loop while relaxing the gates would prove nothing, because acceptance is what makes the result a release candidate rather than a code dump.",
    ])

    # 26 - close
    s = add_slide(C["ink"])
    add_image_cover(s, "living-portfolio")
    wash(s, 0, 0, 720, H, 0.50)
    pill(s, "LITERATE-AI", 70, 48, 132, C["white"], C["ink"])
    text(s, "Build the system\nthat builds the software.", 70, 140, 620, 140, 44, C["white"], True, fit=True)
    line(s, 74, 300, 120, 8, C["orange"])
    text(s, "Durable intent. Renewable implementation.\nEvidence-backed applications. A portfolio that compounds.", 74, 328, 560, 80, 20, C["fog"])
    shape(s, "roundRect", 74, 548, 560, 54, C["orange"], radius="rounded-full")
    text(s, "ONE APPLICATION  →  A REGENERATABLE PORTFOLIO", 90, 564, 528, 24, 15, C["ink"], True, align="center", fit=True)
    text(s, "26", 1162, 698, 48, 16, 12, C["muted"], True, align="right")
    notes(s, [
        "Close on the decision from slide 25, not on the vision.",
        "For follow-up questions on mechanism, return to slides 7 through 11 and 19 through 22. The full authority is docs/architecture/component-execution-plans.md, production-containment-threat-model.md, and source-notes.md's release-engineering section.",
    ])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    p.save(str(OUT))
    return OUT


def _capability_manifest(pptx_path: Path) -> dict:
    return {
        "schema": "literate-ai/document-pair-manifest@1",
        "ecosystem": "google-workspace",
        "members": {
            "presentation": {
                "local_artifact": str(pptx_path),
                "published_location": None,
                "publication_authorized": False,
                "access": {
                    "audience": "organization",
                    "principals": [],
                    "permission": "view",
                    "link_sharing": "organization-restricted",
                },
            }
        },
        "authoring_package": {
            "root": str(SOURCE),
            "elements": {
                "narrative_specification": "narrative-specification.md",
                "factual_ledger": "source-notes.md",
                "generation_prompts": "prompts",
                "build_source": "build_deck.py",
                "assets": "assets",
                "regeneration_entry_point": "regenerate.sh",
                "deliverable_links": "current-deliverables.md",
                "qa_record": "qa-ledger.md",
            },
        },
    }


def main() -> None:
    out = build()
    slide_count = len(Presentation(str(out)).slides)
    print(f"built {slide_count} slides -> {out}")
    manifest_path = _obj_dir() / "literate-ai-manager-overview" / "capability-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(_capability_manifest(out), indent=2) + "\n", encoding="utf-8"
    )
    print(f"capability manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
