#!/usr/bin/env python3
"""Structural checks for the progressive, standalone lab curriculum."""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
labs = sorted(ROOT.glob("[0-9][0-9]-*"))
assert len(labs) == 6, f"expected six labs, found {len(labs)}"
media = set()
for expected, lab in enumerate(labs):
    data = json.loads((lab / "lab.json").read_text())
    assert int(data["level"]) == expected
    assert len(data["nodes"]) >= 4 and len(data["edges"]) >= 3
    assert (lab / "component.md").read_text().startswith("# ")
    page = (lab / "index.html").read_text()
    assert "<script src=" not in page and "<link " not in page and "http://" not in page and "https://" not in page
    assert "Component authority graph" in page and "Readable specification" in page
    assert not re.search(r"\{(?:title|css|runtime|main|config)\}", page)
    media.update(data["media"])
required = {"math", "physics", "audio", "music", "3d", "video", "html"}
assert required <= media, f"missing media coverage: {sorted(required-media)}"
print(f"verified {len(labs)} standalone labs across {', '.join(sorted(media))}")

