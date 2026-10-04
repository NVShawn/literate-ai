"""Stage only verified public media and referenced authoring assets, never raw captures.

Local filesystem operation only. Git publication remains a separate reviewed step.
"""

import argparse
import json
import shutil
from pathlib import Path

from literate_ai.video_courses import plan_course, verify_course


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    args = p.parse_args()
    verify_course(args.receipt, manifest=args.manifest)
    plan = plan_course(args.manifest)
    package, video = args.destination / "package", args.destination / "video"
    for name in ["course.json", *plan["assets"]]:
        source = args.manifest if name == "course.json" else args.manifest.parent / name
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    result = json.loads(args.receipt.read_text())
    video.mkdir(parents=True, exist_ok=True)
    for name in [*result["artifacts"], "video-result.json"]:
        shutil.copy2(args.receipt.parent / name, video / name)
    print(verify_course(video / "video-result.json", manifest=package / "course.json"))
    cues = (video / f"{plan['course_id']}.srt").read_text().split("\n\n")
    starts = {
        int(cue.splitlines()[0]): cue.splitlines()[1].split(" --> ")[0]
        for cue in cues
        if cue.strip()
    }
    production = json.loads((package / "production.json").read_text())
    lines = [
        "# Chapter navigation",
        "",
        "One continuous film; timestamps use its actual subtitle timeline.",
        "",
    ]
    for chapter in production["chapters"]:
        lines.append(f"- {starts[chapter['scene']].split(',')[0]} — {chapter['title']}")
    lines += [
        "",
        "See the [production notes](README.md) for evidence and limitations.",
        "",
    ]
    (args.destination / "CHAPTERS.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
