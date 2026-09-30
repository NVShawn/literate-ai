"""Deterministic, dependency-free YAML subset for authored contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class YamlSubsetError(ValueError):
    def __init__(self, message: str, line: int, column: int = 1) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


@dataclass(frozen=True, slots=True)
class _Line:
    number: int
    indent: int
    text: str


def load_yaml_subset(text: str) -> Any:
    """Parse block mappings/sequences and JSON-like YAML flow collections."""

    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlSubsetError("tabs are not allowed for YAML indentation", number)
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("---"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(_Line(number, indent, stripped[indent:]))
    if not lines:
        return None
    parser = _BlockParser(lines)
    value = parser.parse_node(lines[0].indent)
    if parser.index != len(lines):
        line = lines[parser.index]
        raise YamlSubsetError(
            "unexpected YAML indentation", line.number, line.indent + 1
        )
    return value


class _BlockParser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.index = 0

    def parse_node(self, indent: int) -> Any:
        if self.index >= len(self.lines):
            return None
        line = self.lines[self.index]
        if line.indent != indent:
            raise YamlSubsetError(
                "unexpected YAML indentation", line.number, line.indent + 1
            )
        if line.text == "-" or line.text.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.indent < indent:
                break
            if line.indent != indent or line.text == "-" or line.text.startswith("- "):
                break
            key_text, value_text = _split_mapping(line.text, line.number)
            key = _parse_key(key_text, line.number)
            if key in result:
                raise YamlSubsetError(f"duplicate YAML key {key!r}", line.number)
            self.index += 1
            result[key] = self._mapping_value(value_text, indent, line.number)
        return result

    def _mapping_value(self, value_text: str, indent: int, line_number: int) -> Any:
        if value_text in {"|", "|-", ">", ">-"}:
            return self._block_scalar(indent, folded=value_text.startswith(">"))
        if value_text:
            return _parse_block_value(value_text, line_number)
        if self.index < len(self.lines) and self.lines[self.index].indent > indent:
            return self.parse_node(self.lines[self.index].indent)
        return None

    def _sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.indent != indent or not (
                line.text == "-" or line.text.startswith("- ")
            ):
                break
            content = line.text[1:].lstrip()
            self.index += 1
            if not content:
                if (
                    self.index < len(self.lines)
                    and self.lines[self.index].indent > indent
                ):
                    result.append(self.parse_node(self.lines[self.index].indent))
                else:
                    result.append(None)
                continue
            split = _try_split_mapping(content)
            if split is None:
                result.append(_parse_block_value(content, line.number))
                continue
            key_text, value_text = split
            key = _parse_key(key_text, line.number)
            item = {key: self._mapping_value(value_text, indent + 2, line.number)}
            if (
                self.index < len(self.lines)
                and self.lines[self.index].indent == indent + 2
            ):
                continuation = self._mapping(indent + 2)
                overlap = set(item) & set(continuation)
                if overlap:
                    raise YamlSubsetError(
                        f"duplicate YAML key {next(iter(overlap))!r}",
                        self.lines[self.index - 1].number,
                    )
                item.update(continuation)
            result.append(item)
        return result

    def _block_scalar(self, parent_indent: int, *, folded: bool) -> str:
        collected: list[str] = []
        base_indent: int | None = None
        while (
            self.index < len(self.lines)
            and self.lines[self.index].indent > parent_indent
        ):
            line = self.lines[self.index]
            base_indent = line.indent if base_indent is None else base_indent
            collected.append(" " * max(0, line.indent - base_indent) + line.text)
            self.index += 1
        separator = " " if folded else "\n"
        return separator.join(collected)


class _FlowParser:
    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.index = 0

    def parse_complete(self) -> Any:
        value = self._value()
        self._space()
        if self.index != len(self.text):
            raise YamlSubsetError(
                "unexpected YAML flow content", self.line, self.index + 1
            )
        return value

    def _value(self) -> Any:
        self._space()
        if self.index >= len(self.text):
            return None
        char = self.text[self.index]
        if char == "[":
            return self._sequence()
        if char == "{":
            return self._mapping()
        if char in {'"', "'"}:
            return self._quoted()
        return _plain_value(self._plain(",]}"))

    def _sequence(self) -> list[Any]:
        self.index += 1
        result: list[Any] = []
        self._space()
        if self._consume("]"):
            return result
        while True:
            result.append(self._value())
            self._space()
            if self._consume("]"):
                return result
            self._require(",")

    def _mapping(self) -> dict[str, Any]:
        self.index += 1
        result: dict[str, Any] = {}
        self._space()
        if self._consume("}"):
            return result
        while True:
            self._space()
            key = self._quoted() if self._peek() in {'"', "'"} else self._plain(":")
            if not isinstance(key, str) or not key.strip():
                raise YamlSubsetError(
                    "YAML mapping key must be text", self.line, self.index + 1
                )
            key = key.strip()
            self._require(":")
            if key in result:
                raise YamlSubsetError(f"duplicate YAML key {key!r}", self.line)
            result[key] = self._value()
            self._space()
            if self._consume("}"):
                return result
            self._require(",")

    def _quoted(self) -> str:
        quote = self.text[self.index]
        start = self.index
        self.index += 1
        if quote == '"':
            escaped = False
            while self.index < len(self.text):
                char = self.text[self.index]
                self.index += 1
                if char == '"' and not escaped:
                    try:
                        return json.loads(self.text[start : self.index])
                    except json.JSONDecodeError as exc:
                        raise YamlSubsetError(
                            "invalid quoted YAML string", self.line, start + 1
                        ) from exc
                escaped = char == "\\" and not escaped
                if char != "\\":
                    escaped = False
        else:
            result: list[str] = []
            while self.index < len(self.text):
                char = self.text[self.index]
                self.index += 1
                if char == quote:
                    if self.index < len(self.text) and self.text[self.index] == quote:
                        result.append(quote)
                        self.index += 1
                        continue
                    return "".join(result)
                result.append(char)
        raise YamlSubsetError("unterminated quoted YAML string", self.line, start + 1)

    def _plain(self, delimiters: str) -> str:
        start = self.index
        while self.index < len(self.text) and self.text[self.index] not in delimiters:
            self.index += 1
        value = self.text[start : self.index].strip()
        if not value:
            raise YamlSubsetError("empty YAML scalar", self.line, start + 1)
        return value

    def _space(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1

    def _peek(self) -> str:
        return self.text[self.index] if self.index < len(self.text) else ""

    def _consume(self, value: str) -> bool:
        if self._peek() == value:
            self.index += 1
            return True
        return False

    def _require(self, value: str) -> None:
        self._space()
        if not self._consume(value):
            raise YamlSubsetError(f"expected {value!r}", self.line, self.index + 1)


def _parse_key(text: str, line: int) -> str:
    value = _FlowParser(text.strip(), line).parse_complete()
    if not isinstance(value, str) or not value:
        raise YamlSubsetError("YAML mapping key must be text", line)
    return value


def _parse_block_value(text: str, line: int) -> Any:
    stripped = text.strip()
    if stripped.startswith(("[", "{", '"', "'")):
        return _FlowParser(stripped, line).parse_complete()
    return _plain_value(stripped)


def _plain_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if value and value.lstrip("+-").isdigit():
            return int(value)
        if any(char in value for char in (".", "e", "E")):
            return float(value)
    except ValueError:
        pass
    return value


def _split_mapping(text: str, line: int) -> tuple[str, str]:
    result = _try_split_mapping(text)
    if result is None:
        raise YamlSubsetError("expected a YAML mapping entry", line)
    return result


def _try_split_mapping(text: str) -> tuple[str, str] | None:
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote and not escaped:
                quote = ""
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return text[:index], text[index + 1 :].strip()
    return None


def _strip_comment(text: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if char == quote and not escaped:
                quote = ""
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index]
    return text


__all__ = ["YamlSubsetError", "load_yaml_subset"]
