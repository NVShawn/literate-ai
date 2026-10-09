"""Deterministic, inert inventory of an arbitrary local source checkout."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .contracts import canonical_digest
from .errors import SourceToSpecificationError

_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".codegraph",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "vendor",
    }
)
_SOURCE_LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".ex": "elixir",
    ".exs": "elixir",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".kt": "kotlin",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
}
_CONFIG_NAMES = frozenset(
    {
        "cargo.toml",
        "cmakelists.txt",
        "dockerfile",
        "go.mod",
        "makefile",
        "package.json",
        "pyproject.toml",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
    }
)
_SENSITIVE_PATTERN = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)"
    rb"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.-]{12,}"
)
_PROMPT_INJECTION_PATTERN = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?previous\s+instructions|"
    r"system\s+prompt|you\s+are\s+(?:now\s+)?(?:chatgpt|an?\s+llm)|"
    r"do\s+not\s+follow\s+(?:the\s+)?(?:system|developer))"
)
_GENERATED_PATTERN = re.compile(
    r"(?i)(?:@generated|generated\s+(?:file|code)|do\s+not\s+edit)"
)
_GENERIC_SYMBOL = re.compile(
    r"(?m)^\s*(?:export\s+)?(?:pub\s+)?(?:async\s+)?"
    r"(?:class|def|fn|func|function|interface|struct)\s+([A-Za-z_]\w*)"
)
_TARGET_PATTERNS = (
    ("linux", "platform.os", re.compile(r"(?i)\blinux\b")),
    ("macos", "platform.os", re.compile(r"(?i)\b(?:macos|darwin)\b")),
    ("windows", "platform.os", re.compile(r"(?i)\bwindows\b|\bwin32\b")),
    ("cuda", "accelerator", re.compile(r"(?i)\b(?:cuda|nvidia|nvcc)\b")),
    ("metal", "accelerator", re.compile(r"(?i)\b(?:metal|metalperformance)\b")),
)


class SourceFileClassification(StrEnum):
    SOURCE = "source"
    TEST = "test"
    CONFIGURATION = "configuration"
    DOCUMENTATION = "documentation"
    GENERATED = "generated"
    MINIFIED = "minified"
    SENSITIVE = "sensitive"
    BINARY = "binary"
    LARGE = "large"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FlavorSignal:
    flavor_id: str
    axis: str


@dataclass(frozen=True, slots=True)
class SourceInventoryEntry:
    path: str
    content_digest: str
    size: int
    classification: SourceFileClassification
    language: str | None = None
    symbols: tuple[str, ...] = ()
    flavor_signals: tuple[FlavorSignal, ...] = ()
    prompt_injection: bool = False


@dataclass(frozen=True, slots=True)
class SourceInventory:
    entries: tuple[SourceInventoryEntry, ...]
    excluded_directories: tuple[str, ...]
    inventory_policy_id: str = "static-checkout-inventory@1"

    @property
    def identity(self) -> str:
        return canonical_digest(self)


def source_inventory_from_dict(value: object) -> SourceInventory:
    """Strictly restore the canonical inventory retained in a result bundle."""

    if not isinstance(value, Mapping) or set(value) != {
        "entries",
        "excluded_directories",
        "inventory_policy_id",
    }:
        raise SourceToSpecificationError(
            "inventory.record_invalid", "source inventory has invalid fields"
        )
    raw_entries = value["entries"]
    raw_excluded = value["excluded_directories"]
    policy = value["inventory_policy_id"]
    if not isinstance(raw_entries, list) or not isinstance(raw_excluded, list):
        raise SourceToSpecificationError(
            "inventory.record_invalid", "source inventory arrays are invalid"
        )
    if not all(
        isinstance(item, str) and item for item in raw_excluded
    ) or raw_excluded != sorted(set(raw_excluded)):
        raise SourceToSpecificationError(
            "inventory.record_invalid", "excluded directories are not canonical"
        )
    if not isinstance(policy, str) or not policy:
        raise SourceToSpecificationError(
            "inventory.record_invalid", "source inventory policy is invalid"
        )
    entries: list[SourceInventoryEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "content_digest",
            "size",
            "classification",
            "language",
            "symbols",
            "flavor_signals",
            "prompt_injection",
        }:
            raise SourceToSpecificationError(
                "inventory.record_invalid", "source inventory entry has invalid fields"
            )
        path = raw["path"]
        digest = raw["content_digest"]
        size = raw["size"]
        language = raw["language"]
        symbols = raw["symbols"]
        prompt_injection = raw["prompt_injection"]
        raw_signals = raw["flavor_signals"]
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or (
                language is not None and (not isinstance(language, str) or not language)
            )
            or not isinstance(symbols, list)
            or not all(isinstance(item, str) and item for item in symbols)
            or not isinstance(prompt_injection, bool)
            or not isinstance(raw_signals, list)
        ):
            raise SourceToSpecificationError(
                "inventory.record_invalid", "source inventory entry is invalid"
            )
        signals: list[FlavorSignal] = []
        for signal in raw_signals:
            if (
                not isinstance(signal, Mapping)
                or set(signal) != {"flavor_id", "axis"}
                or not isinstance(signal["flavor_id"], str)
                or not signal["flavor_id"]
                or not isinstance(signal["axis"], str)
                or not signal["axis"]
            ):
                raise SourceToSpecificationError(
                    "inventory.record_invalid",
                    "source inventory Flavor signal is invalid",
                )
            signals.append(FlavorSignal(signal["flavor_id"], signal["axis"]))
        try:
            classification = SourceFileClassification(raw["classification"])
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "inventory.record_invalid", "source inventory classification is invalid"
            ) from exc
        if symbols != sorted(set(symbols)) or signals != sorted(
            set(signals), key=lambda item: (item.axis, item.flavor_id)
        ):
            raise SourceToSpecificationError(
                "inventory.record_noncanonical",
                "source inventory entry is not canonical",
            )
        entries.append(
            SourceInventoryEntry(
                path,
                digest,
                size,
                classification,
                language,
                tuple(symbols),
                tuple(signals),
                prompt_injection,
            )
        )
    if [item.path for item in entries] != sorted({item.path for item in entries}):
        raise SourceToSpecificationError(
            "inventory.record_noncanonical", "source inventory paths are not canonical"
        )
    inventory = SourceInventory(tuple(entries), tuple(raw_excluded), policy)
    if canonical_digest(inventory) != canonical_digest(value):
        raise SourceToSpecificationError(
            "inventory.record_noncanonical", "source inventory is not canonical"
        )
    return inventory


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _python_symbols(text: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    names = {
        item.name
        for item in tree.body
        if isinstance(item, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
        and not item.name.startswith("_")
    }
    return tuple(sorted(names))


def _signals(path: str, text: str, language: str | None) -> tuple[FlavorSignal, ...]:
    searchable = f"{path}\n{text[:131072]}"
    result = {
        FlavorSignal(flavor_id, axis)
        for flavor_id, axis, pattern in _TARGET_PATTERNS
        if pattern.search(searchable)
    }
    if language is not None:
        result.add(FlavorSignal(language, "implementation.language-ecosystem"))
    return tuple(sorted(result, key=lambda item: (item.axis, item.flavor_id)))


def _classify(
    relative: str, content: bytes, size: int
) -> tuple[SourceFileClassification, str | None, str]:
    path = PurePosixPath(relative)
    name = path.name.lower()
    suffix = path.suffix.lower()
    language = _SOURCE_LANGUAGES.get(suffix)
    if (
        name in _SENSITIVE_NAMES
        or suffix in {".key", ".pem", ".p12", ".pfx"}
        or _SENSITIVE_PATTERN.search(content[:1048576])
    ):
        return SourceFileClassification.SENSITIVE, language, ""
    if b"\0" in content[:8192]:
        return SourceFileClassification.BINARY, language, ""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return SourceFileClassification.BINARY, language, ""
    if size > 2 * 1024 * 1024:
        return SourceFileClassification.LARGE, language, text
    if name.endswith((".min.js", ".min.css")) or (
        len(text) > 4096 and text.count("\n") <= 2
    ):
        return SourceFileClassification.MINIFIED, language, text
    generated_path = "generated" in {part.lower() for part in path.parts}
    if generated_path or _GENERATED_PATTERN.search(text[:8192]):
        return SourceFileClassification.GENERATED, language, text
    if (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        or "tests" in {part.lower() for part in path.parts}
    ):
        return SourceFileClassification.TEST, language, text
    if language is not None:
        return SourceFileClassification.SOURCE, language, text
    if name in _CONFIG_NAMES or suffix in {".ini", ".json", ".toml", ".yaml", ".yml"}:
        return SourceFileClassification.CONFIGURATION, None, text
    if suffix in {".md", ".rst", ".txt"}:
        return SourceFileClassification.DOCUMENTATION, None, text
    return SourceFileClassification.OTHER, None, text


def _entry(root: Path, path: Path) -> SourceInventoryEntry:
    relative = path.relative_to(root).as_posix()
    try:
        size = path.stat().st_size
        content = path.read_bytes()
    except OSError as exc:
        raise SourceToSpecificationError(
            "inventory.source_unreadable", f"source file cannot be read: {relative}"
        ) from exc
    classification, language, text = _classify(relative, content, size)
    symbols: tuple[str, ...] = ()
    analyzed_code = {
        SourceFileClassification.SOURCE,
        SourceFileClassification.TEST,
    }
    if classification in analyzed_code:
        symbols = (
            _python_symbols(text)
            if language == "python"
            else tuple(sorted(set(_GENERIC_SYMBOL.findall(text))))
        )
    return SourceInventoryEntry(
        path=relative,
        content_digest=_digest_file(path),
        size=size,
        classification=classification,
        language=language,
        symbols=symbols,
        flavor_signals=_signals(relative, text, language),
        prompt_injection=bool(_PROMPT_INJECTION_PATTERN.search(text[:1048576])),
    )


def inventory_source(source: str | Path) -> SourceInventory:
    """Inventory one file or checkout without importing or executing its contents."""

    configured = Path(source)
    if configured.is_symlink():
        raise SourceToSpecificationError(
            "inventory.source_unavailable", "source must exist and not be a symlink"
        )
    selected = configured.resolve()
    if not selected.exists():
        raise SourceToSpecificationError(
            "inventory.source_unavailable", "source must exist and not be a symlink"
        )
    if selected.is_file():
        return SourceInventory(
            entries=(_entry(selected.parent, selected),), excluded_directories=()
        )
    if not selected.is_dir():
        raise SourceToSpecificationError(
            "inventory.source_unavailable", "source must be a regular file or directory"
        )
    entries: list[SourceInventoryEntry] = []
    excluded: list[str] = []
    for current, directories, filenames in os.walk(selected, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for directory in sorted(directories):
            path = current_path / directory
            relative = path.relative_to(selected).as_posix()
            if path.is_symlink():
                raise SourceToSpecificationError(
                    "inventory.source_symlink",
                    f"analyzed source cannot contain symlinks: {relative}",
                )
            if directory in _IGNORED_DIRECTORIES:
                excluded.append(relative)
            else:
                kept_directories.append(directory)
        directories[:] = kept_directories
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(selected).as_posix()
            if path.is_symlink():
                raise SourceToSpecificationError(
                    "inventory.source_symlink",
                    f"analyzed source cannot contain symlinks: {relative}",
                )
            if path.is_file():
                entries.append(_entry(selected, path))
    if not entries:
        raise SourceToSpecificationError(
            "inventory.source_empty", "analyzed source contains no in-scope files"
        )
    return SourceInventory(
        tuple(sorted(entries, key=lambda item: item.path)),
        tuple(sorted(excluded)),
    )


__all__ = [
    "FlavorSignal",
    "SourceFileClassification",
    "SourceInventory",
    "SourceInventoryEntry",
    "inventory_source",
]
