from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_repository_sources``."""










from literate_ai.application import (
    RepositoryBuildApproval,
    RepositoryBuildVerification,
    RepositorySourceIndexBinding,
)

from literate_ai.contracts import (
    DependencyKind,
    ProjectSourceIntelligencePolicy,
    RepositoryBuildCommand,
    RepositoryBuildOutput,
    RepositoryBuildPlan,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    canonical_identity,
)





def identity(label: str):
    return canonical_identity({"fixture": label})

def dependency(
    selector: RepositoryRevisionSelector | None = None,
) -> RepositorySourceDependency:
    return RepositorySourceDependency(
        "portable-lib",
        "https://example.test/oss/portable-lib.git",
        selector or RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "main"),
        DependencyKind.BUILD,
    )

def source_intelligence_policy(
    provider_id: str,
    repository_mode: SourceIntelligenceMode,
) -> ProjectSourceIntelligencePolicy:
    no_provider = provider_id == "none"
    return ProjectSourceIntelligencePolicy(
        provider_id=provider_id,
        command=None if no_provider else "source-intelligence",
        minimum_version=None if no_provider else "1.0.0",
        artifact_path=None if no_provider else ".intelligence/evidence.bin",
        stages=tuple(
            (
                stage,
                (
                    SourceIntelligenceMode.OFF
                    if no_provider
                    else (
                        repository_mode
                        if stage is SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION
                        else SourceIntelligenceMode.OFF
                    )
                ),
            )
            for stage in SourceIntelligenceStage
        ),
        artifact_publication=(
            SourceIntelligenceArtifactPublication.METADATA_ONLY
            if no_provider
            else SourceIntelligenceArtifactPublication.INCLUDE_ARTIFACT
        ),
    )

class _Indexer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.index_identity = identity("index")

    def index(self, _checkout, capture):
        self.events.append("index")
        return RepositorySourceIndexBinding(
            source_snapshot=capture.snapshot.identity,
            source_tree=capture.snapshot.tree_identity,
            provider=identity("fixture-intelligence-provider"),
            index=self.index_identity,
        )

class _Planner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def plan(self, lock, index_binding, effective_revision, flavor_set, toolchains):
        self.events.append("plan")
        return RepositoryBuildPlan(
            source_lock=lock.identity,
            effective_revision=effective_revision,
            flavor_set=flavor_set,
            evidence=(index_binding,),
            model_decision=identity("model"),
            toolchains=toolchains,
            commands=(RepositoryBuildCommand("build", ("cmake", "--build", "build")),),
            expected_outputs=("build/libportable.a",),
        )

class _Authorizer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def authorize(self, lock, _index, plan):
        self.events.append("authorize")
        return RepositoryBuildApproval(
            lock.identity,
            plan.identity,
            identity("classification"),
            identity("authorization"),
        )

class _Builder:
    def __init__(self, events: list[str], *, passed: bool = True) -> None:
        self.events = events
        self.passed = passed

    def verify(self, _checkout, _capture, lock, plan, approval):
        self.events.append("build")
        return RepositoryBuildVerification(
            source_lock=lock.identity,
            build_plan=plan.identity,
            build_approval=approval.identity,
            build_result=identity("build-result"),
            expected_outputs=plan.expected_outputs,
            build_outputs=(
                RepositoryBuildOutput("build/libportable.a", identity("library")),
            )
            if self.passed
            else (),
            passed=self.passed,
        )

class _Cache:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def admit(self, _checkout, _capture, _admission):
        self.events.append("cache")
        return identity("cache-record")

