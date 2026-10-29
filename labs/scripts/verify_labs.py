#!/usr/bin/env python3
"""Verify offline bundles and their exact, unabridged authored-source inventory."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from build_labs import ROOT, build_page, source_paths


class ConfigParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.collect = False
        self.chunks: list[str] = []
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "lab-config":
            self.collect = True
            self.count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.collect = False

    def handle_data(self, data: str) -> None:
        if self.collect:
            self.chunks.append(data)


def verify_lab(lab: Path, page: str | None = None) -> set[str]:
    data = json.loads((lab / "lab.json").read_text())
    page = (lab / "index.html").read_text() if page is None else page
    parser = ConfigParser()
    parser.feed(page)
    assert parser.count == 1, "expected one inert source/config manifest"
    config = json.loads("".join(parser.chunks))
    canonical = (lab / "project/literate.project.json").exists()
    if canonical:
        project = json.loads((lab / "project/literate.project.json").read_text())
        component = lab / "project" / project["repository_policy"]["default_component"]
        assert config["spec"] == (component / "component.md").read_text()
    else:
        assert config["spec"] == (lab / "component.md").read_bytes().decode("utf-8")
    expected_paths = [
        path.relative_to(ROOT.parent).as_posix() for path in source_paths(lab)
    ]
    assert [s["path"] for s in config["sources"]] == expected_paths, (
        "omitted or reordered source"
    )
    for source, path in zip(config["sources"], source_paths(lab), strict=True):
        raw = path.read_bytes()
        assert source["content"].encode("utf-8") == raw, f"abridged source: {path}"
        assert source["sha256"] == hashlib.sha256(raw).hexdigest(), (
            f"source digest drift: {path}"
        )
    if canonical:
        lock = json.loads((component / "component.lock.json").read_text())
        assert data["diagram_kind"] == "resolved-authority-graph"
        assert "nodes" not in data and "edges" not in data, (
            "do not hand-author a resolved graph"
        )
        assert config["canonical"] is True
        assert config["harness"]["project"] == project["project_id"]
        assert config["harness"]["component_count"] == len(lock["nodes"])
        selected = sorted(
            {
                s["value"]
                for n in lock["nodes"]
                for slot in n["target_flavor_selection"]["slots"]
                for s in slot["selected"]
            }
        )
        assert config["harness"]["selected_flavors"] == selected
        assert (
            config["harness"]["lock_sha256"]
            == hashlib.sha256(
                (component / "component.lock.json").read_bytes()
            ).hexdigest()
        )
        assert config["harness"]["framework_version"] == "1.3.1"
        assert "Resolved authority graph" in page
        assert "illustrative roles" not in page.split("<script>")[0]
        resources = page.replace('<link rel="icon" href="data:,">', "")
        assert "<script src=" not in resources and "<link " not in resources
        assert not re.search(
            r"<(?:script|link|img|iframe)[^>]+(?:src|href)=[\"\']https?://", page
        )
        assert "Readable lab specification" in page and "Exact bundled source" in page
        assert "Surrounding inputs &amp; harness" in page
        assert page == build_page(lab), "stale or modified canonical preview"
        return set(data["media"])
    assert data["diagram_kind"] == "illustrative-lesson-flow"
    project = json.loads((ROOT.parent / "literate.project.json").read_text())
    assert config["harness"]["project"] == project["project_id"]
    assert config["harness"]["selected_flavors"] == []
    assert config["harness"]["documentation_root"] is True
    assert config["harness"]["component_root"] is False
    assert "Surrounding inputs &amp; harness" in page
    assert config["nodes"] == data["nodes"] and config["edges"] == data["edges"]
    ids = [n["id"] for n in data["nodes"]]
    assert len(set(ids)) == len(ids)
    assert all(a in ids and b in ids for a, b in data["edges"])
    assert all(
        "Flavor" not in n["label"] and n["label"] != "Component" for n in data["nodes"]
    )
    assert "Lesson flow (illustrative)" in page
    assert "No resolved Component DAG is supplied." in page
    assert "Readable lab specification" in page and "Exact bundled source" in page
    # An empty data-URL favicon prevents the browser's implicit network request.
    # Every other linked resource remains forbidden for these standalone labs.
    resources = page.replace('<link rel="icon" href="data:,">', "")
    assert "<script src=" not in resources and "<link " not in resources
    assert not re.search(
        r"<(?:script|link|img|iframe)[^>]+(?:src|href)=[\"\']https?://", page
    )
    assert not re.search(
        r"\{(?:title|css|runtime|main|config)\}", page.split("<script>")[0]
    )
    assert page == build_page(lab), "stale or modified bundled executable/style/wrapper"
    return set(data["media"])


def main() -> None:
    labs = sorted(ROOT.glob("[0-9][0-9]-*"))
    assert len(labs) == 6
    media: set[str] = set()
    for expected, lab in enumerate(labs):
        assert int(json.loads((lab / "lab.json").read_text())["level"]) == expected
        media.update(verify_lab(lab))
    required = {"math", "physics", "audio", "music", "3d", "video", "html"}
    assert required <= media, f"missing media coverage: {sorted(required - media)}"
    print(
        f"verified {len(labs)} offline lab bundles, authority diagrams and exact source inventories"
    )


if __name__ == "__main__":
    main()
