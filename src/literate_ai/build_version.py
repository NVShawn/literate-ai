"""Offline CLI build identity, separate from package/protocol version authority.

A source tag is not evidence that a release was published. Never infer the
framework's identity from the user's current project or contact a forge here.
"""

from __future__ import annotations

import json
import re
import subprocess
from importlib import metadata
from pathlib import Path

from literate_ai.version import DISTRIBUTION_VERSION

_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _revision(value: object) -> str | None:
    return value if isinstance(value, str) and _REVISION.fullmatch(value) else None


def _json_document(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "null")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def cli_version_label() -> str:
    """Report source snapshots honestly without changing compatibility pins."""

    package = Path(__file__).resolve().parent
    checkout = package.parent.parent
    revision = None
    kind = "publication unverified"
    if (checkout / ".git").exists() and (checkout / "pyproject.toml").is_file():
        kind = "development source"
        try:
            result = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                revision = _revision(result.stdout.strip())
            status = subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain=v1"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if status.returncode == 0 and status.stdout.strip():
                kind += "; dirty"
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    else:
        try:
            direct = _json_document(
                metadata.distribution("literate-ai").read_text("direct_url.json")
            )
        except (metadata.PackageNotFoundError, OSError, UnicodeError):
            direct = {}
        vcs = direct.get("vcs_info")
        if isinstance(vcs, dict) and vcs.get("vcs") == "git":
            kind = "development Git install; publication unverified"
            revision = _revision(vcs.get("commit_id"))
        # The backend embeds this before pip discards a temporary Git checkout.
        # A local wheel also retains its exact source commit, but that alone is
        # deliberately not interpreted as proof of a published release.
        if revision is None:
            try:
                origin = _json_document(
                    (package / "_distribution_origin.json").read_text("utf-8")
                )
            except (OSError, UnicodeError):
                origin = {}
            revision = _revision(origin.get("git_revision"))
    suffix = f"; git {revision}" if revision else ""
    return f"{DISTRIBUTION_VERSION} ({kind}{suffix})"
