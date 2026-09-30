#!/usr/bin/env python3
"""Independent acceptance oracle for the ``literate-ai.document-pair`` capability.

This executes the scenarios in ``components/document-pair/acceptance/document-pair.md``
against a realized pair. It reads only the capability manifest, the realized artifacts,
the authoring package, and the consuming Component's declaration. It never consults the
build program that produced the artifact, and it imports nothing from the framework, so
a defect in either cannot make this oracle agree with it.

Usage:
    verify_document_pair.py --manifest <path> --component <component.md> [--json <out>]

Exits 0 when every scenario passes, 1 when any fails, 2 on unusable input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import NoReturn
from xml.etree import ElementTree

MANIFEST_SCHEMA = "literate-ai/document-pair-manifest@1"
MEMBERS = ("narrative", "presentation")
AUDIENCES = ("private", "named-principals", "organization", "public")
PERMISSIONS = ("view", "comment", "edit")
PACKAGE_ELEMENTS = (
    "narrative_specification",
    "factual_ledger",
    "generation_prompts",
    "build_source",
    "assets",
    "regeneration_entry_point",
    "deliverable_links",
    "qa_record",
)
EMU_PER_PX = 9525
OVERLAP_EPSILON_PX = 4

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

CREDENTIAL_PATTERNS = (
    ("google-oauth-token", re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer-header", re.compile(r"[Aa]uthorization:\s*Bearer\s+\S{20,}")),
)
PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bTBD\b"),
    re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE),
    re.compile(r"\bLorem ipsum\b", re.IGNORECASE),
)


class Report:
    """Ordered scenario results. A scenario may pass, fail, or be inapplicable."""

    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def record(
        self, requirement: str, scenario: str, state: str, detail: str = ""
    ) -> None:
        self.results.append(
            {
                "requirement": requirement,
                "scenario": scenario,
                "state": state,
                "detail": detail,
            }
        )

    def ok(self, requirement: str, scenario: str, detail: str = "") -> None:
        self.record(requirement, scenario, "pass", detail)

    def fail(self, requirement: str, scenario: str, detail: str) -> None:
        self.record(requirement, scenario, "fail", detail)

    def skip(self, requirement: str, scenario: str, detail: str) -> None:
        self.record(requirement, scenario, "not-applicable", detail)

    @property
    def failed(self) -> list[dict[str, object]]:
        return [r for r in self.results if r["state"] == "fail"]

    def to_dict(self) -> dict[str, object]:
        counts = {
            state: sum(1 for r in self.results if r["state"] == state)
            for state in ("pass", "fail", "not-applicable")
        }
        return {
            "schema": "literate-ai/document-pair-acceptance@1",
            "contract": "components/document-pair/acceptance/document-pair.md",
            "accepted": not self.failed,
            "counts": counts,
            "scenarios": self.results,
        }


def fatal(message: str) -> NoReturn:
    print(json.dumps({"error": message}), file=sys.stderr)
    raise SystemExit(2)


# --- the consuming Component's declaration -----------------------------------------


def parse_declaration(component: Path) -> dict[str, object]:
    """Read the declared realization from the consuming Component's Markdown table."""

    text = component.read_text(encoding="utf-8")
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] and not set(cells[0]) <= {"-", " "}:
            rows[cells[0].casefold()] = cells[1]

    def cell(name: str) -> str:
        return rows.get(name.casefold(), "")

    def bare(value: str) -> str:
        return value.replace("`", "").strip()

    declared_members = [m for m in MEMBERS if m in cell("Members declared")]
    surface = None
    match = re.search(r"(\d+)\s*[x×]\s*(\d+)", cell("Surface geometry"))
    if match:
        surface = (int(match.group(1)), int(match.group(2)))
    link = bare(cell("Link sharing")).casefold()
    return {
        "members": declared_members,
        "surface": surface,
        "audience": bare(cell("Access audience")).casefold(),
        "permission": bare(cell("Permission")).casefold(),
        "link_sharing_enabled": link not in ("", "disabled", "false", "none"),
        "link_sharing": link,
    }


# --- artifact readers ---------------------------------------------------------------


def _xfrm_box(frame: ElementTree.Element) -> tuple[float, float, float, float] | None:
    off, ext = frame.find(f"{_A}off"), frame.find(f"{_A}ext")
    if off is None or ext is None:
        return None
    try:
        left = int(off.attrib["x"]) / EMU_PER_PX
        top = int(off.attrib["y"]) / EMU_PER_PX
        right = left + int(ext.attrib["cx"]) / EMU_PER_PX
        bottom = top + int(ext.attrib["cy"]) / EMU_PER_PX
    except (KeyError, TypeError, ValueError):
        return None
    return left, top, right, bottom


