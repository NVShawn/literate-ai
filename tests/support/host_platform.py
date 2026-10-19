"""Truthful native OS identities for platform-neutral test fixtures."""

from __future__ import annotations

import sys


def host_os_family(platform_name: str | None = None) -> str:
    selected = sys.platform if platform_name is None else platform_name
    if selected.startswith("freebsd"):
        return "freebsd"
    try:
        return {"linux": "linux", "darwin": "macos", "win32": "windows"}[selected]
    except KeyError as exc:
        raise ValueError(f"unsupported native fixture platform: {selected}") from exc
