"""Inert JavaScript import-specifier observation."""

from __future__ import annotations

import re

_IMPORT = re.compile(
    r"(?:from\s+|import\s*\(|(?:module\.)?require(?:\.resolve)?\s*\()"
    r"\s*['\"]([^'\"]+)['\"]"
)
_ESM_IMPORT = re.compile(r"(?:from\s+|import\s*\()\s*['\"]([^'\"]+)['\"]")
_SIDE_EFFECT_IMPORT = re.compile(r"(?:^|[;\n])\s*import\s*['\"]([^'\"]+)['\"]")
_DYNAMIC_IMPORT = re.compile(r"(?<![A-Za-z0-9_$.])import\s*\(")
_LITERAL_DYNAMIC_IMPORT = re.compile(
    r'(?<![A-Za-z0-9_$.])import\s*\(\s*"'
    r'(?P<placeholder>__litai_javascript_string_\d+__)"\s*\)'
)

NODE_BUILTIN_MODULES = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

_REGEX_PREFIX_IDENTIFIERS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)


def javascript_import_specifiers(content: str) -> set[str]:
    """Return literal import specifiers without interpreting inert source text."""

    projected, string_literals = _project_code(content)
    placeholders = _IMPORT.findall(projected)
    placeholders.extend(_SIDE_EFFECT_IMPORT.findall(projected))
    return {string_literals[placeholder] for placeholder in placeholders}


def javascript_esm_import_specifiers(content: str) -> set[str]:
    """Return statically quoted ESM import specifiers."""

    projected, string_literals = _project_code(content)
    placeholders = _ESM_IMPORT.findall(projected)
    placeholders.extend(_SIDE_EFFECT_IMPORT.findall(projected))
    return {string_literals[placeholder] for placeholder in placeholders}


def javascript_package_imports(
    content: str, *, builtin_modules: frozenset[str] = NODE_BUILTIN_MODULES
) -> set[str]:
    """Return exact bare npm package names used by literal import forms.

    This deliberately observes only statically quoted import, require,
    require.resolve, and module.require forms.  It does not claim to resolve
    computed specifiers or arbitrary program behavior.
    """

    return {
        name
        for value in javascript_import_specifiers(content)
        if (name := javascript_package_name(value, builtin_modules=builtin_modules))
        is not None
    }


def javascript_package_name(
    specifier: str, *, builtin_modules: frozenset[str] = NODE_BUILTIN_MODULES
) -> str | None:
    """Return the exact bare npm package named by one import specifier."""

    if specifier.startswith((".", "/", "node:")):
        return None
    name = (
        "/".join(specifier.split("/")[:2])
        if specifier.startswith("@")
        else specifier.split("/", 1)[0]
    )
    return None if name in builtin_modules else name


def has_computed_dynamic_import(content: str) -> bool:
    """Return whether source contains a dynamic import not provably one literal."""

    projected, _string_literals = _project_code(content)
    literal_starts = {
        match.start() for match in _LITERAL_DYNAMIC_IMPORT.finditer(projected)
    }
    return any(
        match.start() not in literal_starts
        for match in _DYNAMIC_IMPORT.finditer(projected)
    )


def javascript_dynamic_import_specifiers(content: str) -> set[str]:
    """Return statically quoted dynamic-import specifiers."""

    projected, string_literals = _project_code(content)
    return {
        string_literals[match.group("placeholder")]
        for match in _LITERAL_DYNAMIC_IMPORT.finditer(projected)
    }


def _project_code(content: str) -> tuple[str, dict[str, str]]:
    """Replace quoted strings with keys and mask all other inert lexical forms."""

    projected: list[str] = []
    strings: dict[str, str] = {}
    index = 0
    can_start_regex = True
    while index < len(content):
        char = content[index]
        following = content[index + 1] if index + 1 < len(content) else ""
        if char.isspace():
            projected.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index = _mask_line_comment(content, index, projected)
            continue
        if char == "/" and following == "*":
            index = _mask_block_comment(content, index, projected)
            continue
        if char in {"'", '"'}:
            end = _quoted_literal_end(content, index, char)
            value = content[index + 1 : end - 1]
            placeholder = f"__litai_javascript_string_{len(strings)}__"
            strings[placeholder] = value
            projected.append(f'"{placeholder}"')
            index = end
            can_start_regex = False
            continue
        if char == "`":
            index = _mask_template(content, index, projected)
            can_start_regex = False
            continue
        if char == "/" and can_start_regex:
            end = _regex_literal_end(content, index)
            if end is not None:
                _append_mask(content[index:end], projected)
                index = end
                can_start_regex = False
                continue
        if char.isalpha() or char in {"_", "$"}:
            end = index + 1
            while end < len(content) and (
                content[end].isalnum() or content[end] in {"_", "$"}
            ):
                end += 1
            identifier = content[index:end]
            projected.append(identifier)
            can_start_regex = identifier in _REGEX_PREFIX_IDENTIFIERS
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(content) and (
                content[end].isalnum() or content[end] in {"_", "."}
            ):
                end += 1
            projected.append(content[index:end])
            index = end
            can_start_regex = False
            continue
        if char in {"+", "-"} and following == char:
            projected.append(char + following)
            index += 2
            can_start_regex = False
            continue
        projected.append(char)
        can_start_regex = char not in ")]}."
        index += 1
    return "".join(projected), strings


def _append_mask(value: str, projected: list[str]) -> None:
    projected.extend("\n" if char == "\n" else " " for char in value)


def _mask_line_comment(content: str, index: int, projected: list[str]) -> int:
    end = content.find("\n", index + 2)
    end = len(content) if end < 0 else end
    _append_mask(content[index:end], projected)
    return end


def _mask_block_comment(content: str, index: int, projected: list[str]) -> int:
    closing = content.find("*/", index + 2)
    end = len(content) if closing < 0 else closing + 2
    _append_mask(content[index:end], projected)
    return end


def _quoted_literal_end(content: str, index: int, quote: str) -> int:
    cursor = index + 1
    while cursor < len(content):
        if content[cursor] == "\\":
            cursor += 2
            continue
        if content[cursor] == quote:
            return cursor + 1
        cursor += 1
    return len(content)


def _mask_template(content: str, index: int, projected: list[str]) -> int:
    cursor = index + 1
    while cursor < len(content):
        if content[cursor] == "\\":
            cursor += 2
            continue
        if content[cursor] == "`":
            cursor += 1
            break
        cursor += 1
    _append_mask(content[index:cursor], projected)
    return cursor


def _regex_literal_end(content: str, index: int) -> int | None:
    cursor = index + 1
    in_character_class = False
    while cursor < len(content):
        char = content[cursor]
        if char in {"\n", "\r"}:
            return None
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            in_character_class = True
        elif char == "]":
            in_character_class = False
        elif char == "/" and not in_character_class:
            cursor += 1
            while cursor < len(content) and content[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    return None
