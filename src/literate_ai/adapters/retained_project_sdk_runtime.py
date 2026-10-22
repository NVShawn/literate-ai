"""Own fresh locked SDK producers through public retained execution and admission."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from literate_ai.adapters.component_lock_application import project_component_roots
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.retained_project_sdks import read_retained_sdk_policy
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
    ):
        if project.root.absolute() != Path(project.root).resolve(strict=True):
            raise ValueError("retained.sdk.direct-project-root-required")
        self.project, self.binding, self.plan = project, binding, plan
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
        self.binding.require_unchanged()
        if (
            canonical_identity(self.binding.trust_binding.to_dict())
            != self.plan.standard_binding_identity
            or self.current_plan() != self.plan
        ):
            raise AuthorizationError("retained.sdk.project-authority-changed")
        for owner in self.owners:
            owner.require_unchanged()

    def current_revocations(self):
        self.require_current()
        return self.revocations

    def _snapshots(self):
        reader = FilesystemLockedGenerationAuthorityReader()
        expected = {lock.identity: lock for lock in self.locks}
        selected = {}
        for root in project_component_roots(self.project):
            if not (root / "component.lock.json").exists():
                continue
            # Read the persisted target before resolving its current exact graph.
            from literate_ai.adapters.component_locks import ComponentLockStore

            catalog = reader.load_catalog(
                root, flavor_roots=self.project.roots("flavor")
            )
            lock = ComponentLockStore(root).read_from_catalog(
                authorings=catalog.authorings
            )
            snapshot = reader.resolve(
                catalog,
                target_name=lock.target_name,
                flavor_selectors=self.project.flavor_selectors_for(root, ()),
            )
            snapshot.require_unchanged()
            identity = snapshot.authority.lock.identity
            if (
                expected.get(identity) != snapshot.authority.lock
                or identity in selected
            ):
                raise ValueError("retained.sdk.current-lock-set-changed")
            selected[identity] = snapshot
        if set(selected) != set(expected):
            raise ValueError("retained.sdk.complete-lock-snapshots-required")
        return tuple(selected[lock.identity] for lock in self.locks)

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
            acquire = NativeSdkProjectAcquisition(
                cache_root=self.cache_root / snapshot.authority.lock.identity.digest,
                source_intelligence_policy=self.project.definition.source_intelligence,
                security_policy=SecurityPolicy(
                    canonical_identity(
                        {
                            "policy": "standard-retained-native-sdk-build@1",
                            "standard_policy": self.binding.policy.identity.uri,
                            "retained_plan": self.plan.identity.uri,
                        }
                    ).uri
                ),
                revocations=self.current_revocations,
                environment=self.environment,
                actor="standard-retained-project",
                reason="Build the exact reviewed original-project SDK dependencies",
                host_build_acknowledged=True,
                store=self.store,
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
            expires_at=now + timedelta(minutes=5),
        )

    def record(self, document):
        self.require_current()
        return ContentIdentity.parse_uri(self.store.put_manifest(document).identity)
