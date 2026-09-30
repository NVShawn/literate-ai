"""Observe declared HTML without cache access, publication, network, or new clocks.

A current input hash is necessary but insufficient: compare completed bytes with
the existing pure emitter at the retained timestamp. Old provenance is parsed
independently of current markup so an older renderer can be reported as stale.
These are snapshot/race checks, not isolation from another privileged process.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.html_emitter import emit_html, load_html_inputs
from literate_ai.adapters.html_framework import (
    HtmlFrameworkObservation,
    observe_html_framework,
)
from literate_ai.adapters.html_publication import html_output_path, read_output
from literate_ai.adapters.html_surfaces import HtmlSurfaceSource
from literate_ai.contracts.html_observability import (
    HtmlProvenance,
    HtmlRendererBinding,
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlStalenessReport,
    render_inputs_identity,
)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate provenance JSON field")
        value[key] = item
    return value


class _ProvenanceParser(HTMLParser):
    """Read exactly one inline JSON provenance block, not arbitrary script text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks = 0
        self.capturing = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if any(key == "id" and value == "litai-provenance" for key, value in attrs):
            attributes = dict(attrs)
            if (
                tag != "script"
                or self.blocks
                or len(attributes) != len(attrs)
                or attributes.get("type") != "application/json"
                or "src" in attributes
            ):
                raise ValueError("provenance must be one inline JSON script")
            self.blocks += 1
            self.capturing = True

    def handle_endtag(self, tag) -> None:
        if tag == "script":
            self.capturing = False

    def handle_data(self, data) -> None:
        if self.capturing:
            self.parts.append(data)

    def provenance(self, content: bytes) -> HtmlProvenance:
        self.feed(content.decode("utf-8"))
        self.close()
        if self.blocks != 1 or self.capturing:
            raise ValueError("missing or incomplete provenance block")
        return HtmlProvenance.from_dict(
            json.loads("".join(self.parts), object_pairs_hook=_unique_object)
        )


def _inputs(
    root: Path, request: HtmlRenderRequest, framework: HtmlFrameworkObservation
) -> tuple[HtmlSurfaceSource, HtmlRendererBinding] | HtmlRenderRefusal:
    inputs = load_html_inputs(
        root,
        request,
        framework_distribution_identity=framework.framework_distribution_identity,
        schema_catalog_release=framework.schema_catalog_release,
    )
    if isinstance(inputs, HtmlRenderRefusal):
        return inputs
    return inputs.source, inputs.renderer


def verify_html_artifact(
    root: Path, request: HtmlRenderRequest
) -> HtmlStalenessReport | HtmlRenderRefusal:
    """Verify one explicit declaration; a refusal cannot invent an expected hash."""
    try:
        root = root.resolve(strict=True)
        framework = observe_html_framework()
        if isinstance(framework, HtmlRenderRefusal):
            return framework
        inputs = _inputs(root, request, framework)
        if isinstance(inputs, HtmlRenderRefusal):
            return inputs
        source, renderer = inputs
        expected = render_inputs_identity((source.binding,), request.view, renderer)

        def report(status, observed=None, labels=()):
            return HtmlStalenessReport(
                request.output_path, status, expected, observed, labels
            )

        try:
            target = html_output_path(root, request.output_path)
            snapshot = read_output(target)
        except (OSError, ValueError, RuntimeError, UnsafeFilesystemPathError):
            return report("unreadable")
        if snapshot is None:
            result = report("missing")
        else:
            try:
                provenance = _ProvenanceParser().provenance(snapshot.content)
            except (ValueError, TypeError, KeyError, RecursionError):
                provenance = None
            if provenance is None:
                result = report("unpinned")
            elif provenance.render_inputs_identity != expected:
                labels = []
                if provenance.source_bindings != (source.binding,):
                    labels.append(source.binding.source_label)
                if provenance.renderer != renderer:
                    labels.append("renderer")
                if provenance.view != request.view:
                    labels.append("view")
                result = report(
                    "stale", provenance.render_inputs_identity, tuple(labels)
                )
            else:
                emitted = emit_html(
                    root,
                    request,
                    framework_distribution_identity=framework.framework_distribution_identity,
                    schema_catalog_release=framework.schema_catalog_release,
                    generated_at=provenance.generated_at,
                )
                if isinstance(emitted, HtmlRenderRefusal):
                    return emitted
                if emitted.artifact.provenance.render_inputs_identity != expected:
                    raise ValueError("HTML inputs changed during verification")
                result = (
                    report("current", expected)
                    if emitted.content == snapshot.content
                    else report("unpinned")
                )

        if (
            observe_html_framework() != framework
            or _inputs(root, request, framework) != inputs
            or read_output(target) != snapshot
        ):
            raise ValueError("HTML inputs or artifact changed during verification")
        return result
    except (OSError, ValueError, RuntimeError, UnsafeFilesystemPathError):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "Current HTML inputs or artifact are unavailable, unsafe, "
            "or changed during verification.",
        )
