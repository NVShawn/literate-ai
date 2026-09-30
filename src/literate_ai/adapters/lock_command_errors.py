"""Shared lock orchestration refusal; CLI presentation is a separate adapter."""

from __future__ import annotations


class LockCommandError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
