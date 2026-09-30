"""Mechanical, structural detection of declared-but-unimplemented Component surface.

Issue #64: a green ``litai build``/``litai test`` is not evidence the generated
implementation does what the specification describes, because the same coding-CLI
call that writes the implementation also writes its own tests -- tests only exercise
whatever surface that call decided to implement.

This is a narrow, honest MVP, not a coverage-completeness system. The Component
schema (``literate_ai.contracts.components``) only makes two things structurally
machine-readable today: declared ``Entrypoint`` records (name/kind/path) and declared
``Capability`` records (``provides``). There is no structured field for HTTP routes,
MCP tools, or prose-stated invariants -- extracting those mechanically from free
specification prose is a much harder problem this module does not attempt.

A per-entrypoint *path-scoped* scan was the original design, but ``Entrypoint.path``
turns out to be a purely declarative/logical value in this framework, not a
generated-source-tree lookup path: nothing in ``standard_project.py`` resolves a file
or directory at that path inside the generated ``sources/`` tree (it is only echoed
into an identity dict alongside ``entrypoint.kind``). Scoping the scan to
``source_root / entrypoint.path`` therefore checks essentially arbitrary locations
against most real Standard-lifecycle-generated layouts, which vary by
language/build-system Flavor. This module instead scans the *whole* generated source
tree for common stub markers -- less precisely targeted than a true per-entrypoint
check would be, but actually correct against how source is really laid out, rather
than falsely precise against a location that usually doesn't exist.

This catches the exact shape of one of the two real-world examples in #64 (route
handlers literally set to ``None``) but not the other (a worker that never calls its
own upstream client) -- that failure mode is a dead-code/call-graph question, a
fundamentally different and heavier static-analysis feature, explicitly deferred
rather than faked here. Direction (B), an adversarial second coding-CLI pass, is
evaluated in ``docs/history/roadmap/coverage-gap-detection.md``.

After characterization against checked-in fixture and sample source (not live
generation traffic), high-confidence markers fail the build: path-keyed handler
tables bound to ``None``, and ``NotImplementedError`` in generated product files.
``TODO``/``FIXME`` comments and ``pass``-bodied ``@abstractmethod`` methods stay
advisory. A string-keyed ``None`` that is not a ``/``-prefixed route or handler
path is not treated as a handler table. See COVERAGE-GAP-001.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts import Entrypoint, canonical_identity

COVERAGE_GAP_REPORT_SCHEMA = "literate-ai/coverage-gap-report@3"
GATE_FAIL_CLOSED = "fail_closed"
GATE_ADVISORY = "advisory"
UNIMPLEMENTED_SURFACE_CODE = "build.unimplemented_surface"

_STUB_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("not_implemented", re.compile(r"\bNotImplementedError\b")),
    ("todo_comment", re.compile(r"(?:#|//)\s*(?:TODO|FIXME)\b", re.IGNORECASE)),
    (
        "none_bound_handler",
        re.compile(r"""["']/[A-Za-z0-9_{}().:/-]{1,255}["']\s*:\s*None\b"""),
    ),
    (
        "pass_bodied_abstract",
        re.compile(
            r"@abstractmethod\b[\s\S]{0,240}?\bdef\s+\w+\s*\([^:\n]*\)"
            r"(?:\s*->[^:\n]+)?:\s*(?:pass|\.\.\.)\b"
        ),
    ),
)

_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}
)
_TEST_DIRECTORY_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_FILE_NAME = re.compile(
    r"^(?:test_[^.]+|[^.]+_test|[^.]+_spec|[^.]+(?:\.test|\.spec)(?:\.[^.]+)?)\.[^.]+$",
    re.IGNORECASE,
)

