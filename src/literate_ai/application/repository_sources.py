"""Admission pipeline for exact non-Component repository source dependencies."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from literate_ai.contracts import (
    ContentIdentity,
    RepositoryBuildOutput,
    RepositoryBuildPlan,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceAdmission,
    RepositorySourceDependency,
    RepositorySourceLock,
    canonical_identity,
)
from literate_ai.sources import SourceCapture


class RepositorySourceResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RepositoryCheckout:
    """One temporary checkout whose mutable selector is already exact-resolved."""

    source_root: Path
    resolved_commit: str
    resolver_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.source_root, Path):
            raise TypeError("repository checkout source_root must be a Path")
        if not self.resolved_commit:
            raise ValueError("repository checkout requires a resolved commit")
        if not isinstance(self.resolver_identity, ContentIdentity):
            raise TypeError("repository checkout resolver must be content-identified")


@dataclass(frozen=True, slots=True)
class RepositorySourceIndexBinding:
    """Typed proof that one index describes the captured immutable source."""

    source_snapshot: ContentIdentity
    source_tree: ContentIdentity
    provider: ContentIdentity
    index: ContentIdentity

    def __post_init__(self) -> None:
        for field_name in ("source_snapshot", "source_tree", "provider", "index"):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                raise TypeError(
                    f"repository source index {field_name} must be content-identified"
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "urn:literate-ai:schema:v1:repository-source-index-binding",
            "source_snapshot": self.source_snapshot.to_dict(),
            "source_tree": self.source_tree.to_dict(),
            "provider": self.provider.to_dict(),
            "index": self.index.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RepositoryBuildApproval:
    source_lock: ContentIdentity
    build_plan: ContentIdentity
    classification: ContentIdentity
    authorization: ContentIdentity

    def __post_init__(self) -> None:
        for field_name in (
            "source_lock",
            "build_plan",
            "classification",
            "authorization",
        ):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                raise TypeError(
                    f"repository build approval {field_name} must be content-identified"
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "source_lock": self.source_lock.to_dict(),
                "build_plan": self.build_plan.to_dict(),
                "classification": self.classification.to_dict(),
                "authorization": self.authorization.to_dict(),
            }
        )


@dataclass(frozen=True, slots=True)
class RepositoryBuildVerification:
    source_lock: ContentIdentity
    build_plan: ContentIdentity
    build_approval: ContentIdentity
    build_result: ContentIdentity
    expected_outputs: tuple[str, ...]
    build_outputs: tuple[RepositoryBuildOutput, ...]
    passed: bool

    def __post_init__(self) -> None:
        for field_name in (
            "source_lock",
            "build_plan",
            "build_approval",
            "build_result",
        ):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                raise TypeError(
                    f"repository build verification {field_name} must be "
                    "content-identified"
                )
        if not isinstance(self.passed, bool):
            raise TypeError("repository build verification passed must be a boolean")
        if any(not isinstance(path, str) for path in self.expected_outputs):
            raise TypeError(
                "repository build verification expected outputs must be paths"
            )
        if not self.expected_outputs or len(self.expected_outputs) > 256:
            raise ValueError(
                "repository build verification requires 1 to 256 expected outputs"
            )
        if len(set(self.expected_outputs)) != len(self.expected_outputs):
            raise ValueError(
                "repository build verification expected output paths must be unique"
            )
        if self.expected_outputs != tuple(sorted(self.expected_outputs)):
            raise ValueError(
                "repository build verification expected output paths must be "
                "in canonical order"
            )
        if any(
            not isinstance(item, RepositoryBuildOutput) for item in self.build_outputs
        ):
            raise TypeError(
                "repository build outputs must bind paths to content identities"
            )
        if len(self.build_outputs) > 256:
            raise ValueError(
                "repository build verification must contain at most 256 outputs"
            )
        output_paths = tuple(item.path for item in self.build_outputs)
        if len(set(output_paths)) != len(output_paths):
            raise ValueError("repository build output paths must be unique")
        if self.passed and output_paths != self.expected_outputs:
            raise ValueError(
                "passing repository build outputs must exactly match expected paths "
                "in canonical order"
            )


@dataclass(frozen=True, slots=True)
class RepositorySourceResolution:
    lock: RepositorySourceLock
    index_binding: RepositorySourceIndexBinding
    build_plan: RepositoryBuildPlan
    build_approval: RepositoryBuildApproval
    build_verification: RepositoryBuildVerification
    admission: RepositorySourceAdmission
    cache_record: ContentIdentity


@runtime_checkable
class RepositorySourceAcquirer(Protocol):
    def acquire(
        self, dependency: RepositorySourceDependency
    ) -> AbstractContextManager[RepositoryCheckout]: ...


@runtime_checkable
class RepositorySourceCapturer(Protocol):
    def capture(self, checkout: RepositoryCheckout) -> SourceCapture: ...


@runtime_checkable
class RepositorySourceIndexer(Protocol):
    def index(
        self, checkout: RepositoryCheckout, capture: SourceCapture
    ) -> RepositorySourceIndexBinding: ...


@runtime_checkable
class RepositoryBuildPlanProvider(Protocol):
    def plan(
        self,
        lock: RepositorySourceLock,
        index_binding: ContentIdentity,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        toolchains: tuple[ContentIdentity, ...],
    ) -> RepositoryBuildPlan: ...


@runtime_checkable
class RepositoryBuildApprovalProvider(Protocol):
    def authorize(
        self,
        lock: RepositorySourceLock,
        index_binding: ContentIdentity,
        plan: RepositoryBuildPlan,
    ) -> RepositoryBuildApproval: ...


@runtime_checkable
class RepositorySourceBuilder(Protocol):
    def verify(
        self,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
        lock: RepositorySourceLock,
        plan: RepositoryBuildPlan,
        approval: RepositoryBuildApproval,
    ) -> RepositoryBuildVerification: ...


@runtime_checkable
class RepositorySourceCache(Protocol):
    def admit(
        self,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
        admission: RepositorySourceAdmission,
    ) -> ContentIdentity: ...


class RepositorySourceLockResolver:
    """Capture exact source authority without indexing, building or admitting it."""

    def __init__(
        self,
        *,
        acquirer: RepositorySourceAcquirer,
        capturer: RepositorySourceCapturer,
    ) -> None:
        self.acquirer = acquirer
        self.capturer = capturer

    def lock(self, dependency: RepositorySourceDependency) -> RepositorySourceLock:
        try:
            with self.acquirer.acquire(dependency) as checkout:
                capture = self._capture_exact(checkout)
                return self._source_lock(dependency, checkout, capture)
        except RepositorySourceResolutionError:
            raise
        except Exception as exc:
            raise RepositorySourceResolutionError(
                "repository-source.lock-failed",
                "repository source dependency could not be locked",
            ) from exc

    @staticmethod
    def _source_lock(
        dependency: RepositorySourceDependency,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
    ) -> RepositorySourceLock:
        if (
            dependency.revision_selector.immutable
            and dependency.revision_selector.value != checkout.resolved_commit
        ):
            raise RepositorySourceResolutionError(
                "repository-source.selector-mismatch",
                "captured commit differs from the authored exact selector",
            )
        return RepositorySourceLock(
            dependency=dependency,
            resolved_commit=checkout.resolved_commit,
            source_snapshot=capture.snapshot.identity,
            source_tree=capture.snapshot.tree_identity,
            resolver=checkout.resolver_identity,
        )

    def _capture_exact(self, checkout: RepositoryCheckout) -> SourceCapture:
        capture = self.capturer.capture(checkout)
        git = capture.git
        if git is None:
            raise RepositorySourceResolutionError(
                "repository-source.git-required",
                "repository dependencies require exact Git capture facts",
            )
        if git.head_commit != checkout.resolved_commit:
            raise RepositorySourceResolutionError(
                "repository-source.resolution-drift",
                "checked-out Git commit differs from the resolved remote revision",
            )
        if git.dirty:
            raise RepositorySourceResolutionError(
                "repository-source.dirty",
                "repository dependency checkout must be clean",
            )
        if capture.lfs_pointers:
            raise RepositorySourceResolutionError(
                "repository-source.lfs-unresolved",
                "repository dependency contains unresolved Git LFS objects",
            )
        if git.submodules:
            raise RepositorySourceResolutionError(
                "repository-source.submodules-unsupported",
                "repository dependency submodules require separate exact acquisition",
            )
        return capture


class RepositorySourceResolver(RepositorySourceLockResolver):
    """Resolve, understand, authorize, build, then cache exact repository source."""

    def __init__(
        self,
        *,
        acquirer: RepositorySourceAcquirer,
        capturer: RepositorySourceCapturer,
        indexer: RepositorySourceIndexer,
        planner: RepositoryBuildPlanProvider,
        authorizer: RepositoryBuildApprovalProvider,
        builder: RepositorySourceBuilder,
        cache: RepositorySourceCache,
    ) -> None:
        super().__init__(acquirer=acquirer, capturer=capturer)
        self.indexer = indexer
        self.planner = planner
        self.authorizer = authorizer
        self.builder = builder
        self.cache = cache

    def resolve(
        self,
        dependency: RepositorySourceDependency,
        *,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        toolchains: tuple[ContentIdentity, ...],
        expected_lock: RepositorySourceLock | None = None,
    ) -> RepositorySourceResolution:
        if not toolchains:
            raise RepositorySourceResolutionError(
                "repository-source.toolchain-required",
                "repository source admission requires exact target toolchains",
            )
        if expected_lock is not None and expected_lock.dependency != dependency:
            raise RepositorySourceResolutionError(
                "repository-source.lock-dependency-mismatch",
                "expected source lock describes another authored dependency",
            )
        acquisition_dependency = (
            dependency
            if expected_lock is None
            else replace(
                dependency,
                revision_selector=RepositoryRevisionSelector(
                    RepositoryRevisionKind.COMMIT, expected_lock.resolved_commit
                ),
            )
        )
        try:
            context = self.acquirer.acquire(acquisition_dependency)
            with context as checkout:
                capture = self._capture_exact(checkout)
                lock = self._source_lock(dependency, checkout, capture)
                if expected_lock is not None and lock != expected_lock:
                    raise RepositorySourceResolutionError(
                        "repository-source.lock-mismatch",
                        "reacquired source differs from the exact dependency lock",
                    )
                index_binding = self.indexer.index(checkout, capture)
                self._require_index_binding(lock, index_binding)
                self._require_unchanged(checkout, capture, stage="indexing")
                plan = self.planner.plan(
                    lock,
                    index_binding.identity,
                    effective_revision,
                    flavor_set,
                    toolchains,
                )
                self._require_plan(
                    lock,
                    index_binding.identity,
                    effective_revision,
                    flavor_set,
                    toolchains,
                    plan,
                )
                approval = self.authorizer.authorize(lock, index_binding.identity, plan)
                self._require_approval(lock, plan, approval)
                verification = self.builder.verify(
                    checkout, capture, lock, plan, approval
                )
                self._require_verification(lock, plan, approval, verification)
                self._require_unchanged(checkout, capture, stage="build")
                admission = RepositorySourceAdmission.create(
                    dependency_id=dependency.dependency_id,
                    source_lock=lock.identity,
                    source_snapshot=lock.source_snapshot,
                    source_tree=lock.source_tree,
                    effective_revision=effective_revision,
                    flavor_set=flavor_set,
                    index_binding=index_binding.identity,
                    build_plan=plan.identity,
                    classification=approval.classification,
                    authorization=approval.authorization,
                    build_result=verification.build_result,
                    expected_outputs=plan.expected_outputs,
                    build_outputs=verification.build_outputs,
                )
                cache_record = self.cache.admit(checkout, capture, admission)
                if not isinstance(cache_record, ContentIdentity):
                    raise RepositorySourceResolutionError(
                        "repository-source.cache-result-invalid",
                        "repository source cache must return a ContentIdentity",
                    )
                return RepositorySourceResolution(
                    lock,
                    index_binding,
                    plan,
                    approval,
                    verification,
                    admission,
                    cache_record,
                )
        except RepositorySourceResolutionError:
            raise
        except Exception as exc:
            raise RepositorySourceResolutionError(
                "repository-source.lifecycle-failed",
                "repository source dependency could not be admitted",
            ) from exc

    @staticmethod
    def _require_index_binding(
        lock: RepositorySourceLock,
        binding: RepositorySourceIndexBinding,
    ) -> None:
        if not isinstance(binding, RepositorySourceIndexBinding):
            raise RepositorySourceResolutionError(
                "repository-source.index-binding-invalid",
                "repository source indexer must return a typed index binding",
            )
        if (
            binding.source_snapshot != lock.source_snapshot
            or binding.source_tree != lock.source_tree
        ):
            raise RepositorySourceResolutionError(
                "repository-source.index-binding-mismatch",
                "repository source index describes another source capture",
            )

    def _require_unchanged(
        self,
        checkout: RepositoryCheckout,
        expected: SourceCapture,
        *,
        stage: str,
    ) -> None:
        actual = self._capture_exact(checkout)
        if actual.snapshot != expected.snapshot or actual.git != expected.git:
            raise RepositorySourceResolutionError(
                "repository-source.source-changed",
                f"repository source changed during {stage}",
            )

    @staticmethod
    def _require_plan(
        lock: RepositorySourceLock,
        index_binding: ContentIdentity,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        toolchains: tuple[ContentIdentity, ...],
        plan: RepositoryBuildPlan,
    ) -> None:
        if plan.source_lock != lock.identity:
            raise RepositorySourceResolutionError(
                "repository-source.plan-lock-mismatch",
                "repository build plan targets another source lock",
            )
        if plan.effective_revision != effective_revision:
            raise RepositorySourceResolutionError(
                "repository-source.plan-target-mismatch",
                "repository build plan targets another effective revision",
            )
        if plan.flavor_set != flavor_set:
            raise RepositorySourceResolutionError(
                "repository-source.plan-flavor-mismatch",
                "repository build plan targets another resolved flavor set",
            )
        if index_binding not in plan.evidence:
            raise RepositorySourceResolutionError(
                "repository-source.plan-evidence-missing",
                "repository build plan does not bind its exact source index",
            )
        if plan.toolchains != toolchains:
            raise RepositorySourceResolutionError(
                "repository-source.plan-toolchain-mismatch",
                "repository build plan changed the exact target toolchains",
            )

    @staticmethod
    def _require_approval(
        lock: RepositorySourceLock,
        plan: RepositoryBuildPlan,
        approval: RepositoryBuildApproval,
    ) -> None:
        if (
            approval.source_lock != lock.identity
            or approval.build_plan != plan.identity
        ):
            raise RepositorySourceResolutionError(
                "repository-source.authorization-mismatch",
                "repository build authorization targets another lock or plan",
            )

    @staticmethod
    def _require_verification(
        lock: RepositorySourceLock,
        plan: RepositoryBuildPlan,
        approval: RepositoryBuildApproval,
        verification: RepositoryBuildVerification,
    ) -> None:
        if (
            verification.source_lock != lock.identity
            or verification.build_plan != plan.identity
            or verification.build_approval != approval.identity
        ):
            raise RepositorySourceResolutionError(
                "repository-source.build-result-mismatch",
                "repository build result targets another lock, plan, or approval",
            )
        if verification.expected_outputs != plan.expected_outputs:
            raise RepositorySourceResolutionError(
                "repository-source.build-output-plan-mismatch",
                "repository build result names another expected output path set",
            )
        if not verification.passed or not verification.build_outputs:
            raise RepositorySourceResolutionError(
                "repository-source.build-failed",
                "repository source cannot enter the cache without verified outputs",
            )


__all__ = [
    "RepositoryBuildApproval",
    "RepositoryBuildApprovalProvider",
    "RepositoryBuildPlanProvider",
    "RepositoryBuildVerification",
    "RepositoryCheckout",
    "RepositorySourceAcquirer",
    "RepositorySourceBuilder",
    "RepositorySourceCache",
    "RepositorySourceCapturer",
    "RepositorySourceIndexer",
    "RepositorySourceIndexBinding",
    "RepositorySourceResolution",
    "RepositorySourceResolutionError",
    "RepositorySourceResolver",
]
