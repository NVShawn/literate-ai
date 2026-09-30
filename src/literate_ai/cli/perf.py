"""``litai perf``: report recorded per-step timing from the disposable build root."""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path
from typing import Any

from literate_ai.cache_directories import CacheDirectoryError, resolve_cache_directories
from literate_ai.perf import (
    PerformanceSpan,
    read_performance_spans,
    read_performance_spans_from_directory,
)

from .errors import CliFailure

PERF_SUMMARY_SCHEMA = "literate-ai/perf-summary@1"
PERF_CHART_SCHEMA = "literate-ai/perf-chart@1"


def _group_value(span: PerformanceSpan, group_by: str) -> str:
    value = getattr(span, group_by)
    return "(none)" if value is None else str(value)


def _filtered_spans(
    spans: list[PerformanceSpan], *, stage: str | None, run_id: str | None
) -> list[PerformanceSpan]:
    filtered = spans
    if stage is not None:
        filtered = [item for item in filtered if item.stage == stage]
    if run_id is not None:
        filtered = [item for item in filtered if item.run_id == run_id]
    return filtered


def _summarize(spans: list[PerformanceSpan], group_by: str) -> list[dict[str, object]]:
    buckets: dict[str, list[PerformanceSpan]] = {}
    for span in spans:
        buckets.setdefault(_group_value(span, group_by), []).append(span)
    groups: list[dict[str, object]] = []
    for key in sorted(buckets):
        members = buckets[key]
        durations = [item.duration_ms for item in members]
        failed = sum(1 for item in members if not item.ok)
        groups.append(
            {
                "group": key,
                "count": len(members),
                "failed": failed,
                "total_ms": sum(durations),
                "mean_ms": round(statistics.fmean(durations)) if durations else 0,
                "min_ms": min(durations) if durations else 0,
                "max_ms": max(durations) if durations else 0,
            }
        )
    groups.sort(key=lambda item: item["total_ms"], reverse=True)
    return groups


def _resolve_build_root(project: str) -> Path:
    project_root = Path(project).resolve(strict=True)
    try:
        directories = resolve_cache_directories(project_root)
    except CacheDirectoryError as exc:
        raise CliFailure("perf.build_root_unavailable", str(exc)) from exc
    return directories.obj_dir


def _resolve_spans(args: argparse.Namespace) -> tuple[Path, list[PerformanceSpan]]:
    """Read spans from ``--dir`` when given, else the project's disposable build root.

    ``--dir`` names the exact directory holding the ``*.jsonl`` span files
    directly -- the shape produced by archiving ``OBJ_DIR/.litai/perf`` before a
    clean, not a project root. This is what lets a run's performance data
    survive being reported on after the build directory that recorded it is
    long gone.
    """

    directory = getattr(args, "dir", None)
    if directory is not None:
        try:
            perf_directory = Path(directory).resolve(strict=True)
        except OSError as exc:
            raise CliFailure(
                "perf.directory_unavailable", f"{directory} is not a directory"
            ) from exc
        return perf_directory, read_performance_spans_from_directory(perf_directory)
    build_root = _resolve_build_root(args.project)
    return build_root, read_performance_spans(build_root)


def _render_table(groups: list[dict[str, object]], group_by: str) -> str:
    if not groups:
        return "No recorded performance spans found.\n"
    headers = (group_by, "count", "failed", "total_ms", "mean_ms", "min_ms", "max_ms")
    rows = [
        (
            str(item["group"]),
            str(item["count"]),
            str(item["failed"]),
            str(item["total_ms"]),
            str(item["mean_ms"]),
            str(item["min_ms"]),
            str(item["max_ms"]),
        )
        for item in groups
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(
            header.ljust(width) for header, width in zip(headers, widths, strict=True)
        ),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _handle_show(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    source, spans = _resolve_spans(args)
    spans = _filtered_spans(spans, stage=args.stage, run_id=args.run_id)
    groups = _summarize(spans, args.group_by)
    return {
        "schema": PERF_SUMMARY_SCHEMA,
        "build_root": str(source),
        "group_by": args.group_by,
        "span_count": len(spans),
        "groups": groups,
        "table": _render_table(groups, args.group_by),
    }, 0


def _svg_bar_chart(groups: list[dict[str, object]], group_by: str) -> str:
    """A minimal, dependency-free horizontal bar chart of total_ms per group."""

    bar_height = 28
    label_width = 260
    chart_width = 640
    margin = 16
    height = margin * 2 + bar_height * max(len(groups), 1)
    width = margin * 2 + label_width + chart_width
    maximum = max((item["total_ms"] for item in groups), default=1) or 1
    bars = []
    for index, item in enumerate(groups):
        y = margin + index * bar_height
        bar_len = round((item["total_ms"] / maximum) * chart_width)
        label = f"{item['group']} ({item['total_ms']} ms, n={item['count']})"
        bars.append(
            f'<text x="{margin}" y="{y + bar_height * 0.65:.1f}" '
            f'font-family="monospace" font-size="12">{_xml_escape(label)}</text>'
        )
        bars.append(
            f'<rect x="{margin + label_width}" y="{y + 4}" '
            f'width="{max(bar_len, 1)}" height="{bar_height - 8}" fill="#2f6fed" />'
        )
    body = "\n".join(bars)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'<rect width="{width}" height="{height}" fill="#ffffff" />\n'
        f"{body}\n"
        "</svg>\n"
    )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _handle_chart(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    source, spans = _resolve_spans(args)
    spans = _filtered_spans(spans, stage=args.stage, run_id=args.run_id)
    groups = _summarize(spans, args.group_by)
    svg = _svg_bar_chart(groups, args.group_by)
    destination = Path(args.output)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(svg, encoding="utf-8")
    except OSError as exc:
        raise CliFailure("perf.chart_write_failed", str(exc)) from exc
    return {
        "schema": PERF_CHART_SCHEMA,
        "build_root": str(source),
        "group_by": args.group_by,
        "span_count": len(spans),
        "output": str(destination),
    }, 0


def perf_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.perf_command == "show":
        return _handle_show(args)
    if args.perf_command == "chart":
        return _handle_chart(args)
    raise CliFailure(
        "perf.command_unknown", f"unknown perf command {args.perf_command!r}"
    )


def _render_show(result: dict[str, Any]) -> str:
    return str(result["table"])


def _render_chart(result: dict[str, Any]) -> str:
    return (
        f"Wrote {result['span_count']} span(s) grouped by {result['group_by']} to "
        f"{result['output']}\n"
    )


PERF_HUMAN_RENDERERS = {
    "perf.show": _render_show,
    "perf.chart": _render_chart,
}

__all__ = ["perf_from_args", "PERF_HUMAN_RENDERERS"]
