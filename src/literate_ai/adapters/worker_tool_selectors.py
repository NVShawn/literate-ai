"""Resolve authored selectors against private registered tools without execution."""

import os
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.builders.cpp import CppToolchain


def _refuse():
    raise ActionWireError(
        "action_tools.selector_mismatch",
        "command selector does not identify the registered worker invocation",
    )


def _resolve(selector, environment):
    name = selector[0]
    path = Path(name)
    if path.is_absolute():
        if ".." in path.parts:
            _refuse()
        candidates = (path,)
    else:
        if (
            path.name != name
            or name in {".", ".."}
            or any(char in name for char in ("/", "\\", ":"))
        ):
            _refuse()

        def setting(key):
            values = [
                value
                for candidate, value in environment.items()
                if candidate == key
                or (os.name == "nt" and candidate.casefold() == key.casefold())
            ]
            if len(values) != 1 or not isinstance(values[0], str):
                _refuse()
            return values[0]

        raw_path = setting("PATH")
        if len(raw_path) > 128 * 4097:
            _refuse()
        directories = raw_path.split(os.pathsep)
        if not 1 <= len(directories) <= 128 or any(
            not entry or not Path(entry).is_absolute() or ".." in Path(entry).parts
            for entry in directories
        ):
            _refuse()
        names = (name,)
        if os.name == "nt":
            suffixes = setting("PATHEXT").split(os.pathsep)
            if not 1 <= len(suffixes) <= 32 or any(
                not suffix.startswith(".")
                or not suffix[1:].isascii()
                or not suffix[1:].isalnum()
                for suffix in suffixes
            ):
                _refuse()
            if not any(
                name.casefold().endswith(suffix.casefold()) for suffix in suffixes
            ):
                names = tuple(name + suffix for suffix in suffixes)
        candidates = tuple(
            Path(directory) / value for directory in directories for value in names
        )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    _refuse()


def verify_worker_tool_selector(tool, selector, *, environment, require_current):
    if not callable(require_current):
        raise TypeError("selector verification requires a live guard")
    if (
        not isinstance(selector, tuple)
        or not 1 <= len(selector) <= 64
        or any(
            not isinstance(item, str) or not item or len(item) > 4096 or "\0" in item
            for item in selector
        )
    ):
        _refuse()
    private = dict(environment)
    overridden = {key.casefold() for key, _ in getattr(tool, "environment", ())}
    private = {
        key: value for key, value in private.items() if key.casefold() not in overridden
    }
    private.update(dict(getattr(tool, "environment", ())))
    if len(private) > 2048 or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\0" in key
        or "\0" in value
        for key, value in private.items()
    ):
        _refuse()

    def check():
        require_current()
        tool.require_unchanged()
        found = _resolve(selector, private)
        # CppToolchain discovery canonicalizes compiler paths. Invocation-sensitive
        # tools such as Python environments and Rust shims retain their paths.
        invocation = (
            found.resolve(strict=True)
            if isinstance(tool, CppToolchain)
            else found.absolute()
        )
        if (
            os.path.normcase(str(invocation)) != os.path.normcase(tool.command[0])
            or selector[1:] != tool.command[1:]
        ):
            _refuse()
        return invocation

    first = check()
    if check() != first:
        _refuse()
    tool.require_unchanged()
    require_current()
