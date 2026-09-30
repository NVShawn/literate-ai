"""Bind HTML rendering to the imported wheel and the catalog actually in use.

The existing Standard observer owns wheel inventory and distribution identity.
This adapter adds import-location and catalog-byte correspondence; it does not
invent an identity for source checkouts or editable installations. Call again
before publication and require the same observation. Like other in-process
observers, this trusts the interpreter and installed code, not an OS sandbox.
"""

from __future__ import annotations

import importlib.metadata
import sys
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.html_source_excerpts import _read_bound_source
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
)
from literate_ai.contracts.html_observability import HtmlRenderRefusal
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.diagnostics import debug_stage
from literate_ai.schema_catalog import SchemaCatalogError, schema_catalog_root
from literate_ai.version import SCHEMA_CATALOG_RELEASES


@dataclass(frozen=True, slots=True)
class HtmlFrameworkObservation:
    framework_distribution_identity: ContentIdentity
    schema_catalog_release: str


def _imported_framework_files() -> tuple[Path, ...]:
    paths = []
    for name, module in tuple(sys.modules.items()):
        if name == "literate_ai" or name.startswith("literate_ai."):
            if module is None:
                continue
            filename = getattr(module, "__file__", None)
            if not filename:
                raise ValueError("imported framework module has no file binding")
            paths.append(Path(filename).resolve(strict=True))
    return tuple(paths)


def observe_html_framework() -> HtmlFrameworkObservation | HtmlRenderRefusal:
    """Observe real installation authority without fetching or writing anything."""
    try:
        package = sys.modules["literate_ai"]
        imported_init = Path(package.__file__).resolve(strict=True)
        candidates = tuple(
            distribution
            for distribution in importlib.metadata.distributions(name="literate-ai")
            if Path(distribution.locate_file("literate_ai/__init__.py")).resolve(
                strict=True
            )
            == imported_init
        )
        # Ambiguous metadata remains an error even when it names the same files.
        if len(candidates) != 1:
            raise ValueError("one distribution must supply the imported framework")
        distribution = candidates[0]
        with debug_stage("html.framework.distribution"):
            observed = observe_installed_framework_distribution(
                distribution_finder=lambda _name: candidates
            )
        members = {member.path: member for member in observed.members}
        package_parent = imported_init.parent.parent
        for imported in _imported_framework_files():
            logical = imported.relative_to(package_parent).as_posix()
            if (
                logical not in members
                or Path(distribution.locate_file(logical)).resolve(strict=True)
                != imported
            ):
                raise ValueError("imported module is not in the observed wheel")

        for version in ("v1", "v2"):
            root = schema_catalog_root(version)
            prefix = f"share/literate-ai/schemas/{version}/"
            selected = {
                name.removeprefix(prefix): member
                for name, member in members.items()
                if name.startswith(prefix)
            }
            if not selected or set(selected) != {path.name for path in root.iterdir()}:
                raise ValueError("active catalog differs from the wheel inventory")
            with debug_stage("html.framework.catalog", version=version):
                for relative, member in selected.items():
                    _read_bound_source(
                        root,
                        relative,
                        ContentIdentity.parse_uri(member.digest),
                        member.size,
                    )
        return HtmlFrameworkObservation(
            observed.identity, SCHEMA_CATALOG_RELEASES["v2"]
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StandardLifecycleBindingError,
        SchemaCatalogError,
        UnsafeFilesystemPathError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "HTML rendering requires the imported non-editable framework wheel "
            "and its exact schema catalog; installation authority is unavailable "
            "or changed.",
        )
