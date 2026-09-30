"""Shared dependency-lifecycle error and observation result types."""

from __future__ import annotations

import re
from dataclasses import dataclass


class DependencyObservationError(RuntimeError):
    """The selected host cannot prove a complete dependency closure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HostDependencyObservation:
    components: tuple[dict[str, object], ...]
    edges: tuple[tuple[str, str], ...]


def _normalized_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.casefold())