def _text_bearing_boxes(
    root: ElementTree.Element,
) -> list[tuple[str, float, float, float, float]]:
    boxes: list[tuple[str, float, float, float, float]] = []
    for shape in root.iter(f"{_P}sp"):
        text = "".join(node.text or "" for node in shape.iter(f"{_A}t")).strip()
        if not text:
            continue
        properties = shape.find(f"{_P}spPr")
        frame = None if properties is None else properties.find(f"{_A}xfrm")
        if frame is None:
            continue
        box = _xfrm_box(frame)
        if box is None:
            continue
        boxes.append((text.splitlines()[0][:40], *box))
    return boxes


def _overlap_hits(
    boxes: list[tuple[str, float, float, float, float]], page: int
) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    epsilon = OVERLAP_EPSILON_PX
    for index, (label, left, top, right, bottom) in enumerate(boxes):
        for other, other_left, other_top, other_right, other_bottom in boxes[
            index + 1 :
        ]:
            if (
                left < other_right - epsilon
                and other_left < right - epsilon
                and top < other_bottom - epsilon
                and other_top < bottom - epsilon
            ):
                hits.append({"page": page, "a": label, "b": other})
    return hits


def _slide_key(name: str) -> int:
    match = re.search(r"(\d+)", name.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else 0


def read_presentation(path: Path) -> dict[str, object]:
    """Read pages, notes, geometry, and text straight out of the OOXML package."""

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        slides = sorted(
            (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=_slide_key,
        )
        notes = sorted(
            (
                n
                for n in names
                if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)
            ),
            key=_slide_key,
        )

        surface = None
        if "ppt/presentation.xml" in names:
            root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
            size = root.find(f"{_P}sldSz")
            if size is not None:
                surface = (
                    round(int(size.attrib["cx"]) / EMU_PER_PX),
                    round(int(size.attrib["cy"]) / EMU_PER_PX),
                )

        def text_of(entry: str) -> str:
            root = ElementTree.fromstring(archive.read(entry))
            return "".join(node.text or "" for node in root.iter(f"{_A}t"))

        escapes: list[dict[str, object]] = []
        overlaps: list[dict[str, object]] = []
        if surface:
            width, height = surface
            for index, entry in enumerate(slides, start=1):
                root = ElementTree.fromstring(archive.read(entry))
                for frame in root.iter(f"{_A}xfrm"):
                    box = _xfrm_box(frame)
                    if box is None:
                        continue
                    left, top, right, bottom = box
                    if (
                        left < -0.5
                        or top < -0.5
                        or right > width + 0.5
                        or bottom > height + 0.5
                    ):
                        escapes.append(
                            {
                                "page": index,
                                "bbox": [
                                    round(left),
                                    round(top),
                                    round(right),
                                    round(bottom),
                                ],
                            }
                        )
                overlaps.extend(_overlap_hits(_text_bearing_boxes(root), index))
        else:
            for index, entry in enumerate(slides, start=1):
                root = ElementTree.fromstring(archive.read(entry))
                overlaps.extend(_overlap_hits(_text_bearing_boxes(root), index))

        notes_text = []
        for entry in notes:
            value = text_of(entry)
            # A notes page carries the slide-number placeholder; ignore a bare number.
            notes_text.append(re.sub(r"^\s*\d+\s*$", "", value.strip()).strip())

        return {
            "pages": len(slides),
            "notes_pages": len(notes),
            "notes_text": notes_text,
            "surface": surface,
            "escapes": escapes,
            "overlaps": overlaps,
            "text": " ".join(text_of(entry) for entry in slides),
        }


def read_narrative(path: Path) -> dict[str, object]:
    """Read heading levels and text from a Word package."""

    with zipfile.ZipFile(path) as archive:
        if "word/document.xml" not in archive.namelist():
            return {"levels": [], "text": ""}
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        levels: list[int] = []
        text: list[str] = []
        for para in root.iter(f"{w}p"):
            style = para.find(f"{w}pPr/{w}pStyle")
            if style is not None:
                value = style.attrib.get(f"{w}val", "")
                match = re.fullmatch(r"[Hh]eading\s*(\d)", value)
                if match:
                    levels.append(int(match.group(1)))
            text.extend(node.text or "" for node in para.iter(f"{w}t"))
        return {"levels": levels, "text": " ".join(text)}


# --- scenarios ----------------------------------------------------------------------


def check_manifest(report: Report, manifest: dict, root: Path) -> dict[str, dict]:
    req = "The manifest resolves"
    if manifest.get("schema") != MANIFEST_SCHEMA:
        report.fail(req, "Manifest is complete and well-formed", "unexpected schema")
        return {}
    ecosystem = manifest.get("ecosystem")
    members = {
        name: body
        for name, body in (manifest.get("members") or {}).items()
        if name in MEMBERS and isinstance(body, dict)
    }
    problems = []
    if not ecosystem:
        problems.append("no ecosystem named")
    if not members:
        problems.append("no member realized")
    for name, body in members.items():
        artifact = body.get("local_artifact")
        if not artifact:
            problems.append(f"{name}: no local_artifact")
            continue
        path = Path(artifact)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            problems.append(f"{name}: local_artifact missing at {path}")
    if problems:
        report.fail(req, "Manifest is complete and well-formed", "; ".join(problems))
    else:
        report.ok(
            req,
            "Manifest is complete and well-formed",
            f"ecosystem={ecosystem}, members={sorted(members)}",
        )
    return members


def check_credentials(report: Report, manifest_text: str, package: Path) -> None:
    req = "The manifest resolves"
    hits = []
    for label, pattern in CREDENTIAL_PATTERNS:
        if pattern.search(manifest_text):
            hits.append(f"manifest:{label}")
    for path in sorted(package.rglob("*")):
        if not path.is_file() or path.suffix.casefold() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".pptx",
            ".docx",
            ".pdf",
        }:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in CREDENTIAL_PATTERNS:
            if pattern.search(body):
                hits.append(f"{path.name}:{label}")
    if hits:
        report.fail(req, "No credential material is present", "; ".join(sorted(hits)))
    else:
        report.ok(
            req,
            "No credential material is present",
            "manifest and package scanned",
        )


