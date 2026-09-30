"""Stable errors for the source-to-specification bounded context."""

from __future__ import annotations


class SourceToSpecificationError(ValueError):
    """A stable, machine-readable source-to-specification failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SourceMutationError(SourceToSpecificationError):
    """The analyzed source tree changed during a non-mutating workflow."""


__all__ = ["SourceMutationError", "SourceToSpecificationError"]
