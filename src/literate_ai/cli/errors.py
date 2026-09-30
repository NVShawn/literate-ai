"""Stable CLI failure, JSON parser, and stdout envelope schemas."""

from __future__ import annotations

import argparse
import json
from typing import Any

CLI_ERROR_MESSAGE_CHARS = 512
CLI_ERROR_MESSAGE_MAX_CHARS = 66_048


class CliFailure(ValueError):
    """Stable, safe failure raised by the CLI adapter."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        message_limit: int = CLI_ERROR_MESSAGE_CHARS,
    ) -> None:
        if (
            isinstance(message_limit, bool)
            or not isinstance(message_limit, int)
            or not 1 <= message_limit <= CLI_ERROR_MESSAGE_MAX_CHARS
        ):
            raise ValueError("CLI error message limit is outside its finite bound")
        super().__init__(message)
        self.code = code
        self.message = message
        self.message_limit = message_limit


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliFailure("cli.usage", message)


def _json_text(value: Any) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


CLI_RESULT_SCHEMA = "literate-ai/cli-result@1"
CLI_ERROR_SCHEMA = "literate-ai/cli-error@1"
HELP_SCHEMA = "literate-ai/cli-help@1"
