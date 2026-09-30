"""Explicit retained-child operations, separate from repository pin authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._validation import fields
from .paths import canonical_relative_posix_path

OPERATIONS = ("build", "package", "containerize")
SCHEMA = "literate-ai/repository-lifecycle@1"


def relative(value: str, *, dot: bool = False) -> str:
    if dot and value == ".":
        return value
    canonical_relative_posix_path(value, label="lifecycle path")
    if value.split("/")[0].casefold() == ".git":
        raise ValueError("lifecycle paths cannot address Git metadata")
    return value


def texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item or "\0" in item for item in value
    ):
        raise ValueError("expected a list of nonempty strings")
    return tuple(value)


@dataclass(frozen=True)
class ChildOperation:
    cwd: str
    commands: tuple[tuple[str, ...], ...]
    outputs: tuple[str, ...]
    timeout_seconds: int
    environment: dict[str, str]

    @classmethod
    def from_dict(cls, value: Any) -> ChildOperation:
        data = fields(
            value,
            path="ChildOperation",
            required=frozenset(
                {"cwd", "commands", "outputs", "timeout_seconds", "environment"}
            ),
        )
        if not isinstance(data["commands"], list) or not data["commands"]:
            raise ValueError("each supported operation requires real commands")
        commands = tuple(texts(argv) for argv in data["commands"])
        if any(not argv for argv in commands):
            raise ValueError("empty commands are not operations")
        outputs = texts(data["outputs"])
        if not outputs:
            raise ValueError("each operation requires observable product files")
        for pattern in outputs:
            relative(pattern)
        timeout = data["timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError("operation deadline must be 1..86400 seconds")
        environment = data["environment"]
        if not isinstance(environment, dict) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(item, str)
            or "\0" in item
            for key, item in environment.items()
        ):
            raise ValueError("operation environment must contain string assignments")
        return cls(
            relative(data["cwd"], dot=True), commands, outputs, timeout, environment
        )


@dataclass(frozen=True)
class ChildLifecycle:
    path: str
    role: str
    dependencies: tuple[str, ...]
    operations: dict[str, ChildOperation | None]

    @classmethod
    def from_dict(cls, value: Any) -> ChildLifecycle:
        data = fields(
            value,
            path="ChildLifecycle",
            required=frozenset({"path", "role", "dependencies", "operations"}),
        )
        if data["role"] not in {"source", "sdk", "application"}:
            raise ValueError("child role must be source, sdk or application")
        dependencies = texts(data["dependencies"])
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("duplicate child dependencies")
        for dependency in dependencies:
            relative(dependency)
        operations = data["operations"]
        if not isinstance(operations, dict) or set(operations) != set(OPERATIONS):
            raise ValueError("every child must account for all lifecycle operations")
        return cls(
            relative(data["path"]),
            data["role"],
            dependencies,
            {
                key: None if item is None else ChildOperation.from_dict(item)
                for key, item in operations.items()
            },
        )


@dataclass(frozen=True)
class RepositoryLifecycle:
    target: str
    inputs: tuple[str, ...]
    children: tuple[ChildLifecycle, ...]

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryLifecycle:
        data = fields(
            value,
            path="RepositoryLifecycle",
            required=frozenset({"schema", "target", "inputs", "children"}),
        )
        if data["schema"] != SCHEMA:
            raise ValueError("unsupported repository lifecycle schema")
        if data["target"] not in {
            "linux-x86_64",
            "linux-aarch64",
            "macos-aarch64",
            "macos-x86_64",
            "windows-x86_64",
        }:
            raise ValueError("unsupported lifecycle target")
        if (
            not isinstance(data["children"], list)
            or not 1 <= len(data["children"]) <= 128
        ):
            raise ValueError("lifecycle requires 1..128 children")
        children = tuple(ChildLifecycle.from_dict(item) for item in data["children"])
        paths = {child.path for child in children}
        if len(paths) != len(children):
            raise ValueError("duplicate lifecycle child")
        if any(set(child.dependencies) - paths for child in children):
            raise ValueError("unknown lifecycle dependency")
        inputs = texts(data["inputs"])
        if len(set(inputs)) != len(inputs):
            raise ValueError("duplicate root lifecycle input")
        for path in inputs:
            relative(path)
        return cls(data["target"], inputs, children)

    def ordered(self, selected: tuple[str, ...] = ()) -> tuple[ChildLifecycle, ...]:
        children = {child.path: child for child in self.children}
        if set(selected) - children.keys():
            raise ValueError("unknown selected child")
        result: list[ChildLifecycle] = []
        active: set[str] = set()
        done: set[str] = set()

        def visit(path: str) -> None:
            if path in active:
                raise ValueError("lifecycle dependency cycle")
            if path in done:
                return
            active.add(path)
            for dependency in children[path].dependencies:
                visit(dependency)
            active.remove(path)
            done.add(path)
            result.append(children[path])

        # Validate the whole graph even when a subset is explicitly selected.
        for path in children:
            visit(path)
        if not selected:
            return tuple(result)
        result.clear()
        done.clear()
        for path in selected:
            visit(path)
        return tuple(result)
