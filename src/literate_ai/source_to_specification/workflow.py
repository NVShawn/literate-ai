"""Durable-value, non-mutating source-to-specification orchestration."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from .contracts import (
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    ComponentDefinitionDraft,
    ComponentGraphDraft,
    CoverageEntry,
    CoverageState,
    DraftArtifact,
    DraftStatement,
    EvidenceReference,
    ProviderValidation,
    SourceSpecificationCoverageMap,
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SourceToSpecificationRun,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    SpecificationDraftSet,
    UncertaintyItem,
    UncertaintyKind,
    UncertaintyLedger,
    canonical_digest,
)
from .errors import SourceMutationError, SourceToSpecificationError
from .skills import resolve_skill_set


class SkillExecutor(Protocol):
    """Execute one already-resolved skill over immutable evidence references."""

    def __call__(
        self,
        skill: SpecAuthoringSkill,
        request: SourceToSpecificationRequest,
        surfaces: tuple[BehaviorSurface, ...],
        evidence: tuple[EvidenceReference, ...],
    ) -> tuple[BehaviorObservation, ...]: ...


class DraftRenderer(Protocol):
    """Render provider artifacts and normalized statements without writing them."""

    def __call__(
        self,
        request: SourceToSpecificationRequest,
        observations: tuple[BehaviorObservation, ...],
        uncertainty: UncertaintyLedger,
    ) -> tuple[tuple[DraftStatement, ...], tuple[DraftArtifact, ...]]: ...


class ProviderValidator(Protocol):
    """Validate an in-memory complete draft tree with the selected provider."""

    def __call__(
        self, provider: str, artifacts: tuple[DraftArtifact, ...]
    ) -> ProviderValidation: ...


def validate_component_graph_projection(
    result: SourceToSpecificationResult,
    graph: ComponentGraphDraft,
) -> None:
    """Validate a reviewed graph against one exact inverse result."""

    _validate_component_graph_projection(
        request=result.request,
        observations=result.observations,
        component=result.component_definition_draft,
        graph=graph,
    )


def _validate_component_graph_projection(
    *,
    request: SourceToSpecificationRequest,
    observations: tuple[BehaviorObservation, ...],
    component: ComponentDefinitionDraft | None,
    graph: ComponentGraphDraft,
) -> None:
    """Shared graph closure validation for derivation and signed review."""

    if graph.source_snapshot_id != request.source_snapshot_id:
        raise SourceToSpecificationError(
            "workflow.component_graph_source_mismatch",
            "Component graph draft must describe the requested source snapshot",
        )
    if component is None:
        raise SourceToSpecificationError(
            "workflow.component_draft_missing",
            "Component graph review requires a source-derived Component draft",
        )
    if component.coordinate != graph.root_coordinate:
        raise SourceToSpecificationError(
            "workflow.component_graph_root_mismatch",
            "Component definition draft must project the Component graph root",
        )
    root_node = next(
        item for item in graph.nodes if item.coordinate == graph.root_coordinate
    )
    if component.title != root_node.title:
        raise SourceToSpecificationError(
            "workflow.component_graph_root_projection_mismatch",
            "Component definition draft title must exactly project the graph root",
        )
    observation_by_id = {item.observation_id: item for item in observations}
    graph_observations = {
        observation_id
        for node in graph.nodes
        for observation_id in node.observation_ids
    }
    graph_evidence = {
        evidence_id for node in graph.nodes for evidence_id in node.evidence_ids
    } | {evidence_id for edge in graph.edges for evidence_id in edge.evidence_ids}
    graph_paths = {path for node in graph.nodes for path in node.source_paths}
    evidence_by_id = {
        reference.evidence_id: reference
        for observation in observations
        for reference in observation.evidence
    }
    if graph_observations != set(observation_by_id):
        raise SourceToSpecificationError(
            "workflow.component_graph_observation_unknown",
            "Component graph draft must assign every exact observation once",
        )
    if not graph_evidence.issubset(evidence_by_id):
        raise SourceToSpecificationError(
            "workflow.component_graph_evidence_unknown",
            "Component graph draft references unknown exact evidence",
        )
    evidence_paths = {item.path for item in evidence_by_id.values()}
    if not graph_paths.issubset(evidence_paths):
        raise SourceToSpecificationError(
            "workflow.component_graph_path_unknown",
            "Component graph draft references a path outside exact evidence",
        )
    for node in graph.nodes:
        node_evidence = set(node.evidence_ids)
        expected_evidence = {
            reference.evidence_id
            for observation_id in node.observation_ids
            for reference in observation_by_id[observation_id].evidence
        }
        if node_evidence != expected_evidence:
            raise SourceToSpecificationError(
                "workflow.component_graph_evidence_incomplete",
                "each Component node must close over the exact evidence for its "
                "assigned observations",
            )
        expected_paths = {evidence_by_id[item].path for item in node_evidence}
        if set(node.source_paths) != expected_paths:
            raise SourceToSpecificationError(
                "workflow.component_graph_path_incomplete",
                "each Component node must name the exact paths for its evidence",
            )
        for contract in node.capability_contracts:
            if not set(contract.evidence_ids).issubset(node_evidence):
                raise SourceToSpecificationError(
                    "workflow.component_graph_contract_evidence_unknown",
                    "capability contract evidence must belong to its Component node",
                )
        for entrypoint in node.entrypoints:
            if (
                not set(entrypoint.evidence_ids).issubset(node_evidence)
                or entrypoint.path not in node.source_paths
            ):
                raise SourceToSpecificationError(
                    "workflow.component_graph_entrypoint_evidence_unknown",
                    "entrypoint path and evidence must belong to its Component node",
                )


class SourceTreeFingerprint:
    """Read-only source-tree fingerprint used to enforce workflow non-mutation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SourceToSpecificationError(
                "workflow.source_root_invalid", "source root must be a directory"
            )
        self._before = self._capture()

    def _capture(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix().encode()
            stat = path.lstat()
            digest.update(relative)
            digest.update(str(stat.st_mode).encode())
            if path.is_symlink():
                digest.update(b"L")
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(b"F")
                with path.open("rb") as source_file:
                    while chunk := source_file.read(1024 * 1024):
                        digest.update(chunk)
            elif path.is_dir():
                digest.update(b"D")
        return digest.hexdigest()

    @property
    def digest(self) -> str:
        """Content identity of the exact tree captured by this guard."""

        return f"sha256:{self._before}"

    def require_unchanged(self) -> None:
        if self._before != self._capture():
            raise SourceMutationError(
                "workflow.source_mutated",
                "the analyzed source tree changed during a non-mutating run",
            )


class SourceToSpecificationWorkflow:
    """Coordinate exact skills, observations, draft validation, and coverage."""

    def run(
        self,
        *,
        request: SourceToSpecificationRequest,
        skill_set: SpecAuthoringSkillSet,
        skill_catalog: dict[str, SpecAuthoringSkill],
        surfaces: tuple[BehaviorSurface, ...],
        evidence: tuple[EvidenceReference, ...],
        execute_skill: SkillExecutor,
        render_draft: DraftRenderer,
        validate_provider: ProviderValidator,
        component_definition_draft: ComponentDefinitionDraft | None = None,
        component_graph_draft: ComponentGraphDraft | None = None,
        source_root_guard: str | Path | None = None,
        trusted_skill_classifications: tuple[str, ...] = ("builtin-reviewed",),
    ) -> SourceToSpecificationResult:
        """Produce a provider-valid draft result without writing analyzed source."""

        guard = (
            SourceTreeFingerprint(source_root_guard)
            if source_root_guard is not None
            else None
        )
        try:
            return self._run(
                request=request,
                skill_set=skill_set,
                skill_catalog=skill_catalog,
                surfaces=surfaces,
                evidence=evidence,
                execute_skill=execute_skill,
                render_draft=render_draft,
                validate_provider=validate_provider,
                component_definition_draft=component_definition_draft,
                component_graph_draft=component_graph_draft,
                trusted_skill_classifications=trusted_skill_classifications,
            )
        finally:
            if guard is not None:
                guard.require_unchanged()

    def _run(
        self,
        *,
        request: SourceToSpecificationRequest,
        skill_set: SpecAuthoringSkillSet,
        skill_catalog: dict[str, SpecAuthoringSkill],
        surfaces: tuple[BehaviorSurface, ...],
        evidence: tuple[EvidenceReference, ...],
        execute_skill: SkillExecutor,
        render_draft: DraftRenderer,
        validate_provider: ProviderValidator,
        component_definition_draft: ComponentDefinitionDraft | None,
        component_graph_draft: ComponentGraphDraft | None,
        trusted_skill_classifications: tuple[str, ...],
    ) -> SourceToSpecificationResult:
        if request.skill_set_id != skill_set.skill_set_id:
            raise SourceToSpecificationError(
                "workflow.skill_set_mismatch",
                "request does not select the supplied skill set",
            )
        if (
            component_definition_draft is not None
            and component_definition_draft.source_snapshot_id
            != request.source_snapshot_id
        ):
            raise SourceToSpecificationError(
                "workflow.component_source_mismatch",
                "component definition draft must describe the requested "
                "source snapshot",
            )
        resolved = resolve_skill_set(skill_set, skill_catalog)
        untrusted = tuple(
            item.skill_id
            for item in resolved
            if item.trust_classification not in trusted_skill_classifications
        )
        if untrusted:
            raise SourceToSpecificationError(
                "workflow.skill_untrusted",
                "skill execution is not authorized for trust classification: "
                + ", ".join(untrusted),
            )
        covered_facets = {facet for skill in resolved for facet in skill.facets}
        missing_facets = set(request.facets) - covered_facets
        if missing_facets:
            raise SourceToSpecificationError(
                "workflow.facet_unsupported",
                f"skill set does not cover facets: {', '.join(sorted(missing_facets))}",
            )
        surfaces_by_id = {item.surface_id: item for item in surfaces}
        if len(surfaces_by_id) != len(surfaces):
            raise SourceToSpecificationError(
                "workflow.duplicate_surface", "behavior surfaces must have unique IDs"
            )
        evidence_by_id = {item.evidence_id: item for item in evidence}
        if len(evidence_by_id) != len(evidence):
            raise SourceToSpecificationError(
                "workflow.duplicate_evidence", "evidence must have unique IDs"
            )
        if any(
            item.source_snapshot_id != request.source_snapshot_id for item in evidence
        ):
            raise SourceToSpecificationError(
                "workflow.evidence_source_mismatch",
                "all evidence must match the exact requested source snapshot",
            )

        observations: list[BehaviorObservation] = []
        for skill in resolved:
            emitted = execute_skill(skill, request, surfaces, evidence)
            for observation in emitted:
                if observation.skill != skill.ref:
                    raise SourceToSpecificationError(
                        "workflow.observation_skill_mismatch",
                        "observation does not identify the executing skill",
                    )
                if observation.facet not in skill.facets:
                    raise SourceToSpecificationError(
                        "workflow.observation_facet_mismatch",
                        "skill emitted an undeclared observation facet",
                    )
                if not set(observation.surface_ids).issubset(surfaces_by_id):
                    raise SourceToSpecificationError(
                        "workflow.observation_surface_unknown",
                        "observation references an unknown behavior surface",
                    )
                for reference in observation.evidence:
                    if evidence_by_id.get(reference.evidence_id) != reference:
                        raise SourceToSpecificationError(
                            "workflow.observation_evidence_mismatch",
                            "observation evidence is missing or changed",
                        )
                observations.append(observation)
        observation_by_id = {item.observation_id: item for item in observations}
        if len(observation_by_id) != len(observations):
            raise SourceToSpecificationError(
                "workflow.duplicate_observation", "observation IDs must be unique"
            )
        observation_tuple = tuple(observations)
        if component_graph_draft is not None:
            _validate_component_graph_projection(
                request=request,
                observations=observation_tuple,
                component=component_definition_draft,
                graph=component_graph_draft,
            )
        uncertainty = self._uncertainty(request, observation_tuple)
        statements, artifacts = render_draft(request, observation_tuple, uncertainty)
        statement_ids = {item.statement_id for item in statements}
        if len(statement_ids) != len(statements):
            raise SourceToSpecificationError(
                "workflow.duplicate_statement", "draft statement IDs must be unique"
            )
        for statement in statements:
            if not set(statement.observation_ids).issubset(observation_by_id):
                raise SourceToSpecificationError(
                    "workflow.statement_observation_unknown",
                    "draft statement references an unknown observation",
                )
            kinds = {
                observation_by_id[item].claim_kind for item in statement.observation_ids
            }
            if kinds.issubset(
                {
                    ClaimKind.SUSPECTED_DEFECT,
                    ClaimKind.CONFLICT,
                    ClaimKind.UNKNOWN,
                }
            ):
                raise SourceToSpecificationError(
                    "workflow.unsupported_normative_statement",
                    "uncertain or defective observations cannot alone become normative",
                )
        validation = validate_provider(request.output_provider, artifacts)
        if validation.provider != request.output_provider:
            raise SourceToSpecificationError(
                "workflow.provider_mismatch",
                "validation did not use the selected specification provider",
            )
        if not validation.valid:
            raise SourceToSpecificationError(
                "workflow.draft_invalid",
                "specification provider rejected the generated draft tree",
            )
        draft_payload = {
            "request_id": request.request_id,
            "source_snapshot_id": request.source_snapshot_id,
            "output_provider": request.output_provider,
            "skill_set_identity": skill_set.identity,
            "statements": statements,
            "artifacts": artifacts,
            "validation": validation,
        }
        draft = SpecificationDraftSet(
            draft_id=canonical_digest(draft_payload),
            request_id=request.request_id,
            source_snapshot_id=request.source_snapshot_id,
            output_provider=request.output_provider,
            skill_set_identity=skill_set.identity,
            statements=statements,
            artifacts=artifacts,
            validation=validation,
        )
        coverage = self._coverage(request, surfaces, observation_tuple, statements)
        run_payload = {
            "request_identity": request.identity,
            "skill_set_identity": skill_set.identity,
            "observation_ids": tuple(item.observation_id for item in observations),
            "draft_identity": draft.identity,
        }
        run = SourceToSpecificationRun(
            run_id=canonical_digest(run_payload),
            request_identity=request.identity,
            skill_set_identity=skill_set.identity,
            observation_ids=tuple(item.observation_id for item in observations),
            draft_identity=draft.identity,
        )
        return SourceToSpecificationResult(
            request=request,
            observations=observation_tuple,
            draft=draft,
            coverage=coverage,
            uncertainty=uncertainty,
            run=run,
            component_definition_draft=component_definition_draft,
            component_graph_draft=component_graph_draft,
        )

    @staticmethod
    def _uncertainty(
        request: SourceToSpecificationRequest,
        observations: tuple[BehaviorObservation, ...],
    ) -> UncertaintyLedger:
        items: list[UncertaintyItem] = []
        kind_mapping = {
            ClaimKind.CONFLICT: UncertaintyKind.CONFLICT,
            ClaimKind.SUSPECTED_DEFECT: UncertaintyKind.SUSPECTED_DEFECT,
            ClaimKind.UNKNOWN: UncertaintyKind.UNKNOWN,
            ClaimKind.INFERRED_INTENT: UncertaintyKind.AMBIGUITY,
        }
        for observation in observations:
            uncertainty_kind = kind_mapping.get(observation.claim_kind)
            if uncertainty_kind is None:
                continue
            items.append(
                UncertaintyItem(
                    uncertainty_id=f"uncertainty:{observation.observation_id}",
                    kind=uncertainty_kind,
                    message=(
                        f"{observation.claim_kind.value}: {observation.statement}"
                    ),
                    observation_ids=(observation.observation_id,),
                    surface_ids=observation.surface_ids,
                )
            )
        return UncertaintyLedger(request_id=request.request_id, items=tuple(items))

    @staticmethod
    def _coverage(
        request: SourceToSpecificationRequest,
        surfaces: tuple[BehaviorSurface, ...],
        observations: tuple[BehaviorObservation, ...],
        statements: tuple[DraftStatement, ...],
    ) -> SourceSpecificationCoverageMap:
        observations_by_surface = {
            surface.surface_id: tuple(
                item for item in observations if surface.surface_id in item.surface_ids
            )
            for surface in surfaces
        }
        statements_by_surface: dict[str, tuple[str, ...]] = {}
        observation_by_id = {item.observation_id: item for item in observations}
        for surface in surfaces:
            statement_ids = []
            for statement in statements:
                bound_surfaces = {
                    surface_id
                    for observation_id in statement.observation_ids
                    for surface_id in observation_by_id[observation_id].surface_ids
                }
                if surface.surface_id in bound_surfaces:
                    statement_ids.append(statement.statement_id)
            statements_by_surface[surface.surface_id] = tuple(statement_ids)

        entries: list[CoverageEntry] = []
        for surface in surfaces:
            related = observations_by_surface[surface.surface_id]
            related_ids = tuple(item.observation_id for item in related)
            statement_ids = statements_by_surface[surface.surface_id]
            if surface.declared_state is not None:
                state = surface.declared_state
                reason = surface.reason
            elif statement_ids:
                state = CoverageState.COVERED
                reason = ""
            elif any(item.claim_kind is ClaimKind.CONFLICT for item in related):
                state = CoverageState.CONFLICTING
                reason = "source-backed observations conflict"
            elif any(item.claim_kind is ClaimKind.UNKNOWN for item in related):
                state = CoverageState.UNRESOLVED
                reason = "available evidence does not support a claim"
            elif related:
                state = CoverageState.UNSUPPORTED
                reason = "observations were not sufficient for a draft statement"
            else:
                state = CoverageState.UNSUPPORTED
                reason = "no observation covers this in-scope surface"
            entries.append(
                CoverageEntry(
                    surface_id=surface.surface_id,
                    state=state,
                    observation_ids=related_ids,
                    statement_ids=statement_ids,
                    reason=reason,
                )
            )
        return SourceSpecificationCoverageMap(
            request_id=request.request_id, entries=tuple(entries)
        )


__all__ = [
    "DraftRenderer",
    "ProviderValidator",
    "SkillExecutor",
    "SourceToSpecificationWorkflow",
    "SourceTreeFingerprint",
    "validate_component_graph_projection",
]
