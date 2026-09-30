"""Validation over proposed source, including derived API-use reconciliation."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol


class ValidationSeverity(StrEnum):
    INFORMATIONAL = "informational"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    validator_id: str
    category: str
    severity: ValidationSeverity
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    validator_ids: tuple[str, ...]
    categories: tuple[str, ...]
    findings: tuple[ValidationFinding, ...]
    passed: bool


class SourceValidator(Protocol):
    validator_id: str
    category: str

    def validate(self, files: Mapping[str, bytes]) -> Sequence[ValidationFinding]: ...


@dataclass(frozen=True, slots=True)
class SourceContract:
    contract_id: str
    component_revision_digest: str
    modules: tuple[str, ...]
    symbols: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.contract_id or not self.evidence_ids:
            raise ValueError("source contract requires identity and durable evidence")
        if not self.modules:
            raise ValueError("source contract requires at least one module")


class PythonSyntaxValidator:
    validator_id = "validator:python-syntax@1"
    category = "syntax"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        for path, content in sorted(files.items()):
            if PurePosixPath(path).suffix != ".py":
                continue
            try:
                source = content.decode("utf-8")
                ast.parse(source, filename=path)
            except UnicodeDecodeError as exc:
                findings.append(
                    ValidationFinding(
                        self.validator_id,
                        self.category,
                        ValidationSeverity.ERROR,
                        "validation.python_not_utf8",
                        str(exc),
                        path,
                    )
                )
            except SyntaxError as exc:
                findings.append(
                    ValidationFinding(
                        self.validator_id,
                        self.category,
                        ValidationSeverity.ERROR,
                        "validation.python_syntax",
                        exc.msg,
                        path,
                        exc.lineno,
                    )
                )
        return tuple(findings)


class CppSourceValidator:
    """Validate the inert lexical boundary before an authorized C++ compilation."""

    validator_id = "validator:cpp-source@1"
    category = "syntax"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        translation_units = 0
        for path, content in sorted(files.items()):
            suffix = PurePosixPath(path).suffix
            if suffix not in {
                ".cc",
                ".cpp",
                ".cu",
                ".cxx",
                ".h",
                ".hh",
                ".hpp",
                ".hxx",
            }:
                continue
            if suffix in {".cc", ".cpp", ".cu", ".cxx"}:
                translation_units += 1
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                findings.append(
                    ValidationFinding(
                        self.validator_id,
                        self.category,
                        ValidationSeverity.ERROR,
                        "validation.cpp_not_utf8",
                        str(exc),
                        path,
                    )
                )
                continue
            if "\x00" in source:
                findings.append(
                    ValidationFinding(
                        self.validator_id,
                        self.category,
                        ValidationSeverity.ERROR,
                        "validation.cpp_nul_byte",
                        "C++ source contains a NUL byte",
                        path,
                    )
                )
        if translation_units == 0:
            findings.append(
                ValidationFinding(
                    self.validator_id,
                    self.category,
                    ValidationSeverity.ERROR,
                    "validation.cpp_translation_unit_missing",
                    "C++ generation requires at least one translation unit",
                )
            )
        return tuple(findings)


_CPP_CLASS_DEFINITION = re.compile(r"\b(?:class|struct)\s+[A-Za-z_]\w*[^;{}]*\{")
_CPP_NONOWNING_TEXT_MEMBER = re.compile(
    r"""
    ^[ \t]*(?:(?:public|protected|private)[ \t]*:[ \t]*)?
    (?:(?:mutable|static|inline|constexpr|constinit)[ \t]+)*
    (?:
        (?:const[ \t]+)?std::string(?:[ \t]+const)?[ \t]*&
        |
        (?:const[ \t]+)?std::string_view(?:[ \t]+const)?(?:[ \t]*&)?
    )
    [ \t]+[A-Za-z_]\w*
    (?:[ \t]*=[^;\n]*)?[ \t]*;
    """,
    re.MULTILINE | re.VERBOSE,
)


def _mask_cpp_comments_and_literals(source: str) -> str:
    """Retain source positions while hiding inert text from lexical policy rules."""

    masked = list(source)
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end == -1 else end
            for position in range(index, end):
                masked[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end == -1 else end + 2
            for position in range(index, end):
                if source[position] != "\n":
                    masked[position] = " "
            index = end
            continue
        if source[index] in {'"', "'"}:
            quote = source[index]
            masked[index] = " "
            position = index + 1
            while position < len(source):
                if source[position] != "\n":
                    masked[position] = " "
                if source[position] == "\\":
                    position += 1
                    if position < len(source):
                        if source[position] != "\n":
                            masked[position] = " "
                        position += 1
                    continue
                if source[position] == quote:
                    position += 1
                    break
                position += 1
            index = position
            continue
        index += 1
    return "".join(masked)


def _cpp_nonowning_text_member_lines(source: str) -> tuple[int, ...]:
    """Locate direct non-owning text members in named C++ class definitions.

    This intentionally recognizes a small, high-confidence portable subset instead of
    pretending to parse C++. Function bodies and nested scopes are hidden before the
    member declaration rule runs, so parameters and local references remain valid.
    """

    code = _mask_cpp_comments_and_literals(source)
    lines: set[int] = set()
    for definition in _CPP_CLASS_DEFINITION.finditer(code):
        opening = code.rfind("{", definition.start(), definition.end())
        depth = 1
        closing = opening + 1
        while closing < len(code) and depth:
            if code[closing] == "{":
                depth += 1
            elif code[closing] == "}":
                depth -= 1
            closing += 1
        if depth:
            continue
        body = list(code[opening + 1 : closing - 1])
        depth = 1
        for index, character in enumerate(body):
            if character == "{":
                depth += 1
                body[index] = " "
            elif character == "}":
                body[index] = " "
                depth -= 1
            elif depth != 1 and character != "\n":
                body[index] = " "
        top_level_body = "".join(body)
        for member in _CPP_NONOWNING_TEXT_MEMBER.finditer(top_level_body):
            absolute = opening + 1 + member.start()
            lines.add(code.count("\n", 0, absolute) + 1)
    return tuple(sorted(lines))


class CppPortableLifetimeValidator:
    """Reject non-owning text data members outside the portable generated-app subset.

    A generated parser can safely own ``std::string`` or use named request storage for
    the duration of a parse. Retained string references and ``std::string_view`` data
    members make that lifetime implicit and allow an ``argv[1]`` conversion temporary
    to dangle while still compiling cleanly on the supported toolchains.
    """

    validator_id = "validator:cpp-portable-lifetimes@1"
    category = "correctness"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        for path, content in sorted(files.items()):
            if PurePosixPath(path).suffix not in {
                ".cc",
                ".cpp",
                ".cu",
                ".cxx",
                ".h",
                ".hh",
                ".hpp",
                ".hxx",
            }:
                continue
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            findings.extend(
                ValidationFinding(
                    self.validator_id,
                    self.category,
                    ValidationSeverity.ERROR,
                    "validation.cpp_nonowning_text_member",
                    "portable generated C++ must own text stored in data members",
                    path,
                    line,
                )
                for line in _cpp_nonowning_text_member_lines(source)
            )
        return tuple(findings)


_CPP_IMPLEMENTATION_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cu", ".cxx"})
_CPP_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
_CPP_INCLUDE_DIRECTIVE = re.compile(r"^[ \t]*#[ \t]*include\b", re.MULTILINE)


def _cpp_direct_include_target(
    source: str, *, after_directive: int, line_end: int
) -> str | None:
    position = after_directive
    while position < line_end:
        if source[position] in " \t":
            position += 1
            continue
        if source.startswith("/*", position):
            comment_end = source.find("*/", position + 2, line_end)
            if comment_end == -1:
                return None
            position = comment_end + 2
            continue
        break
    if position >= line_end or source[position] not in {'"', "<"}:
        return None
    closing = '"' if source[position] == '"' else ">"
    target_end = source.find(closing, position + 1, line_end)
    if target_end == -1:
        return None
    return source[position + 1 : target_end]


class CppTranslationUnitIncludeValidator:
    """Reject unsafe or incomplete includes in portable multi-file C++ source.

    The C++ builder compiles every generated translation unit independently. Including
    another ``.c``/``.cc``/``.cpp``/``.cxx`` file can therefore compile successfully
    but define the same external symbols in multiple object files at link time. A
    generated translation unit that names a same-stem companion header must also emit
    that header; catching this before the build preserves candidate-repair evidence
    instead of misclassifying a compiler rejection as a framework failure.
    """

    validator_id = "validator:cpp-translation-unit-includes@1"
    category = "correctness"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        for path, content in sorted(files.items()):
            if PurePosixPath(path).suffix not in {
                ".cc",
                ".cpp",
                ".cu",
                ".cxx",
                ".h",
                ".hh",
                ".hpp",
                ".hxx",
            }:
                continue
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            code = _mask_cpp_comments_and_literals(source)
            for directive in _CPP_INCLUDE_DIRECTIVE.finditer(code):
                line_end = source.find("\n", directive.end())
                line_end = len(source) if line_end == -1 else line_end
                target = _cpp_direct_include_target(
                    source,
                    after_directive=directive.end(),
                    line_end=line_end,
                )
                if target is None:
                    continue
                include = PurePosixPath(target)
                suffix = include.suffix.casefold()
                line = source.count("\n", 0, directive.start()) + 1
                if suffix in _CPP_IMPLEMENTATION_SUFFIXES:
                    findings.append(
                        ValidationFinding(
                            self.validator_id,
                            self.category,
                            ValidationSeverity.ERROR,
                            "validation.cpp_translation_unit_included",
                            (
                                "portable generated C++ must not include "
                                "implementation files"
                            ),
                            path,
                            line,
                        )
                    )
                    continue
                source_path = PurePosixPath(path)
                if (
                    suffix in _CPP_HEADER_SUFFIXES
                    and len(include.parts) == 1
                    and include.stem == source_path.stem
                    and not any(
                        PurePosixPath(candidate).name == target for candidate in files
                    )
                ):
                    findings.append(
                        ValidationFinding(
                            self.validator_id,
                            self.category,
                            ValidationSeverity.ERROR,
                            "validation.cpp_companion_header_missing",
                            (
                                "generated C++ includes a same-stem companion header "
                                "that is absent from the candidate tree"
                            ),
                            path,
                            line,
                        )
                    )
        return tuple(findings)


def _validate_portable_text_sources(
    files: Mapping[str, bytes],
    *,
    suffixes: frozenset[str],
    validator_id: str,
    language: str,
) -> tuple[ValidationFinding, ...]:
    """Validate inert text before the authorized compiler/runtime syntax check."""

    findings: list[ValidationFinding] = []
    source_count = 0
    for path, content in sorted(files.items()):
        if PurePosixPath(path).suffix.casefold() not in suffixes:
            continue
        source_count += 1
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            findings.append(
                ValidationFinding(
                    validator_id,
                    "syntax",
                    ValidationSeverity.ERROR,
                    f"validation.{language}_not_utf8",
                    str(exc),
                    path,
                )
            )
            continue
        if "\x00" in source:
            findings.append(
                ValidationFinding(
                    validator_id,
                    "syntax",
                    ValidationSeverity.ERROR,
                    f"validation.{language}_nul_byte",
                    f"{language.title()} source contains a NUL byte",
                    path,
                )
            )
    if source_count == 0:
        findings.append(
            ValidationFinding(
                validator_id,
                "syntax",
                ValidationSeverity.ERROR,
                f"validation.{language}_source_missing",
                f"{language.title()} generation requires at least one source file",
            )
        )
    return tuple(findings)


class RustSourceValidator:
    """Validate Rust as inert UTF-8 before the authorized ``rustc`` build."""

    validator_id = "validator:rust-source@1"
    category = "syntax"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        return _validate_portable_text_sources(
            files,
            suffixes=frozenset({".rs"}),
            validator_id=self.validator_id,
            language="rust",
        )


class SwiftSourceValidator:
    """Validate Swift as inert UTF-8 before an authorized ``swiftc`` build."""

    validator_id = "validator:swift-source@1"
    category = "syntax"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        return _validate_portable_text_sources(
            files,
            suffixes=frozenset({".swift"}),
            validator_id=self.validator_id,
            language="swift",
        )


class JavaScriptSourceValidator:
    """Validate JavaScript as inert UTF-8 before authorized ``node --check``."""

    validator_id = "validator:javascript-source@1"
    category = "syntax"

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        return _validate_portable_text_sources(
            files,
            suffixes=frozenset({".cjs", ".js", ".mjs"}),
            validator_id=self.validator_id,
            language="javascript",
        )


class ApiUsageValidator:
    validator_id = "validator:python-api-usage@1"
    category = "source-contract"

    def __init__(self, contracts: Sequence[SourceContract]) -> None:
        self.contracts = tuple(contracts)
        self._module_contracts = {
            module: contract for contract in contracts for module in contract.modules
        }

    def validate(self, files: Mapping[str, bytes]) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        for path, content in sorted(files.items()):
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(content.decode("utf-8"), filename=path)
            except (UnicodeDecodeError, SyntaxError):
                continue
            aliases: dict[str, tuple[str, SourceContract]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        contract = self._contract_for_module(alias.name)
                        if contract:
                            aliases[alias.asname or alias.name.split(".")[0]] = (
                                alias.name,
                                contract,
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    contract = self._contract_for_module(node.module)
                    if contract:
                        for alias in node.names:
                            aliases[alias.asname or alias.name] = (
                                f"{node.module}.{alias.name}",
                                contract,
                            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted_name(node.func)
                if not dotted:
                    continue
                root, _, suffix = dotted.partition(".")
                binding = aliases.get(root)
                if not binding:
                    continue
                imported, contract = binding
                used = f"{imported}.{suffix}" if suffix else imported
                if not _symbol_allowed(used, contract.symbols):
                    findings.append(
                        ValidationFinding(
                            self.validator_id,
                            self.category,
                            ValidationSeverity.ERROR,
                            "validation.api_not_in_source_contract",
                            f"API use {used!r} is not in {contract.contract_id!r}",
                            path,
                            getattr(node, "lineno", None),
                            contract.evidence_ids,
                        )
                    )
        return tuple(findings)

    def _contract_for_module(self, module: str) -> SourceContract | None:
        candidates = [
            (prefix, contract)
            for prefix, contract in self._module_contracts.items()
            if module == prefix or module.startswith(prefix + ".")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: len(item[0]))[1]


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _symbol_allowed(used: str, allowed: tuple[str, ...]) -> bool:
    return any(used == symbol or used.startswith(symbol + ".") for symbol in allowed)


class ValidationPipeline:
    def __init__(
        self,
        validators: Sequence[SourceValidator],
        *,
        required_categories: Sequence[str],
    ) -> None:
        self.validators = tuple(validators)
        self.required_categories = frozenset(required_categories)
        categories = [item.category for item in validators]
        missing = self.required_categories - set(categories)
        if missing:
            raise ValueError(
                f"validation pipeline lacks required categories: {sorted(missing)}"
            )
        if len({item.validator_id for item in validators}) != len(validators):
            raise ValueError("validation pipeline has duplicate validator identities")

    def validate(self, files: Mapping[str, bytes]) -> ValidationReport:
        findings = tuple(
            finding
            for validator in self.validators
            for finding in validator.validate(files)
        )
        return ValidationReport(
            validator_ids=tuple(item.validator_id for item in self.validators),
            categories=tuple(item.category for item in self.validators),
            findings=findings,
            passed=not any(
                item.severity is ValidationSeverity.ERROR for item in findings
            ),
        )
