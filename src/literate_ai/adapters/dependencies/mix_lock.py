"""Inert native Mix lock graph validation, without evaluating Elixir code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent

from .types import DependencyObservationError

_TOKEN = re.compile(
    r'\s+|\#[^\n]*|%\{|=>|[{}\[\],]|"(?:[^"\\\n]|\\.)*"'
    r"|:[a-z][a-z0-9_]*|[a-z][a-z0-9_]*:|true\b|false\b"
    r"|:"
)
_MAX_BYTES = 2 * 1024 * 1024
_MAX_PACKAGES = 512


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.mix-lock-invalid", message)


@dataclass(frozen=True)
class _Atom:
    name: str


class _LiteralReader:
    def __init__(self, content: bytes):
        if not isinstance(content, bytes) or len(content) > _MAX_BYTES:
            _fail("Mix lock exceeds the bounded UTF-8 literal contract")
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            _fail("Mix lock must be UTF-8")
        self.tokens: list[str] = []
        offset = 0
        while offset < len(text):
            token = _TOKEN.match(text, offset)
            if token is None:
                _fail("Mix lock contains executable or unsupported syntax")
            value = token.group()
            offset = token.end()
            if value.isspace() or value.startswith("#"):
                continue
            self.tokens.append(value)
            if len(self.tokens) > 65536:
                _fail("Mix lock exceeds the token budget")
        self.index = 0

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        value = self.peek()
        if not value or (expected is not None and value != expected):
            _fail("Mix lock literal is incomplete or malformed")
        self.index += 1
        return value

    def value(self, depth: int = 0) -> object:
        if depth > 16:
            _fail("Mix lock exceeds the nesting budget")
        token = self.take()
        if token.startswith('"'):
            try:
                value = json.loads(token)
            except ValueError:
                _fail("Mix lock has an unsupported string escape")
            if "#{" in value:
                _fail("Mix lock string interpolation is forbidden")
            return value
        if token.startswith(":"):
            return _Atom(token[1:])
        if token in {"true", "false"}:
            return _Atom(token)
        if token.endswith(":"):
            return (_Atom(token[:-1]), self.value(depth + 1))
        if token not in {"{", "["}:
            _fail("Mix lock accepts only inert strings, atoms, tuples and lists")
        end = "}" if token == "{" else "]"
        items = []
        while self.peek() != end:
            items.append(self.value(depth + 1))
            if len(items) > 2048:
                _fail("Mix lock literal exceeds the collection budget")
            if self.peek() == end:
                break
            self.take(",")
        self.take(end)
        return tuple(items) if token == "{" else items

    def root(self) -> dict[str, object]:
        self.take("%{")
        result = {}
        while self.peek() != "}":
            token = self.peek()
            if token.endswith(":") and not token.startswith(":"):
                key = self.take()[:-1]
            else:
                key = self.value()
                if isinstance(key, _Atom):
                    key = key.name
                    self.take("=>")
                elif isinstance(key, str):
                    # Mix writes quoted keyword keys (atoms), not string keys.
                    self.take(":")
            if not isinstance(key, str) or key in result:
                _fail("Mix lock keys must be distinct application strings")
            result[key] = self.value()
            if len(result) > _MAX_PACKAGES:
                _fail("Mix lock exceeds the package budget")
            if self.peek() == "}":
                break
            self.take(",")
        self.take("}")
        if self.peek():
            _fail("Mix lock contains trailing executable or literal data")
        return result


@dataclass(frozen=True)
class MixLockedDependency:
    name: str
    requirement: str
    optional: bool


@dataclass(frozen=True)
class MixLockedPackage:
    name: str
    version: str
    inner_sha256: str
    outer_sha256: str
    dependencies: tuple[MixLockedDependency, ...]


@dataclass(frozen=True)
class MixLock:
    """Structural graph/checksums; never acquisition or execution authorization."""

    packages: tuple[MixLockedPackage, ...]
    edges: tuple[tuple[str, str], ...]

    @classmethod
    def from_bytes(cls, content: bytes, *, project: MixProjectIntent) -> MixLock:
        if not isinstance(project, MixProjectIntent):
            raise TypeError("Mix lock requires checked project intent")
        raw = _LiteralReader(content).root()
        packages = []
        for name, value in sorted(raw.items()):
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                or name == project.app
                or not isinstance(value, tuple)
                or len(value) != 8
                or value[0] != _Atom("hex")
                or value[1] != _Atom(name)
                or not isinstance(value[2], str)
                or not re.fullmatch(
                    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
                    r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?"
                    r"(?:\+[0-9A-Za-z.-]+)?",
                    value[2],
                )
                or any(
                    not isinstance(value[index], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value[index])
                    for index in (3, 7)
                )
                or value[4] != [_Atom("mix")]
                or not isinstance(value[5], list)
                or value[6] != "hexpm"
            ):
                _fail("Mix lock requires exact public Hex packages built by Mix")
            dependencies = []
            names = set()
            for dependency in value[5]:
                if (
                    not isinstance(dependency, tuple)
                    or len(dependency) != 3
                    or not isinstance(dependency[0], _Atom)
                    or not isinstance(dependency[1], str)
                    or not isinstance(dependency[2], list)
                ):
                    _fail("Mix lock dependency tuple is invalid")
                dep_name = dependency[0].name
                try:
                    MixDependencyIntent(dep_name, dependency[1])
                except ValueError:
                    _fail("Mix lock dependency intent is invalid")
                options = {}
                for option in dependency[2]:
                    if (
                        not isinstance(option, tuple)
                        or len(option) != 2
                        or not isinstance(option[0], _Atom)
                        or option[0].name in options
                    ):
                        _fail("Mix lock dependency options are invalid or duplicate")
                    options[option[0].name] = option[1]
                if (
                    set(options) != {"hex", "repo", "optional"}
                    or options["hex"] != _Atom(dep_name)
                    or options["repo"] != "hexpm"
                    or options["optional"] not in (_Atom("true"), _Atom("false"))
                    or dep_name in names
                ):
                    _fail("Mix lock dependency uses unsupported or duplicate authority")
                names.add(dep_name)
                dependencies.append(
                    MixLockedDependency(
                        dep_name, dependency[1], options["optional"] == _Atom("true")
                    )
                )
            packages.append(
                MixLockedPackage(
                    name, value[2], value[3], value[7], tuple(dependencies)
                )
            )
        edges = set()
        for dependency in project.dependencies:
            if dependency.name not in raw:
                _fail("Mix lock omits a declared root dependency")
            edges.add(("@root", dependency.name))
        for package in packages:
            for dependency in package.dependencies:
                if dependency.name not in raw:
                    if dependency.optional:
                        continue
                    _fail("Mix lock omits a required transitive dependency")
                edges.add((package.name, dependency.name))
        reached = {"@root"}
        pending = ["@root"]
        outgoing: dict[str, list[str]] = {}
        for parent, child in edges:
            outgoing.setdefault(parent, []).append(child)
        while pending:
            for child in outgoing.get(pending.pop(), []):
                if child not in reached:
                    reached.add(child)
                    pending.append(child)
        if set(raw) != reached - {"@root"}:
            _fail("Mix lock contains packages outside the root dependency closure")
        return cls(tuple(packages), tuple(sorted(edges)))
