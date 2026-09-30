"""Actual source inspection, policy authorization and cache custody for SDK builds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from literate_ai.adapters.lifecycle.local import PolicyBuildAuthorizer
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.native_sdk_build import (
    NativeSdkBuiltProduct,
    NativeSdkRepositoryBuilder,
)
from literate_ai.adapters.native_sdk_recipes import (
    NativeSdkRecipePlanner,
    NativeSdkRecipeSelection,
    select_native_sdk_recipes,
)
from literate_ai.adapters.source.repository_cache import QuarantineRepositorySourceCache
from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.application.repository_sources import (
    RepositoryBuildApproval,
    RepositoryCheckout,
    RepositorySourceIndexBinding,
    RepositorySourceResolution,
    RepositorySourceResolver,
)
from literate_ai.contracts import ProjectSourceIntelligencePolicy, SourceEntryType
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildPlan, RepositorySourceLock
from literate_ai.projects import PinnedInputClosure
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildAuthorization,
    LiveBuildAuthorizationVerifier,
    OriginAttestation,
    RuleBasedSourceScanner,
    SecurityPolicy,
    SecurityScanReport,
    SourceModule,
    baseline_cpp_rules,
    baseline_javascript_rules,
    baseline_python_rules,
    baseline_rust_rules,
    baseline_swift_rules,
)
from literate_ai.sources import QuarantineStore, SourceCapture
from literate_ai.storage import FileSystemCAS, ReferenceIndex


@dataclass(frozen=True, slots=True)
class NativeSdkSourceBuild:
    """Successful repository build, not a Standard consumer execution grant."""

    selection: NativeSdkRecipeSelection
    resolution: RepositorySourceResolution
    product: NativeSdkBuiltProduct


@dataclass(frozen=True, slots=True)
class _SourceInspection:
    binding: RepositorySourceIndexBinding
    files: PinnedInputClosure
    origin: OriginAttestation
    scan: SecurityScanReport


class NativeSdkSourceBuildService:
    """Compose existing production adapters around a current locked SDK recipe.

    Security policy, operator acknowledgement and live revocation are trusted host
    inputs. The origin observation verifies local locked bytes, not vendor authorship.
    Returned records do not independently authenticate a transported SDK or admit its
    imports to a generated consumer.
    """

    def __init__(
        self,
        *,
        snapshot: LockedGenerationAuthoritySnapshot,
        selection: NativeSdkRecipeSelection,
        tools: Mapping[str, LocalComponentToolBinding],
        store: FileSystemCAS,
        quarantine: QuarantineStore,
        source_intelligence_policy: ProjectSourceIntelligencePolicy,
        security_policy: SecurityPolicy,
        revocations: Callable[[], AuthorizationRevocationSet],
        actor: str,
        reason: str,
        environment: Mapping[str, str],
        host_build_acknowledged: bool = False,
        references: ReferenceIndex | None = None,
    ) -> None:
        if selection not in select_native_sdk_recipes(
            snapshot, selection.component_revision
        ):
            raise ValueError("SDK source build requires current recipe authority")
        if not callable(revocations):
            raise TypeError("SDK source builds require live revocation state")
        self._started = False
        self._result: NativeSdkSourceBuild | None = None
        self.snapshot = snapshot
        self.selection = selection
        self.store = store
        self.policy = security_policy
        self.source_intelligence_policy = source_intelligence_policy
        self.revocations = revocations
        self.authority_files = snapshot.input_closure
        self._inspections: dict[ContentIdentity, _SourceInspection] = {}
        self._grants: dict[ContentIdentity, BuildAuthorization] = {}
        self.scanner = RuleBasedSourceScanner(
            "scanner:native-sdk-captured-source@1",
            (
                *baseline_python_rules(),
                *(
                    replace(rule, suffixes=(*rule.suffixes, ".c"))
                    for rule in baseline_cpp_rules()
                ),
                *baseline_javascript_rules(),
                *baseline_rust_rules(),
                *baseline_swift_rules(),
            ),
        )
        self.authorizer = PolicyBuildAuthorizer(
            security_policy,
            actor,
            reason,
            yolo_acknowledged=host_build_acknowledged,
        )
        self.verifier = LiveBuildAuthorizationVerifier(self._current_revocations)
        self.builder = NativeSdkRepositoryBuilder(
            layout=selection.recipe.layout,
            tools=tools,
            store=store,
            grant_lookup=self._grant,
            environment=environment,
            authorization_verifier=self.verifier,
        )
        self.planner = NativeSdkRecipePlanner(
            snapshot=snapshot,
            selection=selection,
            tools=tools,
            store=store,
        )
        self.cache = QuarantineRepositorySourceCache.from_project_policy(
            quarantine,
            store,
            source_intelligence_policy,
            references,
        )

    def _current_revocations(self) -> AuthorizationRevocationSet:
        self.authority_files.require_unchanged()
        for inspection in self._inspections.values():
            inspection.files.require_unchanged()
        current = self.revocations()
        if not isinstance(current, AuthorizationRevocationSet):
            raise AuthorizationError("security.live_revocation_verifier_invalid")
        return current

    def _grant(self, identity: ContentIdentity) -> BuildAuthorization:
        try:
            return self._grants[identity]
        except KeyError as exc:
            raise AuthorizationError("security.sdk_grant_not_issued") from exc

    def index(
        self,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
    ) -> RepositorySourceIndexBinding:
        lock = self.selection.source_lock
        actual = GitRepositorySourceCapturer().capture(checkout)
        if (
            actual != capture
            or actual.git is None
            or actual.git.dirty
            or checkout.resolved_commit != lock.resolved_commit
            or checkout.resolver_identity != lock.resolver
            or actual.git.submodules
            or actual.lfs_pointers
            or actual.git.head_commit != lock.resolved_commit
            or actual.snapshot.identity != lock.source_snapshot
            or actual.snapshot.tree_identity != lock.source_tree
        ):
            raise ValueError("SDK inspection requires the actual locked source capture")
        self.authority_files.require_unchanged()
        files = PinnedInputClosure(
            maximum_files=16384,
            maximum_file_bytes=16 * 1024 * 1024,
            maximum_total_bytes=128 * 1024 * 1024,
        )
        modules = []
        inventory = []
        for entry in capture.snapshot.entries:
            if entry.entry_type is not SourceEntryType.FILE:
                raise ValueError("SDK inspection requires regular captured files")
            content = files.pin(
                checkout.source_root / entry.path,
                boundary=checkout.source_root,
                label=f"sdk-source:{entry.path}",
                expected_identity=entry.identity,
            )
            if len(content) != entry.size:
                raise ValueError("SDK source file length differs from its capture")
            blob = self.store.put_bytes(content)
            inventory.append({"entry": entry.to_dict(), "blob": blob.to_dict()})
            modules.append(SourceModule.create(entry.path, content))
        scan = self.scanner.scan(tuple(modules))
        scan_blob = self.store.put_manifest(scan.to_dict())
        files.require_unchanged()
        self.authority_files.require_unchanged()
        provider = canonical_identity(
            {
                "indexer": "native-sdk-captured-files@1",
                "scanner": scan.scanner_identity,
                "rules": scan.rule_set_digest,
                "source_intelligence_policy": (
                    self.source_intelligence_policy.identity.to_dict()
                ),
            }
        )
        inventory_blob = self.store.put_manifest(
            {
                "schema": "literate-ai/native-sdk-source-inventory@1",
                "source_lock": lock.identity.to_dict(),
                "files": inventory,
                "scan": scan_blob.to_dict(),
                "provider": provider.to_dict(),
                "semantic_graph": False,
            }
        )
        binding = RepositorySourceIndexBinding(
            lock.source_snapshot,
            lock.source_tree,
            provider,
            ContentIdentity.parse_uri(inventory_blob.identity),
        )
        self.store.put_manifest(binding.to_dict())
        observation = self.store.put_manifest(
            {
                "schema": "literate-ai/local-sdk-source-verification@1",
                "scope": "coordinator-observed-exact-locked-source",
                "source_lock": lock.to_dict(),
                "index": binding.identity.to_dict(),
                "authority_closure": self.snapshot.input_closure_identity,
                "vendor_signature_verified": False,
            }
        )
        origin = OriginAttestation(
            source_digest=lock.source_snapshot.uri,
            signer="local-sdk-source-inspector",
            trust_root=self.snapshot.catalog_audit_identity.uri,
            signature_identity=observation.identity,
            verified=True,
        )
        self.store.put_manifest(origin.to_dict())
        self._inspections[binding.identity] = _SourceInspection(
            binding, files, origin, scan
        )
        return binding

    def authorize(
        self,
        lock: RepositorySourceLock,
        index_binding: ContentIdentity,
        plan: RepositoryBuildPlan,
    ) -> RepositoryBuildApproval:
        if lock != self.selection.source_lock or index_binding not in self._inspections:
            raise ValueError(
                "SDK authorization requires this service's source inspection"
            )
        inspected = self._inspections[index_binding]
        inspected.files.require_unchanged()
        self.authority_files.require_unchanged()
        expected = self.planner.plan(
            lock,
            index_binding,
            self.selection.component_revision,
            self.selection.target_identity,
            tuple(tool.toolchain_identity for tool in self.builder.tools.values()),
        )
        if plan != expected:
            raise ValueError(
                "SDK authorization requires the exact authored recipe plan"
            )
        request = self.builder.request(lock, plan)
        classification = self.policy.classify(
            effective_revision_digest=plan.effective_revision.uri,
            attestations=(inspected.origin,),
            findings=inspected.scan.findings,
        )
        grant = BuildAuthorization.from_dict(
            self.authorizer.authorize(
                classification.to_dict(),
                request.to_dict(),
            )
        )
        self.verifier.require_build_valid(grant, request, now=datetime.now(UTC))
        classification_id = ContentIdentity.parse_uri(classification.digest)
        grant_id = canonical_identity(grant.to_dict())
        self.store.put_manifest(classification.to_dict())
        self.store.put_manifest(grant.to_dict())
        self._grants[grant_id] = grant
        return RepositoryBuildApproval(
            lock.identity, plan.identity, classification_id, grant_id
        )

    def build(self) -> NativeSdkSourceBuild:
        if self._started:
            raise ValueError("SDK source build services are single-use")
        self._started = True
        selected = self.selection
        result = RepositorySourceResolver(
            acquirer=GitRepositorySourceAcquirer(),
            capturer=GitRepositorySourceCapturer(),
            indexer=self,
            planner=self.planner,
            authorizer=self,
            builder=self.builder,
            cache=self.cache,
        ).resolve(
            selected.source_lock.dependency,
            expected_lock=selected.source_lock,
            effective_revision=selected.component_revision,
            flavor_set=selected.target_identity,
            toolchains=tuple(
                tool.toolchain_identity for tool in self.builder.tools.values()
            ),
        )
        self.snapshot.require_unchanged()
        self._result = NativeSdkSourceBuild(
            selected, result, self.builder.product(result.build_verification)
        )
        return self.completed_result()

    def completed_result(self) -> NativeSdkSourceBuild:
        """Read the completed build without reacquiring or executing source."""
        if self._result is None:
            raise ValueError("SDK source build has not completed")
        self.snapshot.require_unchanged()
        result = self._result
        if self.builder.product(result.resolution.build_verification) != result.product:
            raise ValueError("SDK source build product differs from its producer")
        for blob in (
            result.product.snapshot_blob,
            result.product.runtime_observation,
            result.product.result_blob,
            *(item.blob for item in result.product.snapshot.files),
        ):
            self.store.verify(blob)
        return result
