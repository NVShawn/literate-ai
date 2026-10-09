"""Stage digest-pinned Bazelisk beneath an explicitly supplied object directory.

This contributor helper does not install or upgrade a host package. Qualification
uses the repository's .bazelversion and a private Bazelisk cache supplied by its
runner; the Standard adapter subsequently binds the downloaded Bazel executable.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import subprocess
import urllib.request
from pathlib import Path

_VERSION = "1.29.0"
_ASSETS = {
    ("Darwin", "arm64"): (
        "darwin-arm64",
        "cee851f726789227d5561004e9904a52be45c3efb56f8b38b6993d6adbaa0409",
    ),
    ("Darwin", "x86_64"): (
        "darwin-amd64",
        "16c3d7aa15323a9fb69f56c7ec5733ed18bedb786680d0ba13bb12a3c8083007",
    ),
    ("Linux", "x86_64"): (
        "linux-amd64",
        "5a408715e932c0250d28bd84555f12edbf70117de42f9181691c736eacc4a992",
    ),
    ("Linux", "aarch64"): (
        "linux-arm64",
        "e20e8b0f4f240091b7a55bf17b9398bd4f40ee70ae0208dff95dd4c445fb4010",
    ),
    ("Windows", "AMD64"): (
        "windows-amd64.exe",
        "092a8738d5b41aae7a85c42cc961b1034e3389aba43ffc20c0fabda7b43e095b",
    ),
}


def pinned_bazel_environment(repository: Path) -> dict[str, str]:
    """Bind the authored pin even when Bazelisk cannot locate a workspace."""
    pin = (repository / ".bazelversion").read_text(encoding="utf-8")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\n?", pin) is None:
        raise ValueError("Repository Bazel version must be one exact release")
    return {**os.environ, "USE_BAZEL_VERSION": pin.rstrip("\n")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    environment = pinned_bazel_environment(Path(__file__).resolve().parents[1])
    asset, expected = _ASSETS[(platform.system(), platform.machine())]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    executable = output / ("bazel.exe" if platform.system() == "Windows" else "bazel")
    if executable.exists():
        content = executable.read_bytes()
    else:
        url = f"https://github.com/bazelbuild/bazelisk/releases/download/v{_VERSION}/bazelisk-{asset}"
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read(64 * 1024 * 1024 + 1)
    if hashlib.sha256(content).hexdigest() != expected:
        raise SystemExit("Bazelisk artifact digest mismatch; refusing execution")
    executable.write_bytes(content)
    executable.chmod(0o755)
    subprocess.run(
        [str(executable), "--version"], env=environment, check=True, timeout=180
    )


if __name__ == "__main__":
    main()
