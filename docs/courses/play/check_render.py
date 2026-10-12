"""Refuse a rendered film whose scene clips or final media are empty or truncated.

python check_render.py RENDER_DIR/video-result.json
Complements `litai video verify` (integrity and captions) with decoded-frame
counts: every staged clip and the final MP4 must contain real picture and sound
for their stated durations.
"""

import json
import subprocess
import sys
from pathlib import Path


def decoded(path, stream):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_packets",
            "-select_streams",
            stream,
            "-show_entries",
            "stream=nb_read_packets:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)
    packets = int((data.get("streams") or [{}])[0].get("nb_read_packets") or 0)
    return packets, float(data.get("format", {}).get("duration") or 0)


def main():
    receipt = Path(sys.argv[1])
    result = json.loads(receipt.read_text())
    stage = receipt.parent
    films = [stage / name for name in result["artifacts"] if name.endswith(".mp4")]
    clips = sorted(stage.glob("s[0-9][0-9][0-9]-t*-c*.mp4"))
    problems = []
    for path in [*clips, *films]:
        frames, seconds = decoded(path, "v:0")
        audio, _ = decoded(path, "a:0")
        if frames < max(1, int(seconds * 24 * 0.95)) or audio == 0 or seconds < 0.5:
            problems.append(
                f"{path.name}: {frames} frames, {audio} audio packets, "
                f"{seconds:.2f}s, {path.stat().st_size} bytes"
            )
    total = sum(decoded(path, "v:0")[1] for path in clips)
    film = decoded(films[0], "v:0")[1]
    if clips and abs(total - film) > 1.0:
        problems.append(f"clip durations {total:.1f}s differ from film {film:.1f}s")
    if problems:
        print("\n".join(problems))
        raise SystemExit(f"refused: {len(problems)} problem(s)")
    print(
        json.dumps(
            {"clips": len(clips), "film_seconds": round(film, 2), "accepted": True}
        )
    )


if __name__ == "__main__":
    main()
