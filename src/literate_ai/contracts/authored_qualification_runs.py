"""Closed authored run records; decoding never restores execution or admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, list_value
from .authored_qualification_comparisons import AuthoredQualificationComparison
from .authored_qualification_plans import AuthoredQualificationPlan
from .authored_qualification_requests import AuthoredQualificationLifecycleRequest
from .executable_components import (
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
    SourceGenerationRunOutput,
)
from .identity import ContentIdentity, contract_identity
from .standard_post_source_evidence import StandardComponentAcceptanceEvidence
from .standard_root_integration import StandardRootIntegrationEvidence
from .testing import ProjectTestReceipt


@dataclass(frozen=True, slots=True)
class AuthoredQualificationNodeRun:
    """Retain actual per-node records, including repeated content across nodes."""

    source_generation: SourceGenerationNodeResult
    source_output: SourceGenerationRunOutput
    index_identity: ContentIdentity
    acceptance: StandardComponentAcceptanceEvidence
    node_result_identity: ContentIdentity

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-node-run"

    def __post_init__(self):
        for name, kind in (
            ("source_generation", SourceGenerationNodeResult),
            ("source_output", SourceGenerationRunOutput),
            ("index_identity", ContentIdentity),
            ("acceptance", StandardComponentAcceptanceEvidence),
            ("node_result_identity", ContentIdentity),
        ):
            if not isinstance(getattr(self, name), kind):
                fail(name, f"must be a {kind.__name__}")
        generation, output, accepted = (
            self.source_generation,
            self.source_output,
            self.acceptance,
        )
        candidate = output.candidate
        if (
            generation.disposition is not SourceGenerationDisposition.GENERATED
            or generation.failure_code is not None
            or output.provenance.retained_source_identity is not None
        ):
            fail("source_generation", "must be a genuine fresh model generation")
        if (
            generation.component_revision != candidate.component_revision
            or generation.component_revision != accepted.component_revision
            or generation.candidate_identity != output.candidate_identity
            or generation.provenance_identity != output.provenance_identity
            or generation.generation_plan_identity
            != candidate.component_generation_plan_identity
            or generation.generation_key_identity != candidate.generation_key_identity
            or generation.context_manifest_identity
            != candidate.context_manifest_identity
            or generation.prompt_identity != candidate.prompt_identity
            or generation.recipe_identity != candidate.recipe_identity
            or generation.workspace_allocation_identity
            != candidate.workspace_allocation_identity
            or accepted.source_generation_identity != output.identity
            or accepted.build.source_tree_identity != candidate.tree_identity
            or accepted.build.source_sbom.bom_identity != candidate.source_bom_identity
            or accepted.generated_test_suite_identity
            != candidate.generated_test_suite_identity
        ):
            fail("source_output", "must join the exact generated and accepted node")

    @property
    def component_revision(self):
        return self.source_generation.component_revision

    @property
    def generated_test_total(self):
        return len(self.acceptance.generated_tests.cases)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "source_generation",
                    "source_output",
                    "index_identity",
                    "acceptance",
                    "node_result_identity",
                )
            },
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_generation",
                    "source_output",
                    "index_identity",
                    "acceptance",
                    "node_result_identity",
                }
            ),
        )
        return cls(
            SourceGenerationNodeResult.from_dict(data["source_generation"]),
            SourceGenerationRunOutput.from_dict(data["source_output"]),
            ContentIdentity.from_dict(data["index_identity"]),
            StandardComponentAcceptanceEvidence.from_dict(data["acceptance"]),
            ContentIdentity.from_dict(data["node_result_identity"]),
        )


@dataclass(frozen=True, slots=True)
class AuthoredQualificationRun:
    request: AuthoredQualificationLifecycleRequest
    driver_identity: ContentIdentity
    lifecycle_policy_identity: ContentIdentity
    framework_distribution_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    lifecycle_invocation_identity: ContentIdentity
    lifecycle_result_identity: ContentIdentity
    receipt: ProjectTestReceipt
    root: StandardRootIntegrationEvidence
    comparison: AuthoredQualificationComparison
    nodes: tuple[AuthoredQualificationNodeRun, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-run"
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "driver_identity",
        "lifecycle_policy_identity",
        "framework_distribution_identity",
        "lifecycle_request_identity",
        "lifecycle_invocation_identity",
        "lifecycle_result_identity",
    )

    def __post_init__(self):
        for name, kind in (
            ("request", AuthoredQualificationLifecycleRequest),
            ("receipt", ProjectTestReceipt),
            ("root", StandardRootIntegrationEvidence),
            ("comparison", AuthoredQualificationComparison),
            *((name, ContentIdentity) for name in self._IDENTITIES),
        ):
            if not isinstance(getattr(self, name), kind):
                fail(name, f"must be a {kind.__name__}")
        if (
            not isinstance(self.nodes, tuple)
            or not self.nodes
            or any(
                not isinstance(node, AuthoredQualificationNodeRun)
                for node in self.nodes
            )
        ):
            fail("nodes", "must be a nonempty typed tuple")
        revisions = tuple(node.component_revision.uri for node in self.nodes)
        if revisions != tuple(sorted(set(revisions))):
            fail("nodes", "must retain every node once in canonical order")
        comparison, request = self.comparison, self.request
        if (
            comparison.origin != request.origin
            or comparison.plan_identity != request.plan_identity
            or comparison.run_identity != request.run_identity
            or comparison.oracle_set_identity != request.oracle_set_identity
            or comparison.independent_acceptance_identity
            != self.root.independent_acceptance_identity
            or tuple(n.component_revision_identity.uri for n in comparison.nodes)
            != revisions
            or self.root.component_lock_identity != request.component_lock_identity
            or self.receipt.result_identity != self.lifecycle_result_identity
            or self.receipt.summary.total != self.generated_test_total
            or self.receipt.summary.passed != self.generated_test_total
            or self.receipt.summary.failed != 0
            or self.receipt.summary.skipped != 0
        ):
            fail("request", "must join the exact complete run, comparison and receipt")
        workspaces = [
            n.source_generation.workspace_allocation_identity for n in self.nodes
        ]
        if len(set(workspaces)) != len(workspaces):
            fail("nodes", "Component workspaces must be distinct")

    @property
    def generated_test_total(self):
        return sum(node.generated_test_total for node in self.nodes)

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "request",
                    *self._IDENTITIES,
                    "receipt",
                    "root",
                    "comparison",
                )
            },
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"request", *cls._IDENTITIES, "receipt", "root", "comparison", "nodes"}
            ),
        )
        return cls(
            request=AuthoredQualificationLifecycleRequest.from_dict(data["request"]),
            **{name: ContentIdentity.from_dict(data[name]) for name in cls._IDENTITIES},
            receipt=ProjectTestReceipt.from_dict(data["receipt"]),
            root=StandardRootIntegrationEvidence.from_dict(data["root"]),
            comparison=AuthoredQualificationComparison.from_dict(data["comparison"]),
            nodes=tuple(
                AuthoredQualificationNodeRun.from_dict(n)
                for n in list_value(data["nodes"], "nodes")
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthoredQualificationResult:
    """Complete all-node observations; no source baseline or publication grant."""

    plan: AuthoredQualificationPlan
    runs: tuple[AuthoredQualificationRun, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-result"

    def __post_init__(self):
        if not isinstance(self.plan, AuthoredQualificationPlan):
            fail("plan", "must be an AuthoredQualificationPlan")
        if (
            not isinstance(self.runs, tuple)
            or any(not isinstance(run, AuthoredQualificationRun) for run in self.runs)
            or len(self.runs) < self.plan.minimum_clean_runs
        ):
            fail("runs", "must contain the required complete clean runs")
        plans = {
            p.component_revision: p for p in self.plan.execution_plan.generation_plans
        }
        # Standard invocation identities describe deterministic configuration,
        # not a fresh execution event. Actual results bind each run's distinct
        # generated node/workspace observations even when that intent is shared.
        seen_runs, seen_workspaces, seen_results = set(), set(), set()
        for run in self.runs:
            request = run.request
            if (
                request.origin != self.plan.origin
                or request.plan_identity != self.plan.identity
                or request.oracle_set_identity != self.plan.oracle_set_identity
                or run.driver_identity != self.plan.driver_identity
                or run.lifecycle_policy_identity != self.plan.lifecycle_policy_identity
                or run.framework_distribution_identity
                != self.plan.framework_distribution_identity
                or run.root.execution_plan_identity != self.plan.execution_plan.identity
                or {n.component_revision for n in run.nodes} != set(plans)
            ):
                fail("runs", "must bind the original complete plan and native policy")
            workspaces = {request.workspace_allocation_identity} | {
                n.source_generation.workspace_allocation_identity for n in run.nodes
            }
            if (
                request.run_identity in seen_runs
                or workspaces & seen_workspaces
                or run.lifecycle_result_identity in seen_results
                or len(workspaces) != len(run.nodes) + 1
            ):
                fail("runs", "must use distinct runs, results and workspaces")
            seen_runs.add(request.run_identity)
            seen_workspaces.update(workspaces)
            seen_results.add(run.lifecycle_result_identity)
            for node in run.nodes:
                plan = plans[node.component_revision]
                if (
                    node.source_generation.generation_plan_identity != plan.identity
                    or node.source_generation.generation_key_identity
                    != plan.generation_key.identity
                ):
                    fail("nodes", "must execute each exact planned generation")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "plan": self.plan.to_dict(),
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"plan", "runs"}),
        )
        return cls(
            AuthoredQualificationPlan.from_dict(data["plan"]),
            tuple(
                AuthoredQualificationRun.from_dict(r)
                for r in list_value(data["runs"], "runs")
            ),
        )