_MAXIMUM_FILE_BYTES = 2 * 1024 * 1024
_MAXIMUM_FILES_SCANNED = 4096
_MAXIMUM_GAPS = 256
_MAXIMUM_DETAIL_CHARS = 1024
_MAXIMUM_PATH_CHARS = 1024
_COVERAGE_GAP_REASONS = frozenset(reason for reason, _pattern in _STUB_MARKER_PATTERNS)
_COVERAGE_GAP_GATES = frozenset({GATE_FAIL_CLOSED, GATE_ADVISORY})


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """One finding: a generated source file that looks stubbed."""

    file_path: str
    reason: str
    detail: str
    gate: str = GATE_ADVISORY

    def to_dict(self) -> dict[str, str]:
        return {
            "file_path": self.file_path,
            "reason": self.reason,
            "detail": self.detail,
            "gate": self.gate,
        }

    @classmethod
    def from_dict(cls, value: object, *, path: str = "CoverageGap") -> CoverageGap:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be an object")
        try:
            file_path = str(value["file_path"])
            reason = str(value["reason"])
            detail = str(value["detail"])
            gate = str(value["gate"])
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{path} is missing a required field") from exc
        if reason not in _COVERAGE_GAP_REASONS or gate not in _COVERAGE_GAP_GATES:
            raise ValueError(f"{path} has an unknown reason or gate")
        if not file_path or not detail:
            raise ValueError(f"{path} must carry a file path and detail")
        return cls(file_path, reason, detail, gate)


@dataclass(frozen=True, slots=True)
class CoverageGapReport:
    """Typed, content-addressed evidence for one coverage-gap scan."""

    entrypoint_count: int
    files_scanned: int
    gaps: tuple[CoverageGap, ...]

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": COVERAGE_GAP_REPORT_SCHEMA,
                "entrypoint_count": self.entrypoint_count,
                "files_scanned": self.files_scanned,
                "gaps": [gap.to_dict() for gap in self.gaps],
            }
        )

    @property
    def blocking_gaps(self) -> tuple[CoverageGap, ...]:
        return tuple(gap for gap in self.gaps if gap.gate == GATE_FAIL_CLOSED)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": COVERAGE_GAP_REPORT_SCHEMA,
            "entrypoint_count": self.entrypoint_count,
            "files_scanned": self.files_scanned,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "identity": self.identity.uri,
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "CoverageGapReport"
    ) -> CoverageGapReport:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be an object")
        if value.get("schema") != COVERAGE_GAP_REPORT_SCHEMA:
            raise ValueError(f"{path}.schema must be {COVERAGE_GAP_REPORT_SCHEMA!r}")
        try:
            entrypoint_count = int(value["entrypoint_count"])
            files_scanned = int(value["files_scanned"])
            raw_gaps = value["gaps"]
            identity = str(value["identity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path} is missing a required field") from exc
        if isinstance(raw_gaps, (str, bytes)) or not isinstance(
            raw_gaps, (tuple, list)
        ):
            raise ValueError(f"{path}.gaps must be an array")
        gaps = tuple(
            CoverageGap.from_dict(item, path=f"{path}.gaps[{index}]")
            for index, item in enumerate(raw_gaps)
        )
        report = cls(entrypoint_count, files_scanned, gaps)
        if report.identity.uri != identity:
            raise ValueError(f"{path}.identity does not match the canonical payload")
        return report


def is_generated_product_file(relative_path: str) -> bool:
    """Return whether a relative path looks like product source rather than tests."""

    parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    if not parts:
        return False
    if any(part in _TEST_DIRECTORY_NAMES for part in parts[:-1]):
        return False
    return _TEST_FILE_NAME.match(parts[-1]) is None


def _gate_for(reason: str, relative_path: str) -> str:
    if reason == "none_bound_handler":
        return GATE_FAIL_CLOSED
    if reason == "not_implemented" and is_generated_product_file(relative_path):
        return GATE_FAIL_CLOSED
    return GATE_ADVISORY


def _clip(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 3] + "..."


def _source_files(source_root: Path) -> list[Path]:
    if not source_root.is_dir():
        return []
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES
    )[:_MAXIMUM_FILES_SCANNED]


