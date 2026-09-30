"""Reject tracked references that must not enter a public repository export."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DeniedReference:
    label: str
    pattern: re.Pattern[bytes]


DENIED_REFERENCES = (
    DeniedReference(
        "non-public NVIDIA-dev repository",
        re.compile(
            b"NVIDIA" + rb"-dev/(?!literate-ai(?:\.git)?(?:[^A-Za-z0-9_.-]|$))",
            re.I,
        ),
    ),
    DeniedReference(
        "NVIDIA internal forge or tracker host",
        re.compile(rb"(?:gitlab-master|jirasw)\.nvidia\.com", re.I),
    ),
    DeniedReference(
        "NVIDIA internal worker host",
        re.compile(rb"[A-Za-z0-9_.-]+\.hrd\.nvidia\.com", re.I),
    ),
    DeniedReference("NVIDIA employee address", re.compile(rb"@nvidia\.com", re.I)),
    DeniedReference(
        "organization-internal Jira issue",
        re.compile(rb"\bOM" + rb"PE-[0-9]+\b", re.I),
    ),
    DeniedReference(
        "organization-internal Slack channel",
        re.compile(rb"#c" + rb"dd-literate-ai\b", re.I),
    ),
    DeniedReference(
        "private downstream project name",
        re.compile(
            rb"(?:horde-"
            + rb"utilization|ov-"
            + rb"tokenomics|ov-web-dev-"
            + rb"mcp|OV"
            + rb"Studio)",
            re.I,
        ),
    ),
    DeniedReference(
        "organization-private model or MCP provider",
        re.compile(
            rb"(?:NVIDIA Ma" + rb"aS|nvidia-inference" + rb"/|switch" + rb"yard)",
            re.I,
        ),
    ),
)

_ARCHIVE_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})


def denied_labels(content: bytes) -> tuple[str, ...]:
    """Return stable labels for every denied reference found in *content*."""

    return tuple(
        item.label for item in DENIED_REFERENCES if item.pattern.search(content)
    )


def _tracked_paths(repository: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return tuple(
        repository / Path(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    )


def _members(path: Path) -> tuple[tuple[str, bytes], ...]:
    if path.suffix.casefold() not in _ARCHIVE_SUFFIXES:
        return ((path.name, path.read_bytes()),)
    with zipfile.ZipFile(path) as archive:
        return tuple(
            (member.filename, archive.read(member))
            for member in archive.infolist()
            if not member.is_dir()
        )


def audit(repository: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for path in _tracked_paths(repository):
        for member, content in _members(path):
            for label in denied_labels(content):
                relative = path.relative_to(repository).as_posix()
                location = relative if member == path.name else f"{relative}!{member}"
                findings.append(f"{location}: {label}")
    return tuple(sorted(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", nargs="?", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    repository = arguments.repository.resolve()
    findings = audit(repository)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    print("public-export audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
