"""Bounded static directory pages from a canonical composed-tree observation.

This generator performs no filesystem writes and grants no publication authority.
Its consumer must stage every page, verify the complete generation, and revalidate
source and output custody before making any index visible. Partial iteration is
never a complete artifact. Unit provenance is not inferred from directories.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import PurePosixPath

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.native_tree_shards import ComposedNativeTree

from .html_emitter import _STYLE, _definition
from .html_tree_projection import project_html_tree


@dataclass(frozen=True, slots=True)
class HtmlTreePage:
    path: str
    directory: str
    ordinal: int
    commitment: BlobRef
    content: bytes


class _PageBuffer:
    def __init__(self, limits, already_generated):
        self.limits = limits
        self.already_generated = already_generated
        self.parts = []
        self.size = 0
        self.links = set()

    def append(self, text):
        content = text.encode("utf-8")
        if self.size + len(content) > self.limits.max_page_bytes:
            raise ValueError("html.tree.page-bytes-policy-exceeded")
        if (
            self.already_generated + self.size + len(content)
            > self.limits.max_total_generated_bytes
        ):
            raise ValueError("html.tree.generated-bytes-policy-exceeded")
        self.parts.append(content)
        self.size += len(content)

    def link(self, target, label):
        self.links.add(target)
        return f'<a href="{escape(target, quote=True)}">{escape(label)}</a>'


def directory_page_path(coordinate: str, ordinal: int = 0) -> str:
    """Generated filenames never interpolate source names or host paths."""
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("html.tree.invalid-page-ordinal")
    digest = hashlib.sha256(coordinate.encode("utf-8")).hexdigest()
    return f"d-{digest}-{ordinal}.html"


class _StaticInspection(HTMLParser):
    """Closed generated markup, with links only to selected generation pages."""

    _TAGS = frozenset(
        "html head meta title style body main header footer p h1 h2 "
        "strong nav a section dl dt dd code ul li".split()
    )

    def __init__(self, links):
        super().__init__(convert_charrefs=True)
        self.links = links
        self.stack = []
        self.doctypes = 0
        self.styles = []

    def handle_decl(self, decl):
        if decl.lower() != "doctype html":
            raise ValueError("html.tree.unsupported-declaration")
        self.doctypes += 1

    def handle_comment(self, data):
        raise ValueError("html.tree.unexpected-comment")

    def handle_startendtag(self, tag, attrs):
        raise ValueError("html.tree.unexpected-self-closing-tag")

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in self._TAGS or len(attributes) != len(attrs):
            raise ValueError("html.tree.unexpected-markup")
        if tag == "html":
            valid = attributes == {"lang": "en"}
        elif tag == "meta":
            valid = attributes in (
                {"charset": "utf-8"},
                {"name": "viewport", "content": "width=device-width, initial-scale=1"},
            )
        elif tag == "nav":
            valid = attributes == {"aria-label": "Directory navigation"}
        elif tag == "a":
            valid = set(attributes) == {"href"} and attributes["href"] in self.links
        else:
            valid = not attributes
        if not valid:
            raise ValueError("html.tree.unexpected-attribute-or-navigation")
        if tag != "meta":
            self.stack.append(tag)
        if tag == "style":
            self.styles.append("")

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "style":
            self.styles[-1] += data

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("html.tree.unbalanced-markup")


def emit_html_tree_pages(
    tree: ComposedNativeTree, request: HtmlTreeRenderRequest, *, reserved_memory_bytes=0
):
    """Yield all directory pages, refusing rather than truncating any group.

    Page data is bounded before each append. Output is charged before yielding;
    only one rendered page is retained here. Canonical projection metadata has
    its own explicit allocation and descriptor limits. A later refusal requires
    the publication owner to retain an unpublished staging attempt, not expose
    previously yielded pages as a complete generation.
    """
    if not isinstance(tree, ComposedNativeTree) or not isinstance(
        request, HtmlTreeRenderRequest
    ):
        raise TypeError("html.tree.typed-observation-and-request-required")
    limits = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.invalid-memory-reservation")
    # Reserve the secondary lookup, bounded decoded records/navigation and all
    # copies of the current page before constructing the retained projection.
    # A tree has at most one directory record per member plus root, and no more
    # nonempty child groups than source members. This is an allocation estimate,
    # not a claim about measured process RSS.
    rendering_memory = 16 * limits.max_page_bytes + 8192 * (tree.index.member_count + 1)
    projection = project_html_tree(
        tree,
        request,
        reserved_memory_bytes=reserved_memory_bytes + rendering_memory,
    )
    records = {reference.identity: raw for reference, raw in projection.records}
    generated_bytes = 0
    page_count = 0
    for reference, raw in projection.records:
        record = json.loads(raw)
        if record["schema"] != "literate-ai/retained-tree-html-directory@1":
            continue
        coordinate = record["coordinate"]
        groups = record["groups"]
        for ordinal in range(record["page_count"]):
            path = directory_page_path(coordinate, ordinal)
            buffer = _PageBuffer(limits, generated_bytes)
            append = buffer.append
            link = buffer.link

            append(
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{escape(coordinate)} — Directory information</title>"
                f"<style>{_STYLE}</style></head><body><main><header>"
                f"<h1>{escape(coordinate)}</h1><p>Directory information</p></header>"
                '<nav aria-label="Directory navigation">'
            )
            append(link(directory_page_path("."), "Tree root"))
            if coordinate != ".":
                append(
                    link(
                        directory_page_path(
                            PurePosixPath(coordinate).parent.as_posix()
                        ),
                        "Parent",
                    )
                )
            if ordinal:
                append(
                    link(directory_page_path(coordinate, ordinal - 1), "Previous page")
                )
            if ordinal + 1 < record["page_count"]:
                append(link(directory_page_path(coordinate, ordinal + 1), "Next page"))
            append(
                "</nav><p>Metadata only; file contents are not included. "
                "A directory is not an admitted Component or package.</p><dl>"
                + _definition("Children", str(record["child_count"]))
                + _definition("Page", f"{ordinal + 1} of {record['page_count']}")
                + _definition("Observed tree", record["source_identity"])
                + _definition("Render request", record["request_identity"])
                + _definition("Directory commitment", reference.identity)
                + "</dl><section><h2>Children</h2><ul>"
            )
            start = ordinal * limits.max_groups_per_page
            for group_value in groups[start : start + limits.max_groups_per_page]:
                group_reference = BlobRef.from_dict(group_value)
                group = json.loads(records[group_reference.identity])
                for child in group["children"]:
                    name = PurePosixPath(child["coordinate"]).name
                    label = (
                        link(directory_page_path(child["coordinate"]), name)
                        if child["kind"] == "directory"
                        else escape(name)
                    )
                    target = (
                        ""
                        if child["target"] is None
                        else "<p>Target: <code>"
                        + escape(child["target"])
                        + "</code></p>"
                    )
                    append(
                        f"<li><strong>{label}</strong> · {escape(child['kind'])}"
                        + target
                        + f"<p><code>{escape(child['identity'])}</code></p></li>"
                    )
            if record["child_count"] == 0:
                append("<li>No children in the captured directory.</li>")
            append(
                "</ul></section><footer>Regenerable metadata view. "
                "This page grants no source, execution or publication authority."
                "</footer></main></body></html>\n"
            )
            content = b"".join(buffer.parts)
            parser = _StaticInspection(buffer.links)
            parser.feed(content.decode("utf-8"))
            parser.close()
            if parser.doctypes != 1 or parser.stack or parser.styles != [_STYLE]:
                raise ValueError("html.tree.incomplete-or-foreign-page")
            generated_bytes += len(content)
            page_count += 1
            yield HtmlTreePage(path, coordinate, ordinal, reference, content)
    if page_count != projection.page_count:
        raise ValueError("html.tree.incomplete-page-generation")
