"""Read-only release queue projection; Markdown remains the status authority."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from literate_ai.contracts import canonical_identity
from literate_ai.documentation import _visible_markdown_lines

HEADING = re.compile(r"^### \[([ x~])\] ([A-Z][A-Z0-9-]{2,63}) — (.+)$", re.M)
SECTION_HEADING = re.compile(r"^ {0,3}#{1,3}(?:[ \t]+.*)?$", re.M)


def summarize(
    content: str, release: str, *, require_all_open_targeted: bool = False
) -> dict[str, object]:
    if re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", release) is None:
        raise ValueError("release must be a major.minor or major.minor.patch version")
    line = ".".join(release.split(".")[:2])
    target = re.compile(
        r"^- \*\*Release target:\*\* " + re.escape(line) + r"(?:\.\d+)?(?=\D|$)", re.M
    )
    # Use visible Markdown only to locate section boundaries; retain the original
    # titles and acceptance text (including inline code) in the report.
    content = "\n".join(content.splitlines())
    visible = "\n".join(_visible_markdown_lines(content))
    headings = list(SECTION_HEADING.finditer(visible))
    items = []
    seen = set()
    for index, boundary in enumerate(headings):
        heading = HEADING.match(content, boundary.start())
        if heading is None:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        section = content[heading.end() : end]
        state, work_id, title = heading.groups()
        remaining = re.findall(r"^\s+- \[[ ~]\] (.+)$", section, re.M)
        if target.search(section) is None:
            if require_all_open_targeted and (state != "x" or remaining):
                raise ValueError(
                    f"unresolved item is not targeted to {line}: {work_id}"
                )
            continue
        if work_id in seen:
            raise ValueError(f"duplicate release work item: {work_id}")
        seen.add(work_id)
        if state == "x" and remaining:
            raise ValueError(f"closed item has unchecked acceptance: {work_id}")
        items.append(
            {
                "id": work_id,
                "title": title,
                "closed": state == "x",
                "remaining": remaining,
            }
        )
    value = {
        "schema": "literate-ai/release-roadmap-summary@1",
        "release_line": line,
        "items": items,
        "ready": bool(items) and all(item["closed"] for item in items),
    }
    value["identity"] = canonical_identity(value).uri
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release")
    parser.add_argument(
        "--roadmap", type=Path, default=Path("docs/roadmap/active-work.md")
    )
    parser.add_argument(
        "--require-all-open-targeted",
        action="store_true",
        help="fail if any unresolved queue item is outside this release line",
    )
    args = parser.parse_args()
    try:
        report = summarize(
            args.roadmap.read_text(encoding="utf-8"),
            args.release,
            require_all_open_targeted=args.require_all_open_targeted,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
