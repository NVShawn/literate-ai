"""JSON-only command adapter for external code-intelligence engines."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from literate_ai.adapters.source import CommandRunner, SubprocessCommandRunner
from literate_ai.storage import canonical_json_bytes

from .structured import IntelligenceAdapterError


class JsonCommandIntelligenceEngine:
    """Require JSON stdout; never scrape or regex human-oriented output."""

    def __init__(
        self,
        *,
        provider_id: str,
        provider_version: str,
        binary: str,
        runner: CommandRunner | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        if not provider_id or not provider_version or not binary:
            raise ValueError("command engine identity and binary must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("command engine timeout must be positive")
        self.provider_id = provider_id
        self.provider_version = provider_version
        self.binary = binary
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds

    def ensure_index(
        self,
        source_path: Path,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._run(
            source_path,
            (
                self.binary,
                "index",
                "--format=json",
                "--source",
                str(source_path),
                "--request-json",
                canonical_json_bytes(dict(request)).decode("utf-8"),
            ),
        )

    def query(
        self,
        engine_index_key: str,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._run(
            Path.cwd(),
            (
                self.binary,
                "query",
                "--format=json",
                "--index-key",
                engine_index_key,
                "--request-json",
                canonical_json_bytes(dict(request)).decode("utf-8"),
            ),
        )

    def _run(self, cwd: Path, args: tuple[str, ...]) -> Mapping[str, Any]:
        result = self.runner.run(
            args,
            cwd=cwd,
            timeout_seconds=self.timeout_seconds,
        )
        if result.args != args:
            raise IntelligenceAdapterError(
                "command runner returned mismatched arguments"
            )
        if result.returncode != 0:
            raise IntelligenceAdapterError(
                f"structured intelligence command failed: {result.stderr.strip()}"
            )
        try:
            value = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IntelligenceAdapterError(
                "intelligence command did not return valid JSON"
            ) from exc
        if not isinstance(value, dict) or any(
            not isinstance(key, str) for key in value
        ):
            raise IntelligenceAdapterError(
                "intelligence command JSON root must be an object"
            )
        return value


__all__ = ["JsonCommandIntelligenceEngine"]
