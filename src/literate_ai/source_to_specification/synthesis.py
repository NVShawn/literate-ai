"""Provider-valid synthesis shared by static and model-backed inverse workflows."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from literate_ai.adapters.specifications import OpenSpecError, OpenSpecProvider
from literate_ai.adapters.specifications.literate_markdown import (
    LiterateMarkdownProvider,
)

from .contracts import (
    ComponentCapabilityContractDraft,
    ComponentGraphDraft,
    ComponentGraphNodeDraft,
    DraftArtifact,
    DraftStatement,
    ProviderValidation,
    UncertaintyLedger,
)


def _is_pinned_asset(path: str) -> bool:
    """A pinned literal-data asset (see `.literal_data`) is not a spec document."""

    return path.startswith("assets/")


def capability_contract_path(capability: str) -> str:
    readable = re.sub(r"[^a-z0-9_-]+", "-", capability.lower()).strip("-_")
    suffix = hashlib.sha256(capability.encode()).hexdigest()[:10]
    return f"specs/derived/interface-{(readable or 'capability')[:72]}-{suffix}.md"


def render_capability_contract_markdown(
    contract: ComponentCapabilityContractDraft,
) -> str:
    return "\n".join(
        (
            f"# Public interface: {contract.name}",
            "",
            "## ADDED Requirements",
            "",
            f"### Requirement: {contract.name}",
            "",
            contract.contract,
            "",
            "#### Scenario: Preserve the reviewed public contract",
            "",
            "- **WHEN** a consumer uses this capability",
            "- **THEN** the implementation satisfies the reviewed contract",
            "",
            "Evidence: " + ", ".join(f"`{item}`" for item in contract.evidence_ids),
            "",
        )
    )


def _component_semantics(node: ComponentGraphNodeDraft) -> str:
    lines = [
        "## Reviewed Component contract",
        "",
        f"- Kind: `{node.kind}`",
        f"- Profiles: {', '.join(f'`{item}`' for item in node.profiles)}",
        f"- Build needs: {', '.join(f'`{item}`' for item in node.build_needs)}",
        "",
        "### Provided capabilities",
        "",
    ]
    for contract in node.capability_contracts:
        lines.extend(
            [
                f"#### `{contract.name}`",
                "",
                contract.contract,
                "",
                "Evidence: " + ", ".join(f"`{item}`" for item in contract.evidence_ids),
                "",
            ]
        )
    if node.library_imports:
        lines.extend(["### Reviewed native imports", ""])
        for item in node.library_imports:
            lines.append(
                f"- `{item.capability}` ({item.language}): package `{item.package}`, "
                f"module `{item.module}`, symbols "
                + ", ".join(f"`{s}`" for s in item.symbols)
            )
        lines.append("")
    lines.extend(["### Entrypoints", ""])
    if not node.entrypoints:
        lines.extend(["None. This Component is a library.", ""])
    for entrypoint in node.entrypoints:
        lines.extend(
            [
                f"- `{entrypoint.name}` ({entrypoint.kind}) at `{entrypoint.path}`; "
                + "evidence: "
                + ", ".join(f"`{item}`" for item in entrypoint.evidence_ids),
                "",
            ]
        )
    return "\n".join(lines)


def render_component_openspec_markdown(
    node: ComponentGraphNodeDraft, statements: tuple[DraftStatement, ...]
) -> str:
    return _component_semantics(node) + "\n" + render_openspec_markdown(statements)


def component_name(root: Path) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", root.name).strip("-.")
    return normalized or "local-component"


def component_specification_path(coordinate: str, *, graph_size: int) -> str:
    """Return the deterministic promoted OpenSpec path for one graph node."""

    if graph_size == 1:
        return "specs/derived/spec.md"
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", coordinate).strip("-.")
    if not normalized:
        raise ValueError("Component coordinate has no portable specification path")
    return f"specs/components/{normalized}/spec.md"


def render_openspec_markdown(statements: tuple[DraftStatement, ...]) -> str:
    lines = ["# Derived source-backed specification", "", "## ADDED Requirements", ""]
    for statement in statements:
        lines.extend(
            [
                f"### Requirement: {statement.capability} [{statement.statement_id}]",
                "",
                statement.requirement,
                "",
            ]
        )
        for scenario in statement.scenarios:
            lines.extend(
                [
                    f"#### Scenario: {scenario.name}",
                    "",
                    f"- **WHEN** {scenario.when}",
                    f"- **THEN** {scenario.then}",
                    "",
                ]
            )
    return "\n".join(lines)


def benefits_from_literate_markdown(
    graph: ComponentGraphDraft, statements: tuple[DraftStatement, ...]
) -> bool:
    """Select layered output only when it makes the recovered draft narrower."""

    return len(graph.nodes) > 1 or len(statements) > 1


def render_literate_markdown_artifacts(
    graph: ComponentGraphDraft,
    statements_by_node: tuple[tuple[str, tuple[DraftStatement, ...]], ...],
    uncertainty: UncertaintyLedger,
) -> tuple[DraftArtifact, ...]:
    """Render one deterministic, provider-valid layered inverse specification.

    The model recovers semantics and Component boundaries.  This renderer owns only
    deterministic presentation: it never combines or chooses between conflicting
    claims, and preserves each normative statement in exactly one narrow document.
    """

    statement_map = dict(statements_by_node)
    if set(statement_map) != {node.coordinate for node in graph.nodes}:
        raise ValueError("Every recovered Component must have one statement partition")
    segments = _component_segments(tuple(node.coordinate for node in graph.nodes))
    node_by_coordinate = {node.coordinate: node for node in graph.nodes}
    artifacts = [
        DraftArtifact(
            "specs/derived/spec.md",
            _literate_document(
                name=node_by_coordinate[graph.root_coordinate].title,
                summary="Recovered source-backed Component graph proposed for review",
                kind="application",
                body=_graph_body(graph, uncertainty),
            ),
        ),
        DraftArtifact(
            "specs/derived/components/spec.md",
            _literate_document(
                name="Recovered Components",
                summary="Narrow source-backed Component boundaries proposed for review",
                kind="registry",
                body=(
                    "# Recovered Components\n\n"
                    "Each child document preserves one proposed Component boundary. "
                    "These boundaries remain reviewable inverse-translation evidence.\n"
                ),
            ),
        ),
    ]
    for node in graph.nodes:
        selected = statement_map[node.coordinate]
        segment = segments[node.coordinate]
        component_root = f"specs/derived/components/{segment}"
        component_body = (
            _component_body(node.coordinate, node.source_paths)
            + "\n"
            + _component_semantics(node)
        )
        if len(selected) == 1:
            component_body += "\n" + render_openspec_markdown(selected)
        artifacts.append(
            DraftArtifact(
                f"{component_root}/spec.md",
                _literate_document(
                    name=node.title,
                    summary=_component_summary(node.provided_capabilities),
                    kind="component",
                    body=component_body,
                ),
            )
        )
        if len(selected) > 1:
            for statement in selected:
                artifacts.append(
                    DraftArtifact(
                        f"{component_root}/{_statement_segment(statement)}.md",
                        _literate_document(
                            name=statement.capability,
                            summary=statement.requirement,
                            kind="behavior",
                            body=render_openspec_markdown((statement,)),
                        ),
                    )
                )
        for contract in node.capability_contracts:
            relative = capability_contract_path(contract.name).removeprefix(
                "specs/derived/"
            )
            artifacts.append(
                DraftArtifact(
                    f"{component_root}/{relative}",
                    _literate_document(
                        name=f"{contract.name} public interface",
                        summary=contract.contract,
                        kind="interface",
                        body=render_capability_contract_markdown(contract),
                    ),
                )
            )
    return tuple(artifacts)


def promoted_component_specification_projection(
    *,
    provider: str,
    graph: ComponentGraphDraft,
    artifacts: tuple[DraftArtifact, ...],
    coordinate: str,
) -> tuple[tuple[str, DraftArtifact], ...]:
    """Project reviewed inverse artifacts into one Component-local corpus.

    Cross-Component behavior stays in typed Component edges and public interfaces. It
    must never be represented by importing another Component's private specification
    hierarchy into the coding-agent context.
    """

    if coordinate not in {node.coordinate for node in graph.nodes}:
        raise ValueError("Promoted specification coordinate is absent from the graph")
    if provider == "openspec":
        expected = component_specification_path(coordinate, graph_size=len(graph.nodes))
        selected = tuple(
            item
            for item in artifacts
            if item.path == expected or item.path.startswith("specs/derived/interface-")
        )
        if sum(item.path == expected for item in selected) != 1:
            raise ValueError("Promoted OpenSpec Component has no exact specification")
        return tuple(
            (item.path, item) for item in sorted(selected, key=lambda x: x.path)
        )
    if provider != "literate-markdown":
        raise ValueError(f"Unsupported promoted specification provider: {provider!r}")

    segment = _component_segments(tuple(node.coordinate for node in graph.nodes))[
        coordinate
    ]
    prefix = f"specs/derived/components/{segment}/"
    selected = tuple(item for item in artifacts if item.path.startswith(prefix))
    root_path = prefix + "spec.md"
    if not selected or sum(item.path == root_path for item in selected) != 1:
        raise ValueError("Promoted layered Component has no exact root specification")
    rebased: list[tuple[str, DraftArtifact]] = []
    for artifact in sorted(
        selected,
        key=lambda item: (item.path != root_path, item.path),
    ):
        relative = artifact.path.removeprefix(prefix)
        rebased.append(
            (
                artifact.path,
                DraftArtifact(
                    "specs/derived/spec.md"
                    if relative == "spec.md"
                    else f"specs/derived/{relative}",
                    artifact.content,
                ),
            )
        )
    return tuple(rebased)


def validate_literate_markdown_draft(
    provider: str,
    artifacts: tuple[DraftArtifact, ...],
) -> ProviderValidation:
    if provider != "literate-markdown":
        return ProviderValidation(
            provider,
            "literate-markdown@1",
            False,
            ("unsupported provider",),
        )
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for artifact in artifacts:
                target = root.joinpath(*Path(artifact.path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(artifact.content.encode("utf-8"))
            LiterateMarkdownProvider().load(
                root,
                [item.path for item in artifacts if not _is_pinned_asset(item.path)],
                id_prefix="derived",
            )
    except (OSError, OpenSpecError) as exc:
        return ProviderValidation(provider, "literate-markdown@1", False, (str(exc),))
    return ProviderValidation(provider, "literate-markdown@1", True)


def _component_segments(coordinates: tuple[str, ...]) -> dict[str, str]:
    bases: dict[str, str] = {}
    for coordinate in coordinates:
        readable = (
            re.sub(r"[^a-z0-9_-]+", "-", coordinate.lower()).strip("-_") or "component"
        )
        bases[coordinate] = (
            readable
            if len(readable) <= 96
            else (
                f"{readable[:85]}-"
                f"{hashlib.sha256(coordinate.encode()).hexdigest()[:10]}"
            )
        )
    counts = {base: tuple(bases.values()).count(base) for base in set(bases.values())}
    return {
        coordinate: (
            base
            if counts[base] == 1
            else f"{base}-{hashlib.sha256(coordinate.encode()).hexdigest()[:10]}"
        )
        for coordinate, base in bases.items()
    }


def _statement_segment(statement: DraftStatement) -> str:
    readable = (
        re.sub(r"[^a-z0-9_-]+", "-", statement.capability.lower()).strip("-_")
        or "behavior"
    )
    suffix = hashlib.sha256(statement.statement_id.encode()).hexdigest()[:10]
    return f"{readable[:85]}-{suffix}"


def _component_summary(capabilities: tuple[str, ...]) -> str:
    return "Provides " + ", ".join(capabilities) + "."


def _literate_document(
    *,
    name: str,
    summary: str,
    kind: str,
    body: str,
    references: tuple[str, ...] = (),
) -> str:
    metadata_name = name[:256]
    metadata_summary = summary[:1024]
    lines = [
        "---",
        f"name: {json.dumps(metadata_name, ensure_ascii=False, separators=(',', ':'))}",
        "summary: "
        + json.dumps(metadata_summary, ensure_ascii=False, separators=(",", ":")),
        f"kind: {kind}",
        "status: review",
    ]
    if references:
        lines.append("references:")
        lines.extend(f"  - {reference}" for reference in references)
    lines.extend(("---", body.rstrip(), ""))
    return "\n".join(lines)


def _graph_body(graph: ComponentGraphDraft, uncertainty: UncertaintyLedger) -> str:
    lines = [
        "# Recovered source-backed specification",
        "",
        "## Required human review",
        "",
        "This draft partitions exact source-backed observations into narrow documents. "
        "It does not resolve semantic conflicts or promote inferred glue to authority.",
        "",
    ]
    if uncertainty.items:
        lines.append("The inverse workflow recorded these unresolved review items:")
        lines.append("")
        lines.extend(
            f"- `{item.uncertainty_id}`: {item.message}" for item in uncertainty.items
        )
    else:
        lines.append(
            "The uncertainty ledger reported no items; that is not evidence that the "
            "recovered intent is complete."
        )
    lines.extend(("", "## Proposed Component graph", ""))
    lines.extend(f"- `{node.coordinate}` — {node.title}" for node in graph.nodes)
    if graph.edges:
        lines.extend(("", "### Proposed dependencies", ""))
        lines.extend(
            f"- `{edge.source_coordinate}` requires `{edge.capability}` from "
            f"`{edge.target_coordinate}` (`{edge.requirement_id}`)"
            for edge in graph.edges
        )
    return "\n".join(lines)


def _component_body(coordinate: str, source_paths: tuple[str, ...]) -> str:
    lines = [
        f"# {coordinate}",
        "",
        "Recovered from exact evidence associated with:",
        "",
        *(f"- `{path}`" for path in source_paths),
        "",
    ]
    return "\n".join(lines)


def validate_openspec_draft(
    provider: str,
    artifacts: tuple[DraftArtifact, ...],
) -> ProviderValidation:
    if provider != "openspec":
        return ProviderValidation(
            provider, "openspec@1", False, ("unsupported provider",)
        )
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for artifact in artifacts:
                target = root.joinpath(*Path(artifact.path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(artifact.content.encode("utf-8"))
            OpenSpecProvider().load(
                root,
                [item.path for item in artifacts if not _is_pinned_asset(item.path)],
            )
    except (OSError, OpenSpecError) as exc:
        return ProviderValidation(provider, "openspec@1", False, (str(exc),))
    return ProviderValidation(provider, "openspec@1", True)


__all__ = [
    "component_name",
    "component_specification_path",
    "benefits_from_literate_markdown",
    "promoted_component_specification_projection",
    "render_literate_markdown_artifacts",
    "render_openspec_markdown",
    "validate_literate_markdown_draft",
    "validate_openspec_draft",
]
