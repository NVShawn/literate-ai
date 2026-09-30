"""Deterministic spec↔source maps for `--debug` generation and runtime tracing.

Coding CLIs place `litai:spec PATH:LINE [kind]` anchors. This module assigns
generated line numbers by scanning those comments and writes a sidecar. A tiny
Python helper, copied into generated trees, prints maps on stderr when
``LITAI_DEBUG`` is set. Generated application stdout stays the product JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts.identity import ContentIdentity, ContentReference
from literate_ai.contracts.skills import ResolvedSpecificationToSourceSkill
from literate_ai.diagnostics import debug_enabled, emit_log_event

SPEC_MAP_SCHEMA = "literate-ai/spec-source-map@1"
SPEC_MAP_RELATIVE = Path(".literate") / "spec-map.json"
PYTHON_HELPER_NAME = "litai_debug.py"
DEBUG_SKILL_ID = "debug-spec-map"
DEBUG_SKILL_URI = "skills/specification-to-source/debug-spec-map/SKILL.md"

_ANCHOR = re.compile(
    r"litai:spec\s+(?P<path>\S+):(?P<line>\d+)(?:\s+(?P<kind>[A-Za-z][\w-]*)?)?"
)
_SKIP_DIRECTORY_NAMES = frozenset(
    {
        "build",
        "dist",
        "out",
        "target",
        "obj",
        "bin",
        "coverage",
        "vendor",
        "node_modules",
        "__pycache__",
        ".git",
    }
)
_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".cpp",
        ".cc",
        ".c",
        ".h",
    }
)

DEBUG_GENERATION_INSTRUCTIONS = """\
When debug instrumentation is requested, place a `litai:spec PATH:LINE [kind]`
anchor in a language comment immediately above each public entrypoint and each
specified behavior block. PATH is the Component-relative specification file
(for example `samples/hello-component/component.md`). LINE is the 1-based line
of the requirement in that file, copied from the specification in this prompt.
kind is `entrypoint`, `behavior`, or `error`. Do not invent line numbers for
generated code — the framework scanner records those. Do not print these maps
on standard output; the product JSON result stays on stdout. Runtime tracing
is env-gated (`LITAI_DEBUG`) and belongs on stderr only.
"""

PYTHON_RUNTIME_HELPER = '''\
"""Env-gated spec-map tracing for generated Python applications.

No-op unless LITAI_DEBUG is set. Never writes to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

_SCHEMA = "literate-ai/debug-event@1"
_SEEN: set[tuple[str, int]] = set()
_MAP: list[dict] | None = None
_INSTALLED = False


