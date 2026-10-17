"""Inert, complete source authority for the optional Mix lifecycle."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.mix_projects import MixProjectIntent

from .types import DependencyObservationError


@dataclass(frozen=True)
class MixSourceAuthority:
    """Selected declarative intent and exact text inventory; no execution grant."""

    project: MixProjectIntent
    inventory_identity: ContentIdentity


def prepare_mix_source_authority(files: Mapping[str, object]) -> MixSourceAuthority:
    """Accept one canonical intent, without evaluating or acquiring anything."""
    inventory = []
    if any(not isinstance(name, str) for name in files):
        raise DependencyObservationError(
            "dependencies.mix-source-invalid", "Mix source paths must be strings"
        )
    for name, content in sorted(files.items()):
        if not isinstance(name, str) or not isinstance(content, str):
            raise DependencyObservationError(
                "dependencies.mix-source-invalid", "Mix source requires text files"
            )
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or path.as_posix() != name
            or ".." in path.parts
            or "\\" in name
            or not name.startswith("source/")
            or path.name.casefold() in {"mix.exs", "mix.lock", ".iex.exs"}
            or (
                path.name.casefold() == "mix-project.json"
                and name != "source/mix-project.json"
            )
        ):
            raise DependencyObservationError(
                "dependencies.mix-source-invalid",
                "Mix source contains an unselected or executable authority",
            )
        inventory.append(
            {"path": name, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        )
    content = files.get("source/mix-project.json")
    if not isinstance(content, str):
        raise DependencyObservationError(
            "dependencies.mix-source-invalid", "Mix source omits declarative intent"
        )
    try:
        project = MixProjectIntent.from_bytes(content.encode())
    except ValueError as exc:
        raise DependencyObservationError(
            "dependencies.mix-source-invalid", "Invalid declarative Mix intent"
        ) from exc
    return MixSourceAuthority(project, canonical_identity(inventory))
