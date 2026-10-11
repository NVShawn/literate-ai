"""Original sound for the film: an ambient bed and UI cues synthesized by ffmpeg.

No third-party recordings or samples: every sound is generated from the formulas
below, so there is nothing to license. Narration stays dominant: the bed sits far
below speech, cues are brief, and each turn's mix is checked for loudness.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

RATE = 48000
# A slow A-minor-ninth drone; partial gains breathe at unrelated rates.
DRONE = "+".join(
    f"{gain}*sin(2*PI*{freq}*t)*(0.65+0.35*sin(2*PI*{lfo}*t+{phase}))"
    for gain, freq, lfo, phase in (
        (0.05, 110.0, 0.031, 0.0),
        (0.035, 164.81, 0.047, 1.3),
        (0.03, 220.0, 0.023, 2.1),
        (0.02, 261.63, 0.059, 0.7),
        (0.015, 493.88, 0.017, 2.9),
    )
)
CUES = {
    # Soft two-step tick for a terminal page reveal.
    "tick": "0.35*sin(2*PI*1320*t)*exp(-60*t)+0.25*sin(2*PI*1980*(t-0.06))"
    "*exp(-70*(t-0.06))*gt(t,0.06)",
    # Filtered-noise swell for a chapter card.
    "whoosh": None,
}


def ffmpeg(*argv: str) -> None:
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *argv], capture_output=True, text=True
    )
    if done.returncode:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.strip()[-800:]}")


def bed(out: Path, seconds: float) -> Path:
    """Render the whole-film bed once, normalized far below speech."""
    if out.exists():
        return out
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{DRONE}':s={RATE}:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color=pink:amplitude=0.05:d={seconds}:r={RATE}",
        "-filter_complex",
        "[1:a]lowpass=f=600,volume=0.4[n];[0:a][n]amix=inputs=2:normalize=0,"
        "aecho=0.8:0.7:180|340:0.25|0.18,lowpass=f=2400,"
        "loudnorm=I=-40:TP=-9:LRA=7,aformat=channel_layouts=mono",
        "-ar",
        str(RATE),
        str(out),
    )
    return out


def cue(kind: str, out: Path) -> Path:
    if out.exists():
        return out
    if kind == "whoosh":
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:amplitude=0.3:d=0.9:r={RATE}",
            "-af",
            "highpass=f=300,lowpass=f=3000,afade=in:d=0.55,"
            "afade=out:st=0.55:d=0.35,volume=0.25,aformat=channel_layouts=mono",
            str(out),
        )
    else:
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc='{CUES[kind]}':s={RATE}:d=0.3",
            "-af",
            "volume=0.2,aformat=channel_layouts=mono",
            str(out),
        )
    return out


def mix(
    speech: Path,
    out: Path,
    bed_file: Path,
    offset: float,
    seconds: float,
    cues: list[tuple[Path, float]],
) -> Path:
    """Speech over a bed slice that continues across turns, plus timed cues."""
    inputs = [
        "-i",
        str(speech),
        "-ss",
        f"{offset:.3f}",
        "-t",
        f"{seconds:.3f}",
        "-i",
        str(bed_file),
    ]
    chain = [
        "[0:a]aresample=48000,aformat=channel_layouts=mono[s]",
        f"[1:a]afade=in:d=0.06,afade=out:st={max(0.0, seconds - 0.06):.3f}:d=0.06[b]",
    ]
    labels = ["[s]", "[b]"]
    for index, (sound, at) in enumerate(cues, 2):
        inputs += ["-i", str(sound)]
        delay = int(at * 1000)
        chain.append(f"[{index}:a]adelay={delay}|{delay}[c{index}]")
        labels.append(f"[c{index}]")
    chain.append(
        "".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=first[m]"
    )
    ffmpeg(
        *inputs,
        "-filter_complex",
        ";".join(chain),
        "-map",
        "[m]",
        "-ar",
        str(RATE),
        str(out),
    )
    return out
