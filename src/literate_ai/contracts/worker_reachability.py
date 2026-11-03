"""Operator-owned literal routes; parsing never grants remote execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, list_value


def _route(value: object, placeholders: frozenset[str], path: str) -> None:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 128:
        fail(path, "requires 1 to 128 literal arguments")
    for argument in value:
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument) > 4096
            or any(character in argument for character in "\x00\r\n")
        ):
            fail(path, "requires bounded nonempty single-line literal arguments")
        if ("{" in argument or "}" in argument) and argument not in placeholders:
            fail(path, "supports only this operation's complete placeholders")
    if value[0] in placeholders:
        fail(path, "the executable must be literal")
    if any(value.count(placeholder) != 1 for placeholder in placeholders):
        fail(path, "requires each operation placeholder exactly once")


@dataclass(frozen=True, slots=True)
class WorkerReachability:
    execute: tuple[str, ...]
    upload: tuple[str, ...]
    download: tuple[str, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:worker-reachability"

    def __post_init__(self) -> None:
        _route(
            self.execute, frozenset({"{remote_command}"}), "WorkerReachability.execute"
        )
        for name in ("upload", "download"):
            _route(
                getattr(self, name),
                frozenset({"{local_file}", "{remote_file}"}),
                "WorkerReachability." + name,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execute": list(self.execute),
            "upload": list(self.upload),
            "download": list(self.download),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "WorkerReachability"
    ) -> WorkerReachability:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"execute", "upload", "download"}),
        )
        return cls(
            *(
                tuple(list_value(data[name], path + "." + name))
                for name in ("execute", "upload", "download")
            )
        )
