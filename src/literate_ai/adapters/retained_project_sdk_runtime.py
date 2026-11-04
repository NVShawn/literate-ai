"""Own fresh locked SDK producers through public retained execution and admission."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.retained_project_sdks import read_retained_sdk_policy
from literate_ai.adapters.retained_sdk_authority import current_retained_sdk_snapshots
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkSelectionPolicy,
)
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildAuthorization,
    SecurityPolicy,
    SecurityProfile,
)


class RetainedProjectSdkRuntime:
    """A host-owned execution session, never a deserialized admission handle.

    The selected policy pins reproducible recipe and snapshot identities. Every
    invocation acquires fresh real producers and keeps them alive through command
    and receipt checks. The earlier run-bound policy is intentionally unchanged.
    """

    def __init__(
        self,
        project,
        binding,
        manifest,
        plan,
        *,
        component_locks,
        current_plan,
        cache_root,
        store,
        environment,
        receiver_context=None,
    ):
        if project.root.absolute() != Path(project.root).resolve(strict=True):
            raise ValueError("retained.sdk.direct-project-root-required")
        self.project, self.binding, self.plan = project, binding, plan
        if receiver_context is not None:
            from .retained_receiver_context import RetainedReceiverContext

            if not isinstance(receiver_context, RetainedReceiverContext):
                raise TypeError("retained.sdk.live-receiver-context-required")
            receiver_context.require_custody_current()
            if receiver_context.retained_plan_identity != plan.identity:
                raise ValueError("retained.sdk.receiver-plan-mismatch")
        self.receiver_context = receiver_context
        self.locks = tuple(component_locks)
        if tuple(lock.identity for lock in self.locks) != plan.component_locks:
            raise ValueError("retained.sdk.exact-lock-owner-closure-required")
        self.current_plan = current_plan
        self.cache_root, self.store = Path(cache_root), store
        self.environment = dict(environment)
        self.policy = read_retained_sdk_policy(
            project.root, manifest, plan.component_locks
        )
        if not isinstance(self.policy, RetainedProjectSdkSelectionPolicy):
            raise ValueError("retained.sdk.public-selection-policy-required")
        self.revocations = AuthorizationRevocationSet()
        self.owners = ()
        self._started = False
        self._grant_sequence = 0
        self._session_id = uuid4().hex
        self.require_current()

    def require_current(self):
        if self.receiver_context is not None:
            self.receiver_context.require_custody_current()
        self.binding.require_unchanged()
        if (
            canonical_identity(self.binding.trust_binding.to_dict())
            != self.plan.standard_binding_identity
            or self.current_plan() != self.plan
        ):
            raise AuthorizationError("retained.sdk.project-authority-changed")
        for owner in self.owners:
            owner.require_unchanged()
        if self.receiver_context is not None:
            self.receiver_context.require_custody_current()

    def current_revocations(self):
        self.require_current()
        return self.revocations

    def _snapshots(self):
        return current_retained_sdk_snapshots(self.project, self.locks)

    def acquire(self):
        if self._started:
            raise ValueError("retained.sdk.public-acquisition-single-use")
        self._started = True
        self.require_current()
        snapshots = self._snapshots()
        recipes = {
            (snapshot.authority.lock.identity, selection.identity)
            for snapshot in snapshots
            for node in snapshot.authority.lock.nodes
            if node.revision.repository_sources
            for selection in select_native_sdk_recipes(snapshot, node.revision.identity)
        }
        if recipes != {
            (binding.component_lock_identity, binding.selection_identity)
            for binding in self.policy.bindings
        }:
            raise ValueError("retained.sdk.complete-selected-recipes-required")
        owners = []
        for snapshot in snapshots:
            policy_inputs = {
                "policy": "standard-retained-native-sdk-build@1",
                "standard_policy": self.binding.policy.identity.uri,
                "retained_plan": self.plan.identity.uri,
            }
            policy_options = {}
            deadline = None
            if self.receiver_context is not None:
                self.receiver_context.require_custody_current()
                deadline = self.receiver_context.deadline.expires_at
                policy_inputs.update(
                    policy="standard-retained-native-sdk-build@2",
                    action_request=self.receiver_context.request.identity.uri,
                    action_deadline=self.receiver_context.deadline.identity.uri,
                    maximum_authorization_lifetime_seconds=10800,
                )
                policy_options["maximum_authorization_lifetime"] = timedelta(hours=3)
            acquire = NativeSdkProjectAcquisition(
                cache_root=self.cache_root / snapshot.authority.lock.identity.digest,
                source_intelligence_policy=self.project.definition.source_intelligence,
                security_policy=SecurityPolicy(
                    canonical_identity(policy_inputs).uri, **policy_options
                ),
                revocations=self.current_revocations,
                environment=self.environment,
                actor="standard-retained-project",
                reason="Build the exact reviewed original-project SDK dependencies",
                host_build_acknowledged=True,
                store=self.store,
                authorization_deadline=deadline,
            )
            owner = acquire(snapshot)
            if owner is None:
                owner = NativeSdkConsumerInputs(snapshot=snapshot, services=())
            owner.require_unchanged()
            owners.append(owner)
        self.owners = tuple(owners)
        self.require_current()
        # Match snapshots before constructing any retained command workspace.
        actual = {
            (
                lock.identity,
                value.build.selection.identity,
                value.build.product.snapshot.identity,
            )
            for lock, owner in zip(self.locks, self.owners, strict=True)
            for value in owner.for_scope(lock.root_revision)
        }
        expected = {
            (b.component_lock_identity, b.selection_identity, b.snapshot_identity)
            for b in self.policy.bindings
        }
        if actual != expected:
            raise ValueError("retained.sdk.complete-selected-snapshot-closure-required")
        return self.owners

    def authorize(self, request, runtime_identity):
        self.require_current()
        if request.requested_privileges != (
            "execute-native-sdk",
        ) or request.effective_revision_digest not in {
            node.revision.identity.uri for lock in self.locks for node in lock.nodes
        }:
            raise AuthorizationError("retained.sdk.command-outside-reviewed-authority")
        self._grant_sequence += 1
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=5)
        if self.receiver_context is not None:
            self.receiver_context.require_custody_current()
            expires_at = min(
                now + timedelta(hours=3), self.receiver_context.deadline.expires_at
            )
        return BuildAuthorization(
            authorization_id=f"retained-sdk:{self._session_id}:{self._grant_sequence}",
            classification_digest=runtime_identity.uri,
            request_digest=canonical_identity(request.to_dict()).uri,
            effective_revision_digest=request.effective_revision_digest,
            actor="standard-retained-project",
            reason="Execute the reviewed retained command with current live SDK owners",
            profile=SecurityProfile.CONSTRAINED,
            privileges=request.requested_privileges,
            issued_at=now,
            expires_at=expires_at,
        )

    def record(self, document):
        self.require_current()
        return ContentIdentity.parse_uri(self.store.put_manifest(document).identity)