def find_coverage_gaps(
    entrypoints: tuple[Entrypoint, ...], source_root: Path
) -> CoverageGapReport:
    """Scan the whole generated source tree for common stub markers.

    High-confidence markers (``none_bound_handler``, and ``not_implemented`` in
    generated product files) are ``fail_closed``. ``TODO``/``FIXME`` and pass-bodied
    abstracts stay advisory. Findings are file-level, not bound to a specific
    declared entrypoint -- see the module docstring for why.
    """

    source_root = Path(source_root)
    files = _source_files(source_root)
    gaps: list[CoverageGap] = []
    for file in files:
        try:
            if file.stat().st_size > _MAXIMUM_FILE_BYTES:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            relative = file.relative_to(source_root).as_posix()
        except ValueError:
            relative = file.name
        relative = _clip(relative, _MAXIMUM_PATH_CHARS)
        for reason, pattern in _STUB_MARKER_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            gaps.append(
                CoverageGap(
                    relative,
                    reason,
                    _clip(f"{match.group(0)!r} at {relative}", _MAXIMUM_DETAIL_CHARS),
                    _gate_for(reason, relative),
                )
            )
            if len(gaps) >= _MAXIMUM_GAPS:
                return CoverageGapReport(len(entrypoints), len(files), tuple(gaps))
    return CoverageGapReport(len(entrypoints), len(files), tuple(gaps))


def scan_runtime_coverage_gaps(
    project_root: Path, component: str, runtime_root: object
) -> CoverageGapReport | None:
    """Best-effort scan of a lifecycle runtime's generated ``sources/`` tree.

    Returns ``None`` when there is no generated source to inspect. Authoring parse
    defects degrade to an empty entrypoint list rather than suppressing the scan:
    stub markers in generated source are still reportable. Scan-side I/O defects
    return ``None`` so they cannot fail or obscure a real build.
    """

    if not runtime_root:
        return None
    from literate_ai.adapters.component_markdown import (
        ComponentMarkdownError,
        parse_component_markdown,
    )

    sources = Path(str(runtime_root)) / "sources"
    if not sources.is_dir():
        return None
    entrypoints: tuple[Entrypoint, ...] = ()
    component_path = (Path(project_root) / component / "component.md").resolve()
    try:
        if component_path.is_file():
            text = component_path.read_text(encoding="utf-8")
            authoring = parse_component_markdown(
                component_path, text, project_root=Path(project_root)
            )
            entrypoints = authoring.entrypoints
    except (OSError, UnicodeError, ComponentMarkdownError):
        entrypoints = ()
    try:
        return find_coverage_gaps(entrypoints, sources)
    except OSError:
        return None


def unimplemented_surface_message(
    report: CoverageGapReport | Mapping[str, Any] | None,
) -> str | None:
    """Return a fail-closed message when high-confidence stub markers are present."""

    if report is None:
        return None
    parsed: CoverageGapReport
    if isinstance(report, CoverageGapReport):
        parsed = report
    else:
        try:
            parsed = CoverageGapReport.from_dict(report)
        except (TypeError, ValueError):
            return None
    blocking = parsed.blocking_gaps
    if not blocking:
        return None
    rendered = "; ".join(f"{gap.reason} in {gap.file_path}" for gap in blocking)
    return f"generated source has unimplemented surface ({rendered})"


__all__ = [
    "COVERAGE_GAP_REPORT_SCHEMA",
    "CoverageGap",
    "CoverageGapReport",
    "GATE_ADVISORY",
    "GATE_FAIL_CLOSED",
    "UNIMPLEMENTED_SURFACE_CODE",
    "find_coverage_gaps",
    "is_generated_product_file",
    "scan_runtime_coverage_gaps",
    "unimplemented_surface_message",
]
