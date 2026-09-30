"""Capture explicitly requested demo commands; never run during video rendering.

Raw output stays in ignored build custody. Public excerpts are reviewed separately.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[0] == "--" else args.argv
    started = time.monotonic()
    result = subprocess.run(
        argv, cwd=args.cwd, capture_output=True, text=True, timeout=1800
    )
    record = {
        "label": args.label,
        "argv": argv,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "label": args.label,
                "exit_code": result.returncode,
                "elapsed_seconds": record["elapsed_seconds"],
                "capture": str(args.output),
            }
        )
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
