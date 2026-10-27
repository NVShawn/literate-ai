#!/usr/bin/env python3
"""Check generated conference navigation, local assets, and the offline kit."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_build/conference-site/literate-ai"
BASE = "/literate-ai/"


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src", "poster"} and value:
                self.links.append(value)


def main():
    findings = []
    count = 0
    pages = list(SITE.rglob("*.html"))
    for page in pages:
        parser = Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for link in parser.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            location = unquote(parsed.path)
            if location.startswith(BASE):
                target = SITE / location[len(BASE) :]
            elif location.startswith("/"):
                findings.append(
                    f"{page.relative_to(SITE)}: outside site prefix: {link}"
                )
                continue
            else:
                target = page.parent / location
            if target.is_dir():
                target /= "index.html"
            count += 1
            if not target.is_file():
                findings.append(f"{page.relative_to(SITE)}: missing {link}")
    required = [
        "index.html",
        "docs/README.html",
        "docs/user/getting-started.html",
        "docs/user/html-repository-explorer.html",
        "docs/conference/presenter-guide.html",
        "docs/presentations/literate-ai-evolution-conference/index.html",
        "labs/00-grumpy-counter/index.html",
        "downloads/literate-ai-conference-kit.zip",
        "media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4",
        "media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt",
    ]
    for item in required:
        if not (SITE / item).is_file():
            findings.append(f"missing attendee artifact: {item}")
    with ZipFile(SITE / "downloads/literate-ai-conference-kit.zip") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            findings.append("offline kit contains duplicate paths")
        for item in [
            "START-HERE.txt",
            "slides.html",
            "slides.pptx",
            "onboarding.mp4",
            "onboarding.vtt",
            "onboarding.srt",
        ]:
            if item not in names:
                findings.append(f"offline kit missing {item}")
        if (
            len(
                [
                    name
                    for name in names
                    if name.startswith("labs/") and name.endswith("/index.html")
                ]
            )
            != 6
        ):
            findings.append("offline kit must contain six independent lab HTML files")
        bad = archive.testzip()
        if bad:
            findings.append(f"offline kit has corrupt member: {bad}")
    result = {
        "html_pages": len(pages),
        "local_links_checked": count,
        "findings": findings,
    }
    evidence = ROOT / "_build/conference-site-link-check.json"
    evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
