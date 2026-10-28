"""Export guest diagnostics before the host applies the original verdict."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PATTERNS = (
    "*.json",
    "*.stdout",
    "*.stderr",
    "transport/result.json",
    "transport/qualification/*.json",
    "transport/qualification/*.stdout",
    "transport/qualification/*.stderr",
)


def collect(source: Path, destination: Path, guest_exit_code: int) -> None:
    if destination.is_symlink():
        raise ValueError("destination cannot be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    terminal = {
        "schema": "literate-ai/native-sdk-guest-terminal@1",
        "guest_exit_code": guest_exit_code,
        "collection_complete": False,
        "files": [],
    }
    try:
        if source.is_symlink():
            raise ValueError("source cannot be a symlink")
        selected = sorted(
            {path for pattern in PATTERNS for path in source.glob(pattern)}
        )
        for path in selected:
            relative = path.relative_to(source)
            current = source
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError(f"evidence cannot traverse a symlink: {relative}")
            if not path.is_file():
                continue
            target = destination / relative
            current = destination
            for part in relative.parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise ValueError("destination cannot traverse a symlink")
                current.mkdir(exist_ok=True)
            with path.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            content = target.read_bytes()
            terminal["files"].append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        terminal["collection_complete"] = True
    except BaseException as exc:
        terminal["collection_error"] = type(exc).__name__
        raise
    finally:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination, delete=False
        ) as stream:
            stream.write(json.dumps(terminal, indent=2) + "\n")
            temporary = stream.name
        os.replace(temporary, destination / "guest-terminal.json")


def require_passed(destination: Path) -> None:
    terminal = json.loads((destination / "guest-terminal.json").read_bytes())
    if (
        terminal.get("schema") != "literate-ai/native-sdk-guest-terminal@1"
        or terminal.get("collection_complete") is not True
        or type(terminal.get("guest_exit_code")) is not int
        or terminal["guest_exit_code"] != 0
    ):
        raise RuntimeError(
            f"guest qualification failed: exit {terminal.get('guest_exit_code')}; "
            "inspect retained diagnostics"
        )
    result = json.loads((destination / "result.json").read_bytes())
    if (
        result.get("schema") != "literate-ai/native-sdk-qualification@1"
        or result.get("passed") is not True
    ):
        raise RuntimeError("guest did not produce passing native SDK acceptance")
    for entry in terminal["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("unsafe retained diagnostic path")
        content = (destination / relative).read_bytes()
        if (
            len(content) != entry["bytes"]
            or hashlib.sha256(content).hexdigest() != entry["sha256"]
        ):
            raise RuntimeError("retained diagnostics changed during copyback")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--guest-exit-code", type=int)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        require_passed(args.destination)
    else:
        if args.source is None or args.guest_exit_code is None:
            parser.error("collection requires --source and --guest-exit-code")
        collect(args.source, args.destination, args.guest_exit_code)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
