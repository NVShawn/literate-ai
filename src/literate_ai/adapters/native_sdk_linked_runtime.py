"""Live SDK imports bound to exact linked Component artifacts and producer policy."""

from contextlib import ExitStack, contextmanager

from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_package_scope import NativeSdkPackageExecutionScope
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.security import AuthorizationError, AuthorizationRevocationSet


def native_sdk_runtime_revisions(snapshot, revision):
    """Follow artifact-consuming edges, preserving generation-only boundaries."""
    snapshot.require_unchanged()
    lock = snapshot.authority.lock
    if revision not in {node.revision.identity for node in lock.nodes}:
        raise ValueError("SDK runtime consumer is absent from the exact lock")
    reached, pending = set(), [revision]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(
            edge.provider_revision
            for edge in lock.edges
            if edge.consumer_revision == current
            and edge.kind in (DependencyKind.BUILD, DependencyKind.RUNTIME)
        )
    return tuple(sorted(reached, key=lambda item: item.uri))


class LinkedNativeSdkExecutionInputs:
    """A live wrapper; portable scope alone cannot provide SDK custody or grants."""

    def __init__(self, owner, scope):
        if not isinstance(owner, NativeSdkConsumerInputs) or not isinstance(
            scope, NativeSdkPackageExecutionScope
        ):
            raise TypeError("linked SDK execution requires live inputs and typed scope")
        if scope.execution_revision is None:
            raise ValueError("linked SDK execution requires its executing Component")
        self.owner, self.execution_scope = owner, scope
        self.require_unchanged()

    def require_unchanged(self):
        self.owner.require_unchanged()
        scope = self.execution_scope
        if self.owner.snapshot.authority.lock != scope.component_lock:
            raise ValueError("linked SDK execution scope differs from live authority")
        scope.select_bindings(
            self.owner.for_scope(scope.root_revision), scope.root_revision
        )

    def for_consumer(self, revision, *, target_identity):
        self.require_unchanged()
        scope = self.execution_scope
        if (
            revision != scope.root_revision
            or target_identity
            != scope.contract_for(revision).artifact_export.target_identity
        ):
            raise ValueError("linked SDK execution consumer or target differs")
        return scope.select_bindings(self.owner.for_scope(revision), revision)

    @contextmanager
    def materialize(self, revision, *, target_identity, parent):
        bindings = self.for_consumer(revision, target_identity=target_identity)
        owners = {
            (b.build.selection.component_revision, b.build.selection.target_identity)
            for b in bindings
        }
        with ExitStack() as stack:
            values = {}
            for owner_revision, target in sorted(owners, key=lambda pair: pair[0].uri):
                for value in stack.enter_context(
                    self.owner.materialize(
                        owner_revision, target_identity=target, parent=parent
                    )
                ):
                    values[value.binding.identity] = value
            self.require_unchanged()
            yield tuple(values[b.identity] for b in bindings)
            self.require_unchanged()

    def reobserve_runtime(self, value):
        self.require_unchanged()
        scope = self.execution_scope
        if value.binding not in scope.select_bindings(
            self.owner.for_scope(scope.root_revision), scope.root_revision
        ):
            raise ValueError("runtime SDK is outside the linked execution scope")
        self.owner.reobserve_runtime(value)

    def require_execution_authorized(self, revision, request, authorization, *, now):
        scope = self.execution_scope
        bindings = self.for_consumer(
            revision,
            target_identity=scope.contract_for(
                revision
            ).artifact_export.target_identity,
        )
        if request.effective_revision_digest != revision.uri:
            raise ValueError("linked SDK execution grant differs from its consumer")
        sources = dict(self.owner.execution_revocation_sources(revision))
        evidence = []
        for binding in bindings:
            current = sources[binding.identity]()
            if not isinstance(current, AuthorizationRevocationSet):
                raise AuthorizationError("security.live_revocation_verifier_invalid")
            current.require_build_valid(authorization, request, now=now)
            evidence.append(
                {
                    "input_identity": binding.identity.to_dict(),
                    "state": current.to_dict(),
                }
            )
        return tuple(evidence)