def check_access(
    report: Report, members: dict[str, dict], declared: dict[str, object]
) -> None:
    req = "Access matches the declared audience"
    declared_audience = str(declared["audience"])
    if declared_audience not in AUDIENCES:
        report.fail(
            req, "Audience is not widened", "declaration names no valid audience"
        )
        return
    ceiling = AUDIENCES.index(declared_audience)
    widened, invalid = [], []
    for name, body in members.items():
        access = body.get("access") or {}
        audience = str(access.get("audience", ""))
        if audience not in AUDIENCES:
            invalid.append(f"{name}: audience {audience!r}")
            continue
        if AUDIENCES.index(audience) > ceiling:
            widened.append(f"{name}: {audience} exceeds declared {declared_audience}")
        permission = str(access.get("permission", ""))
        if permission not in PERMISSIONS:
            invalid.append(f"{name}: permission {permission!r}")
    if widened or invalid:
        report.fail(req, "Audience is not widened", "; ".join(widened + invalid))
    else:
        report.ok(
            req,
            "Audience is not widened",
            f"realized at or below declared {declared_audience}",
        )

    unauthorized = [
        f"{name}: published to {body.get('published_location')}"
        for name, body in members.items()
        if not body.get("publication_authorized") and body.get("published_location")
    ]
    if unauthorized:
        report.fail(
            req, "Unauthorized publication does not occur", "; ".join(unauthorized)
        )
    else:
        report.ok(
            req,
            "Unauthorized publication does not occur",
            "no member published without recorded authorization",
        )


def check_presentation(
    report: Report, body: dict, artifact: Path, declared: dict[str, object]
) -> str:
    req = "The presentation member satisfies the geometry contract"
    facts = read_presentation(artifact)
    surface = facts["surface"]
    declared_surface = declared.get("surface")
    if declared_surface and surface and tuple(surface) != tuple(declared_surface):
        report.fail(
            req,
            "No element escapes the surface",
            f"surface {surface} does not match declared {tuple(declared_surface)}",
        )
    elif facts["escapes"]:
        sample = "; ".join(
            f"page {e['page']} bbox {e['bbox']}" for e in facts["escapes"][:4]
        )
        report.fail(
            req,
            "No element escapes the surface",
            f"{len(facts['escapes'])} element(s) outside {surface}: {sample}",
        )
    else:
        report.ok(
            req,
            "No element escapes the surface",
            f"{facts['pages']} pages at {surface}, 0 escaping elements",
        )

    overlap_hits = facts["overlaps"]
    if overlap_hits:
        sample = "; ".join(
            f"page {hit['page']} {hit['a']!r} overlaps {hit['b']!r}"
            for hit in overlap_hits[:4]
        )
        report.fail(
            req,
            "Text-bearing frames do not overlap",
            f"{len(overlap_hits)} overlap(s): {sample}",
        )
    else:
        report.ok(
            req,
            "Text-bearing frames do not overlap",
            f"{facts['pages']} pages, 0 overlapping text-bearing frames",
        )

    notes_text = facts["notes_text"]
    empty = sum(1 for value in notes_text if not value)
    if facts["notes_pages"] != facts["pages"] or empty:
        report.fail(
            req,
            "Every page carries notes",
            f"{facts['notes_pages']} notes pages for {facts['pages']} pages, "
            f"{empty} empty",
        )
    else:
        report.ok(
            req,
            "Every page carries notes",
            f"{facts['notes_pages']}/{facts['pages']} pages carry non-empty notes",
        )
    return str(facts["text"])


