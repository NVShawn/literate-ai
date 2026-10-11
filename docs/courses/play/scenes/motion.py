"""ffmpeg motion for the film: Ken Burns stills, 3D clip timing, overlays, checks.

Every clip is 1280x720, 24 fps, yuv420p H.264 without audio; narration is attached
later by `litai video build`. `validate` refuses empty or short clips so a broken
encode can never ship as a silent gap.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

FPS = 24
ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


def ffmpeg(*argv: str) -> None:
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *argv], capture_output=True, text=True
    )
    if done.returncode:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.strip()[-800:]}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames,width,height:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    return {
        "frames": int(stream.get("nb_read_frames") or 0),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "bytes": int(data.get("format", {}).get("size") or 0),
    }


def validate(path: Path, seconds: float) -> dict:
    """Refuse clips with no picture, the wrong size, or too few frames."""
    facts = probe(path)
    needed = int(seconds * FPS * 0.95)
    if (
        facts["frames"] < max(needed, 1)
        or facts["bytes"] < 10_000
        or (facts["width"], facts["height"]) != (1280, 720)
    ):
        raise ValueError(f"Refusing broken clip {path.name}: {facts}, need {needed}")
    return facts


def still(
    image: Path, seconds: float, out: Path, zoom: float = 0.04, drift: str = "center"
) -> Path:
    """Slow Ken Burns push on a still; upscaled first to avoid zoompan jitter."""
    frames = max(1, round(seconds * FPS))
    x = {"center": "iw/2-(iw/zoom/2)", "left": "0", "right": "iw-iw/zoom"}[drift]
    ffmpeg(
        "-loop",
        "1",
        "-i",
        str(image),
        "-vf",
        "scale=3840:2160,"
        f"zoompan=z='1+{zoom}*on/{frames}':x='{x}':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s=1280x720:fps={FPS},format=yuv420p",
        "-frames:v",
        str(frames),
        *ENCODE,
        str(out),
    )
    return out


def sequence(frames_dir: Path, out: Path) -> Path:
    """Encode a Blender PNG sequence (0001.png ...) once."""
    ffmpeg(
        "-framerate", str(FPS), "-i", str(frames_dir / "%04d.png"), *ENCODE, str(out)
    )
    return out


def fit(clip: Path, seconds: float, out: Path) -> Path:
    """Cover `seconds` with a 3D clip, ping-ponging rather than jump-looping."""
    frames = max(1, round(seconds * FPS))
    ffmpeg(
        "-i",
        str(clip),
        "-filter_complex",
        "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[pp];"
        f"[pp]loop=loop=-1:size=32767,trim=end_frame={frames},setpts=N/{FPS}/TB",
        "-frames:v",
        str(frames),
        *ENCODE,
        str(out),
    )
    return out


def join(clips: list[Path], out: Path) -> Path:
    if len(clips) == 1:
        ffmpeg("-i", str(clips[0]), "-c", "copy", str(out))
        return out
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clips))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), *ENCODE, str(out))
    listing.unlink()
    return out


def dress(
    clip: Path,
    overlays: list[tuple[Path, int, int]],
    seconds: float,
    out: Path,
    fade: bool,
) -> Path:
    """Composite still overlays (tag, close-up, caption) over moving picture."""
    inputs, chain, last = ["-i", str(clip)], [], "[0:v]"
    for index, (image, x, y) in enumerate(overlays, 1):
        inputs += ["-i", str(image)]
        chain.append(f"{last}[{index}:v]overlay={x}:{y}[v{index}]")
        last = f"[v{index}]"
    tail = ",fade=in:st=0:d=0.35" if fade else ""
    chain.append(f"{last}format=yuv420p{tail}[out]")
    ffmpeg(
        *inputs,
        "-filter_complex",
        ";".join(chain),
        "-map",
        "[out]",
        "-frames:v",
        str(max(1, round(seconds * FPS))),
        *ENCODE,
        str(out),
    )
    return out
