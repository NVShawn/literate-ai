"""Deterministic validation of generated source-tree build boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.adapters.dependencies.javascript_imports import (
    javascript_import_specifiers,
)


@dataclass(frozen=True, slots=True)
class GeneratedSourceValidationError(ValueError):
    """A generated tree violates a statically provable build invariant."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


_RUST_RULE_START = re.compile(r"\b(rust_binary|rust_test)\s*\(")
_CPP_EXECUTABLE_RULE_START = re.compile(r"\b(cc_binary|cc_test)\s*\(")
_CPP_BINARY_RULE_START = re.compile(r"\bcc_binary\s*\(")
_GENRULE_START = re.compile(r"\bgenrule\s*\(")
_PATH_MODULE = re.compile(
    r'^\s*#\s*\[\s*path\s*=\s*"([^"]+)"\s*\]\s*'
    r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+[A-Za-z_][A-Za-z0-9_]*\s*;",
    re.MULTILINE,
)
_PLAIN_MODULE = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
_QUOTED_VALUE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_MAKE_LANGUAGE_TOOL = "$(LITAI_LANGUAGE_TOOL)"
_JS_SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".jsx", ".mjs"})
_JS_LOCAL_SUFFIXES = frozenset({".cjs", ".js", ".json", ".jsx", ".mjs", ".node"})
_LITAI_MODULE_MAP = "__litaiModules"
_REQUIRE_MAIN_GUARD = re.compile(r"require\s*\.\s*main\s*===\s*module")
_MODULE_MAP_KEY = re.compile(r"""['"](\.[^'"]+)['"]\s*:""")
_MODULE_PATH_ARRAY = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\[")
_JAVASCRIPT_QUOTED_VALUE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_DISPATCH_AFTER_REQUIRE = re.compile(
    r"__litaiRequire\s*\(\s*['\"][^'\"]+['\"]\s*\)\s*\."
)


def validate_javascript_generation_handoff(files: Mapping[str, str]) -> None:
    """Reject JavaScript source that the single-file loader cannot execute.

    The skill-required `node source/main.js --litai-test` self-check can pass
    while the selected bundle fails: Node resolves extensionless CommonJS IDs,
    but a generated `__litaiModules` map keyed by `.js` paths does not. Catch
    that mismatch, missing transitive modules, extra shebangs, and a registry
    load that never dispatches `--litai-test` before source intelligence.
    """

    sources = {
        path: content
        for path, content in files.items()
        if PurePosixPath(path).suffix.casefold() in _JS_SOURCE_SUFFIXES
        and isinstance(content, str)
    }
    if not sources:
        return
    bundles = {
        path: content
        for path, content in sources.items()
        if _LITAI_MODULE_MAP in content
    }
    application = {
        path: content for path, content in sources.items() if path not in bundles
    }
    for path, content in sorted(application.items()):
        for specifier in sorted(javascript_import_specifiers(content)):
            if _javascript_local_specifier_is_extensionless(specifier):
                raise GeneratedSourceValidationError(
                    "coding_cli.generated_javascript_local_specifier_extensionless",
                    "generated JavaScript "
                    f"{path} imports local module {specifier!r} without a file "
                    "extension; the single-file bundle loader cannot resolve it",
                )
    if not bundles:
        return
    required_ids = _javascript_application_module_ids(application)
    for path, content in sorted(bundles.items()):
        keys = _javascript_bundle_module_keys(content)
        missing = sorted(required_ids - keys)
        if missing:
            raise GeneratedSourceValidationError(
                "coding_cli.generated_javascript_bundle_module_missing",
                f"generated JavaScript bundle {path} omits reachable module "
                f"{missing[0]}; collect every require match before recursing",
            )
        if _javascript_bundle_has_embedded_shebang(content):
            raise GeneratedSourceValidationError(
                "coding_cli.generated_javascript_bundle_shebang",
                f"generated JavaScript bundle {path} embeds a shebang after the "
                "process entry line; strip shebangs from wrapped module bodies",
            )
        if _javascript_source_uses_require_main(
            application
        ) and not _javascript_bundle_dispatches_after_load(content):
            raise GeneratedSourceValidationError(
                "coding_cli.generated_javascript_bundle_entrypoint_undispatched",
                f"generated JavaScript bundle {path} loads the entry module "
                "through its private registry without dispatching the exported "
                "CLI; require.main is false inside __litaiRequire",
            )


def validate_make_language_tool_quoting(files: Mapping[str, str]) -> None:
    """Require the framework-selected executable to survive shell tokenization."""

    content = files.get("source/Makefile")
    if content is None:
        return
    quoted = f'"{_MAKE_LANGUAGE_TOOL}"'
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.startswith("\t"):
            continue
        recipe = line[1:].lstrip("@-+ ")
        if recipe.startswith("#"):
            continue
        if _MAKE_LANGUAGE_TOOL in recipe.replace(quoted, ""):
            raise GeneratedSourceValidationError(
                "coding_cli.generated_make_language_tool_unquoted",
                "generated source/Makefile recipe line "
                f"{line_number} expands {_MAKE_LANGUAGE_TOOL} without double "
                "quotes; framework-selected executables may contain spaces on "
                "Windows",
            )


def validate_rust_bazel_source_closure(files: Mapping[str, str]) -> None:
    """Require each generated Bazel Rust rule to declare its module closure.

    Bazel sandboxes expose only declared inputs. Rust's ``mod`` and ``#[path]``
    declarations can therefore compile outside Bazel while failing in a real
    Bazel action. This validation follows statically named module edges before
    the generated candidate reaches source intelligence or the source cache.
    """

    for build_path, content in files.items():
        if PurePosixPath(build_path).name not in {"BUILD", "BUILD.bazel"}:
            continue
        package = PurePosixPath(build_path).parent
        for rule_kind, body in _rust_rule_bodies(content):
            srcs = _attribute_expression(body, "srcs")
            if srcs is None:
                raise GeneratedSourceValidationError(
                    "coding_cli.generated_rust_bazel_source_closure_unverifiable",
                    f"generated {rule_kind} in {build_path} has no statically "
                    "verifiable srcs attribute",
                )
            declared = _declared_rust_sources(srcs, package, files)
            if not declared:
                raise GeneratedSourceValidationError(
                    "coding_cli.generated_rust_bazel_source_closure_unverifiable",
                    f"generated {rule_kind} in {build_path} declares no statically "
                    "verifiable Rust sources",
                )
            for source in sorted(declared):
                _require_declared_module_closure(
                    source,
                    module_base=source.parent,
                    declared=declared,
                    files=files,
                    build_path=build_path,
                    rule_kind=rule_kind,
                    visited=set(),
                )


def validate_cpp_bazel_rule_attributes(files: Mapping[str, str]) -> None:
    """Reject rules_cc executable attributes that Bazel cannot analyze.

    ``cc_binary`` and ``cc_test`` do not expose the ``hdrs`` attribute in the
    Starlark rules_cc API.  A generated candidate can compile with a direct host
    compiler and still spend a costly Bazel resolution cycle before this typo is
    discovered.  Keep that invariant at the generated-tree boundary instead.
    """

    for build_path, content in files.items():
        if PurePosixPath(build_path).name not in {"BUILD", "BUILD.bazel"}:
            continue
        binary_names = {
            name
            for body in _rule_bodies(content, _CPP_BINARY_RULE_START)
            if (name := _literal_attribute(body, "name")) is not None
        }
        binary_outputs = {
            PurePosixPath(output)
            for binary_name in binary_names
            for output in (binary_name, f"{binary_name}.exe")
        }
        for body in _rule_bodies(content, _GENRULE_START):
            outs = _attribute_expression(body, "outs")
            if outs is None:
                continue
            for output in (
                _unescape_starlark_string(value)
                for value in _QUOTED_VALUE.findall(outs)
            ):
                if PurePosixPath(output) in binary_outputs:
                    raise GeneratedSourceValidationError(
                        "coding_cli.generated_cpp_bazel_output_collision",
                        f"generated genrule in {build_path} declares {output!r}, "
                        "which collides with a cc_binary output on a supported host; "
                        "use the cc_binary target directly",
                    )
        for rule_kind, body in _cpp_executable_rule_bodies(content):
            if _attribute_expression(body, "hdrs") is not None:
                raise GeneratedSourceValidationError(
                    "coding_cli.generated_cpp_bazel_hdrs_unsupported",
                    f"generated {rule_kind} in {build_path} uses unsupported hdrs; "
                    "list private headers in srcs or expose shared headers from a "
                    "cc_library dependency",
                )
            includes = _attribute_expression(body, "includes")
            if includes is not None and any(
                PurePosixPath(_unescape_starlark_string(value)) == PurePosixPath(".")
                for value in _QUOTED_VALUE.findall(includes)
            ):
                raise GeneratedSourceValidationError(
                    "coding_cli.generated_cpp_bazel_workspace_include_unsupported",
                    f'generated {rule_kind} in {build_path} uses includes = ["."]; '
                    "include package-relative headers directly or expose shared "
                    "headers from a cc_library dependency",
                )


def _javascript_local_specifier_is_extensionless(specifier: str) -> bool:
    if not specifier.startswith((".", "/")):
        return False
    name = PurePosixPath(specifier.split("?", 1)[0].split("#", 1)[0]).name
    if not name or name in {".", ".."}:
        return True
    return not any(name.endswith(suffix) for suffix in _JS_LOCAL_SUFFIXES)


def _javascript_application_module_ids(application: Mapping[str, str]) -> set[str]:
    pending = [
        path
        for path in application
        if PurePosixPath(path).as_posix() in {"source/main.js", "source/main.cjs"}
        or PurePosixPath(path).name == "main.js"
    ]
    if not pending:
        pending = list(application)
    required: set[str] = set()
    seen: set[str] = set()
    while pending:
        path = pending.pop()
        if path in seen or path not in application:
            continue
        seen.add(path)
        required.add(_javascript_source_module_id(path))
        parent = PurePosixPath(path).parent
        for specifier in javascript_import_specifiers(application[path]):
            if not specifier.startswith("."):
                continue
            resolved = _javascript_resolved_source_path(parent, specifier)
            if resolved is not None:
                pending.append(resolved)
    return required


def _javascript_source_module_id(path: str) -> str:
    posix = PurePosixPath(path)
    if posix.parts and posix.parts[0] == "source":
        posix = PurePosixPath(*posix.parts[1:])
    return f"./{posix.as_posix()}"


def _javascript_resolved_source_path(
    parent: PurePosixPath, specifier: str
) -> str | None:
    parts: list[str] = list(parent.parts)
    for part in PurePosixPath(specifier).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    if not parts:
        return None
    return str(PurePosixPath(*parts))


def _javascript_bundle_module_keys(content: str) -> set[str]:
    start = content.find(_LITAI_MODULE_MAP)
    if start < 0:
        return set()
    opening = content.find("{", start)
    if opening < 0:
        return set()
    closing = _matching_delimiter(content, opening, "{", "}")
    body = content[opening : closing + 1] if closing is not None else content[opening:]
    keys = {match.group(1) for match in _MODULE_MAP_KEY.finditer(body)}
    keys.update(_javascript_dynamic_bundle_module_keys(content))
    return keys


def _javascript_dynamic_bundle_module_keys(content: str) -> set[str]:
    """Read explicit module IDs used to populate a registry in a loop."""

    keys: set[str] = set()
    for assignment in _MODULE_PATH_ARRAY.finditer(content):
        opening = content.find("[", assignment.start())
        closing = _matching_delimiter(content, opening, "[", "]")
        if closing is None:
            continue
        collection = assignment.group(1)
        loop = re.search(
            rf"\bfor\s*\(\s*const\s+([A-Za-z_$][\w$]*)\s+of\s+"
            rf"{re.escape(collection)}\s*\)",
            content[closing + 1 :],
        )
        if loop is None:
            continue
        item = loop.group(1)
        loop_start = closing + 1 + loop.end()
        loop_opening = content.find("{", loop_start)
        if loop_opening < 0:
            continue
        loop_closing_match = re.search(r"(?m)^\s*}", content[loop_opening + 1 :])
        if loop_closing_match is None:
            continue
        loop_closing = loop_opening + 1 + loop_closing_match.start()
        loop_body = content[loop_opening + 1 : loop_closing]
        if (
            re.search(
                rf"\b{re.escape(_LITAI_MODULE_MAP)}\s*\[\s*"
                rf"(?:\$\{{\s*)?(?:JSON\.stringify\(\s*)?{re.escape(item)}\b",
                loop_body,
            )
            is None
        ):
            continue
        array = content[opening + 1 : closing]
        for match in _JAVASCRIPT_QUOTED_VALUE.finditer(array):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            assert value is not None
            module_id = _unescape_javascript_string(value)
            if module_id.startswith("./"):
                keys.add(module_id)
            elif module_id and not module_id.startswith(("/", "../")):
                keys.add(f"./{module_id}")
    return keys


def _unescape_javascript_string(value: str) -> str:
    """Decode the bounded escapes needed for generated literal module IDs."""

    return re.sub(r"\\([\\'\"])", r"\1", value)


def _javascript_bundle_has_embedded_shebang(content: str) -> bool:
    shebangs = [
        index
        for index, line in enumerate(content.splitlines())
        if line.startswith("#!")
    ]
    return bool(shebangs) and (shebangs[0] != 0 or len(shebangs) > 1)


def _javascript_source_uses_require_main(application: Mapping[str, str]) -> bool:
    return any(_REQUIRE_MAIN_GUARD.search(content) for content in application.values())


def _javascript_bundle_dispatches_after_load(content: str) -> bool:
    marker = "function __litaiRequire"
    start = content.rfind(marker)
    footer = content if start < 0 else content[start:]
    if start >= 0:
        opening = content.find("{", start)
        closing = (
            None if opening < 0 else _matching_delimiter(content, opening, "{", "}")
        )
        if closing is not None:
            footer = content[closing + 1 :]
    if _DISPATCH_AFTER_REQUIRE.search(footer):
        return True
    return bool(
        re.search(r"__litaiRequire\s*\(", footer)
        and re.search(r"\.\s*[A-Za-z_$][\w$]*\s*\(", footer)
    )


def _rust_rule_bodies(content: str):
    for match in _RUST_RULE_START.finditer(content):
        opening = content.find("(", match.start())
        closing = _matching_delimiter(content, opening, "(", ")")
        if closing is not None:
            yield match.group(1), content[opening + 1 : closing]


def _cpp_executable_rule_bodies(content: str):
    for match in _CPP_EXECUTABLE_RULE_START.finditer(content):
        opening = content.find("(", match.start())
        closing = _matching_delimiter(content, opening, "(", ")")
        if closing is not None:
            yield match.group(1), content[opening + 1 : closing]


def _rule_bodies(content: str, start_pattern: re.Pattern[str]):
    for match in start_pattern.finditer(content):
        opening = content.find("(", match.start())
        closing = _matching_delimiter(content, opening, "(", ")")
        if closing is not None:
            yield content[opening + 1 : closing]


def _literal_attribute(body: str, attribute: str) -> str | None:
    expression = _attribute_expression(body, attribute)
    if expression is None:
        return None
    match = _QUOTED_VALUE.fullmatch(expression.strip())
    return None if match is None else _unescape_starlark_string(match.group(1))


def _attribute_expression(body: str, attribute: str) -> str | None:
    match = re.search(rf"\b{re.escape(attribute)}\s*=", body)
    if match is None:
        return None
    start = match.end()
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(body)):
        character = body[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            return body[start:index]
    return body[start:]


def _matching_delimiter(
    content: str, start: int, opening: str, closing: str
) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    comment = False
    for index in range(start, len(content)):
        character = content[index]
        if comment:
            if character == "\n":
                comment = False
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == "#":
            comment = True
        elif character in {'"', "'"}:
            quote = character
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _declared_rust_sources(
    expression: str,
    package: PurePosixPath,
    files: Mapping[str, str],
) -> set[PurePosixPath]:
    declared: set[PurePosixPath] = set()
    values = tuple(
        _unescape_starlark_string(item) for item in _QUOTED_VALUE.findall(expression)
    )
    for value in values:
        if value.endswith(".rs") and not value.startswith(("@", "//", ":")):
            declared.add(package / value)
    if "glob" in expression:
        for path in files:
            candidate = PurePosixPath(path)
            try:
                relative = candidate.relative_to(package)
            except ValueError:
                continue
            if candidate.suffix == ".rs" and any(
                relative.match(pattern) for pattern in values
            ):
                declared.add(candidate)
    return declared


def _unescape_starlark_string(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _require_declared_module_closure(
    source: PurePosixPath,
    *,
    module_base: PurePosixPath,
    declared: set[PurePosixPath],
    files: Mapping[str, str],
    build_path: str,
    rule_kind: str,
    visited: set[tuple[PurePosixPath, PurePosixPath]],
) -> None:
    visit = (source, module_base)
    if visit in visited:
        return
    visited.add(visit)
    content = files.get(source.as_posix())
    if content is None:
        return
    edges: list[tuple[PurePosixPath, PurePosixPath]] = []
    path_spans: list[tuple[int, int]] = []
    for match in _PATH_MODULE.finditer(content):
        target = _normalized_relative_source(source.parent, match.group(1))
        if target is not None:
            edges.append((target, _child_module_base(target)))
        path_spans.append(match.span())
    remaining = list(content)
    for start, end in path_spans:
        for index in range(start, end):
            if remaining[index] != "\n":
                remaining[index] = " "
    without_path_modules = "".join(remaining)
    for match in _PLAIN_MODULE.finditer(without_path_modules):
        name = match.group(1)
        flat = module_base / f"{name}.rs"
        nested = module_base / name / "mod.rs"
        if flat.as_posix() in files:
            edges.append((flat, _child_module_base(flat)))
        elif nested.as_posix() in files:
            edges.append((nested, _child_module_base(nested)))
    for target, child_base in edges:
        if target not in declared:
            raise GeneratedSourceValidationError(
                "coding_cli.generated_rust_bazel_source_closure_incomplete",
                f"generated {rule_kind} in {build_path} omits reachable Rust "
                f"source {target.as_posix()} from srcs",
            )
        _require_declared_module_closure(
            target,
            module_base=child_base,
            declared=declared,
            files=files,
            build_path=build_path,
            rule_kind=rule_kind,
            visited=visited,
        )


def _normalized_relative_source(
    parent: PurePosixPath, value: str
) -> PurePosixPath | None:
    candidate = parent / value
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".rs":
        return None
    return candidate


def _child_module_base(source: PurePosixPath) -> PurePosixPath:
    if source.name == "mod.rs":
        return source.parent
    return source.parent / source.stem
