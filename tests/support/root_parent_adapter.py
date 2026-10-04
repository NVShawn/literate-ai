"""Project initialization fixture independent of checkout release tags."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.contracts import RepositoryParentSelection


class RootParentProjectInitializationAdapter(FilesystemProjectInitializationAdapter):
    """Use explicit root authority unless a test selects another parent."""

    def initialize(self, target: Path, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("parent_selection", RepositoryParentSelection.root())
        return super().initialize(target, **kwargs)


def root_parent_for_fixture_project(arguments: tuple[str, ...]):
    """Keep throwaway CLI project fixtures independent of checkout history."""

    if (arguments and arguments[0] == "init") or (
        arguments[:2] == ("spec", "accept") and "--integrate-project" not in arguments
    ):
        return mock.patch(
            "literate_ai.adapters.project_initialization._default_repository_parent",
            return_value=RepositoryParentSelection.root(),
        )
    return nullcontext()
