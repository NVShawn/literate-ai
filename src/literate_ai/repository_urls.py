"""Conservative semantic comparison for repository transport URLs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_GITHUB_HOST = "github.com"
_GITHUB_SCP = re.compile(r"^git@(?P<host>[^/:]+):(?P<path>[^?#]+)$")
_GITHUB_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _github_repository_coordinate(value: str) -> tuple[str, str, str] | None:
    """Return one GitHub owner/repository coordinate for supported URL spellings."""

    raw = value.strip()
    if raw != value:
        return None
    scp = _GITHUB_SCP.fullmatch(raw)
    if scp is not None:
        host = scp.group("host").casefold()
        path = scp.group("path")
    else:
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold()
        if parsed.query or parsed.fragment or parsed.password is not None:
            return None
        if parsed.scheme == "https":
            if parsed.username is not None or port not in {None, 443}:
                return None
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or port not in {None, 22}:
                return None
        else:
            return None
        if not parsed.path.startswith("/"):
            return None
        path = parsed.path[1:]
    if host != _GITHUB_HOST or not path or path.startswith("/"):
        return None
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if (
        len(parts) != 2
        or any(not part or part in {".", ".."} for part in parts)
        or any(_GITHUB_SEGMENT.fullmatch(part) is None for part in parts)
    ):
        return None
    return host, parts[0], parts[1]


def repository_urls_equivalent(first: str, second: str) -> bool:
    """Compare exact URLs or known GitHub SSH/HTTPS transport aliases.

    Exact equality retains support for other network and local origins. Only the
    well-known GitHub host is transport-normalized; different hosts, owners,
    repositories, credentials, non-default ports, and local/file origins remain
    distinct.
    """

    if first == second:
        return True
    first_coordinate = _github_repository_coordinate(first)
    return (
        first_coordinate is not None
        and first_coordinate == _github_repository_coordinate(second)
    )


def canonical_repository_origin(value: str) -> str:
    """Give known transport aliases one origin spelling without changing repositories.

    Other hosts, local paths, credentials and non-default ports remain exact.
    This is origin serialization, not authorization to use a transport.
    """

    coordinate = _github_repository_coordinate(value)
    if coordinate is None:
        return value
    host, owner, repository = coordinate
    return f"https://{host}/{owner}/{repository}"


def github_repository_coordinate(value: str) -> tuple[str, str] | None:
    """Return the owner/repository pair for one safe GitHub transport URL."""

    coordinate = _github_repository_coordinate(value)
    return None if coordinate is None else coordinate[1:]


__all__ = [
    "canonical_repository_origin",
    "github_repository_coordinate",
    "repository_urls_equivalent",
]