def _enabled() -> bool:
    return os.environ.get("LITAI_DEBUG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _json_mode() -> bool:
    if os.environ.get("LITAI_DEBUG_JSON", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    stream = sys.stderr
    try:
        return not bool(stream.isatty())
    except (AttributeError, OSError):
        return True


def _map_path() -> Path:
    override = os.environ.get("LITAI_SPEC_MAP", "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    return here / ".literate" / "spec-map.json"


def _load_map() -> list[dict]:
    global _MAP
    if _MAP is not None:
        return _MAP
    path = _map_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _MAP = []
        return _MAP
    entries = document.get("entries") if isinstance(document, dict) else None
    _MAP = list(entries) if isinstance(entries, list) else []
    return _MAP


def _emit(entry: dict, *, message: str = "") -> None:
    spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
    generated = (
        entry.get("generated") if isinstance(entry.get("generated"), dict) else {}
    )
    kind = str(entry.get("kind") or "behavior")
    spec_path = str(spec.get("path") or "")
    spec_line = spec.get("line")
    generated_path = str(generated.get("path") or "")
    generated_line = generated.get("line")
    payload = {
        "schema": _SCHEMA,
        "event": "spec-map",
        "stage": "spec-map",
        "spec": {"path": spec_path, "line": spec_line},
        "generated": {"path": generated_path, "line": generated_line},
        "kind": kind,
        "message": message,
    }
    if _json_mode():
        sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    else:
        sys.stderr.write(
            f"[litai:map] {spec_path}:{spec_line}:"
            f"{generated_path}:{generated_line} {kind}"
        )
        if message:
            sys.stderr.write(f" {message}")
        sys.stderr.write("\\n")
    sys.stderr.flush()


def _entry_for_frame(filename: str, lineno: int) -> dict | None:
    path = Path(filename)
    candidates: list[tuple[int, dict]] = []
    for entry in _load_map():
        generated = (
            entry.get("generated") if isinstance(entry.get("generated"), dict) else {}
        )
        generated_path = str(generated.get("path") or "")
        generated_line = generated.get("line")
        if not isinstance(generated_line, int):
            continue
        mapped = Path(generated_path)
        if mapped.name != path.name and str(path) != generated_path:
            if path.as_posix().endswith("/" + generated_path.lstrip("./")):
                pass
            else:
                continue
        if generated_line <= lineno:
            candidates.append((generated_line, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _emit_once(entry: dict, *, message: str = "") -> None:
    generated = (
        entry.get("generated") if isinstance(entry.get("generated"), dict) else {}
    )
    key = (str(generated.get("path") or ""), int(generated.get("line") or 0))
    if key in _SEEN:
        return
    _SEEN.add(key)
    _emit(entry, message=message)


def trace_entrypoint() -> None:
    """Emit one map hit for the enclosing public entrypoint. No-op if disabled."""

    if not _enabled():
        return
    frame = sys._getframe(1)
    entry = _entry_for_frame(frame.f_code.co_filename, frame.f_lineno)
    if entry is None:
        for item in _load_map():
            if item.get("kind") == "entrypoint":
                entry = item
                break
    if entry is not None:
        _emit_once(entry, message="public invocation")


def _excepthook(exc_type, exc, tb) -> None:
    if _enabled() and tb is not None:
        chosen = None
        for frame, lineno in traceback.walk_tb(tb):
            found = _entry_for_frame(frame.f_code.co_filename, lineno)
            if found is not None:
                chosen = found
        if chosen is None:
            for item in _load_map():
                if item.get("kind") == "error":
                    chosen = item
                    break
        if chosen is not None:
            _emit_once(
                chosen,
                message=f"{getattr(exc_type, '__name__', type(exc).__name__)}: {exc}",
            )
    sys.__excepthook__(exc_type, exc, tb)


def install() -> None:
    """Install the exception hook. Safe to call more than once."""

    global _INSTALLED
    if _INSTALLED or not _enabled():
        return
    sys.excepthook = _excepthook
    _INSTALLED = True
'''


@dataclass(frozen=True, slots=True)
class SpecMapEntry:
    spec_path: str
    spec_line: int
    generated_path: str
    generated_line: int
    kind: str

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": {"path": self.spec_path, "line": self.spec_line},
            "generated": {"path": self.generated_path, "line": self.generated_line},
            "kind": self.kind,
        }


def _debug_skill_path() -> Path:
    return (
        Path(__file__)
        .resolve()
        .parent.joinpath("project_template", *DEBUG_SKILL_URI.split("/"))
    )


def debug_spec_map_skill_bytes() -> bytes:
    return _debug_skill_path().read_bytes()


def load_debug_spec_map_skill() -> ResolvedSpecificationToSourceSkill | None:
    """Load the packaged debug-spec-map skill when `--debug` is active."""

    if not debug_enabled():
        return None
    path = _debug_skill_path()
    if not path.is_file():
        return None
    content = path.read_bytes()
    identity = ContentIdentity.parse_uri(
        f"sha256:{hashlib.sha256(content).hexdigest()}"
    )
    reference = ContentReference(
        "specification-to-source-skill",
        DEBUG_SKILL_URI,
        identity,
    )
    return ResolvedSpecificationToSourceSkill.from_reference(
        reference,
        content,
        source="debug instrumentation",
    )


def with_debug_skill_identities(
    identities: Sequence[ContentIdentity],
) -> tuple[ContentIdentity, ...]:
    """Append the debug-spec-map identity when debug is on; keep canonical order."""

    current = tuple(identities)
    skill = load_debug_spec_map_skill()
    if skill is None:
        return current
    extra = skill.content_identity
    if extra.uri in {item.uri for item in current}:
        return current
    return tuple(sorted((*current, extra), key=lambda item: item.uri))


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(
            part in _SKIP_DIRECTORY_NAMES or part.startswith(".")
            for part in relative.parts[:-1]
        ):
            continue
        if path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        yield path


def scan_spec_anchors(source_root: Path) -> tuple[SpecMapEntry, ...]:
    """Assign generated line numbers from `litai:spec` comments in a source tree."""

    root = Path(source_root)
    entries: list[SpecMapEntry] = []
    for path in _iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root).as_posix()
        for index, line in enumerate(text.splitlines(), start=1):
            match = _ANCHOR.search(line)
            if match is None:
                continue
            kind = (match.group("kind") or "behavior").strip() or "behavior"
            entries.append(
                SpecMapEntry(
                    spec_path=match.group("path"),
                    spec_line=int(match.group("line")),
                    generated_path=relative,
                    generated_line=index,
                    kind=kind,
                )
            )
    return tuple(entries)


def spec_map_document(entries: Sequence[SpecMapEntry]) -> dict[str, object]:
    return {
        "schema": SPEC_MAP_SCHEMA,
        "entries": [item.to_dict() for item in entries],
    }


def write_spec_map(source_root: Path, entries: Sequence[SpecMapEntry]) -> Path:
    """Write the sidecar beside the source tree (`<root>/.literate/spec-map.json`)."""

    destination = source_root / SPEC_MAP_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = spec_map_document(entries)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination


def install_python_runtime_helper(source_root: Path) -> Path:
    destination = source_root / PYTHON_HELPER_NAME
    destination.write_text(PYTHON_RUNTIME_HELPER, encoding="utf-8", newline="\n")
    return destination


def instrument_generated_tree(source_root: Path) -> Mapping[str, Any]:
    """Scan anchors, write the sidecar, and install the Python helper.

    Missing maps are reported when debug is on; they never fail the lifecycle.
    """

    root = Path(source_root)
    if not root.is_dir():
        if debug_enabled():
            emit_log_event(
                "spec-map.unavailable",
                stage="spec-map",
                message="generated source tree is not a directory",
                path=str(root),
            )
        return {"schema": SPEC_MAP_SCHEMA, "entries": [], "unavailable": True}
    entries = scan_spec_anchors(root)
    sidecar = write_spec_map(root, entries)
    helper = None
    if any(path.suffix.casefold() == ".py" for path in _iter_source_files(root)):
        helper = install_python_runtime_helper(root)
    if debug_enabled() and not entries:
        emit_log_event(
            "spec-map.unavailable",
            stage="spec-map",
            message="no litai:spec anchors in generated source",
            path=str(root),
        )
    elif debug_enabled():
        emit_log_event(
            "spec-map.written",
            stage="spec-map",
            path=str(sidecar),
            entries=len(entries),
        )
    report: dict[str, Any] = {
        "schema": SPEC_MAP_SCHEMA,
        "sidecar": str(sidecar),
        "entries": [item.to_dict() for item in entries],
    }
    if helper is not None:
        report["python_helper"] = str(helper)
    return report


__all__ = [
    "DEBUG_GENERATION_INSTRUCTIONS",
    "DEBUG_SKILL_ID",
    "DEBUG_SKILL_URI",
    "PYTHON_HELPER_NAME",
    "PYTHON_RUNTIME_HELPER",
    "SPEC_MAP_RELATIVE",
    "SPEC_MAP_SCHEMA",
    "SpecMapEntry",
    "debug_spec_map_skill_bytes",
    "instrument_generated_tree",
    "load_debug_spec_map_skill",
    "scan_spec_anchors",
    "with_debug_skill_identities",
]
