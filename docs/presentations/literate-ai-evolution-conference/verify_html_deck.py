#!/usr/bin/env python3
"""Verify structural and offline guarantees of the HTML5 conference deck."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK = HERE / "index.html"
REPORT = HERE / "html-validation.json"


class Inspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.slides = 0
        self.notes = 0
        self.images: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "section" and "slide" in classes:
            self.slides += 1
        if tag == "aside" and "speaker-note" in classes:
            self.notes += 1
        if tag == "img":
            self.images.append(values.get("src") or "")
        if values.get("id"):
            self.ids.add(values["id"] or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-review", choices=("pending", "pass"), default="pending")
    args = parser.parse_args()
    raw = DECK.read_bytes()
    text = raw.decode("utf-8")
    inspector = Inspector()
    inspector.feed(text)
    findings: list[str] = []
    if inspector.slides != 14:
        findings.append(f"expected 14 slides, found {inspector.slides}")
    if inspector.notes != 14:
        findings.append(f"expected 14 speaker notes, found {inspector.notes}")
    if len(inspector.images) != 4 or any(not src.startswith("data:image/png;base64,") for src in inspector.images):
        findings.append("all four image uses must be embedded PNG data URIs")
    for slide_id in (f"slide-{number}" for number in range(1, 15)):
        if slide_id not in inspector.ids:
            findings.append(f"missing {slide_id}")
    for required in ("Content-Security-Policy", "prefers-reduced-motion", "@media print", "requestFullscreen", "touchstart", "hashchange"):
        if required not in text:
            findings.append(f"missing required capability: {required}")
    scrubbed = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", text)
    if re.search(r"(?:src|href)=[\"'](?:https?:|//)", scrubbed, re.IGNORECASE):
        findings.append("external resource reference found")
    if "default-src 'none'" not in text:
        findings.append("restrictive offline CSP missing")
    report = {
        "schema": "literate-ai/html5-conference-deck-validation@1",
        "status": "pass" if not findings and args.browser_review == "pass" else "built",
        "slide_count": inspector.slides,
        "expected_slide_count": 14,
        "speaker_notes_count": inspector.notes,
        "embedded_image_count": len(inspector.images),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
        "browser_review": args.browser_review,
        "findings": findings,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if findings:
        raise SystemExit("\n".join(findings))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