def check_narrative(report: Report, artifact: Path) -> str:
    req = "The narrative member satisfies the structure contract"
    facts = read_narrative(artifact)
    levels = facts["levels"]
    skips = [
        f"{previous}->{current}"
        for previous, current in zip(levels, levels[1:], strict=False)
        if current > previous + 1
    ]
    if skips:
        report.fail(req, "Heading levels do not skip", "; ".join(skips[:5]))
    else:
        report.ok(
            req,
            "Heading levels do not skip",
            f"{len(levels)} headings, no skipped level",
        )
    return str(facts["text"])


def check_package(report: Report, manifest: dict, root: Path) -> Path:
    req = "The authoring package reproduces the artifact"
    package = manifest.get("authoring_package") or {}
    package_root = Path(str(package.get("root", "")))
    if not package_root.is_absolute():
        package_root = root / package_root
    elements = package.get("elements") or {}
    missing = []
    for name in PACKAGE_ELEMENTS:
        value = elements.get(name)
        if not value:
            missing.append(f"{name}: not declared")
            continue
        path = package_root / value
        if not path.exists():
            missing.append(f"{name}: {value} missing")
    if missing:
        report.fail(
            req, "Every required package element is present", "; ".join(missing)
        )
    else:
        report.ok(
            req,
            "Every required package element is present",
            f"{len(PACKAGE_ELEMENTS)} elements under {package_root.name}",
        )
    return package_root


def check_placeholders(report: Report, text: str) -> None:
    req = "The authoring package reproduces the artifact"
    hits = []
    for pattern in PLACEHOLDER_PATTERNS:
        hits.extend(pattern.findall(text))
    if hits:
        report.fail(
            req,
            "No unresolved placeholder ships",
            f"{len(hits)} placeholder token(s): {sorted(set(hits))[:5]}",
        )
    else:
        report.ok(req, "No unresolved placeholder ships", "member text scanned")


def verify_document_pair(
    manifest_path: Path, component_path: Path
) -> dict[str, object]:
    """Return the exact structural acceptance report without printing or writing."""

    if not manifest_path.is_file():
        raise ValueError(f"manifest not found: {manifest_path}")
    if not component_path.is_file():
        raise ValueError(f"component not found: {component_path}")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"manifest is not valid JSON: {error}") from error

    root = manifest_path.parent
    declared = parse_declaration(component_path)
    report = Report()

    members = check_manifest(report, manifest, root)
    check_credentials(report, manifest_text, check_package(report, manifest, root))
    check_access(report, members, declared)

    declared_members = declared.get("members") or []
    undeclared = sorted(set(members) - set(declared_members))
    if undeclared:
        report.fail(
            "The manifest resolves",
            "Realized members match the declaration",
            f"realized but not declared: {undeclared}",
        )
    else:
        report.ok(
            "The manifest resolves",
            "Realized members match the declaration",
            f"declared={declared_members}, realized={sorted(members)}",
        )

    text = ""
    for name in ("presentation", "narrative"):
        body = members.get(name)
        if body is None:
            report.skip(
                f"The {name} member satisfies its contract",
                "Member is realized",
                f"no {name} member is claimed",
            )
            continue
        artifact = Path(str(body.get("local_artifact")))
        if not artifact.is_absolute():
            artifact = root / artifact
        if not artifact.is_file():
            continue
        if name == "presentation":
            text += check_presentation(report, body, artifact, declared)
        else:
            text += check_narrative(report, artifact)
    check_placeholders(report, text)

    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--component", required=True, type=Path)
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the report on stdout"
    )
    args = parser.parse_args(argv)
    try:
        result = verify_document_pair(args.manifest, args.component)
    except (OSError, UnicodeError, ValueError) as error:
        fatal(str(error))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
